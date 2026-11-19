"""Small, read-only summaries for the CRM reports page."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from chat_followups import named_offerings
from crm_storage import _lead_key


IST = ZoneInfo("Asia/Kolkata")
ANALYTICS_VERSION = "2026-11-16-report-outcomes-v2"


def event_time(value):
    """Parse mixed legacy timestamps; unzoned CRM times were recorded in IST."""
    if value is None or pd.isna(value) or not str(value).strip():
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed).astimezone(IST)


def within_days(frame: pd.DataFrame, days: int, now: datetime | None = None) -> pd.DataFrame:
    """Keep rows within the selected rolling period and attach sortable time."""
    now = (now or datetime.now(IST)).astimezone(IST)
    if frame is None or frame.empty:
        return pd.DataFrame(columns=[*(frame.columns if frame is not None else []), "_event_time"])
    result = frame.copy()
    result["_event_time"] = result.get("timestamp", pd.Series(None, index=result.index)).map(event_time)
    start = now - timedelta(days=days)
    return result.loc[result["_event_time"].map(
        lambda value: value is not None and start <= value <= now
    )].copy()


def last_recorded_at(*frames: pd.DataFrame):
    latest = None
    for frame in frames:
        if frame is None or frame.empty or "timestamp" not in frame:
            continue
        for value in frame["timestamp"]:
            parsed = event_time(value)
            if parsed and (latest is None or parsed > latest):
                latest = parsed
    return latest


def customer_chats(history: pd.DataFrame) -> pd.DataFrame:
    """Only well-formed customer and assistant turns count as chat activity."""
    columns = ["visitor_id", "lead", "role", "content", "_event_time", "_person_key"]
    if history is None or history.empty or "role" not in history:
        return pd.DataFrame(columns=columns)
    result = history.loc[history["role"].fillna("").astype(str).str.casefold().isin({"user", "assistant"})].copy()
    if "_event_time" not in result:
        result["_event_time"] = result.get("timestamp", pd.Series(None, index=result.index)).map(event_time)
    result = result.loc[result["_event_time"].notna()].copy()
    visitor = result.get("visitor_id", pd.Series("", index=result.index)).fillna("").astype(str).str.strip()
    name = result.get("lead", pd.Series("", index=result.index)).fillna("").astype(str).str.strip()
    result["_person_key"] = visitor.where(visitor.ne(""), "name:" + name.str.casefold())
    return result.loc[result["_person_key"].ne("name:")].sort_values("_event_time", kind="stable")


def chat_people_summary(history: pd.DataFrame):
    """Return unique customer count, message count, and active chat threads."""
    chats = customer_chats(history)
    if chats.empty:
        return 0, 0, []
    customer = chats.loc[chats["role"].astype(str).str.casefold().eq("user")]
    people = int(customer["_person_key"].nunique())
    threads = []
    for key, thread in chats.groupby("_person_key", sort=False):
        customer_turns = thread.loc[thread["role"].astype(str).str.casefold().eq("user")]
        if customer_turns.empty:
            continue
        latest = thread.iloc[-1]
        named = customer_turns.iloc[-1]
        threads.append({
            "key": key,
            "name": str(named.get("lead", "") or "").strip(),
            "visitor_id": str(named.get("visitor_id", "") or "").strip(),
            "messages": len(customer_turns),
            "last_role": str(latest.get("role", "") or "").casefold(),
            "last_at": latest["_event_time"],
        })
    return people, len(customer), threads


def match_chat_lead(thread: dict, leads: pd.DataFrame):
    """Resolve a chat to one lead, preferring name when visitor IDs collide."""
    if leads is None or leads.empty:
        return None
    visitor_id = thread.get("visitor_id", "")
    name = str(thread.get("name", "") or "").strip().casefold()
    matches = leads.iloc[0:0]
    if visitor_id and "Visitor ID" in leads:
        matches = leads.loc[leads["Visitor ID"].fillna("").astype(str).str.strip().eq(visitor_id)]
        if len(matches) == 1:
            return matches.iloc[0].to_dict()
        if len(matches) > 1 and name and "Name" in matches:
            named = matches.loc[matches["Name"].fillna("").astype(str).str.strip().str.casefold().eq(name)]
            if len(named) == 1:
                return named.iloc[0].to_dict()
    if name and "Name" in leads:
        named = leads.loc[leads["Name"].fillna("").astype(str).str.strip().str.casefold().eq(name)]
        if len(named) == 1:
            return named.iloc[0].to_dict()
    return None


def _interest_band(lead: dict | None) -> str:
    """Use the saved CRM rating; never infer a rating from a question alone."""
    if not lead:
        return "Not rated"
    rating = str(lead.get("Interest Level") or "").casefold()
    if "very low" in rating or "low" in rating:
        return "Low"
    if "moderate" in rating or "medium" in rating:
        return "Moderate"
    if "high" in rating:
        return "High"
    return "Not rated"


def offering_interest_summary(history: pd.DataFrame, leads: pd.DataFrame,
                              offerings: pd.DataFrame):
    """Count distinct chat participants per explicitly named catalog offering."""
    names = []
    if offerings is not None and "Offering Name" in offerings:
        names = list(dict.fromkeys(
            str(value).strip() for value in offerings["Offering Name"].dropna()
            if str(value).strip()
        ))
    catalog = [{"Offering Name": name} for name in names]
    chats = customer_chats(history)
    if chats.empty:
        return pd.DataFrame(columns=["Offering", "Interest", "People"]), {}

    customer = chats.loc[chats["role"].astype(str).str.casefold().eq("user")]
    people = {}
    for key, thread in customer.groupby("_person_key", sort=False):
        latest = thread.iloc[-1]
        identity = {
            "name": str(latest.get("lead") or "").strip(),
            "visitor_id": str(latest.get("visitor_id") or "").strip(),
        }
        lead = match_chat_lead(identity, leads)
        person_key = _lead_key(lead) if lead else "chat:" + str(key)
        entry = people.setdefault(person_key, {
            "lead": lead, "name": identity["name"] or identity["visitor_id"] or "Visitor",
            "offerings": set(),
        })
        for message in thread.get("content", pd.Series("", index=thread.index)).fillna("").astype(str):
            entry["offerings"].update(
                match["Offering Name"] for match in named_offerings(message, catalog)
            )

    by_offering = {name: [] for name in [*names, "Unclassified"]}
    for entry in people.values():
        for name in entry["offerings"] or {"Unclassified"}:
            by_offering[name].append({
                "name": entry["name"], "lead": entry["lead"],
                "interest": _interest_band(entry["lead"]),
            })
    bands = ("High", "Moderate", "Low", "Not rated")
    rows = [
        {"Offering": name, "Interest": band,
         "People": sum(person["interest"] == band for person in by_offering[name])}
        for name in by_offering for band in bands
    ]
    return pd.DataFrame(rows), by_offering


def match_activity_lead(activity: dict, leads: pd.DataFrame):
    """Resolve a saved activity only when its CRM identity is unambiguous."""
    if leads is None or leads.empty:
        return None
    import_key = str(activity.get("lead_import_key") or "").strip()
    if import_key and "Lead Import Key" in leads:
        found = leads.loc[leads["Lead Import Key"].fillna("").astype(str).eq(import_key)]
        if len(found) == 1:
            return found.iloc[0].to_dict()
    name = str(activity.get("lead") or "").strip().casefold()
    if name and "Name" in leads:
        found = leads.loc[leads["Name"].fillna("").astype(str).str.strip().str.casefold().eq(name)]
        if len(found) == 1:
            return found.iloc[0].to_dict()
    return None


def intent_activity_details(activities: pd.DataFrame, leads: pd.DataFrame,
                            history: pd.DataFrame, intent: str):
    """Return inspectable activity rows with a nearby, verified customer turn."""
    if activities is None or activities.empty or "intent" not in activities:
        return []
    selected = activities.loc[activities["intent"].fillna("Unknown").astype(str).str.strip()
                              .replace("", "Unknown").eq(intent)]
    customer = customer_chats(history)
    customer = customer.loc[customer["role"].astype(str).str.casefold().eq("user")]
    details = []
    for row in selected.sort_values("_event_time", ascending=False).to_dict("records"):
        lead = match_activity_lead(row, leads)
        activity_at = row.get("_event_time") or event_time(row.get("timestamp"))
        message = ""
        if lead and activity_at and not customer.empty:
            visitor = str(lead.get("Visitor ID") or "").strip()
            name = str(lead.get("Name") or "").strip().casefold()
            candidate = customer.iloc[0:0]
            if visitor:
                same_visitor = leads.get("Visitor ID", pd.Series("", index=leads.index))
                shared_id = same_visitor.fillna("").astype(str).str.strip().eq(visitor).sum() > 1
                visitor_mask = customer.get("visitor_id", pd.Series("", index=customer.index))
                visitor_mask = visitor_mask.fillna("").astype(str).str.strip().eq(visitor)
                if shared_id:
                    visitor_mask = (
                        customer.get("visitor_id", pd.Series("", index=customer.index))
                        .fillna("").astype(str).str.strip().eq(visitor)
                        & customer.get("lead", pd.Series("", index=customer.index))
                        .fillna("").astype(str).str.strip().str.casefold().eq(name)
                    )
                candidate = customer.loc[visitor_mask]
            if candidate.empty:
                same_name = leads.get("Name", pd.Series("", index=leads.index))
                if same_name.fillna("").astype(str).str.strip().str.casefold().eq(name).sum() == 1:
                    candidate = customer.loc[customer.get("lead", pd.Series("", index=customer.index))
                                             .fillna("").astype(str).str.strip().str.casefold().eq(name)]
            candidate = candidate.loc[candidate["_event_time"].map(
                lambda value: timedelta(0) <= activity_at - value <= timedelta(minutes=5)
            )]
            if not candidate.empty:
                message = str(candidate.iloc[-1].get("content") or "").strip()
        details.append({
            "lead": lead, "name": str(row.get("lead") or "").strip() or "Unlinked activity",
            "action": str(row.get("action") or "").strip(),
            "message": message, "at": activity_at,
        })
    return details


def _period_total(frame: pd.DataFrame, timestamp_col: str, days: int,
                  now: datetime, previous: bool = False) -> int:
    if frame is None or frame.empty or timestamp_col not in frame:
        return 0
    end = now - timedelta(days=days) if previous else now
    start = end - timedelta(days=days)
    return sum(
        (parsed := event_time(value)) is not None and start <= parsed < end
        for value in frame[timestamp_col]
    )


def report_outcomes(activity: pd.DataFrame, inbox: pd.DataFrame,
                    sent_events: pd.DataFrame | None, leads: pd.DataFrame,
                    days: int, now: datetime):
    """Separate date-stamped outcomes from current lead-state snapshots."""
    replies = inbox.copy() if inbox is not None else pd.DataFrame()
    if not replies.empty and "message_id" in replies:
        replies = replies.drop_duplicates("message_id", keep="last")
    chat_starts = activity.loc[
        activity.get("action", pd.Series("", index=activity.index)).fillna("").astype(str)
        .str.strip().eq("Chat started from email link.")
    ] if activity is not None else pd.DataFrame()
    sent = sent_events if sent_events is not None else pd.DataFrame()
    status = leads.get("Status", pd.Series("", index=leads.index)).fillna("").astype(str).str.strip().str.casefold()
    priority = leads.get("Priority", pd.Series("", index=leads.index)).fillna("").astype(str).str.strip().str.casefold()
    converted = status.isin({"converted", "won", "closed won", "customer", "converted to customer"})
    return {
        "emails": None if sent_events is None else (
            _period_total(sent, "updated_at", days, now),
            _period_total(sent, "updated_at", days, now, True),
        ),
        "replies": (
            _period_total(replies, "received_at", days, now),
            _period_total(replies, "received_at", days, now, True),
        ),
        "email_chats": (
            _period_total(chat_starts, "timestamp", days, now),
            _period_total(chat_starts, "timestamp", days, now, True),
        ),
        "high_priority": int((priority.eq("high") & ~converted).sum()),
        "converted": int(converted.sum()),
    }
