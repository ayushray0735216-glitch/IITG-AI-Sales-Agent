import os
import base64
import hashlib
import hmac
import json
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
IST = ZoneInfo("Asia/Kolkata")
from flask import Flask, request
from twilio.rest import Client as TwilioClient
from twilio.request_validator import RequestValidator
import threading
import time
import re
import zlib
from urllib.parse import urlencode
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv
from google import genai
load_dotenv()
from crm_storage import (
    crm_path,
    ensure_seed_file,
    initialize_database,
    load_offerings_data,
    record_communication_event,
    load_autopilot_settings,
    load_crm_table,
    load_contact_suppressions,
    append_sales_activity,
    sync_after_csv_write,
)
import autopilot

ensure_seed_file("leads.csv")
ensure_seed_file("offerings.csv")
initialize_database()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

app = Flask(__name__)

conversation_history = {}
call_leads = {}
call_numbers = {}
call_catalog_context = {}


def _voice_contact_guard(channel, destination, lead_name):
    """Use shared CRM consent, suppression, and cadence rules before follow-up."""
    leads = load_crm_table("leads.csv")
    identity = "Email" if channel == "email" else "Phone"
    if leads.empty or identity not in leads.columns:
        return None, "No matching CRM lead was found; no follow-up was sent."
    match = leads[
        leads[identity].fillna("").astype(str).str.strip().str.casefold().eq(
            str(destination or "").strip().casefold()
        )
    ]
    if len(match.index) != 1 and "Name" in leads.columns:
        match = leads[
            leads["Name"].fillna("").astype(str).str.strip().str.casefold().eq(
                str(lead_name or "").strip().casefold()
            )
        ]
    if len(match.index) != 1:
        return None, "The customer could not be matched to one CRM record; no follow-up was sent."
    lead = match.iloc[0]
    reason = autopilot.contact_action_block_reason(
        lead, channel, str(destination or ""),
        activity=load_crm_table("sales_activity.csv"),
        settings=load_autopilot_settings(),
        suppressions=load_contact_suppressions(),
        now=datetime.now(IST),
    )
    return lead.to_dict(), reason


