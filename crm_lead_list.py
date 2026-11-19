"""Compact, safe-to-display fields for the CRM lead table."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


LEAD_LIST_COLUMNS = [
    "Name / Visitor ID",
    "Product",
    "Intent",
    "Priority",
    "Status",
    "Last activity",
    "Next follow-up",
]

_STATUS_LABELS = {
    "new": "New",
    "conversation started": "Conversation started",
    "contact details requested": "Contact details requested",
    "ready to buy": "Ready to buy",
    "demo requested": "Demo requested",
    "call scheduled": "Call scheduled",
    "pricing requested": "Pricing requested",
    "information requested": "Information requested",
    "follow-up required": "Follow-up required",
    "interested": "Interested",
    "escalated": "Escalated",
    "not interested": "Not interested",
    "closed": "Closed",
}


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    value = str(value).strip()
    return "" if value.casefold() in {"nan", "none", "null"} else value


def _as_date(value: object) -> date | None:
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed.date()


def _pretty_date(value: date) -> str:
    return f"{value.day} {value.strftime('%b %Y')}"


def build_clean_lead_list(
    leads: pd.DataFrame,
    activity: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return only the seven default list fields; source records are untouched."""
    activity = activity if activity is not None else pd.DataFrame()
    latest_activity: dict[str, date] = {}
    if {"lead", "timestamp"}.issubset(activity.columns):
        for _, row in activity.iterrows():
            name_key = _text(row.get("lead", "")).casefold()
            activity_date = _as_date(row.get("timestamp", ""))
            if name_key and activity_date is not None:
                if name_key not in latest_activity or activity_date > latest_activity[name_key]:
                    latest_activity[name_key] = activity_date

    display_rows = []
    for _, lead in leads.iterrows():
        name = _text(lead.get("Name", ""))
        visitor_id = _text(lead.get("Visitor ID", ""))
        contact_name = _text(lead.get("Contact Name", ""))
        has_shared_contact = any(
            _text(lead.get(column, ""))
            for column in ("Contact Name", "Email", "Phone")
        )
        if has_shared_contact and contact_name:
            name = contact_name
        elif visitor_id and not has_shared_contact:
            name = f"Anonymous visitor · {visitor_id}"
        elif not name:
            name = f"Visitor {visitor_id}" if visitor_id else "Anonymous visitor"

        raw_status = _text(lead.get("Status", ""))
        status = _STATUS_LABELS.get(raw_status.casefold(), raw_status or "Not recorded")
        raw_priority = _text(lead.get("Priority", ""))
        priority = raw_priority.title() if raw_priority else "Not rated"

        last_interaction = _as_date(lead.get("Last Interaction", ""))
        recent_activity = latest_activity.get(name.casefold())
        if last_interaction and (
            recent_activity is None or last_interaction > recent_activity
        ):
            recent_activity = last_interaction
        last_activity_label = (
            _pretty_date(recent_activity)
            if recent_activity
            else "Not recorded"
        )

        explicit_follow_up = _as_date(lead.get("Next Follow-up", ""))
        estimated = False
        if explicit_follow_up is None and last_interaction is not None:
            explicit_follow_up = last_interaction + timedelta(days=3)
            estimated = True
        if explicit_follow_up is None:
            follow_up_label = "Not scheduled"
        else:
            follow_up_label = _pretty_date(explicit_follow_up)
            if estimated:
                follow_up_label += " · estimated"

        display_rows.append(
            {
                "Name / Visitor ID": name,
                "Product": _text(lead.get("Product", "")) or "Not specified",
                "Intent": _text(lead.get("Last Intent", "")) or "Not recorded",
                "Priority": priority,
                "Status": status,
                "Last activity": last_activity_label,
                "Next follow-up": follow_up_label,
            }
        )

    return pd.DataFrame(display_rows, columns=LEAD_LIST_COLUMNS, index=leads.index)
