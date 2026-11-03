"""Customer-facing facts and comparisons from current CRM rows, without AI calls."""

import re
from datetime import datetime
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

import pandas as pd

from chat_followups import context_offerings, named_offerings, referenced_offerings

MISSING = "Not provided"
EVENT_TYPES = {"course", "workshop", "seminar", "webinar", "event", "conference", "training", "program"}
TERMINAL = {"archived", "cancelled", "expired", "completed"}


def clean_value(value):
    text = str(value).strip() if value is not None else ""
    return "" if text.casefold() in {
        "", "nan", "none", "null", "nat", "<na>", "n/a", "na", "-", "—",
        "tbd", "tba", "not provided", "not available", "to be confirmed", "to be announced",
    } else text


def offering_key(row):
    return clean_value(row.get("Offering ID")) or clean_value(row.get("Offering Name"))


def saved_web_url(value):
    text = clean_value(value)
    if not text or re.search(r"[\s\x00-\x1f\x7f]", text):
        return ""
    if ":" not in text and "." in text.split("/")[0]:
        text = "https://" + text
    try:
        parsed = urlparse(text)
        parsed.port
        if (parsed.scheme in {"https", "http"} and parsed.hostname
                and not parsed.username and not parsed.password):
            return text
    except ValueError:
        pass
    return ""