@app.route("/call-status", methods=["POST"])
def call_status():
    """Verify and persist Twilio's asynchronous outbound-call status callback."""
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    signature = request.headers.get("X-Twilio-Signature", "")
    callback_base = os.getenv(
        "TWILIO_PUBLIC_BASE_URL", "https://iitg-ai-sales-agent.onrender.com"
    ).strip().rstrip("/")
    callback_url = os.getenv("TWILIO_CALLBACK_URL", "").strip() or f"{callback_base}/call-status"
    if not auth_token or not signature or not RequestValidator(auth_token).validate(
        callback_url, request.form, signature
    ):
        return "Invalid callback signature", 403

    data = request.form.to_dict(flat=True)
    call_sid = str(data.get("CallSid", "")).strip()
    call_status = str(data.get("CallStatus", "")).strip().casefold()
    if not call_sid or call_status not in {
        "queued", "initiated", "ringing", "answered", "in-progress", "completed",
        "busy", "failed", "no-answer", "canceled",
    }:
        return "Invalid call status payload", 400

    at = datetime.now(IST).isoformat()
    details = {
        "provider": "twilio",
        "provider_id": call_sid,
        "channel": "phone",
        "direction": "outbound",
        "status": call_status,
        "call_duration_seconds": str(data.get("CallDuration", "")),
        "provider_error_code": str(data.get("ErrorCode", "")),
        "updated_at": at,
        "source": "Twilio status callback",
    }
    from crm_storage import load_communication_events
    existing = load_communication_events(limit=5000)
    current_id = f"twilio-call:{call_sid}"
    current = existing[
        existing.get("event_id", pd.Series(dtype=str)).fillna("").astype(str).eq(current_id)
    ] if not existing.empty else pd.DataFrame()
    if not current.empty:
        prior_call = current.iloc[0].to_dict()
        for field in ("lead_name", "visitor_id", "destination"):
            if prior_call.get(field):
                details[field] = prior_call[field]
    elif call_leads.get(call_sid):
        details["lead_name"] = call_leads[call_sid]
    # Keep each callback as an immutable event, and also maintain a current row.
    callback_id = f"twilio-event:{call_sid}:{call_status}:{data.get('Timestamp', at)}"
    record_communication_event({**details, "event_id": callback_id})
    ranks = {
        "queued": 0, "initiated": 1, "ringing": 2, "answered": 3, "in-progress": 3,
        "completed": 10, "busy": 10, "failed": 10, "no-answer": 10, "canceled": 10,
    }
    current_status = str(current.iloc[0].get("status", "")).casefold() if not current.empty else ""
    terminal_statuses = {"completed", "busy", "failed", "no-answer", "canceled"}
    should_update_current = current.empty or (
        current_status not in terminal_statuses
        and ranks[call_status] >= ranks.get(current_status, -1)
    ) or call_status == current_status
    if should_update_current:
        record_communication_event({**details, "event_id": current_id})
    if call_status in terminal_statuses and should_update_current:
        lead_name = str(details.get("lead_name") or call_leads.get(call_sid) or "").strip()
        visitor_id = str(details.get("visitor_id") or "").strip()
        duration = str(data.get("CallDuration", "")).strip()
        status_message = {
            "completed": "Outbound call completed",
            "busy": "Outbound call reached a busy line",
            "failed": "Outbound call failed",
            "no-answer": "Outbound call was not answered",
            "canceled": "Outbound call was canceled",
        }[call_status]
        if duration and call_status == "completed":
            status_message += f" after {duration} seconds"
        error_code = str(data.get("ErrorCode", "")).strip()
        if error_code:
            status_message += f" (provider error {error_code})"
        _record_voice_activity(
            f"twilio-call-result:{call_sid}:{call_status}",
            lead_name,
            visitor_id,
            "CRM Call Result",
            status_message + ".",
            timestamp=at,
            call_sid=call_sid,
        )
    return "OK", 200


