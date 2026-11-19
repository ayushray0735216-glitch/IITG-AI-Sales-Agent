"""Predicates for the CRM's built-in saved views."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


_CONVERTED_STATUSES = {
    "converted",
    "won",
    "closed won",
    "customer",
    "converted to customer",
}
_INACTIVE_STATUSES = {"closed", "not interested"} | _CONVERTED_STATUSES
_HIGH_INTENT = {
    "ready to buy",
    "request for demo",
    "demo requested",
    "call request",
    "call scheduled",
    "pricing question",
    "pricing requested",
}


def build_crm_saved_view_masks(
    leads: pd.DataFrame,
    activity: pd.DataFrame | None = None,
    today: date | None = None,
    recent_days: int = 7,
) -> dict[str, pd.Series]:
    """Return index-aligned masks for the built-in lead views.

    Missing next-follow-up dates use the CRM's existing three-day estimate.
    "Recently active" uses the newest of Last Interaction and sales activity
    timestamps and includes the last ``recent_days`` calendar days.
    """
    today = today or date.today()
    activity = activity if activity is not None else pd.DataFrame()

    def column(name: str) -> pd.Series:
        return leads.get(name, pd.Series("", index=leads.index)).fillna("").astype(str).str.strip()

    def as_date(value: object) -> date | None:
        parsed = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()

    status = column("Status").str.casefold()
    open_lead = ~status.isin(_INACTIVE_STATUSES)
    last_intent = column("Last Intent").str.casefold()
    priority_signal = status
    score = pd.to_numeric(column("Interest Score"), errors="coerce").fillna(0)
    high_intent = open_lead & (
        score.ge(7)
        | last_intent.isin(_HIGH_INTENT)
        | priority_signal.isin(_HIGH_INTENT)
    )

    due_date = column("Next Follow-up").map(as_date)
    last_interaction = column("Last Interaction").map(as_date)
    inferred = due_date.isna() & last_interaction.notna()
    due_date.loc[inferred] = last_interaction.loc[inferred].map(
        lambda value: value + timedelta(days=3)
    )
    needs_follow_up = open_lead & due_date.map(
        lambda value: value is not None and value <= today
    )

    source = column("Lead Source").str.casefold()
    visitor_id = column("Visitor ID").str.casefold()
    name = column("Name").str.casefold()
    is_walk_in = (
        source.str.startswith("ai chat")
        | visitor_id.ne("")
        | name.str.startswith("anonymous visitor ")
    )
    no_shared_contact = (
        column("Contact Name").eq("")
        & column("Email").eq("")
        & column("Phone").eq("")
    )
    anonymous = is_walk_in & no_shared_contact & (
        visitor_id.ne("") | name.str.startswith("anonymous visitor ")
    )

    latest_activity = pd.Series(last_interaction, index=leads.index, dtype=object)
    if {"lead", "timestamp"}.issubset(activity.columns):
        activity_by_name: dict[str, date] = {}
        for _, row in activity.iterrows():
            lead_name = str(row.get("lead", "") or "").strip().casefold()
            activity_date = as_date(row.get("timestamp", ""))
            if lead_name and activity_date is not None:
                if lead_name not in activity_by_name or activity_date > activity_by_name[lead_name]:
                    activity_by_name[lead_name] = activity_date

        for index, lead_name in name.items():
            activity_date = activity_by_name.get(lead_name)
            current_date = latest_activity.at[index]
            if activity_date is not None and (
                current_date is None or pd.isna(current_date) or activity_date > current_date
            ):
                latest_activity.at[index] = activity_date

    recent_cutoff = today - timedelta(days=max(0, recent_days - 1))
    recently_active = open_lead & latest_activity.map(
        lambda value: value is not None
        and not pd.isna(value)
        and recent_cutoff <= value <= today
    )

    return {
        "needs_follow_up": needs_follow_up,
        "high_intent": high_intent,
        "anonymous_visitors": anonymous,
        "recently_active": recently_active,
    }