def parse_offering_date(value, range_end=False):
    text = clean_value(value)
    if not text:
        return pd.NaT
    match = re.fullmatch(r"(?i)(\d{1,2})\s*[-–—]\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    try:
        if match:
            month = datetime.strptime(match[3][:3], "%b").month
            return pd.Timestamp(year=int(match[4]), month=month, day=int(match[2 if range_end else 1]))
        # A year is required; never turn an incomplete date into this year.
        if not re.search(r"\b\d{4}\b", text):
            return pd.NaT
        # Parse ISO dates explicitly: dayfirst=True can swap their month/day.
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return pd.to_datetime(text, format="%Y-%m-%d", errors="coerce")
        parsed = pd.to_datetime(text, errors="coerce", dayfirst=True)
        if pd.notna(parsed) and parsed.tzinfo is not None:
            parsed = parsed.tz_convert("Asia/Kolkata").tz_localize(None)
        return parsed.normalize() if pd.notna(parsed) else pd.NaT
    except (ValueError, TypeError, OverflowError):
        return pd.NaT


def offering_lifecycle(row, now=None):
    knowledge = str(row.get("Knowledge Status", "Draft")).strip().casefold()
    lifecycle = str(row.get("Lifecycle Status", "")).strip().casefold()
    if knowledge in {"draft", "cancelled", "archived"}:
        return knowledge.capitalize()
    if lifecycle in {"archived", "cancelled", "expired"}:
        return lifecycle.capitalize()
    start_text, end_text = clean_value(row.get("Start Date")), clean_value(row.get("End Date"))
    start = parse_offering_date(start_text)
    end = parse_offering_date(end_text, range_end=True)
    if not end_text:
        end = parse_offering_date(start_text, range_end=True)
    today = pd.Timestamp((now or datetime.now(ZoneInfo("Asia/Kolkata"))).date())
    if not start_text and not end_text:
        if lifecycle == "completed":
            return "Completed"
        return "Schedule not confirmed" if clean_value(row.get("Offering Type")).casefold() in EVENT_TYPES else "Available"
    if ((start_text and pd.isna(start)) or (end_text and pd.isna(end))
            or (pd.notna(start) and pd.notna(end) and end < start)):
        return "Schedule not confirmed"
    if pd.notna(end) and end < today:
        return "Completed"
    if pd.isna(start):
        return "Schedule not confirmed"
    if today < start:
        return "Upcoming"
    return "Ongoing"


def is_visible_offering(row, now=None):
    return (str(row.get("Knowledge Status", "")).strip().casefold() == "published"
            and str(row.get("AI Active", "")).strip().casefold() in {"true", "yes", "1"}
            and offering_lifecycle(row, now).casefold() not in TERMINAL)


def offering_price(row):
    price = clean_value(row.get("Price"))
    if not price:
        return MISSING
    if price.casefold() in {"free", "free of cost", "no charge"}:
        return "Free"
    try:
        if float(price.replace(",", "")) == 0:
            return "Free"
    except ValueError:
        pass
    currency = clean_value(row.get("Currency"))
    return f"{price} {currency}" if currency else f"{price} (currency not provided)"


def offering_dates(row):
    start, end = clean_value(row.get("Start Date")), clean_value(row.get("End Date"))
    if ((start and pd.isna(parse_offering_date(start)))
            or (end and pd.isna(parse_offering_date(end, True)))):
        return "Schedule not confirmed"
    if start and end and parse_offering_date(end, True) < parse_offering_date(start):
        return "Schedule not confirmed"
    if start and end:
        return " – ".join(dict.fromkeys((start, end)))
    if start:
        return f"Starts {start}; end date not provided"
    if end:
        return f"Ends {end}; start date not provided"
    return MISSING


def offering_times(row):
    start, end = clean_value(row.get("Start Time")), clean_value(row.get("End Time"))
    if start and end:
        return f"{start} – {end}"
    if start:
        return f"Starts {start}; end time not provided"
    if end:
        return f"Ends {end}; start time not provided"
    return MISSING


def markdown_text(value):
    text = re.sub(r"\s+", " ", str(value))
    return re.sub(r"([\\`*_{}\[\]<>|])", r"\\\1", text)


def registration_detail(row):
    registration = saved_web_url(row.get("Registration Link"))
    website = saved_web_url(row.get("Website")) or saved_web_url(row.get("Website URL"))
    if registration:
        return f"[Open registration]({quote(registration, safe=':/?=&%#@+;,$!~*-._')})"
    if website:
        return f"No registration link listed. [Visit website]({quote(website, safe=':/?=&%#@+;,$!~*-._')})"
    return "No registration link listed; ask our team for a follow-up."


def comparison_columns(rows):
    """Save the same facts used in the table for a responsive card presentation."""
    facts = [
        ("Type", lambda row: clean_value(row.get("Offering Type")) or MISSING),
        ("Status", offering_lifecycle), ("Price", offering_price),
        ("Dates", offering_dates), ("Time", offering_times),
        ("Duration", lambda row: clean_value(row.get("Duration")) or MISSING),
        ("Who it’s for", lambda row: clean_value(row.get("Target Audience")) or MISSING),
    ]
    return [{"name": clean_value(row.get("Offering Name")),
             "facts": [[label, markdown_text(getter(row))] for label, getter in facts],
             "registration": registration_detail(row)} for row in rows]


def comparison_reply(rows, columns=None):
    """The durable Markdown table is also the exact transcript stored in CRM."""
    columns = comparison_columns(rows) if columns is None else columns
    if not columns:
        raise ValueError("Choose offerings before building a comparison")
    lines = ["Here’s a comparison using the current offering details:", "",
             "| Detail | " + " | ".join(markdown_text(column["name"]) for column in columns) + " |",
             "| --- | " + " | ".join("---" for _ in rows) + " |"]
    for index, (label, _) in enumerate(columns[0]["facts"]):
        lines.append(f"| {label} | " + " | ".join(column["facts"][index][1] for column in columns) + " |")
    lines.append("| Registration | " + " | ".join(column["registration"] for column in columns) + " |")
    lines += ["", "“Not provided” means that detail hasn’t been confirmed here. You can ask about FAQs or policies for either option."]
    return "\n".join(lines)


def build_grounded_chat_reply(message, conversation, offerings):
    """Handle comparisons and straightforward factual questions deterministically."""
    explicit = named_offerings(message, offerings)
    focused = explicit or referenced_offerings(message, conversation, offerings)
    comparison = bool(re.search(r"\b(?:compare|comparison|versus|vs|differences?)\b|which (?:one )?is better", message, re.I))
    query = message
    for row in explicit:
        query = re.sub(re.escape(clean_value(row.get("Offering Name"))), "", query, flags=re.I)
    # Preserve the ordinary action/AI path for requests beyond stored facts.
    if re.search(r"\b(?:call me|email me|register me|sign me|unsubscribe|interested|excited|love|hate|angry|buy|purchase|join|enroll|enrol|thanks|thank you|discount|tax|gst|emi|installments?|why|recommend|best|budget|under|cheaper|refund|cancel|demo|worth|include|includes|provide|provided|recordings)\b", query, re.I):
        return None
    fields = []
    if re.search(r"\b(?:price|prices|cost|fee|fees|free)\b|how much", query, re.I):
        fields.append(("Price", offering_price))
    if re.search(r"\b(?:dates?|schedule|when|start|end)\b", query, re.I):
        fields.append(("Dates", offering_dates))
    if re.search(r"\b(?:times?|schedule|when)\b", query, re.I):
        fields.append(("Time", offering_times))
    if re.search(r"\b(?:duration|how long)\b", query, re.I):
        fields.append(("Duration", lambda row: clean_value(row.get("Duration")) or MISSING))
    if re.search(r"\b(?:audience|who|intended)\b", query, re.I):
        fields.append(("Who it’s for", lambda row: clean_value(row.get("Target Audience")) or MISSING))
    for field in ("FAQs", "Policies"):
        if re.search(r"\b(?:faq|faqs|frequently asked questions)\b" if field == "FAQs" else r"\b(?:policies|policy)\b", query, re.I):
            fields.append((field, lambda row, key=field: clean_value(row.get(key)) or MISSING))
    if re.search(r"\b(?:link|links|url)\b", query, re.I):
        fields.append(("Registration", registration_detail))
    if not fields and not comparison:
        return None
    if comparison and not focused:
        # A generic comparison can use the immediately preceding exchange.
        focused = context_offerings([
            {"role": match[0].casefold(), "content": match[1].strip()}
            for match in re.findall(r"(?ms)^(USER|ASSISTANT):\s*(.*?)(?=^(?:USER|ASSISTANT):|\Z)", conversation)
        ] + [{"role": "user", "content": message}], offerings)
    if not focused:
        if comparison:
            return {"reply": "Which two offerings would you like to compare? Choose two below, or type their names.",
                    "offering_ids": [], "presentation": "comparison_picker", "intent": "Product Question", "status": None}
        return None
    unavailable = [row for row in focused if not is_visible_offering(row)]
    if unavailable:
        names = ", ".join(f"**{markdown_text(row['Offering Name'])}**" for row in unavailable)
        return {"reply": f"Current details for {names} aren’t available. I can’t confirm a new schedule or registration link. You can use “Share details for follow-up” to ask our team for an update.",
                "offering_ids": [], "presentation": None, "intent": "Request for More Information", "status": "Information Requested"}
    if comparison:
        if len(focused) != 2:
            reply = "Which two offerings would you like to compare? Choose two with the Compare buttons, or type their names."
            return {"reply": reply, "offering_ids": [], "presentation": "comparison_picker", "intent": "Product Question", "status": None}
        columns = comparison_columns(focused)
        return {"reply": comparison_reply(focused, columns), "offering_ids": [offering_key(row) for row in focused],
                "presentation": "offering_comparison", "comparison_columns": columns,
                "intent": "Product Question", "status": "Information Provided"}
    if len(focused) > 1 and not (explicit or re.search(r"\b(?:each|both|all|those|these|them)\b", query, re.I)):
        return {"reply": "Which offering do you mean? Please share its name so I can give you the correct details.",
                "offering_ids": [], "presentation": None, "intent": "Request for More Information", "status": None}
    sections, missing = [], False
    for row in focused:
        lines = [f"**{markdown_text(row['Offering Name'])}**"]
        for label, getter in fields:
            value = getter(row)
            missing |= value == MISSING or "not provided" in value.casefold() or value == "Schedule not confirmed"
            if label == "Registration" and not saved_web_url(row.get("Registration Link")):
                missing = True
            lines.append(f"- **{label}:** {value if label == 'Registration' else markdown_text(value)}")
        sections.append("\n".join(lines))
    if missing:
        sections.append("Those missing details haven’t been confirmed here. You can use “Share details for follow-up” to ask our team.")
    return {"reply": "\n\n".join(sections), "offering_ids": [offering_key(row) for row in focused],
            "presentation": None, "intent": "Pricing Question" if fields[0][0] == "Price" else "Request for More Information",
            "status": "Information Requested" if missing else "Information Provided"}