def verify_catalog_snapshot(encoded, signature):
    """Decode an offering snapshot signed by the CRM before using it on a call."""
    secret = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not encoded or not signature or not secret:
        return None
    expected = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = zlib.decompress(base64.urlsafe_b64decode(padded.encode("ascii")))
        catalog = json.loads(payload.decode("utf-8"))
    except (ValueError, OSError, zlib.error, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return catalog if isinstance(catalog, list) and all(isinstance(row, dict) for row in catalog) else None


def _process_action_url(encoded="", signature=""):
    params = {}
    if encoded and signature:
        params = {"catalog": encoded, "catalog_sig": signature}
    query = urlencode(params)
    return "https://iitg-ai-sales-agent.onrender.com/process" + (f"?{query}" if query else "")


def _call_crm_identity(call_sid):
    """Resolve a call back to the lead record saved at call initiation."""
    lead_name = str(call_leads.get(str(call_sid), "") or "").strip()
    visitor_id = ""
    try:
        from crm_storage import load_communication_events
        events = load_communication_events(limit=5000)
        if not events.empty and "event_id" in events.columns:
            match = events.loc[
                events["event_id"].fillna("").astype(str).eq(f"twilio-call:{call_sid}")
            ]
            if not match.empty:
                event = match.iloc[0]
                lead_name = str(event.get("lead_name") or lead_name).strip()
                visitor_id = str(event.get("visitor_id") or "").strip()
    except Exception as exc:
        print(f"Call-to-CRM lookup failed [{call_sid}]: {exc}")
    return lead_name, visitor_id


def load_leads():
    """Load the lead database."""
    return pd.read_csv(crm_path("leads.csv"))


def update_voice_lead(lead_name, status, last_action):
    """Update a lead after a voice interaction."""

    if not lead_name:
        return False

    leads = load_leads()

    leads["Status"] = leads["Status"].astype("object")
    leads["Last Action"] = leads["Last Action"].astype("object")
    leads["Last Interaction"] = leads["Last Interaction"].astype("object")

    for index, lead in leads.iterrows():

        if str(lead["Name"]).strip().lower() == str(lead_name).strip().lower():

            leads.at[index, "Status"] = status
            leads.at[index, "Last Action"] = last_action
            leads.at[index, "Last Interaction"] = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            leads.to_csv(crm_path("leads.csv"), index=False)
            try:
                sync_after_csv_write("leads.csv")
            except Exception as exc:
                print(f"Could not sync voice lead update to shared CRM: {exc}")

            print(
                f"Lead updated: {lead['Name']} | "
                f"Status: {status} | "
                f"Action: {last_action}"
            )

            return True

    print(f"Lead not found: {lead_name}")
    return False


def _record_voice_activity(activity_id, lead_name, visitor_id, intent, action, timestamp=None, call_sid=""):
    """Persist a call event to the same CRM activity timeline used by the dashboard."""
    lead_name = str(lead_name or "").strip()
    if not lead_name and not visitor_id:
        return False
    try:
        return append_sales_activity({
            "activity_id": str(activity_id),
            "timestamp": str(timestamp or datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z")),
            "lead": lead_name or f"Anonymous Visitor {visitor_id[-6:]}",
            "intent": str(intent or "Voice call"),
            "sentiment": "",
            "priority": "",
            "action": str(action or "Voice call activity recorded."),
            "visitor_id": str(visitor_id or ""),
            "call_sid": str(call_sid or ""),
        })
    except Exception as exc:
        print(f"Could not save voice activity [{call_sid or activity_id}]: {exc}")
        return False
# ============================================================
# VOICE CALLBACK SCHEDULING
# ============================================================

scheduled_voice_calls = {}


def parse_voice_call_time(message):
    """Understand relative and explicit callback times."""

    message_lower = message.lower().strip()
    now = datetime.now(IST)

    # -----------------------------------------
    # Relative time
    # -----------------------------------------

    relative_match = re.search(
        r"(?:after|in)\s+(\d+)\s+(minute|minutes|hour|hours)",
        message_lower
    )

    if relative_match:

        value = int(relative_match.group(1))
        unit = relative_match.group(2)

        if "hour" in unit:
            return now + timedelta(hours=value)

        return now + timedelta(minutes=value)

    # -----------------------------------------
    # Today / tomorrow
    # -----------------------------------------

    if "tomorrow" in message_lower:
        target_date = now.date() + timedelta(days=1)

    else:
        target_date = now.date()

    # -----------------------------------------
    # Exact time
    # -----------------------------------------

    time_match = re.search(
        r"(?:at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        message_lower
    )

    if not time_match:
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    period = time_match.group(3)

    if period:

        if period == "pm" and hour != 12:
            hour += 12

        elif period == "am" and hour == 12:
            hour = 0

    scheduled_time = datetime.combine(
        target_date,
        datetime.min.time()
    ).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0
    )

    # If no explicit date was given and the time has passed,
    # schedule for tomorrow.
    if (
        "tomorrow" not in message_lower
        and scheduled_time <= now
    ):
        scheduled_time += timedelta(days=1)

    if scheduled_time <= now:
        return None

    return scheduled_time


def send_voice_confirmation_email(lead_name, scheduled_time):

    leads = load_leads()

    for _, lead in leads.iterrows():

        if str(lead["Name"]).strip().lower() == str(
            lead_name
        ).strip().lower():

            email = str(lead.get("Email", "")).strip()

            if not email:
                return "Lead email not found."

            lead_record, block_reason = _voice_contact_guard("email", email, lead_name)
            if block_reason:
                return f"Email blocked: {block_reason}"

            sender_email = os.getenv("EMAIL_ADDRESS")
            sender_password = os.getenv("EMAIL_PASSWORD")

            if not sender_email or not sender_password:
                return "Email credentials not configured."

            msg = EmailMessage()
            message_id = make_msgid()
            msg["Message-ID"] = message_id

            msg["From"] = sender_email
            msg["To"] = email
            msg["Subject"] = (
                "Call Rescheduled - IITG AI Sales Agent"
            )

            msg.set_content(
                f"Hello {lead_name},\n\n"
                "As requested, your call has been rescheduled "
                f"for {scheduled_time.strftime('%d %B %Y at %I:%M %p')}.\n\n"
                "Please keep your phone available around the "
                "scheduled time.\n\n"
                "Regards,\n"
                "IITG AI Sales Agent"
            )

            try:

                with smtplib.SMTP_SSL(
                    "smtp.gmail.com",
                    465
                ) as smtp:

                    smtp.login(
                        sender_email,
                        sender_password
                    )

                    refused = smtp.send_message(msg)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)

                record_communication_event({
                    "event_id": f"email:{message_id}", "provider": "gmail_smtp",
                    "provider_id": message_id, "message_id": message_id,
                    "channel": "email", "direction": "outbound", "destination": email,
                    "lead_name": str(lead_name or ""),
                    "visitor_id": str((lead_record or {}).get("Visitor ID", "") or ""),
                    "subject": str(msg.get("Subject", "")), "status": "accepted_by_smtp",
                    "outcome": "SMTP accepted the message; final delivery is not confirmed.",
                    "updated_at": datetime.now(IST).isoformat(), "source": "Voice call follow-up confirmation",
                })

                return "Email sent successfully."

            except Exception as e:
                record_communication_event({
                    "event_id": f"email-failure:{message_id}", "provider": "gmail_smtp",
                    "provider_id": message_id, "message_id": message_id,
                    "channel": "email", "direction": "outbound", "destination": email,
                    "lead_name": str(lead_name or ""), "subject": str(msg.get("Subject", "")),
                    "status": "failed", "outcome": str(e)[:500],
                    "updated_at": datetime.now(IST).isoformat(), "source": "Voice call follow-up confirmation",
                })
                return f"Email failed: {e}"

    return "Lead not found."


