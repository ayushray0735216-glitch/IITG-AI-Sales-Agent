"""Counts and record filters for the compact CRM overview."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


def build_crm_overview(leads: pd.DataFrame, today: date | None = None) -> dict:
    """Return overview counts and index-aligned masks without changing leads.

    Legacy leads without an explicit next follow-up date use the same
    three-day estimate as the Prioritize Today queue. A closed lead is only
    considered converted when its status explicitly indicates conversion.
    """
    today = today or date.today()

    def column(name: str) -> pd.Series:
        return leads.get(name, pd.Series("", index=leads.index)).fillna("").astype(str).str.strip()

    def as_date(value: object) -> date | None:
        parsed = pd.to_datetime(value, errors="coerce")
        return None if pd.isna(parsed) else parsed.date()

    status = column("Status").str.casefold()
    priority = column("Priority").str.casefold()
    source = column("Lead Source").str.casefold()
    visitor_id = column("Visitor ID").str.casefold()
    name = column("Name").str.casefold()

    walk_in = (
        source.str.startswith("ai chat")
        | visitor_id.ne("")
        | name.str.startswith("anonymous visitor ")
    )
    converted = status.isin({"converted", "won", "closed won", "customer", "converted to customer"})
    open_lead = ~status.isin({"closed", "not interested"}) & ~converted
    new_lead = open_lead & status.isin({"new", "conversation started"})
    high_priority = open_lead & priority.eq("high")

    due_date = column("Next Follow-up").map(as_date)
    last_interaction = column("Last Interaction").map(as_date)
    inferred = due_date.isna() & last_interaction.notna()
    due_date.loc[inferred] = last_interaction.loc[inferred].map(
        lambda value: value + timedelta(days=3)
    )
    follow_up_due = open_lead & due_date.map(
        lambda value: value is not None and value <= today
    )

    file_based = ~walk_in

    masks = {
        "new": new_lead,
        "high_priority": high_priority,
        "follow_ups_due": follow_up_due,
        "walk_in": walk_in,
        "file_based": file_based,
        "converted": converted,
    }
    counts = {key: int(mask.sum()) for key, mask in masks.items()}
    return {"counts": counts, "masks": masks}
