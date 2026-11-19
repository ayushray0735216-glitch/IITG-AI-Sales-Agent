"""Saved discovery criteria and read-only Hunter search in the developer UI."""

from __future__ import annotations

import json
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import psycopg2
import pandas as pd
import streamlit as st
from psycopg2.extras import Json

import crm_storage
import prospect_progress
import prospect_discovery
from hunter_prospect_source import (ProspectSourceError, discover_filters, qualify_people,
                                    reveal_emails, search_parameters, search_profile,
                                    uses_company_discovery)


SETTING_KEY = "prospect_search_profiles"
PROSPECT_SEARCH_VERSION = "2026-11-18-experience-v2"
LOCAL_FILE = "prospect_searches.json"
FIELDS = ("countries", "states", "cities", "industries", "job_titles", "keywords", "exclusions")
PROGRESS_FIELDS = (
    "hunter_prospect_preview", "hunter_review_list", "hunter_revealed_emails",
    "hunter_imported_keys", "hunter_import_notice", "hunter_import_issues",
    "hunter_last_import_lead_keys", "hunter_review_selection_version",
)


def _items(value: str | list) -> list[str]:
    """Keep comma/newline lists readable, unique, and bounded."""
    chunks = re.split(r"[,\n]", value) if isinstance(value, str) else value if isinstance(value, list) else []
    result = []
    seen = set()
    for chunk in chunks:
        item = str(chunk).strip()
        if item and item.casefold() not in seen:
            result.append(item[:120])
            seen.add(item.casefold())
    if len(result) > 50:
        raise ValueError("Use at most 50 values in each criteria field.")
    return result