def initiate_scheduled_voice_call(lead_name, to_number):

    try:

        lead_record, block_reason = _voice_contact_guard("phone", to_number, lead_name)
        if block_reason:
            return False, block_reason

        twilio_client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )

        from_number = os.getenv("TWILIO_PHONE_NUMBER")

        if not to_number or not from_number:
            return False, "Phone number is not configured."

        public_base = os.getenv(
            "TWILIO_PUBLIC_BASE_URL", "https://iitg-ai-sales-agent.onrender.com"
        ).strip().rstrip("/")
        call = twilio_client.calls.create(
            to=to_number,
            from_=from_number,
            url=f"{public_base}/voice?lead={lead_name}",
            status_callback=f"{public_base}/call-status",
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )

        record_communication_event({
            "event_id": f"twilio-call:{call.sid}", "provider": "twilio",
            "provider_id": call.sid, "channel": "phone", "direction": "outbound",
            "destination": str(to_number), "lead_name": str(lead_name or ""),
            "visitor_id": str((lead_record or {}).get("Visitor ID", "") or ""),
            "status": str(getattr(call, "status", "queued") or "queued").casefold(),
            "updated_at": datetime.now(IST).isoformat(), "source": "Voice callback",
        })

        return True, call.sid

    except Exception as e:

        return False, str(e)


