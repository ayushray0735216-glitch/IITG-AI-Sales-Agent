"""Eligibility rules for the reviewed Autopilot follow-up queue."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, time, timedelta, timezone

import pandas as pd


TERMINAL_STATUSES = {
    "closed",
    "converted",
    "won",
    "customer",
    "not interested",
    "unsubscribed",
}


def _text(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _identity(lead) -> str:
    return (
        _text(lead.get("Visitor ID", ""))
        or _text(lead.get("Email", "")).casefold()
        or _text(lead.get("Name", "")).casefold()
    )


def _consent_methods(lead) -> set[str]:
    consent = _text(lead.get("Contact Consent", "")).casefold()
    if consent not in {"yes", "true", "1", "consented", "opted in", "opted-in"}:
        return set()
    raw = _text(lead.get("Preferred Contact Methods", "")).casefold()
    methods = set(re.findall(r"[a-z]+", raw))
    return methods


def _has_method(methods: set[str], channel: str) -> bool:
    if channel == "email":
        return "email" in methods
    if channel == "phone":
        return bool(methods.intersection({"phone", "call", "calls"}))
    return False


def _contact_history(lead, activity: pd.DataFrame, now: datetime):
    if activity is None or activity.empty or "lead" not in activity.columns:
        return 0, None
    identity_values = {
        _text(lead.get("Name", "")).casefold(),
        _text(lead.get("Email", "")).casefold(),
        _text(lead.get("Visitor ID", "")).casefold(),
    } - {""}
    matching = activity[
        activity["lead"].fillna("").astype(str).str.strip().str.casefold().isin(identity_values)
    ].copy()
    if matching.empty:
        return 0, None

    intents = matching.get("intent", pd.Series("", index=matching.index)).fillna("").astype(str).str.casefold()
    actions = matching.get("action", pd.Series("", index=matching.index)).fillna("").astype(str).str.casefold()
    is_contact = (
        intents.isin({"crm email", "crm information", "crm call"})
        | actions.str.contains(r"email sent successfully|call initiated", regex=True)
    )
    matching = matching[is_contact]
    if matching.empty:
        return 0, None

    timestamps = pd.to_datetime(
        matching.get("timestamp", pd.Series(dtype=str)),
        errors="coerce",
        utc=True,
    ).dropna()
    if timestamps.empty:
        return 0, None
    cutoff = pd.Timestamp(now.astimezone(timezone.utc) - timedelta(days=30))
    recent = timestamps[timestamps >= cutoff]
    latest = timestamps.max().to_pydatetime()
    return int(len(recent)), latest


def _next_contact_time(now: datetime, start_hour: int, end_hour: int) -> datetime:
    start = time(start_hour)
    end = time(end_hour)
    if start <= now.time() < end:
        return now.replace(second=0, microsecond=0)
    if now.time() >= end:
        tomorrow = now.date() + timedelta(days=1)
        return datetime.combine(tomorrow, start, tzinfo=now.tzinfo)
    return datetime.combine(now.date(), start, tzinfo=now.tzinfo)


def _choose_channel(lead, methods: set[str], channels: dict) -> tuple[str, str] | tuple[None, str]:
    intent = _text(lead.get("Last Intent", "")).casefold()
    wants_call = any(term in intent for term in ("ready to buy", "demo", "call request"))
    preference_order = ["phone", "email"] if wants_call else ["email", "phone"]
    for channel in preference_order:
        if not channels.get(channel, False) or not _has_method(methods, channel):
            continue
        address = _text(lead.get("Phone" if channel == "phone" else "Email", ""))
        if channel == "email" and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address):
            return channel, address
        if channel == "phone" and re.fullmatch(r"\+[1-9]\d{7,14}", address):
            return channel, address
    return None, "No enabled channel has both recorded consent and contact details."


def _is_suppressed(destination: str, channel: str, suppressions: pd.DataFrame | None) -> bool:
    if suppressions is None or suppressions.empty:
        return False
    if not {"destination", "channel"}.issubset(suppressions.columns):
        return False
    destinations = suppressions["destination"].fillna("").astype(str).str.strip().str.casefold()
    channels = suppressions["channel"].fillna("").astype(str).str.strip().str.casefold()
    active = suppressions.get("active", pd.Series(True, index=suppressions.index))
    active = active.fillna(True).astype(str).str.casefold().isin({"true", "1", "yes"})
    return bool(
        (destinations.eq(destination.strip().casefold())
         & channels.isin({channel, "all"})
         & active).any()
    )


def contact_action_block_reason(
    lead,
    channel: str,
    destination: str,
    activity: pd.DataFrame | None = None,
    settings: dict | None = None,
    suppressions: pd.DataFrame | None = None,
    now: datetime | None = None,
) -> str:
    """Fail closed for contact consent, channel preference, suppression, and cadence."""
    now = now or datetime.now().astimezone()
    settings = settings or {}
    channel = str(channel or "").strip().casefold()
    destination = _text(destination)
    if channel not in {"email", "phone"}:
        return "This contact channel is not connected."
    methods = _consent_methods(lead)
    if not methods:
        return "Recorded contact consent is required before outreach."
    if not _has_method(methods, channel):
        return f"The customer has not selected {channel} as an approved contact method."
    if channel == "email":
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", destination):
            return "The email address is missing or invalid."
        if _text(lead.get("Email", "")).casefold() != destination.casefold():
            return "The destination does not match the email address stored for this lead."
    else:
        if not re.fullmatch(r"\+[1-9]\d{7,14}", destination):
            return "Use a valid phone number in international format, such as +919876543210."
        if _text(lead.get("Phone", "")) != destination:
            return "The destination does not match the phone number stored for this lead."
    if _text(lead.get("Status", "")).casefold() in TERMINAL_STATUSES:
        return "This lead is closed or has opted out of follow-up."
    if _is_suppressed(destination, channel, suppressions):
        return "This destination is on the CRM suppression list. Do not contact it."

    max_attempts = max(1, int(settings.get("max_attempts", 3)))
    cooldown_days = max(1, int(settings.get("cooldown_days", 2)))
    activity_frame = activity if activity is not None else pd.DataFrame()
    attempt_count, latest_contact = _contact_history(lead, activity_frame, now)
    if attempt_count >= max_attempts:
        return f"The CRM contact limit of {max_attempts} outreach attempts in 30 days has been reached."
    if latest_contact and now.astimezone(timezone.utc) - latest_contact < timedelta(days=cooldown_days):
        return f"Wait at least {cooldown_days} days between outreach attempts."
    return ""


def build_eligible_actions(
    leads: pd.DataFrame,
    activity: pd.DataFrame,
    settings: dict,
    existing_actions: pd.DataFrame | None = None,
    now: datetime | None = None,
    suppressions: pd.DataFrame | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Return idempotent review tasks and a concise count of excluded leads."""
    now = now or datetime.now().astimezone()
    counts = {
        "scanned": int(len(leads.index)) if leads is not None else 0,
        "not_due": 0,
        "estimated_due": 0,
        "terminal": 0,
        "no_consent_or_channel": 0,
        "attempt_limit": 0,
        "cooldown": 0,
        "already_queued": 0,
        "suppressed": 0,
        "invalid_contact": 0,
    }
    if leads is None or leads.empty or not settings.get("enabled", False):
        return [], counts

    channels = settings.get("channels", {})
    max_attempts = max(1, int(settings.get("max_attempts", 3)))
    cooldown_days = max(1, int(settings.get("cooldown_days", 2)))
    start_hour = int(settings.get("quiet_start_hour", 9))
    end_hour = int(settings.get("quiet_end_hour", 18))

    pending_identities = set()
    known_task_ids = set()
    if existing_actions is not None and not existing_actions.empty:
        for _, task in existing_actions.iterrows():
            task_id = _text(task.get("task_id", ""))
            if task_id:
                known_task_ids.add(task_id)
            if _text(task.get("status", "")).casefold() == "needs review":
                pending_identities.add(_text(task.get("lead_key", "")))

    tasks = []
    for _, lead in leads.iterrows():
        status = _text(lead.get("Status", "")).casefold()
        if status in TERMINAL_STATUSES:
            counts["terminal"] += 1
            continue

        due_value = _text(lead.get("Next Follow-up", ""))
        due = pd.to_datetime(due_value, errors="coerce") if due_value else pd.NaT
        due_estimated = False
        if pd.isna(due):
            last_interaction_value = _text(lead.get("Last Interaction", ""))
            last_interaction = (
                pd.to_datetime(last_interaction_value, errors="coerce")
                if last_interaction_value
                else pd.NaT
            )
            if pd.isna(last_interaction):
                counts["not_due"] += 1
                continue
            due_day = last_interaction.date() + timedelta(days=3)
            due_estimated = True
            counts["estimated_due"] += 1
        else:
            due_day = due.date()

        # Match the prioritization queue's date-based three-day estimate. A
        # date due today is eligible throughout the configured contact window.
        if due_day > now.date():
            counts["not_due"] += 1
            continue
        lead_key = _identity(lead)
        if not lead_key:
            counts["no_consent_or_channel"] += 1
            continue
        if lead_key in pending_identities:
            counts["already_queued"] += 1
            continue

        methods = _consent_methods(lead)
        if not methods:
            counts["no_consent_or_channel"] += 1
            continue
        channel, address_or_reason = _choose_channel(lead, methods, channels)
        if not channel:
            email_value = _text(lead.get("Email", ""))
            phone_value = _text(lead.get("Phone", ""))
            has_enabled_consent = any(
                channels.get(kind, False) and _has_method(methods, kind)
                for kind in ("email", "phone")
            )
            has_invalid_destination = has_enabled_consent and (
                (channels.get("email", False) and _has_method(methods, "email") and email_value
                 and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email_value))
                or (channels.get("phone", False) and _has_method(methods, "phone") and phone_value
                    and not re.fullmatch(r"\+[1-9]\d{7,14}", phone_value))
            )
            counts["invalid_contact" if has_invalid_destination else "no_consent_or_channel"] += 1
            continue
        if _is_suppressed(address_or_reason, channel, suppressions):
            fallback = "phone" if channel == "email" else "email"
            fallback_field = "Phone" if fallback == "phone" else "Email"
            fallback_address = _text(lead.get(fallback_field, ""))
            fallback_valid = bool(
                channels.get(fallback, False)
                and _has_method(methods, fallback)
                and (
                    re.fullmatch(r"\+[1-9]\d{7,14}", fallback_address)
                    if fallback == "phone"
                    else re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", fallback_address)
                )
            )
            if fallback_valid and not _is_suppressed(fallback_address, fallback, suppressions):
                channel, address_or_reason = fallback, fallback_address
            else:
                counts["suppressed"] += 1
                continue

        attempt_count, latest_contact = _contact_history(lead, activity, now)
        if attempt_count >= max_attempts:
            counts["attempt_limit"] += 1
            continue
        if (
            latest_contact
            and now.astimezone(timezone.utc) - latest_contact < timedelta(days=cooldown_days)
        ):
            counts["cooldown"] += 1
            continue

        intent = _text(lead.get("Last Intent", "")) or "Not recorded"
        priority = _text(lead.get("Priority", "")) or "Not rated"
        product = _text(lead.get("Product", ""))
        lead_name = _text(lead.get("Contact Name", "")) or _text(lead.get("Name", ""))
        due_at = due_day.isoformat() + (" · estimated" if due_estimated else "")
        schedule_at = _next_contact_time(now, start_hour, end_hour)
        reason = (
            ("Follow-up date estimated from last interaction + 3 days; " if due_estimated else "Follow-up is due; ")
            + (f"intent is {intent.lower()}" if intent != "Not recorded" else "intent is not recorded")
            + f"; priority is {priority.lower()}."
        )
        signature = f"{lead_key}|{channel}|{due_at}"
        task_id = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]
        if task_id in known_task_ids:
            counts["already_queued"] += 1
            continue

        tasks.append({
            "task_id": task_id,
            "lead_key": lead_key,
            "lead_name": lead_name,
            "visitor_id": _text(lead.get("Visitor ID", "")),
            "email": _text(lead.get("Email", "")),
            "phone": _text(lead.get("Phone", "")),
            "product": product,
            "intent": intent,
            "priority": priority,
            "channel": channel,
            "destination": address_or_reason,
            "reason": reason,
            "due_at": due_at,
            "due_estimated": due_estimated,
            "scheduled_at": schedule_at.strftime("%Y-%m-%d %H:%M %Z"),
            "attempts_last_30_days": attempt_count,
            "status": "Needs review",
            "created_at": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        })
        known_task_ids.add(task_id)
        pending_identities.add(lead_key)

    tasks.sort(
        key=lambda task: (
            0 if task["priority"].casefold() == "high" else 1,
            task["due_at"],
        )
    )
    return tasks, counts