def _clean_profiles(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    profiles = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        profile = {"id": str(item["id"]), "name": str(item["name"]), "updated_at": str(item.get("updated_at", ""))}
        for field in FIELDS:
            profile[field] = _items(item.get(field, []))
        profile["size_min"] = item.get("size_min")
        profile["size_max"] = item.get("size_max")
        profile["experience_min"] = item.get("experience_min")
        profile["experience_max"] = item.get("experience_max")
        profiles.append(profile)
    return profiles[:30]


def load_profiles() -> list[dict]:
    """Read shared settings in PostgreSQL, or durable local JSON without a database."""
    if crm_storage.database_enabled():
        conn = crm_storage.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_settings WHERE row_key = %s", (SETTING_KEY,))
                row = cur.fetchone()
            return _clean_profiles(row[0].get("profiles", [])) if row and isinstance(row[0], dict) else []
        finally:
            conn.close()
    path = Path(crm_storage.crm_path(LOCAL_FILE))
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as stream:
        return _clean_profiles(json.load(stream).get("profiles", []))


def save_profiles(profiles: list[dict]) -> None:
    """Replace the saved criteria only after validating the entire collection."""
    cleaned = _clean_profiles(profiles)
    if len(cleaned) != len(profiles):
        raise ValueError("One or more saved searches are invalid.")
    data = {"version": 1, "profiles": cleaned}
    if crm_storage.database_enabled():
        conn = crm_storage.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO crm_settings (row_key, data) VALUES (%s, %s) "
                    "ON CONFLICT (row_key) DO UPDATE SET data = EXCLUDED.data",
                    (SETTING_KEY, Json(data)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return
    path = Path(crm_storage.crm_path(LOCAL_FILE))
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".prospect-", suffix=".json", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _new_profile(profile_id: str, name: str, fields: dict, size_min: int, size_max: int,
                 experience_min: int = 0, experience_max: int = 0) -> dict:
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Enter a search name of up to 100 characters.")
    profile = {"id": profile_id, "name": name, "updated_at": datetime.now(timezone.utc).isoformat()}
    for field in FIELDS:
        profile[field] = _items(fields[field])
    if not profile["countries"]:
        raise ValueError("Add at least one country or region in Countries.")
    if not any(profile[field] for field in ("industries", "job_titles", "keywords")):
        raise ValueError("Add an industry, job title, or keyword to qualify prospects.")
    if size_min and size_max and size_min > size_max:
        raise ValueError("The minimum company size cannot exceed the maximum.")
    if experience_min and experience_max and experience_min > experience_max:
        raise ValueError("Minimum experience cannot exceed maximum experience.")
    profile["size_min"] = size_min or None
    profile["size_max"] = size_max or None
    profile["experience_min"] = experience_min or None
    profile["experience_max"] = experience_max or None
    return profile


def experience_fit(person: dict, profile: dict) -> str:
    """Only classify confirmed years; Hunter's masked preview has no experience."""
    value = person.get("experience_years")
    if value is None or str(value).strip() == "":
        return "Experience unknown"
    try:
        years = float(value)
    except (TypeError, ValueError):
        return "Experience unknown"
    minimum = profile.get("experience_min") or 0
    maximum = profile.get("experience_max") or 0
    if (minimum and years < float(minimum)) or (maximum and years > float(maximum)):
        return "Outside range"
    return "Matches range"


def _hunter_key() -> str:
    key = os.getenv("HUNTER_API_KEY", "").strip()
    if key:
        return key
    try:
        return str(st.secrets.get("HUNTER_API_KEY", "")).strip()
    except Exception:  # No Streamlit secrets file in local installations.
        return ""


def _criteria_fingerprint(profile: dict) -> str:
    stable = {key: value for key, value in profile.items() if key != "updated_at"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode("utf-8")).hexdigest()


def _preview_fingerprint(profile: dict, valid_only: bool) -> str:
    return hashlib.sha256(f"hunter-broad-v3:{_criteria_fingerprint(profile)}:{valid_only}".encode("utf-8")).hexdigest()


def _progress_snapshot(profile: dict) -> dict:
    state = {field: st.session_state.get(field) for field in PROGRESS_FIELDS}
    state["hunter_imported_keys"] = sorted(state["hunter_imported_keys"] or [])
    return {
        "version": 1, "criteria_fingerprint": _criteria_fingerprint(profile),
        "options": {"valid_only": st.session_state.get("hunter_valid_only", True),
                    "strict_role": st.session_state.get("hunter_strict_role", True),
                    "company_limit": st.session_state.get("hunter_company_limit", 2)},
        "state": state,
    }


def _save_progress(profile: dict) -> None:
    try:
        prospect_progress.save_search(profile["id"], _progress_snapshot(profile))
    except (OSError, json.JSONDecodeError, psycopg2.Error, TypeError, ValueError):
        st.error("Search progress could not be saved. Keep this page open and retry before leaving.")


def _clear_active_progress(profile: dict, keep_imports: bool = True,
                           keep_review: bool = False) -> None:
    imported = st.session_state.get("hunter_imported_keys", set()) if keep_imports else set()
    review = st.session_state.get("hunter_review_list", {}) if keep_review else {}
    revealed = st.session_state.get("hunter_revealed_emails", {}) if keep_review else {}
    for field in PROGRESS_FIELDS:
        st.session_state.pop(field, None)
    st.session_state["hunter_imported_keys"] = imported
    st.session_state["hunter_review_list"] = review
    st.session_state["hunter_revealed_emails"] = revealed
    st.session_state["hunter_review_selection_version"] = 0
    _save_progress(profile)


def _activate_progress(profile: dict) -> None:
    """Restore only the selected search, retaining a hot-reloaded open session."""
    profile_id = profile["id"]
    fingerprint = _criteria_fingerprint(profile)
    active_id = st.session_state.get("hunter_active_profile_id")
    if active_id == profile_id:
        if st.session_state.get("hunter_active_criteria_fingerprint") != fingerprint:
            _clear_active_progress(profile, keep_review=True)
            st.session_state["hunter_active_criteria_fingerprint"] = fingerprint
        return
    saved = prospect_progress.load_search(profile_id)
    if saved and saved.get("criteria_fingerprint") != fingerprint:
        old_state = saved.get("state") or {}
        saved["criteria_fingerprint"] = fingerprint
        saved["state"] = {
            "hunter_review_list": old_state.get("hunter_review_list") or {},
            "hunter_revealed_emails": old_state.get("hunter_revealed_emails") or {},
            "hunter_imported_keys": old_state.get("hunter_imported_keys") or [],
        }
        prospect_progress.save_search(profile_id, saved)
    if not saved and active_id is None:
        open_preview = st.session_state.get("hunter_prospect_preview") or {}
        if open_preview.get("profile_id") == profile_id:
            if not uses_company_discovery(profile):
                open_preview["fingerprint"] = _preview_fingerprint(
                    profile, bool(st.session_state.get("hunter_valid_only", True))
                )
            _save_progress(profile)
            saved = prospect_progress.load_search(profile_id)
    state = saved.get("state") or {}
    for field in PROGRESS_FIELDS:
        value = state.get(field)
        if field == "hunter_imported_keys":
            value = set(value or [])
        elif field == "hunter_review_list" or field == "hunter_revealed_emails":
            value = value if isinstance(value, dict) else {}
        elif field == "hunter_import_issues":
            value = value if isinstance(value, list) else []
        st.session_state[field] = value
    st.session_state["hunter_review_selection_version"] = int(state.get("hunter_review_selection_version") or 0) + 1
    options = saved.get("options") or {}
    st.session_state["hunter_valid_only"] = bool(options.get("valid_only", True))
    st.session_state["hunter_strict_role"] = bool(options.get("strict_role", True))
    st.session_state["hunter_company_limit"] = options.get("company_limit", 2)
    st.session_state["hunter_options_snapshot"] = (
        st.session_state["hunter_valid_only"], st.session_state["hunter_strict_role"],
        st.session_state["hunter_company_limit"],
    )
    st.session_state["hunter_active_profile_id"] = profile_id
    st.session_state["hunter_active_criteria_fingerprint"] = fingerprint


def _run_hunter_search(profile: dict, key: str, cursor: str | None = None,
                       history: list[str | None] | None = None,
                       valid_only: bool = True) -> bool:
    try:
        with st.spinner("Searching Hunter..."):
            result = search_profile(profile, key, cursor, valid_only=valid_only)
    except (ProspectSourceError, ValueError) as exc:
        st.error(str(exc))
        return False
    else:
        fingerprint = _preview_fingerprint(profile, valid_only)
        if history is None:
            st.session_state["hunter_review_list"] = {}
            st.session_state["hunter_revealed_emails"] = {}
        st.session_state["hunter_prospect_preview"] = {
            "profile_id": profile["id"], "fingerprint": fingerprint, "result": result,
            "history": history if history is not None else [cursor],
        }
        _save_progress(profile)
        return True


def _show_hunter_preview(profile: dict, key: str) -> None:
    st.subheader("Search Hunter", anchor=False)
    st.caption("Search manually when needed. Your review list and page position resume where you left them.")
    if not key:
        st.warning("Hunter is not connected. Add HUNTER_API_KEY to the app environment or Streamlit secrets, then restart the app.")
        st.link_button("Hunter API setup", "https://hunter.io/api-documentation/v2/")
        return
    if not profile:
        st.info("Save a search above, then select it to look for prospects.")
        return
    if uses_company_discovery(profile):
        st.caption("Hunter checks saved company location, industry, keywords and size first, then finds people by job title. Free-plan company results are limited to the first 100 matches.")
    else:
        st.caption("Hunter searches by company country, person job title and company size. Exclusions are checked on results shown here.")
        if any(profile.get(field) for field in ("states", "cities", "industries", "keywords")):
            st.caption("Saved states, cities, industries and keywords are retained but are not applied by this broad search.")
    valid_only = st.toggle("Show verified email records only", value=True, key="hunter_valid_only")
    strict_role = st.toggle("Match saved job titles exactly", value=True, key="hunter_strict_role")
    company_limit = st.select_slider("Maximum candidates shown per company on each page", options=[1, 2, 3, 5], value=2,
                                     key="hunter_company_limit")
    current_options = (valid_only, strict_role, company_limit)
    if st.session_state.get("hunter_options_snapshot") != current_options:
        _save_progress(profile)
        st.session_state["hunter_options_snapshot"] = current_options
    preview = st.session_state.get("hunter_prospect_preview") or {}
    fingerprint = _preview_fingerprint(profile, valid_only)
    if preview.get("profile_id") == profile["id"] and preview.get("fingerprint") == fingerprint:
        st.caption("Saved progress is available for this search.")
        if st.button("Start again", key=f"hunter_start_again_{profile['id']}"):
            _clear_active_progress(profile)
            st.rerun()
    elif st.button("Search now", type="primary", icon=":material/search:", key=f"hunter_search_{profile['id']}"):
        _run_hunter_search(profile, key, valid_only=valid_only)
        preview = st.session_state.get("hunter_prospect_preview") or {}
    has_preview = preview.get("profile_id") == profile["id"] and preview.get("fingerprint") == fingerprint
    if not has_preview:
        if preview:
            st.info("Search again to apply the updated criteria or verification filter.")
        result = {"people": [], "total": 0, "next_cursor": ""}
    else:
        result = preview["result"]
    visible, removed = qualify_people(result["people"], profile, strict_role, company_limit)
    history = (preview.get("history") or [None]) if has_preview else [None]
    if has_preview:
        st.caption(f"Hunter found {result['total']:,} broad matches · page {len(history)} · {len(visible)} suitable for review on this page")
        if result.get("coverage_note"):
            st.caption(result["coverage_note"])
        st.caption(f"Filtered on this page: {removed['role']} other roles · {removed['excluded']} excluded · {removed['repeated']} repeated company or candidate")
    if has_preview and not visible:
        st.info("No candidates are visible on this page. Try broader criteria or another page.")
    elif visible:
        st.dataframe([{
            "Name (masked)": person["name"], "Role": person["title"],
            "Company": person["company"], "Domain": person["domain"],
            "Verification": person["verification"],
        } for person in visible], hide_index=True, width="stretch")
    if has_preview:
        st.caption("The total is Hunter's broad match count. Role matching and company limits apply to the displayed page only.")
    choices = {person["handle"]: person for person in visible if person.get("handle")}
    if choices:
        choice_key = hashlib.sha256("|".join(choices).encode("utf-8")).hexdigest()[:12]
        picked = st.multiselect("Select candidates for review", options=list(choices),
                                format_func=lambda handle: f"{choices[handle]['name']} · {choices[handle]['title']} · {choices[handle]['company']}",
                                key=f"hunter_pick_{profile['id']}_{choice_key}")
        if st.button("Add selected to review list", disabled=not picked, key="hunter_add_review"):
            review = st.session_state.setdefault("hunter_review_list", {})
            added = 0
            for handle in picked:
                person = choices[handle]
                domain = (person["domain"] or person["company"]).casefold()
                in_company = sum(1 for other in review.values()
                                 if (other["domain"] or other["company"]).casefold() == domain)
                if handle not in review and in_company < company_limit:
                    review[handle] = person
                    added += 1
            if not added:
                st.warning("These candidates are already listed or the company review limit is reached.")
            else:
                st.success(f"Added {added} candidate(s) to your review list.")
                _save_progress(profile)
    review = st.session_state.setdefault("hunter_review_list", {})
    revealed = st.session_state.setdefault("hunter_revealed_emails", {})
    imported = st.session_state.setdefault("hunter_imported_keys", set())
    notice = st.session_state.pop("hunter_reveal_notice", None)
    if notice:
        st.success(notice)
    import_notice = st.session_state.get("hunter_import_notice")
    if import_notice:
        st.success(import_notice)
        from crm_workspace import open_workspace_page
        if st.button("Open Leads", icon=":material/group:", key="hunter_open_imported_leads"):
            lead_keys = st.session_state.get("hunter_last_import_lead_keys", [])
            st.session_state.pop("hunter_import_notice", None)
            _save_progress(profile)
            if len(lead_keys) == 1:
                open_workspace_page("leads", lead=hashlib.sha256(lead_keys[0].encode("utf-8")).hexdigest()[:24])
            else:
                open_workspace_page("leads", view="file")
    if review:
        st.subheader(f"Review list · {len(review)}", anchor=False)
        st.caption("Hunter does not provide years of experience. Record confirmed years below to check a prospect against your saved range.")
        with st.form(f"hunter_experience_{profile['id']}"):
            experience_handle = st.selectbox("Prospect to check", list(review),
                                            format_func=lambda handle: f"{review[handle]['name']} · {review[handle]['company']}")
            known_years = review[experience_handle].get("experience_years")
            confirmed_years = st.number_input("Confirmed experience (years)", min_value=0.0,
                                              max_value=80.0, step=0.5,
                                              value=float(known_years) if known_years is not None else 0.0)
            save_experience = st.form_submit_button("Save confirmed experience")
        if save_experience:
            review[experience_handle]["experience_years"] = confirmed_years
            _save_progress(profile)
            st.rerun()
        if profile.get("experience_min") or profile.get("experience_max"):
            st.caption(f"Saved range: {profile.get('experience_min') or 0}–{profile.get('experience_max') or 'any'} years. Unknown experience stays visible for review.")
        st.markdown("**Ready to add a prospect?** Use [Import reviewed prospects](#import-reviewed-prospects) below. Select the person's **Import** box, then choose **Import selected to Leads**.")
        st.dataframe([{"Name": person["name"], "Role": person["title"],
                       "Company": person["company"], "Verification": person["verification"],
                       "Experience (years)": person.get("experience_years", "Unknown"),
                       "Experience fit": experience_fit(person, profile),
                       "Email": revealed.get(handle, "Hidden"),
                       "CRM": "In Leads" if hashlib.sha256(handle.encode("utf-8")).hexdigest() in imported else "Not imported"}
                      for handle, person in review.items()], hide_index=True, width="stretch")
        review_labels = lambda handle: f"{review[handle]['name']} · {review[handle]['company']}"
        selection_version = st.session_state.get("hunter_review_selection_version", 0)
        remove = st.multiselect("Remove from review list", list(review), format_func=review_labels,
                                key=f"hunter_remove_review_{profile['id']}_{selection_version}")
        if st.button("Remove selected", disabled=not remove, key="hunter_remove_selected"):
            for handle in remove:
                review.pop(handle, None)
                revealed.pop(handle, None)
            st.session_state["hunter_review_selection_version"] = selection_version + 1
            _save_progress(profile)
            st.rerun()
        unrevealed = [handle for handle in review if handle not in revealed]
        if unrevealed:
            to_reveal = st.multiselect("Choose emails to reveal (up to 10)", unrevealed,
                                       format_func=review_labels, key=f"hunter_reveal_selection_{profile['id']}_{selection_version}")
            st.caption("Hunter may charge up to one credit per selected personal email. Selection alone does not use credits.")
            confirmed = st.checkbox("I reviewed these candidates and approve the possible credit charge.",
                                    key=f"hunter_reveal_approved_{profile['id']}_{selection_version}")
            if st.button("Reveal selected emails", type="primary", disabled=not to_reveal or len(to_reveal) > 10 or not confirmed,
                         key="hunter_reveal_button"):
                try:
                    response = reveal_emails(to_reveal, key)
                except (ProspectSourceError, ValueError) as exc:
                    st.error(str(exc))
                else:
                    revealed.update(response["emails"])
                    for handle, details in response["details"].items():
                        if handle in review:
                            person = review[handle]
                            for field in ("name", "title", "phone", "linkedin"):
                                if details.get(field):
                                    person[field] = details[field]
                            if details.get("name"):
                                person["name_status"] = "Hunter revealed"
                    st.session_state["hunter_reveal_notice"] = (
                        f"Hunter revealed {len(response['emails'])} email(s); "
                        f"{response['credits_charged']} credit(s) charged."
                    )
                    st.session_state["hunter_review_selection_version"] = selection_version + 1
                    _save_progress(profile)
                    st.rerun()
        st.subheader("Import reviewed prospects", anchor="import-reviewed-prospects")
        st.caption("Select the people to save in Leads. Confirm a full name or add experience if you know it; otherwise the masked name and a blank experience field are saved clearly.")
        review_handles = list(review)
        import_rows = [{
            "Import": False, "Name to save": person["name"], "Job role": person["title"],
            "Company": person["company"], "Email": revealed.get(handle, "Not revealed"),
            "Experience (years)": person.get("experience_years", ""),
            "CRM": "In Leads" if hashlib.sha256(handle.encode("utf-8")).hexdigest() in imported else "Not imported",
        } for handle, person in review.items()]
        with st.form("hunter_import_form", border=True):
            edited = st.data_editor(pd.DataFrame(import_rows), hide_index=True, num_rows="fixed",
                                    disabled=["Job role", "Company", "Email", "CRM"], width="stretch",
                                    column_config={"Import": st.column_config.CheckboxColumn("Import", required=True),
                                                   "Name to save": st.column_config.TextColumn("Name to save", required=True),
                                                   "Experience (years)": st.column_config.TextColumn("Experience (years)")},
                                    key=f"hunter_import_editor_{profile['id']}_{selection_version}")
            submit_import = st.form_submit_button("Import selected to Leads", type="primary", icon=":material/person_add:")
        if submit_import:
            chosen = []
            for index, row in edited.iterrows():
                if not bool(row["Import"]):
                    continue
                handle = review_handles[index]
                person = review[handle]
                saved_name = str(row["Name to save"] or "").strip()
                name_status = person.get("name_status", "Hunter masked")
                if saved_name != person["name"]:
                    name_status = "Staff supplied"
                chosen.append({
                    **person, "handle": handle, "name": saved_name,
                    "email": revealed.get(handle, ""), "experience": str(row["Experience (years)"] or "").strip(),
                    "name_status": name_status,
                })
            if not chosen:
                st.info("Select at least one reviewed prospect to import.")
            else:
                try:
                    result, cache_ready = crm_storage.import_hunter_prospects(chosen)
                except ValueError as exc:
                    st.error(str(exc))
                except (OSError, psycopg2.Error):
                    st.error("The CRM could not save these prospects. Please try again.")
                else:
                    imported.update(key.removeprefix("import:hunter:") for key in result["stored"])
                    try:
                        stored = set(result["stored"])
                        prospect_discovery.mark_imported_handles(profile["id"], [
                            item["handle"] for item in chosen
                            if "import:hunter:" + hashlib.sha256(item["handle"].encode("utf-8")).hexdigest() in stored
                        ])
                    except (OSError, json.JSONDecodeError, psycopg2.Error):
                        st.warning("Leads were saved, but the discovery queue status could not be updated.")
                    st.session_state["hunter_import_issues"] = result["details"]
                    if result["stored"]:
                        st.session_state.pop("leads", None)
                        st.session_state["hunter_last_import_lead_keys"] = result["lead_keys"]
                        st.session_state["hunter_import_notice"] = (
                            f"Leads updated: {result['added']} added, {result['updated']} updated, "
                            f"{result['already_present']} already present."
                            + (" Refresh the local lead list after opening it." if not cache_ready else "")
                        )
                        _save_progress(profile)
                        st.rerun()
                    else:
                        _save_progress(profile)
                        st.info("No prospects were imported. Review the possible matches above.")
        for detail in st.session_state.get("hunter_import_issues") or []:
            st.warning(detail)
    st.caption("Importing only saves lead records. It does not record contact consent or send messages.")
    if has_preview:
        left, right = st.columns(2)
        if left.button("Previous page", disabled=len(history) <= 1, key="hunter_previous"):
            if _run_hunter_search(profile, key, history[-2], history[:-1], valid_only):
                st.rerun()
        if right.button("Next page", disabled=not result["next_cursor"], key="hunter_next"):
            if _run_hunter_search(profile, key, result["next_cursor"], [*history, result["next_cursor"]], valid_only):
                st.rerun()


def _show_discovery_panel(profile: dict, key: str) -> None:
    if not profile:
        return
    try:
        state = prospect_discovery.load_state(profile["id"])
    except (OSError, json.JSONDecodeError, psycopg2.Error, ValueError, TypeError):
        st.error("Automatic discovery status could not be loaded. Your saved criteria are unchanged.")
        return
    settings = state["settings"]
    criteria_changed = bool(state["fingerprint"] and
                            state["fingerprint"] != prospect_discovery.criteria_fingerprint(profile))
    if criteria_changed:
        st.info("Search criteria changed. Run discovery again to refresh these matches.")
        state["queue"] = {}
    st.subheader("Prospects to review", anchor="prospects-to-review")
    pending = [(identity, row) for identity, row in state["queue"].items()
               if row.get("status") == "pending"]
    reviewed_count = sum(row.get("status") == "reviewed" for row in state["queue"].values())
    rejected_count = sum(row.get("status") == "rejected" for row in state["queue"].values())
    st.caption(f"{len(pending)} new matches · {reviewed_count} moved to review · {rejected_count} rejected · automatic search {'on' if settings['enabled'] else 'off'}")
    suggested = prospect_discovery.suggested_threshold(state["queue"])
    if suggested is not None:
        st.caption(f"Based on your review decisions, a fit cutoff near {suggested} may separate accepted from rejected matches. You can change it in Automatic search settings.")
    if settings["enabled"]:
        try:
            worker = crm_storage.load_autopilot_worker_status()
            checked = datetime.fromisoformat(str(worker.get("checked_at") or ""))
            fresh = (datetime.now(timezone.utc) - checked.astimezone(timezone.utc)).total_seconds() < 900
            fresh = fresh and bool(worker.get("prospect_discovery"))
        except (OSError, json.JSONDecodeError, psycopg2.Error, ValueError, TypeError):
            fresh = False
        if not fresh:
            st.warning("Automatic search needs the updated background worker. Restart it with `python autopilot_worker.py` to search while this page is closed.")
    if state["last_success"]:
        st.caption(f"Last search: {state['last_success'][:16].replace('T', ' ')} IST · {state['last_added']} new matches")
    if state["coverage_note"]:
        st.caption(state["coverage_note"])
    if state["last_error"]:
        st.warning("Last search: " + state["last_error"])
    if pending:
        st.dataframe([{
            "Name": row["person"].get("name", ""), "Role": row["person"].get("title", ""),
            "Company": row["person"].get("company", ""), "Fit": row["score"],
            "Why it matches": "; ".join(row["reasons"]),
        } for _, row in pending], hide_index=True, width="stretch")
        labels = {identity: f"{row['person'].get('name', '')} · {row['person'].get('company', '')} · {row['score']} fit"
                  for identity, row in pending}
        selected = st.multiselect("Select matches", list(labels), format_func=labels.get,
                                  key=f"discovery_select_{profile['id']}")
        a, b = st.columns(2)
        if a.button("Add to review list", disabled=not selected, type="primary",
                    key=f"discovery_review_{profile['id']}"):
            try:
                review = st.session_state.setdefault("hunter_review_list", {})
                for identity in selected:
                    person = state["queue"][identity]["person"]
                    review[person["handle"]] = person
                _save_progress(profile)
                prospect_discovery.mark_status(profile["id"], selected, "reviewed")
                st.rerun()
            except (OSError, json.JSONDecodeError, psycopg2.Error):
                st.error("Review selection could not be saved. Please try again.")
        if b.button("Reject selected", disabled=not selected, key=f"discovery_reject_{profile['id']}"):
            try:
                prospect_discovery.mark_status(profile["id"], selected, "rejected")
                st.rerun()
            except (OSError, json.JSONDecodeError, psycopg2.Error):
                st.error("The rejection could not be saved. Please try again.")
    else:
        st.info("New good-fit prospects from scheduled searches will appear here. You can also run a search now.")
    st.caption("Fit measures recorded role and company signals. Interest is marked only after a customer responds or chats. Email addresses stay hidden until you approve a reveal.")
    with st.expander("Automatic search settings", expanded=False):
        with st.form(f"discovery_settings_{profile['id']}"):
            enabled = st.toggle("Search automatically", value=settings["enabled"])
            frequency = st.selectbox("Frequency", ["daily", "weekly"],
                                     index=["daily", "weekly"].index(settings["frequency"]))
            hour = st.selectbox("Start after (IST)", list(range(24)), index=settings["hour"],
                                format_func=lambda value: f"{value:02d}:00")
            max_new = st.number_input("Maximum new matches per search", min_value=1, max_value=50,
                                      value=settings["max_new"])
            minimum_score = st.slider("Minimum fit score", 0, 100, settings["minimum_score"], step=5)
            saved = st.form_submit_button("Save search schedule", type="primary")
        if saved:
            try:
                if enabled:
                    if not key:
                        raise ValueError("Connect Hunter before enabling automatic search.")
                    if not profile.get("job_titles"):
                        raise ValueError("Add a target job title before enabling automatic qualification.")
                    if uses_company_discovery(profile):
                        discover_filters(profile)
                    else:
                        search_parameters(profile)
                prospect_discovery.save_settings(profile["id"], {
                    "enabled": enabled, "frequency": frequency, "hour": hour,
                    "max_new": int(max_new), "minimum_score": int(minimum_score),
                })
                st.success("Search schedule saved.")
                st.rerun()
            except (ValueError, OSError, json.JSONDecodeError, psycopg2.Error) as exc:
                st.error(str(exc))
        st.caption("Scheduled searches use no reveal credits and never send messages or import Leads. The separate background worker must be running with HUNTER_API_KEY to search while this page is closed.")
    if st.button("Run discovery now", disabled=not bool(key), key=f"discovery_run_{profile['id']}"):
        try:
            with st.spinner("Finding good-fit prospects…"):
                result = prospect_discovery.scan_once(profile, key, force=True)
            st.success(f"Found {result['last_added']} new matches for review.")
            st.rerun()
        except (ProspectSourceError, ValueError, OSError, json.JSONDecodeError, psycopg2.Error) as exc:
            st.error(str(exc))


def render_prospect_search() -> None:
    """Render saved criteria and the authenticated, read-only source search."""
    from crm_workspace import render_section_header

    render_section_header("find-prospects", "Find prospects", "person_search", "teal")
    st.caption("Define the people and regions Sharngon should look for, then preview masked matches from Hunter.")
    try:
        profiles = load_profiles()
    except (OSError, json.JSONDecodeError, psycopg2.Error, ValueError):
        st.error("Saved searches are temporarily unavailable. Your existing CRM leads were not changed.")
        return

    regions = {country.casefold() for profile in profiles for country in profile["countries"]}
    a, b, c = st.columns(3)
    a.metric("Saved searches", len(profiles))
    b.metric("Target countries", len(regions))
    api_key = _hunter_key()
    c.metric("Source configured", 1 if api_key else 0)

    options = ["new", *[profile["id"] for profile in profiles]]
    names = {profile["id"]: profile["name"] for profile in profiles}
    next_choice = st.session_state.pop("prospect_choice_next", None)
    if next_choice in options:
        st.session_state["prospect_selected_search"] = next_choice
    elif "prospect_selected_search" not in st.session_state:
        try:
            last_selected = prospect_progress.load_last_selected()
            if last_selected in options:
                st.session_state["prospect_selected_search"] = last_selected
        except (OSError, json.JSONDecodeError, psycopg2.Error, ValueError):
            st.warning("The last selected search could not be restored. Choose it from the list.")
    selected_id = st.selectbox(
        "Search to edit", options, format_func=lambda key: "Create a new search" if key == "new" else names[key],
        key="prospect_selected_search",
    )
    selected = next((profile for profile in profiles if profile["id"] == selected_id), {})
    if st.session_state.get("hunter_last_selected_session") != selected_id:
        try:
            prospect_progress.save_last_selected(selected_id)
            st.session_state["hunter_last_selected_session"] = selected_id
        except (OSError, json.JSONDecodeError, psycopg2.Error, ValueError):
            st.warning("The selected search could not be saved for your next visit.")
    if selected:
        try:
            _activate_progress(selected)
        except (OSError, json.JSONDecodeError, psycopg2.Error, TypeError, ValueError):
            st.warning("Saved progress could not be restored. Your saved search criteria are still available.")
    _show_discovery_panel(selected, api_key)
    with st.expander("Target criteria", expanded=selected_id == "new"):
        with st.form(f"prospect_criteria_{selected_id}"):
            name = st.text_input("Search name", value=selected.get("name", ""), placeholder="Product leaders in India")
            col1, col2, col3 = st.columns(3)
            with col1:
                countries = st.text_area("Countries / regions *", value="\n".join(selected.get("countries", [])), placeholder="India\nSingapore", height=104)
            with col2:
                states = st.text_area("States / provinces", value="\n".join(selected.get("states", [])), placeholder="Karnataka\nMaharashtra", height=104)
            with col3:
                cities = st.text_area("Cities", value="\n".join(selected.get("cities", [])), placeholder="Bengaluru\nMumbai", height=104)
            col1, col2 = st.columns(2)
            with col1:
                industries = st.text_area("Industries", value="\n".join(selected.get("industries", [])), placeholder="Software\nFinancial services", height=96)
                size_min = st.number_input("Minimum company employees", min_value=0, max_value=1_000_000, value=int(selected.get("size_min") or 0), step=10)
            with col2:
                job_titles = st.text_area("Job titles", value="\n".join(selected.get("job_titles", [])), placeholder="Product Manager\nL&D Head", height=96)
                size_max = st.number_input("Maximum company employees", min_value=0, max_value=1_000_000, value=int(selected.get("size_max") or 0), step=10)
            st.markdown("**Select experience (years)**")
            exp_col1, exp_col2 = st.columns(2)
            with exp_col1:
                experience_min = st.number_input("Minimum experience", min_value=0, max_value=80,
                                                 value=int(selected.get("experience_min") or 0),
                                                 help="0 means no minimum.")
            with exp_col2:
                experience_max = st.number_input("Maximum experience", min_value=0, max_value=80,
                                                 value=int(selected.get("experience_max") or 0),
                                                 help="0 means no maximum.")
            st.caption("Hunter cannot search by years of experience. The saved range checks confirmed experience in your review list.")
            keywords = st.text_area("Relevant keywords", value="\n".join(selected.get("keywords", [])), placeholder="AI training, analytics workshop", height=80)
            exclusions = st.text_area("Exclude keywords", value="\n".join(selected.get("exclusions", [])), placeholder="Recruiter, student", height=80)
            st.caption("Enter one value per line or separate values with commas. Leave company size at 0 for no limit.")
            submitted = st.form_submit_button("Save search criteria", type="primary", icon=":material/save:")
        if submitted:
            fields = dict(countries=countries, states=states, cities=cities, industries=industries,
                          job_titles=job_titles, keywords=keywords, exclusions=exclusions)
            try:
                profile = _new_profile(selected_id if selected_id != "new" else uuid4().hex,
                                       name, fields, size_min, size_max, experience_min, experience_max)
                if any(existing["name"].casefold() == profile["name"].casefold() and existing["id"] != profile["id"] for existing in profiles):
                    raise ValueError("A saved search with this name already exists.")
                if selected_id == "new" and len(profiles) >= 30:
                    raise ValueError("You can save up to 30 searches. Edit an existing search instead.")
                updated = [profile if existing["id"] == profile["id"] else existing for existing in profiles]
                if selected_id == "new":
                    updated.append(profile)
                save_profiles(updated)
            except (ValueError, OSError, psycopg2.Error) as exc:
                st.error(str(exc))
            else:
                st.session_state["prospect_choice_next"] = profile["id"]
                st.session_state["prospect_save_notice"] = True
                st.rerun()

    if st.session_state.pop("prospect_save_notice", False):
        st.success("Search criteria saved.")

    if profiles:
        with st.expander(f"Saved searches · {len(profiles)}", expanded=False):
            for profile in profiles:
                with st.container(border=True):
                    st.markdown(f"**{profile['name']}**")
                    st.caption("Regions: " + ", ".join(profile["countries"] + profile["states"] + profile["cities"]))
                    st.caption("Targets: " + ", ".join(profile["industries"] + profile["job_titles"] + profile["keywords"]))
    _show_hunter_preview(selected, api_key)
    st.info("Find a prospect, review the masked or revealed details, then import selected records into Leads. No messages are sent automatically.")
