"""Helpers for assembling a lead's conversation and activity history."""

from __future__ import annotations

import pandas as pd


def _text(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def filter_conversation_history(history: pd.DataFrame, lead) -> pd.DataFrame:
    """Return messages belonging to a lead, preferring stable visitor IDs."""
    if history is None or history.empty:
        return pd.DataFrame(columns=list(history.columns) if history is not None else [])

    visitor_id = _text(lead.get("Visitor ID", ""))
    name = _text(lead.get("Name", ""))
    matches = pd.Series(False, index=history.index)

    if visitor_id and "visitor_id" in history.columns:
        matches |= history["visitor_id"].map(_text).eq(visitor_id)
    if name and "lead" in history.columns:
        matches |= history["lead"].map(_text).str.casefold().eq(name.casefold())

    result = history.loc[matches].copy()
    if "timestamp" in result.columns:
        result["_sort_time"] = pd.to_datetime(result["timestamp"], errors="coerce", utc=True)
        result = result.sort_values(["_sort_time"], kind="stable", na_position="last")
        result = result.drop(columns="_sort_time")
    return result.reset_index(drop=True)


def build_activity_timeline(
    history: pd.DataFrame,
    activity: pd.DataFrame,
    lead,
) -> pd.DataFrame:
    """Merge the selected lead's conversation and sales events chronologically."""
    events = []
    messages = filter_conversation_history(history, lead)

    for _, message in messages.iterrows():
        role = _text(message.get("role", "")).casefold()
        events.append({
            "timestamp": _text(message.get("timestamp", "")),
            "Type": "Customer message" if role == "user" else "Assistant reply",
            "Details": _text(message.get("content", "")),
        })

    if activity is not None and not activity.empty and "lead" in activity.columns:
        name = _text(lead.get("Name", ""))
        selected = activity.loc[
            activity["lead"].map(_text).str.casefold().eq(name.casefold())
        ]
        for _, row in selected.iterrows():
            details = _text(row.get("action", "Sales activity"))
            context = [
                f"Intent: {_text(row.get('intent', ''))}",
                f"Priority: {_text(row.get('priority', ''))}",
            ]
            context = [item for item in context if not item.endswith(": ")]
            if context:
                details += " · " + " · ".join(context)
            events.append({
                "timestamp": _text(row.get("timestamp", "")),
                "Type": "Sales activity",
                "Details": details,
            })

    result = pd.DataFrame(events, columns=["timestamp", "Type", "Details"])
    if not result.empty:
        result["_sort_time"] = pd.to_datetime(result["timestamp"], errors="coerce", utc=True)
        result = result.sort_values(["_sort_time"], kind="stable", na_position="last")
        result = result.drop(columns="_sort_time").reset_index(drop=True)
    return result
