"""Deterministic ranking helpers for the CRM's Prioritize Today queue."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import inf

import pandas as pd


QUEUE_COLUMNS = [
    "Rank",
    "Name",
    "Company",
    "Follow-up Due",
    "Buying Intent",
    "Interest Score",
    "Recent Activity",
    "Why This Rank",
]

_CLOSED_VALUES = {"closed", "not interested"}
_INTENT_RANK = {
    "ready to buy": 9,
    "request for demo": 8,
    "call request": 7,
    "pricing question": 7,
    "interested": 6,
    "request for more information": 5,
    "product question": 5,
    "objection": 4,
    "other": 2,
}
_STATUS_INTENT = {
    "ready to buy": "Ready to Buy",
    "demo requested": "Request for Demo",
    "call scheduled": "Call Request",
    "pricing requested": "Pricing Question",
    "information requested": "Request for More Information",
    "interested": "Interested",
    "follow-up required": "Interested",
}


def _text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _as_date(value: object) -> date | None:
    if not _text(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _activity_dates(activity: pd.DataFrame) -> dict[str, date]:
    if activity.empty or not {"lead", "timestamp"}.issubset(activity.columns):
        return {}

    recent: dict[str, date] = {}
    for _, row in activity.iterrows():
        lead_key = _text(row.get("lead", "")).casefold()
        activity_date = _as_date(row.get("timestamp", ""))
        if not lead_key or activity_date is None:
            continue
        if lead_key not in recent or activity_date > recent[lead_key]:
            recent[lead_key] = activity_date
    return recent


def build_prioritize_today_queue(
    leads: pd.DataFrame,
    activity: pd.DataFrame | None = None,
    today: date | None = None,
) -> pd.DataFrame:
    """Rank open leads by due date, buying intent, interest, then activity.

    Existing rows without a scheduled follow-up date use the CRM's established
    three-day follow-up window after their last interaction. The inferred date
    is only displayed in this queue; the source row is never modified.
    """
    today = today or date.today()
    activity = activity if activity is not None else pd.DataFrame()
    latest_activity = _activity_dates(activity)
    ranked: list[tuple[tuple, dict]] = []

    for position, (_, lead) in enumerate(leads.iterrows()):
        status = _text(lead.get("Status", ""))
        raw_intent = _text(lead.get("Last Intent", ""))
        if status.casefold() in _CLOSED_VALUES or raw_intent.casefold() == "not interested":
            continue

        buying_intent = raw_intent or _STATUS_INTENT.get(status.casefold(), "")
        intent_rank = _INTENT_RANK.get(buying_intent.casefold(), 1)

        try:
            interest_score = float(pd.to_numeric(lead.get("Interest Score", 0), errors="coerce"))
            if pd.isna(interest_score):
                interest_score = 0.0
        except (TypeError, ValueError):
            interest_score = 0.0

        last_interaction = _as_date(lead.get("Last Interaction", ""))
        explicit_due = _as_date(lead.get("Next Follow-up", ""))
        due_date = explicit_due
        estimated_due = False
        if due_date is None and last_interaction is not None:
            due_date = last_interaction + timedelta(days=3)
            estimated_due = True

        if due_date is None:
            due_label = "No date scheduled"
            due_reason = "No follow-up date recorded"
            due_group = 3
            due_sort = date.max
        else:
            day_delta = (due_date - today).days
            if day_delta < 0:
                due_label = f"Overdue by {-day_delta} day(s)"
                due_reason = f"Follow-up overdue by {-day_delta} day(s)"
            elif day_delta == 0:
                due_label = "Due today"
                due_reason = "Follow-up due today"
            else:
                due_label = f"{due_date.isoformat()} (in {day_delta} day(s))"
                due_reason = f"Follow-up due in {day_delta} day(s)"
            if estimated_due:
                due_label += " · estimated"
                due_reason += " (estimated from last interaction + 3 days)"
            due_group = 0 if day_delta < 0 else 1 if day_delta == 0 else 2
            due_sort = due_date

        activity_date = latest_activity.get(_text(lead.get("Name", "")).casefold())
        if last_interaction is not None and (activity_date is None or last_interaction > activity_date):
            activity_date = last_interaction
        if activity_date is None:
            recent_label = "No activity recorded"
            activity_days = inf
            activity_reason = "No recent activity recorded"
        else:
            activity_days = max(0, (today - activity_date).days)
            if activity_days == 0:
                recent_label = "Today"
                activity_reason = "Active today"
            elif activity_days == 1:
                recent_label = "Yesterday"
                activity_reason = "Active yesterday"
            else:
                recent_label = f"{activity_days} days ago"
                activity_reason = f"Last active {activity_days} days ago"

        intent_reason = (
            f"Buying intent: {buying_intent}"
            if buying_intent
            else "Buying intent not recorded"
        )
        reason = " · ".join(
            [
                due_reason,
                intent_reason,
                f"Interest {interest_score:.1f}/10",
                activity_reason,
            ]
        )
        record = {
            "Name": _text(lead.get("Name", "")),
            "Company": _text(lead.get("Company", "")),
            "Follow-up Due": due_label,
            "Buying Intent": buying_intent or "Not recorded",
            "Interest Score": round(interest_score, 1),
            "Recent Activity": recent_label,
            "Why This Rank": reason,
        }
        sort_key = (
            due_group,
            due_sort,
            -intent_rank,
            -interest_score,
            activity_days,
            position,
        )
        ranked.append((sort_key, record))

    ranked.sort(key=lambda item: item[0])
    rows = []
    for rank, (_, record) in enumerate(ranked, start=1):
        rows.append({"Rank": rank, **record})
    return pd.DataFrame(rows, columns=QUEUE_COLUMNS)