def schedule_voice_callback(
    lead_name,
    scheduled_time,
    to_number
):

    def wait_and_call():

        wait_seconds = (
            scheduled_time - datetime.now(IST)
        ).total_seconds()

        if wait_seconds > 0:
            time.sleep(wait_seconds)

        success, result = (
            initiate_scheduled_voice_call(
                lead_name,
                to_number
            )
        )

        scheduled_voice_calls[lead_name] = {
            "scheduled_time": scheduled_time,
            "status": (
                "Called"
                if success
                else "Failed"
            ),
            "result": result
        }

    thread = threading.Thread(
        target=wait_and_call,
        daemon=True
    )

    thread.start()

    scheduled_voice_calls[lead_name] = {
        "scheduled_time": scheduled_time,
        "status": "Scheduled"
    }

    return True

def classify_intent(customer_message):
    prompt = f"""
Classify the customer's message into exactly ONE of these categories:

Interested
Pricing Question
Product Question
Request for Demo
Request for More Information
Not Interested
Ready to Buy
Objection
Technical Issue / Support Request
Other

Customer message:
{customer_message}

Return only the category name. Do not add explanations.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    return response.text.strip()
    
def _catalog_value(value):
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def get_current_offering_context(customer_message, history=None, catalog_snapshot=None):
    """Build each call turn's facts from the latest shared CRM offering catalog."""
    history = history or []
    try:
        # CRM data is authoritative on every spoken turn so edits and
        # deactivations take effect during an active call.
        offerings = load_offerings_data()
    except Exception as exc:
        print(f"Live offering catalog lookup failed: {exc}")
        return "The current offering catalog is temporarily unavailable. Do not quote offering details."

    if offerings.empty:
        return "No current verified offering details are available."

    for column in ("Knowledge Status", "AI Active", "Lifecycle Status"):
        if column not in offerings.columns:
            offerings[column] = ""

    published = (
        offerings["Knowledge Status"].fillna("").astype(str)
        .str.strip().str.casefold().eq("published")
    )
    ai_active = (
        offerings["AI Active"].fillna("").astype(str)
        .str.strip().str.casefold().isin({"true", "yes", "1"})
    )
    lifecycle = (
        offerings["Lifecycle Status"].fillna("").astype(str)
        .str.strip().str.casefold()
    )
    offerings = offerings[
        published
        & ai_active
        & ~lifecycle.isin({"completed", "expired", "archived", "cancelled"})
    ].copy()

    if offerings.empty:
        return "There are no currently published, AI-active offerings in the catalog."

    query_parts = [str(customer_message or "")]
    query_parts.extend(str(item.get("customer", "")) for item in history[-6:])
    query = " ".join(query_parts).casefold()
    query_words = set(re.findall(r"[a-z0-9]+", query))

    def relevance(row):
        name = _catalog_value(row.get("Offering Name", ""))
        searchable = " ".join(
            _catalog_value(row.get(field, ""))
            for field in ("Offering Name", "Offering Type", "Topics", "Description")
        ).casefold()
        if name and name.casefold() in query:
            return 100
        return len(query_words & set(re.findall(r"[a-z0-9]+", searchable)))

    ranked = sorted(
        (row for _, row in offerings.iterrows()),
        key=relevance,
        reverse=True,
    )[:8]
    fields = (
        ("Offering Name", "Name"),
        ("Offering Type", "Type"),
        ("Description", "Description"),
        ("Topics", "Topics"),
        ("Product Details", "Product details"),
        ("Target Audience", "Audience"),
        ("Start Date", "Start date"),
        ("End Date", "End date"),
        ("Start Time", "Start time"),
        ("End Time", "End time"),
        ("Duration", "Duration"),
        ("Price", "Price"),
        ("Currency", "Currency"),
        ("Registration Link", "Registration link"),
        ("FAQs", "FAQs"),
        ("Policies", "Policies"),
        ("Updated At", "Catalog updated"),
    )
    blocks = []
    for row in ranked:
        blocks.append("\n".join(
            f"{label}: {_catalog_value(row.get(column, ''))}"
            for column, label in fields
            if _catalog_value(row.get(column, ""))
        ))
    return "\n\n".join(blocks)


