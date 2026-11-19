"""Scheduled, credit-free Hunter discovery and explainable prospect review."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import crm_storage
import prospect_progress
from hunter_prospect_source import ProspectSourceError, qualify_people, search_profile

load_dotenv()
IST = ZoneInfo("Asia/Kolkata")
DEFAULT_SETTINGS = {"enabled": False, "frequency": "daily", "hour": 9,
                    "max_new": 25, "minimum_score": 70}
MAX_QUEUE = 500


def _key(profile_id: str) -> str:
    return "discovery:" + profile_id


def criteria_fingerprint(profile: dict) -> str:
    fields = ("countries", "states", "cities", "industries", "job_titles",
              "keywords", "exclusions", "size_min", "size_max")
    data = {field: profile.get(field) for field in fields}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def load_state(profile_id: str) -> dict:
    raw = prospect_progress.load_search(_key(profile_id))
    settings = {**DEFAULT_SETTINGS, **(raw.get("settings") or {})}
    settings["enabled"] = bool(settings["enabled"])
    settings["frequency"] = settings["frequency"] if settings["frequency"] in {"daily", "weekly"} else "daily"
    for field, low, high in (("hour", 0, 23), ("max_new", 1, 50), ("minimum_score", 0, 100)):
        try:
            settings[field] = max(low, min(int(settings[field]), high))
        except (ValueError, TypeError):
            settings[field] = DEFAULT_SETTINGS[field]
    return {"settings": settings, "fingerprint": str(raw.get("fingerprint") or ""),
            "cursor": str(raw.get("cursor") or ""), "queue": raw.get("queue") if isinstance(raw.get("queue"), dict) else {},
            "seen": raw.get("seen") if isinstance(raw.get("seen"), list) else [],
            "last_success": str(raw.get("last_success") or ""),
            "last_attempt": str(raw.get("last_attempt") or ""),
            "last_error": str(raw.get("last_error") or ""),
            "last_added": int(raw.get("last_added") or 0),
            "coverage_note": str(raw.get("coverage_note") or "")}


def save_state(profile_id: str, state: dict) -> None:
    prospect_progress.save_search(_key(profile_id), state)


def save_settings(profile_id: str, settings: dict) -> dict:
    state = load_state(profile_id)
    updated = dict(state["settings"])
    updated.update(settings)
    updated["enabled"] = bool(updated["enabled"])
    if updated["frequency"] not in {"daily", "weekly"}:
        raise ValueError("Choose daily or weekly discovery.")
    for field, low, high in (("hour", 0, 23), ("max_new", 1, 50), ("minimum_score", 0, 100)):
        updated[field] = int(updated[field])
        if not low <= updated[field] <= high:
            raise ValueError(f"{field} must be between {low} and {high}.")
    state["settings"] = updated
    save_state(profile_id, state)
    return state


def _identity(person: dict) -> str:
    # Hunter handles can change at the billing boundary. Keep a stable masked identity.
    basis = "|".join(str(person.get(field) or "").strip().casefold()
                     for field in ("name", "title", "domain"))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _words(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text).casefold()))


def qualification(person: dict, profile: dict, advanced: bool) -> tuple[int, list[str]]:
    """Score observable fit only; never infer customer interest from a profile."""
    title = _words(person.get("title", ""))
    targets = [_words(item) for item in profile.get("job_titles", [])]
    if not targets or not any(f" {target} " in f" {title} " for target in targets if target):
        return 0, ["Target job title not confirmed"]
    if not person.get("handle") or not person.get("company"):
        return 0, ["Missing a reviewable Hunter record or company"]
    reasons = ["Target job title matched"]
    score = 40
    if advanced:
        score += 15
        reasons.append("Saved company filters applied by Hunter")
    else:
        score += 10
        reasons.append("Saved company country applied by Hunter")
    if str(person.get("verification") or "").casefold() == "valid":
        score += 20
        reasons.append("Work email marked valid by Hunter; address remains hidden")
    if person.get("domain"):
        score += 10
        reasons.append("Company domain available")
    if person.get("decision_maker"):
        score += 10
        reasons.append("Hunter marks this role as a decision maker")
    if person.get("linkedin_exists"):
        score += 5
        reasons.append("Public professional profile available")
    return min(score, 100), reasons


def suggested_threshold(queue: dict) -> int | None:
    """Suggest a cutoff only when staff decisions clearly separate fit scores."""
    accepted = [row["score"] for row in queue.values()
                if row.get("status") in {"reviewed", "imported"} and isinstance(row.get("score"), int)]
    rejected = [row["score"] for row in queue.values()
                if row.get("status") == "rejected" and isinstance(row.get("score"), int)]
    if len(accepted) < 2 or len(rejected) < 2 or min(accepted) <= max(rejected):
        return None
    return round((min(accepted) + max(rejected)) / 2)


def _due(state: dict, now: datetime) -> bool:
    settings = state["settings"]
    if not settings["enabled"] or now.hour < settings["hour"]:
        return False
    last = state["last_success"]
    if last:
        try:
            last_day = datetime.fromisoformat(last).astimezone(IST).date()
            days = (now.date() - last_day).days
            if days < (1 if settings["frequency"] == "daily" else 7):
                return False
        except ValueError:
            pass
    attempted = state["last_attempt"]
    if attempted:
        try:
            if now - datetime.fromisoformat(attempted).astimezone(IST) < timedelta(hours=1):
                return False
        except ValueError:
            pass
    return True


def _crm_hunter_keys() -> set[str]:
    leads = crm_storage.load_crm_table("leads.csv")
    if leads.empty or "Hunter Prospect Key" not in leads:
        return set()
    return set(leads["Hunter Prospect Key"].fillna("").astype(str))


def scan_once(profile: dict, api_key: str, *, now: datetime | None = None,
              force: bool = False, search=search_profile) -> dict:
    """Run one bounded search; do not reveal emails, import leads or send messages."""
    now = (now or datetime.now(IST)).astimezone(IST)
    state = load_state(profile["id"])
    if not profile.get("job_titles"):
        raise ValueError("Add at least one target job title before automatic qualification.")
    fingerprint = criteria_fingerprint(profile)
    if state["fingerprint"] != fingerprint:
        state.update(fingerprint=fingerprint, cursor="", seen=[], queue={}, last_success="", last_attempt="")
    if not force and not _due(state, now):
        return {**state, "skipped": True}
    if not api_key:
        state["last_attempt"] = now.isoformat()
        state["last_error"] = "Add HUNTER_API_KEY to the worker environment before scheduling discovery."
        save_state(profile["id"], state)
        raise ValueError(state["last_error"])
    state["last_attempt"] = now.isoformat()
    try:
        from hunter_prospect_source import uses_company_discovery
        advanced = uses_company_discovery(profile)
        if str(state["cursor"]).startswith("company:"):
            state["cursor"] = ""
            state["coverage_note"] = ""
        result = search(profile, api_key, state["cursor"] or None, True,
                        min(state["settings"]["max_new"], 50))
        visible, _ = qualify_people(result["people"], profile, True, 2)
        seen = set(state["seen"])
        imported = _crm_hunter_keys()
        added = 0
        pending_count = sum(row.get("status") == "pending" for row in state["queue"].values())
        for person in visible:
            identity = _identity(person)
            if identity in seen or "import:hunter:" + hashlib.sha256(
                person["handle"].encode("utf-8")).hexdigest() in imported:
                continue
            score, reasons = qualification(person, profile, advanced)
            if score < state["settings"]["minimum_score"]:
                continue
            if pending_count + added >= MAX_QUEUE or added >= state["settings"]["max_new"]:
                break
            state["queue"][identity] = {"person": person, "score": score, "reasons": reasons,
                                         "status": "pending", "found_at": now.isoformat()}
            seen.add(identity)
            added += 1
        state["seen"] = list(seen)[-10000:]
        if len(state["queue"]) > 5000:
            completed = [identity for identity, row in state["queue"].items()
                         if row.get("status") != "pending"]
            for identity in completed[:len(state["queue"]) - 5000]:
                state["queue"].pop(identity, None)
        state["cursor"] = str(result.get("next_cursor") or "")
        state["last_success"] = now.isoformat()
        state["last_error"] = ""
        state["last_added"] = added
        state["coverage_note"] = str(result.get("coverage_note") or "")
    except (ProspectSourceError, ValueError) as exc:
        state["last_error"] = str(exc)
        save_state(profile["id"], state)
        raise
    save_state(profile["id"], state)
    return {**state, "skipped": False}


def mark_status(profile_id: str, identities: list[str], status: str) -> None:
    if status not in {"reviewed", "rejected", "imported"}:
        raise ValueError("Unknown prospect status.")
    state = load_state(profile_id)
    for identity in identities:
        if identity in state["queue"]:
            state["queue"][identity]["status"] = status
    save_state(profile_id, state)


def mark_imported_handles(profile_id: str, handles: list[str]) -> None:
    state = load_state(profile_id)
    wanted = set(handles)
    for row in state["queue"].values():
        if row.get("person", {}).get("handle") in wanted:
            row["status"] = "imported"
    save_state(profile_id, state)


def scan_due_profiles(*, now: datetime | None = None, search=search_profile) -> list[dict]:
    """Called by the existing background worker, independently of follow-up policy."""
    from prospect_search import load_profiles
    key = os.getenv("HUNTER_API_KEY", "").strip()
    outcomes = []
    processed = 0
    for profile in load_profiles():
        state = load_state(profile["id"])
        if not state["settings"]["enabled"]:
            continue
        if processed >= 3:
            break
        try:
            result = scan_once(profile, key, now=now, search=search)
            if not result.get("skipped"):
                processed += 1
                outcomes.append({"search": profile["name"], "added": result["last_added"], "error": ""})
        except (ProspectSourceError, ValueError, OSError) as exc:
            processed += 1
            outcomes.append({"search": profile["name"], "added": 0, "error": str(exc)})
    return outcomes