def get_ai_response(customer_message, history=None, offering_context=""):
    if history is None:
        history = []
    prompt = f"""
You are the AI sales assistant for IITG.

You are speaking with a customer on a live B2B sales call.

Your goals are to:
- Understand the customer's intent.
- Answer questions about the product clearly.
- Handle objections professionally.
- Identify whether the customer is interested, asking about pricing, asking about the product, requesting a demo, requesting more information, not interested, ready to buy, or raising a concern.
- Move the conversation naturally toward the appropriate next sales step.

Current verified offerings catalog (loaded from the CRM for this call turn):
{offering_context or "No current verified offering details are available."}

Use this current catalog as the source of truth for products, services, dates,
prices, registration links, policies, and features. Never rely on possibly stale
details from earlier conversation turns when they conflict with this catalog.
If the customer asks about an offering that is not listed, say that you do not
have confirmed current details for it. Do not substitute a different offering
unless the customer asks for alternatives.

Conversation history:
{chr(10).join([f"Customer: {item['customer']}\nAssistant: {item['assistant']}" for item in history])}

Current customer message:
{customer_message}

Respond naturally and conversationally.
Keep the response short because this is a phone call.
Do not invent prices, features, discounts, guarantees, ROI, savings, or other unsupported facts.
If you do not have enough information, say so and ask a relevant question.
Do not use markdown, bullet points, or headings.

Return only what should be spoken aloud to the customer.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    return response.text.strip()


@app.route("/voice", methods=["GET", "POST"])
def voice():
    response = VoiceResponse()

    lead_name = request.values.get("lead", "").strip()
    call_sid = request.values.get("CallSid", "unknown")
    caller_number = request.values.get("From", "").strip()
    catalog_token = request.values.get("catalog", "")
    catalog_signature = request.values.get("catalog_sig", "")
    catalog_snapshot = verify_catalog_snapshot(catalog_token, catalog_signature)

    if lead_name:
        call_leads[call_sid] = lead_name

    if caller_number:
        call_numbers[call_sid] = caller_number
    if catalog_snapshot is not None:
        call_catalog_context[call_sid] = catalog_snapshot
    elif catalog_token:
        print(f"Rejected an invalid offering snapshot for call [{call_sid}].")
        response.say(
            "I can't verify the current course and service details right now. "
            "Please contact our team for the latest information. Goodbye."
        )
        response.hangup()
        return str(response)
    gather = Gather(
        input="speech",
        action=(
            _process_action_url(catalog_token, catalog_signature)
            if catalog_snapshot is not None
            else _process_action_url()
        ),
        method="POST",
        speech_timeout="auto",
        language="en-IN"
    )

    gather.say(
        "Hello! This is the IITG AI Sales Agent. "
        "Your call has been connected successfully. "
        "How can I help you today?"
    )

    response.append(gather)

    response.say(
        "I didn't hear a response. Thank you for calling. Goodbye."
    )

    return str(response)


def get_sales_action(intent):
    """Map customer intent to the appropriate sales action."""

    action_map = {
        "Interested": "Schedule Follow-up",
        "Pricing Question": "Send Pricing",
        "Product Question": "Send Information",
        "Request for Demo": "Schedule Demo",
        "Request for More Information": "Send Information",
        "Not Interested": "Close Lead",
        "Ready to Buy": "Escalate to Human",
        "Objection": "Schedule Follow-up",
        "Technical Issue / Support Request": "Escalate to Human",
        "Other": "No Action"
    }

    return action_map.get(intent, "No Action")

@app.route("/process", methods=["GET", "POST"])
def process():
    speech = request.values.get("SpeechResult", "")
    call_sid = request.values.get("CallSid", "unknown")
    lead_name = call_leads.get(call_sid, "")
    if not lead_name:
        lead_name, _ = _call_crm_identity(call_sid)
    catalog_token = request.values.get("catalog", "")
    catalog_signature = request.values.get("catalog_sig", "")
    catalog_snapshot = verify_catalog_snapshot(catalog_token, catalog_signature)
    if catalog_snapshot is None and not catalog_token:
        catalog_snapshot = call_catalog_context.get(call_sid)

    response = VoiceResponse()
    if catalog_token and catalog_snapshot is None:
        response.say(
            "I can't verify the current course and service details right now. "
            "Please contact our team for the latest information. Goodbye."
        )
        response.hangup()
        return str(response)

    history = conversation_history.setdefault(call_sid, [])

    if speech:
                # -----------------------------------------
        # SMART CALLBACK / RESCHEDULING
        # -----------------------------------------

        scheduled_time = parse_voice_call_time(
            speech
        )

        callback_phrases = [
            "call me later",
            "call me tomorrow",
            "call me today",
            "call me again",
            "call me after",
            "call me in",
            "call again",

            "schedule the call",
            "schedule a call",
            "schedule my call",
            "schedule the call after",
            "schedule the call in",
            "schedule a call after",
            "schedule a call in",

            "reschedule",
            "reschedule the call",
            "reschedule my call",

            "i am busy",
            "i'm busy",
            "i don't have time",
            "i do not have time",
            "i cannot talk",
            "i can't talk",
            "unable to talk",
            "not able to talk",
            "not available right now",
            "busy right now",
            "call me back"
        ]

        is_callback_request = any(
            phrase in speech.lower()
            for phrase in callback_phrases
        )

        if (
            is_callback_request
            and scheduled_time
            and lead_name
        ):
            caller_number = call_numbers.get(
                call_sid,
                ""
            )

            schedule_voice_callback(
                lead_name,
                scheduled_time,
                caller_number
            )

            update_voice_lead(
                lead_name,
                "Call Scheduled",
                "Reschedule Call"
            )
            _, visitor_id = _call_crm_identity(call_sid)
            _record_voice_activity(
                f"voice-turn:{call_sid}:{len(history) + 1}",
                lead_name,
                visitor_id,
                "CRM Follow-up",
                f"Customer requested a callback; follow-up scheduled for {scheduled_time.strftime('%d %B %Y at %I:%M %p IST')}.",
                call_sid=call_sid,
            )

            email_status = (
                send_voice_confirmation_email(
                    lead_name,
                    scheduled_time
                )
            )

            formatted_time = (
                scheduled_time.strftime(
                    "%d %B %Y at %I:%M %p"
                )
            )

            print(
                f"Callback scheduled [{call_sid}]: "
                f"{formatted_time}"
            )

            print(
                f"Confirmation email [{call_sid}]: "
                f"{email_status}"
            )

            response.say(
                f"Of course. I've rescheduled your call "
                f"for {formatted_time}. "
                "You will also receive a confirmation email. "
                "Thank you."
            )

            response.hangup()

            return str(response)
        # Detect when the customer wants to end the call
        end_call_phrases = [
            "hang up",
            "hangup",
            "end the call",
            "end call",
            "goodbye",
            "bye",
            "disconnect",
            "you can go",
            "that's all",
            "that is all"
        ]

        if any(phrase in speech.lower() for phrase in end_call_phrases):
            _, visitor_id = _call_crm_identity(call_sid)
            _record_voice_activity(
                f"voice-turn:{call_sid}:{len(history) + 1}",
                lead_name,
                visitor_id,
                "CRM Call Result",
                "Customer indicated the call could end; the AI closed the conversation politely.",
                call_sid=call_sid,
            )
            response.say(
                "Thank you for your time. Goodbye."
            )
            response.hangup()
            return str(response)

        print(f"Customer [{call_sid}]: {speech}")

        # Step 1: Identify customer intent
        intent = classify_intent(speech)

        # Step 2: Automatically determine sales action
        sales_action = get_sales_action(intent)

        # Step 2.5: Update the lead record
        lead_name = call_leads.get(call_sid, "")

        if sales_action != "No Action":
            status_map = {
                "Schedule Follow-up": "Interested",
                "Send Pricing": "Pricing Requested",
                "Send Information": "Information Requested",
                "Schedule Demo": "Demo Requested",
                "Close Lead": "Closed",
                "Escalate to Human": "Needs Human Review"
            }

            lead_status = status_map.get(sales_action, "Contacted")

            update_voice_lead(
                lead_name,
                lead_status,
                sales_action
            )
            
        if intent == "Not Interested":
            history.append({
                "customer": speech,
                "assistant": "Understood. Thank you for your time. We won't follow up further regarding this request. Goodbye.",
                "intent": intent,
                "sales_action": sales_action,
            })
            _, visitor_id = _call_crm_identity(call_sid)
            _record_voice_activity(
                f"voice-turn:{call_sid}:{len(history)}",
                lead_name,
                visitor_id,
                intent,
                "Customer declined further contact; the AI acknowledged the request and ended the call.",
                call_sid=call_sid,
            )
            response.say(
                "Understood. Thank you for your time. We won't follow up further regarding this request. Goodbye."
            )
            response.hangup()
            return str(response)
        status_map = {
            "Close Lead": "Closed",
            "Schedule Follow-up": "Follow-up",
            "Send Pricing": "Contacted",
            "Send Information": "Contacted",
            "Schedule Demo": "Demo Requested",
            "Escalate to Human": "Escalated",
            "No Action": "New"
        }

        lead_status = status_map.get(sales_action, "Contacted")

        update_voice_lead(
            lead_name,
            lead_status,
            sales_action
        )

        print(f"Intent [{call_sid}]: {intent}")
        print(f"Sales Action [{call_sid}]: {sales_action}")

        # Step 3: Generate context-aware response
        ai_reply = get_ai_response(
            f"""
Customer intent: {intent}
Recommended sales action: {sales_action}
Customer message: {speech}
""",
            history,
            get_current_offering_context(speech, history, catalog_snapshot),
        )

        print(f"AI [{call_sid}]: {ai_reply}")

        # Step 4: Store conversation memory
        history.append({
            "customer": speech,
            "assistant": ai_reply,
            "intent": intent,
            "sales_action": sales_action
        })
        _, visitor_id = _call_crm_identity(call_sid)
        _record_voice_activity(
            f"voice-turn:{call_sid}:{len(history)}",
            lead_name,
            visitor_id,
            intent,
            f"Voice conversation: customer asked about {intent}; the AI responded using the latest verified CRM offerings. Recommended next step: {sales_action}.",
            call_sid=call_sid,
        )

        # Step 5: Speak response
        response.say(ai_reply)

        # Step 6: Continue conversation
        follow_up_map = {
            "Interested": "Would you like me to arrange a brief follow-up call?",
            "Pricing Question": "Would you like me to help you with the pricing details?",
            "Product Question": "Would you like me to clarify anything specific about the product?",
            "Request for Demo": "Would you like to discuss a suitable time for the demo?",
            "Request for More Information": "Would you like me to send you the relevant information?",
            "Not Interested": "Understood. Thank you for your time.",
            "Ready to Buy": "Would you like me to connect you with a member of our sales team?",
            "Objection": "Would you like to discuss that concern further?",
            "Technical Issue / Support Request": "Would you like me to connect you with our support team?",
            "Other": "Is there anything specific you'd like to know?"
        }

        follow_up = follow_up_map.get(
            intent,
            "Is there anything specific you'd like to know?"
        )

        gather = Gather(
            input="speech",
            action=(
                _process_action_url(catalog_token, catalog_signature)
                if catalog_snapshot is not None and catalog_token
                else _process_action_url()
            ),
            method="POST",
            speech_timeout="auto",
            language="en-IN"
        )

        gather.say(follow_up)
        response.append(gather)

    else:
        response.say(
            "I didn't hear a response. Thank you for calling. Goodbye."
        )
        response.hangup()

    return str(response)


if __name__ == "__main__":
    app.run(port=5000, debug=True)
