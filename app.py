import os
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from google import genai
from twilio.rest import Client as TwilioClient
import smtplib
import threading
import time
import re
import hmac
from zoneinfo import ZoneInfo
from email.message import EmailMessage
load_dotenv()
from crm_storage import (
    crm_path,
    ensure_seed_file,
    initialize_database,
    sync_after_csv_write,
)

ensure_seed_file("leads.csv")
initialize_database()

if "leads" not in st.session_state:
    st.session_state["leads"] = pd.read_csv(crm_path("leads.csv"))

if "using_uploaded_leads" not in st.session_state:
    st.session_state["using_uploaded_leads"] = False

if "uploaded_file_name" not in st.session_state:
    st.session_state["uploaded_file_name"] = None

load_dotenv()

print("DEVELOPER_PASSWORD configured:", bool(os.getenv("DEVELOPER_PASSWORD")))

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

IST = ZoneInfo("Asia/Kolkata")

scheduled_calls = {}

def parse_call_time(message):
    """Convert a customer's call-time request into a future datetime."""

    message_lower = message.lower().strip()
    now = datetime.now(IST)

    # -----------------------------------------
    # RELATIVE TIME
    # Examples:
    # "after 5 minutes"
    # "in 20 minutes"
    # "after 2 hours"
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
    # DATE CONTEXT
    # -----------------------------------------

    if "tomorrow" in message_lower:

        target_date = now.date() + timedelta(days=1)

    elif "today" in message_lower:

        target_date = now.date()

    else:

        target_date = now.date()

    # -----------------------------------------
    # EXACT TIME
    # Examples:
    # "at 5 PM"
    # "at 7:30 PM"
    # "tomorrow at 5 PM"
    # "today at 7:30 PM"
    # -----------------------------------------

    time_match = re.search(
        r"(?:at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        message_lower
    )

    if time_match:

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

        # If no explicit date was given and the time
        # has already passed, schedule for tomorrow.
        if (
            "tomorrow" not in message_lower
            and "today" not in message_lower
            and scheduled_time <= now
        ):
            scheduled_time += timedelta(days=1)

        # Never allow a past "today" request.
        if scheduled_time <= now:

            if "today" in message_lower:
                return None

        return scheduled_time

    return None

def schedule_demo_call(lead_name, scheduled_time):
    """Schedule a Twilio call for a specific future time."""

    def wait_and_call():
        wait_seconds = (scheduled_time - datetime.now(IST)).total_seconds()

        if wait_seconds > 0:
            time.sleep(wait_seconds)

        success, result = initiate_demo_call(lead_name)

        scheduled_calls[lead_name] = {
            "scheduled_time": scheduled_time,
            "status": "Called" if success else "Failed",
            "result": result
        }

    thread = threading.Thread(
        target=wait_and_call,
        daemon=True
    )

    thread.start()

    scheduled_calls[lead_name] = {
        "scheduled_time": scheduled_time,
        "status": "Scheduled"
    }

    return True, "Call scheduled successfully."

def initiate_demo_call(lead_name):
    """Initiate an outbound Twilio call for the selected lead."""

    try:
        twilio_client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )

        to_number = os.getenv("DEMO_CALL_TO_NUMBER")
        from_number = os.getenv("TWILIO_PHONE_NUMBER")

        if not to_number or not from_number:
            return False, "Demo phone number is not configured."

        call = twilio_client.calls.create(
            to=to_number,
            from_=from_number,
            url=f"https://iitg-ai-sales-agent.onrender.com/voice?lead={lead_name}"
        )

        return True, call.sid

    except Exception as e:
        return False, str(e)
# -----------------------------
# SALES ACTION FUNCTIONS
# -----------------------------
def send_email(to_email, subject, body):
    """Send an email to the matched lead."""

    sender_email = os.getenv("EMAIL_ADDRESS")
    sender_password = os.getenv("EMAIL_PASSWORD")

    if not sender_email or not sender_password:
        return "Email credentials not configured."

    msg = EmailMessage()
    msg["From"] = sender_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender_email, sender_password)
            smtp.send_message(msg)

        return "Email sent successfully."

    except Exception as e:
        return f"Email failed: {e}"
    
def record_action(action, lead=None, intent="", sentiment="", priority=""):
    """Record a sales activity in session state and persist it to CSV."""

    if "action_log" not in st.session_state:
        st.session_state["action_log"] = []

    activity = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "lead": lead.get("Name", "") if lead is not None else "",
        "intent": intent,
        "sentiment": sentiment,
        "priority": priority,
        "action": action,
    }

    # Keep activity available during the current Streamlit session
    st.session_state["action_log"].append(activity)

    # Persist activity to CSV
    activity_file = crm_path("sales_activity.csv")

    activity_df = pd.DataFrame([activity])

    if os.path.exists(activity_file):
        activity_df.to_csv(
            activity_file,
            mode="a",
            header=False,
            index=False
        )
    else:
        activity_df.to_csv(
            activity_file,
            index=False
        )
        
    sync_after_csv_write("sales_activity.csv")


def merge_crm_upload(uploaded_file, filename):
    """Import CRM CSV rows without removing existing records or values."""
    incoming = pd.read_csv(uploaded_file)
    destination = crm_path(filename)

    if filename == "sales_activity.csv":
        current = (
            pd.read_csv(destination)
            if os.path.exists(destination)
            else pd.DataFrame(columns=incoming.columns)
        )
        before = len(current)
        merged = pd.concat([current, incoming], ignore_index=True, sort=False)
        merged = merged.drop_duplicates(keep="first")
        merged.to_csv(destination, index=False)
        sync_after_csv_write(filename)
        return len(merged) - before

    if filename != "leads.csv" or "Name" not in incoming.columns:
        raise ValueError("The leads file must contain a Name column.")

    current = (
        pd.read_csv(destination)
        if os.path.exists(destination)
        else pd.DataFrame(columns=incoming.columns)
    )
    columns = list(dict.fromkeys([*current.columns, *incoming.columns]))
    current = current.reindex(columns=columns).astype("object")
    incoming = incoming.reindex(columns=columns)
    def lead_key(row):
        email = row.get("Email")
        if not pd.isna(email) and str(email).strip():
            return "email:" + str(email).strip().casefold()
        name = row.get("Name")
        if not pd.isna(name) and str(name).strip():
            return "name:" + str(name).strip().casefold()
        return ""

    current_positions = {}
    for index, row in current.iterrows():
        key = lead_key(row)
        if key:
            current_positions[key] = index

    added = 0
    for _, imported in incoming.iterrows():
        key = lead_key(imported)
        if not key or key not in current_positions:
            current = pd.concat([current, imported.to_frame().T], ignore_index=True)
            if key:
                current_positions[key] = len(current) - 1
            added += 1
            continue

        row_index = current_positions[key]
        for column, value in imported.items():
            old_value = current.at[row_index, column]
            if pd.isna(old_value) or not str(old_value).strip():
                current.at[row_index, column] = value

    current.to_csv(destination, index=False)
    sync_after_csv_write(filename)
    return added


def update_lead_record(
    customer_message,
    status,
    last_action,
    lead_name=None,
    priority=None,
    intent=None,
    sentiment=None,
    notes=None,
):
    """Update the matched lead and persist changes to the default CSV."""

    leads = pd.read_csv(crm_path("leads.csv"))

    required_columns = [
        "Status",
        "Last Action",
        "Last Interaction",
        "Priority",
        "Last Intent",
        "Last Sentiment",
        "Notes",
    ]

    for column in required_columns:
        if column not in leads.columns:
            leads[column] = ""

        leads[column] = leads[column].astype("object")

    if not lead_name:
        return False

    target_name = str(lead_name).strip().lower()
    for index, lead in leads.iterrows():

        if str(lead["Name"]).strip().lower() == target_name:

            leads.at[index, "Status"] = status
            leads.at[index, "Last Action"] = last_action

            leads.at[index, "Last Interaction"] = (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            )

            if priority is not None:
                leads.at[index, "Priority"] = priority

            if intent is not None:
                leads.at[index, "Last Intent"] = intent

            if sentiment is not None:
                leads.at[index, "Last Sentiment"] = sentiment

            if notes is not None:
                leads.at[index, "Notes"] = notes

            if not st.session_state.get(
                "using_uploaded_leads",
                False
            ):
            
                leads.to_csv(crm_path("leads.csv"), index=False)
                sync_after_csv_write("leads.csv")
            st.session_state["leads"] = leads.copy()

            st.session_state["customer_lead"] = (
                leads.loc[index].to_dict()
            )

            return True

    return False


def parse_ai_result(ai_result):
    result = {
        "intent": "",
        "sentiment": "",
        "priority": "",
        "recommended_action": "",
        "reply": ""
    }

    current_field = None

    for line in ai_result.splitlines():
        line = line.strip()

        if not line:
            continue

        if line.startswith("INTENT:"):
            value = line.split(":", 1)[1].strip()
            result["intent"] = value
            current_field = "intent"

        elif line.startswith("SENTIMENT:"):
            value = line.split(":", 1)[1].strip()
            result["sentiment"] = value
            current_field = "sentiment"

        elif line.startswith("PRIORITY:"):
            value = line.split(":", 1)[1].strip()
            result["priority"] = value
            current_field = "priority"

        elif line.startswith("RECOMMENDED ACTION:"):
            value = line.split(":", 1)[1].strip()
            result["recommended_action"] = value
            current_field = "recommended_action"

        elif line.startswith("REPLY:"):
            value = line.split(":", 1)[1].strip()
            result["reply"] = value
            current_field = "reply"

        elif current_field == "recommended_action" and not result["recommended_action"]:
            result["recommended_action"] = line

        elif current_field == "reply":
            if result["reply"]:
                result["reply"] += " " + line
            else:
                result["reply"] = line

    return result

def analyze_customer_message(
    customer_message,
    conversation_text="",
    lead=None
):
    """Classify the customer message and recommend a controlled sales action."""

    product = lead.get("Product", "") if lead else ""
    company = lead.get("Company", "") if lead else ""

    prompt = f"""
You are the sales intelligence module of the IITG AI Sales Agent.

Customer company:
{company}

Product:
{product}

Conversation:
{conversation_text}

Latest customer message:
{customer_message}

Classify the latest customer message.

Allowed INTENT values:

Interested
Pricing Question
Product Question
Request for Demo
Request for More Information
Not Interested
Ready to Buy
Objection
Other

Allowed SENTIMENT values:

Positive
Neutral
Negative

Allowed PRIORITY values:

High
Medium
Low

Allowed RECOMMENDED ACTION values:

Send Pricing
Schedule Demo
Send Information
Schedule Follow-up
Escalate to Human
Close Lead
No Action

Routing rules:

- Pricing Question -> Send Pricing
- Product Question -> Send Information
- Request for Demo -> Schedule Demo
- Request for More Information -> Send Information
- Not Interested -> Close Lead
- Ready to Buy -> Schedule Follow-up
- Objection -> Schedule Follow-up
- Interested -> Schedule Follow-up
- Other -> No Action

Return exactly these four lines:

INTENT: <one allowed value>
SENTIMENT: <one allowed value>
PRIORITY: <one allowed value>
RECOMMENDED ACTION: <one allowed value>
"""

    try:

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )

        result = parse_ai_result(response.text)

        allowed_intents = {
            "Interested",
            "Pricing Question",
            "Product Question",
            "Request for Demo",
            "Request for More Information",
            "Not Interested",
            "Ready to Buy",
            "Objection",
            "Other",
        }

        allowed_sentiments = {
            "Positive",
            "Neutral",
            "Negative",
        }

        allowed_priorities = {
            "High",
            "Medium",
            "Low",
        }

        allowed_actions = {
            "Send Pricing",
            "Schedule Demo",
            "Send Information",
            "Schedule Follow-up",
            "Escalate to Human",
            "Close Lead",
            "No Action",
        }

        if result["intent"] not in allowed_intents:
            result["intent"] = "Other"

        if result["sentiment"] not in allowed_sentiments:
            result["sentiment"] = "Neutral"

        if result["priority"] not in allowed_priorities:
            result["priority"] = "Medium"

        if result["recommended_action"] not in allowed_actions:
            result["recommended_action"] = "No Action"

        return result

    except Exception as e:

        print(f"Sales analysis error: {e}")

        return {
            "intent": "Other",
            "sentiment": "Neutral",
            "priority": "Medium",
            "recommended_action": "No Action",
            "reply": "",
        }


def handle_pricing_question(customer_message):
    prompt = f"""
You are a professional B2B sales assistant.

The customer has asked about pricing.

Customer message:
{customer_message}

Create a concise and professional response.

The response should:
- Acknowledge the customer's pricing question.
- Do not invent or state any specific prices.
- Do not invent pricing factors unless the customer has mentioned them.
- Do not invent features, integrations, discounts, guarantees, plans, or implementation details.
- Explain that pricing information can be discussed based on the customer's requirements.
- Ask for relevant requirements if necessary.
- End with a clear and natural next step.
- Do not invent the customer's name.
- Do not use placeholders such as [Name], [Your Name], or [Your Title].

Return only the message that should be sent to the customer.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    action = response.text

    return action

def handle_demo_request(customer_message):
    prompt = f"""
You are a professional B2B sales assistant.

The customer has requested a product demonstration.

Customer message:
{customer_message}

Create a concise and professional demo-scheduling response.

The message should:
- Acknowledge the customer's interest in a demo.
- Be natural and helpful.
- Do not invent the customer's name.
- Do not use placeholders such as [Name], [Your Name], or [Your Title].
- Do not invent specific dates or times.
- Ask the customer for a suitable date and time.
- Keep the message concise.
- End with a clear next step.

Return only the message that should be sent to the customer.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    action = response.text

    return action


def handle_information_request(customer_message):
    prompt = f"""
You are a professional B2B sales assistant.

The customer has requested more information.

Customer message:
{customer_message}

Create a concise and professional response.

The message should:
- Acknowledge the customer's request for information.
- Clearly offer to provide relevant information about the product or service.
- Be helpful and natural.
- Do not invent the customer's name.
- Do not use placeholders such as [Name], [Your Name], or [Your Title].
- Do not make unsupported claims.
- Ask what specific information would be most useful if necessary.
- End with a clear next step.

Return only the message that should be sent to the customer.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    action = response.text

    return action

def handle_interested(customer_message):
    prompt = f"""
You are a professional B2B sales assistant.

The customer has shown interest in the product.

Customer message:
{customer_message}

Create a concise and professional follow-up response.

The response should:
- Acknowledge the customer's interest.
- Thank them naturally.
- Understand what aspect of the product they are most interested in.
- Encourage the conversation toward a useful next step.
- Do not invent prices, features, integrations, guarantees, or technical details.
- Do not invent the customer's name.
- Do not use placeholders such as [Name], [Your Name], or [Your Title].
- Keep the response concise and natural.

Return only the message that should be sent to the customer.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    action = response.text

    return action
def handle_product_question(customer_message):
    prompt = f"""
You are a professional and helpful B2B sales assistant.

The customer has asked a question about the product.

Customer message:
{customer_message}

Create a concise and professional response.

The response should:
- Directly address the customer's product question.
- Only state product information that is explicitly provided in the customer message or in verified product information given in this prompt.
- Never assume or infer that the product has a particular feature, integration, capability, compatibility, pricing, guarantee, or technical specification.
- If the customer's question asks about information that is not explicitly available, clearly say that the information cannot be confirmed from the available information.
- When information cannot be confirmed, suggest speaking with the sales team or having the requirement reviewed.
- Do not invent prices, features, integrations, guarantees, supported platforms, or technical specifications.
- Do not mention specific third-party companies or platforms unless they are explicitly provided in verified product information.
- Maintain a helpful, professional, and natural tone.
- Ask a useful next-step question when appropriate.
- Do not invent the customer's name.
- Do not use placeholders such as [Name], [Your Name], or [Your Title].
- Keep the response concise.

Return only the message that should be sent to the customer.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    action = response.text

    return action

def handle_escalate_to_human():
    action = "Lead escalated to human representative."
    return action


def handle_not_interested():
    action = "Lead marked as not interested."
    return action


def handle_ready_to_buy():
    action = "Lead marked as high priority and ready to buy."
    return action


def handle_objection(customer_message):
    prompt = f"""
You are a professional and consultative B2B sales assistant.

The customer has raised an objection.

Customer message:
{customer_message}

Create a concise, professional response that addresses the customer's objection.

The response should:
- Acknowledge the customer's concern respectfully.
- Never argue with or pressure the customer.
- Address the objection using only information explicitly provided by the customer or verified product information available in the conversation.
- Do not invent or imply facts, prices, discounts, savings, ROI, performance improvements, features, integrations, guarantees, or technical capabilities.
- Do not claim that the product will reduce costs, improve efficiency, or deliver a return on investment unless that information has been explicitly provided.
- Do not invent the customer's name.
- Do not use placeholders such as [Name], [Your Name], or [Your Title].
- Focus on understanding the customer's needs.
- If appropriate, suggest a brief conversation or another useful next step.

Return only the message that should be sent to the customer.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    action = response.text

    return action


def handle_other():
    return "No specific sales action required."

def load_leads():
    """Load and normalize the active lead database."""

    if "leads" not in st.session_state:
        st.session_state["leads"] = pd.read_csv(crm_path("leads.csv"))

    leads = st.session_state["leads"]

    required_columns = {
        "Priority": "",
        "Status": "New",
        "Last Action": "",
        "Last Interaction": "",
        "Last Intent": "",
        "Last Sentiment": "",
    }

    for column, default_value in required_columns.items():

        if column not in leads.columns:
            leads[column] = default_value

    st.session_state["leads"] = leads

    return leads


def find_lead(lead_name=None, email=None):
    """Find a lead by name first, or by email if provided."""

    leads = load_leads()

    if lead_name:

        name = str(lead_name).strip().lower()

        matched = leads[
            leads["Name"].astype(str).str.strip().str.lower()
            == name
        ]

        if not matched.empty:
            return matched.iloc[0].to_dict()

    if email and "Email" in leads.columns:

        email_value = str(email).strip().lower()

        matched = leads[
            leads["Email"].astype(str).str.strip().str.lower()
            == email_value
        ]

        if not matched.empty:
            return matched.iloc[0].to_dict()

    return None


def execute_sales_action(intent, recommended_action, customer_message):
    """
    Execute the appropriate sales action based on the
    AI-recommended action.
    """

    if recommended_action == "Send Pricing":
        return handle_pricing_question(customer_message)

    elif recommended_action == "Schedule Demo":
        return handle_demo_request(customer_message)

    elif recommended_action == "Send Information":
        if intent == "Product Question":
            return handle_product_question(customer_message)
        else:
            return handle_information_request(customer_message)

    elif recommended_action == "Escalate to Human":
        return handle_escalate_to_human()

    elif recommended_action == "Schedule Follow-up":

        if intent == "Objection":
            return handle_objection(customer_message)

        elif intent == "Interested":
            return handle_interested(customer_message)

        elif intent == "Ready to Buy":
            return handle_ready_to_buy()

        else:
            return handle_other()

    elif recommended_action == "Close Lead":
        return "Lead closed. No follow-up email required."

    elif recommended_action == "No Action":
        return "No sales action required."

    else:
        return handle_other()


def generate_customer_message(customer_message, action_result):
    if action_result == "Lead closed. No follow-up email required.":
        return "Understood. We won’t follow up further regarding this request. Wishing you all the best."

    prompt = f"""
You are a professional B2B sales assistant.

Generate a concise customer-facing message based strictly on the customer's
response and the sales action taken.

Customer response:
{customer_message}

Sales action:
{action_result}

Rules:
- Acknowledge the customer's request or interest naturally.
- Clearly communicate the next step.
- Use only information provided in the customer response or sales action.
- If the sales action is "Close Lead", do not claim that the customer was removed from a mailing list, unsubscribed, deleted, or changed in any external system. Simply acknowledge the request and state that no further follow-up will be made regarding this request.
- Do not invent dates, times, deadlines, availability, prices, discounts,
  features, integrations, guarantees, technical details, or company claims.
- Do not invent or assume the customer's name.
- Do not mention "next week", "tomorrow", or any specific timeframe unless
  the customer explicitly mentioned it.
- Do not add a signature or closing such as "Best regards", "Professional
  Sales Team", or "Sales Team".
- Do not use placeholders.
- Keep the message short, professional, and ready to send.

Return only the customer-facing message.
"""

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=prompt
    )

    return response.text

def execute_and_update_sales_action(
    intent,
    recommended_action,
    customer_message,
    selected_lead=None,
    priority=None,
    sentiment=None,
):
    """Execute a sales action, update the matched lead, and return CRM results."""

    action_result = execute_sales_action(
        intent,
        recommended_action,
        customer_message
    )

    if recommended_action == "Close Lead":

        customer_message_generated = (
            "Understood. We won’t follow up further regarding this request. "
            "Wishing you all the best."
        )

    else:

        customer_message_generated = generate_customer_message(
            customer_message,
            action_result
        )

    if intent == "Ready to Buy":
        status = "Ready to Buy"

    elif intent == "Not Interested":
        status = "Not Interested"

    else:

        status_map = {
            "Schedule Demo": "Demo Requested",
            "Send Pricing": "Pricing Requested",
            "Send Information": "Information Requested",
            "Schedule Follow-up": "Follow-up Required",
            "Escalate to Human": "Escalated",
            "Close Lead": "Closed",
        }

        status = status_map.get(
            recommended_action,
            "No Change"
        )

    log_messages = {

        "Schedule Demo":
            "Demo requested.",

        "Send Pricing":
            "Pricing requested.",

        "Send Information":
            "Information requested.",

        "Schedule Follow-up":
            "Follow-up required.",

        "Escalate to Human":
            "Lead escalated to human representative.",

        "Close Lead":
            "Lead closed.",

        "No Action":
            "No sales action required.",
    }

    lead = selected_lead

    record_action(
        log_messages.get(
            recommended_action,
            "Sales action completed."
        ),
        lead=lead,
        intent=intent,
        sentiment=sentiment or "",
        priority=priority or "",
    )

    if recommended_action == "No Action":

        lead_updated = False

    else:

        lead_updated = update_lead_record(
            customer_message,
            status,
            recommended_action,
            lead.get("Name") if lead else None,
            priority,
            intent,
            sentiment,
        )

    email_actions = {
        "Send Pricing",
        "Schedule Demo",
        "Send Information",
        "Schedule Follow-up",
        "Escalate to Human",
    }

    if (
        recommended_action in email_actions
        and lead
        and lead.get("Email")
    ):

        email_status = send_email(
            lead["Email"],
            f"Follow-up regarding {recommended_action}",
            customer_message_generated
        )

    elif recommended_action in email_actions:

        email_status = "Lead email not found."

    else:

        email_status = "No email required for this action."

    st.session_state["email_status"] = email_status

    st.session_state["last_analysis"] = {
        "intent": intent,
        "sentiment": sentiment or "",
        "priority": priority or "",
        "recommended_action": recommended_action,
        "status": status,
    }

    if lead_updated:

        action_message = (
            f"{recommended_action} completed and "
            "the lead record was updated."
        )

    elif recommended_action == "No Action":

        action_message = (
            "No sales action was required. "
            "The lead was not changed."
        )

    else:

        action_message = (
            f"The action '{recommended_action}' was completed, "
            "but no matching lead was found in the database."
        )

    return {
        "action_result": action_result,
        "customer_message": customer_message_generated,
        "action_message": action_message,
        "lead_updated": lead_updated,
        "email_status": email_status,
        "status": status,
    }
SENDER_NAME = "Ayush Roy"
SENDER_TITLE = "AI Sales Assistant"
SENDER_COMPANY = "IITG AI Sales Agent"

# ============================================================
# CUSTOMER-FACING AI SALES ASSISTANT
# ============================================================

st.set_page_config(
    page_title="IITG AI Sales Assistant",
    page_icon="🤖",
    layout="wide"
)

# -----------------------------
# CUSTOMER SESSION
# -----------------------------

if "customer_name" not in st.session_state:
    st.session_state["customer_name"] = ""

if "customer_messages" not in st.session_state:
    st.session_state["customer_messages"] = []

if "customer_lead" not in st.session_state:
    st.session_state["customer_lead"] = None

developer_mode = st.query_params.get("mode", "customer") == "developer"

if developer_mode and not st.session_state.get("developer_authenticated", False):
    developer_password = os.getenv("DEVELOPER_PASSWORD")

    st.markdown(
        '<div style="color:#17324d;font-size:2rem;font-weight:750;'
        'letter-spacing:-0.03em;margin:0.6rem 0 0.25rem;">'
        'Developer Console</div>',
        unsafe_allow_html=True,
    )
    st.caption("Private CRM, lead management, sales activity, and AI analytics")

    if not developer_password:
        st.error("DEVELOPER_PASSWORD is not configured.")
        st.stop()

    entered_password = st.text_input(
        "Developer Password",
        type="password"
    )

    if st.button(
        "Unlock Developer Console",
        type="primary"
    ):
        if hmac.compare_digest(
            entered_password,
            developer_password
        ):
            st.session_state["developer_authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect password.")

    st.stop()

if developer_mode:

    st.sidebar.title("Developer")

    if st.sidebar.button(
        "Refresh dashboard",
        width="stretch"
    ):
        st.rerun()

    if st.sidebar.button(
        "Sign out",
        width="stretch"
    ):
        st.session_state["developer_authenticated"] = False
        st.rerun()



# -----------------------------
# CUSTOM CSS
# -----------------------------

st.markdown(
    """
    <style>

    .main-title {
        text-align: center;
        font-size: 34px;
        font-weight: 700;
        margin-bottom: 4px;
    }

    .subtitle {
        text-align: center;
        color: #777;
        margin-bottom: 30px;
    }

    .welcome-card {
        padding: 22px;
        border-radius: 14px;
        background: rgba(128,128,128,0.08);
        margin-bottom: 20px;
    }

    .assistant-label {
        font-weight: 600;
        margin-bottom: 4px;
    }

    .developer-login-title {
        color: #17324d;
        font-size: 2rem;
        font-weight: 750;
        letter-spacing: -0.03em;
        margin: 0.6rem 0 0.25rem;
    }

    .developer-console-header {
        border: 1px solid rgba(70, 110, 150, 0.22);
        border-radius: 16px;
        padding: 1.1rem 1.35rem;
        margin: 0.5rem 0 1.2rem;
        background: linear-gradient(115deg, rgba(35, 91, 130, 0.12), rgba(99, 82, 160, 0.07));
    }

    .developer-console-header h1 {
        margin: 0;
        font-size: 1.75rem;
        letter-spacing: -0.03em;
    }

    .developer-console-header p {
        margin: 0.35rem 0 0;
        opacity: 0.75;
    }

    </style>
    """,
    unsafe_allow_html=True
)


# -----------------------------
# HEADER
# -----------------------------

if not developer_mode:
    st.markdown(
        '<div class="main-title">🤖 IITG AI Sales Assistant</div>',
        unsafe_allow_html=True
    )

    st.markdown(
        '<div class="subtitle">Your intelligent sales assistant</div>',
        unsafe_allow_html=True
    )


# -----------------------------
# CUSTOMER NAME
# -----------------------------

if not developer_mode and not st.session_state["customer_name"]:

    st.markdown(
        """
        <div class="welcome-card">
        <h3>Welcome 👋</h3>
        <p>Please enter your name to begin your conversation.</p>
        </div>
        """,
        unsafe_allow_html=True
    )

    name = st.text_input(
        "Your Name",
        placeholder="Enter your name..."
    )

    if st.button("Start Conversation", type="primary", width="stretch"):

        if name.strip():

            st.session_state["customer_name"] = name.strip()
            st.session_state["customer_messages"] = []

            leads = load_leads()

            matched = leads[
                leads["Name"].astype(str).str.strip().str.lower()
                == name.strip().lower()
            ]

            if not matched.empty:
                st.session_state["customer_lead"] = (
                    matched.iloc[0].to_dict()
                )
            else:
                st.session_state["customer_lead"] = None

            st.rerun()

        else:
            st.warning("Please enter your name.")


# ============================================================
# CHAT INTERFACE
# ============================================================

elif not developer_mode:

    customer_name = st.session_state["customer_name"]

    # -----------------------------
    # CUSTOMER CALL CONTROLS
    # -----------------------------

    with st.sidebar:

        st.markdown("Contact Sales")

        active_lead = st.session_state.get(
            "customer_lead"
        )

        if st.button(
            "Call me now",
            width="stretch",
            disabled=not active_lead
        ):

            success, result = initiate_demo_call(
                active_lead["Name"]
            )

            if success:

                update_lead_record(
                    "Customer requested an immediate call.",
                    "Call Initiated",
                    "Initiate Call",
                    active_lead["Name"],
                    None,
                    "Call Request",
                    "Positive",
                )

                record_action(
                    "Call initiated.",
                    lead=active_lead,
                    intent="Call Request",
                    sentiment="Positive",
                    priority=""
                )

                st.success(
                    "Your call is being connected."
                )

            else:

                st.error(
                    f"Call failed: {result}"
                )

        with st.expander(
            "Schedule a call",
            expanded=False
        ):

            if not active_lead:

                st.info(
                    "Start a conversation first."
                )

            else:

                today = datetime.now(
                    IST
                ).date()

                call_date = st.date_input(
                    "Date (IST)",
                    value=today,
                    min_value=today
                )

                call_time = st.time_input(
                    "Time (IST)",
                    value=datetime.now(
                        IST
                    ).replace(
                        second=0,
                        microsecond=0
                    ).time()
                )

                if st.button(
                    "Confirm scheduled call",
                    type="primary",
                    width="stretch"
                ):

                    scheduled_datetime = datetime.combine(
                        call_date,
                        call_time
                    ).replace(
                        tzinfo=IST
                    )

                    if scheduled_datetime <= datetime.now(
                        IST
                    ):

                        st.error(
                            "Please choose a future time."
                        )

                    else:

                        success, result = schedule_demo_call(
                            active_lead["Name"],
                            scheduled_datetime
                        )

                        if success:

                            update_lead_record(
                                "Customer scheduled a call.",
                                "Call Scheduled",
                                "Schedule Call",
                                active_lead["Name"],
                                None,
                                "Call Request",
                                "Positive",
                            )

                            record_action(
                                "Call scheduled.",
                                lead=active_lead,
                                intent="Call Request",
                                sentiment="Positive",
                                priority=""
                            )

                            st.success(
                                "Call scheduled for "
                                f"{scheduled_datetime.strftime('%d %b %Y, %I:%M %p')} IST"
                            )

                        else:

                            st.error(
                                f"Could not schedule call: {result}"
                            )


    # -----------------------------
    # TOP CUSTOMER BAR
    # -----------------------------

    st.markdown(
        f"""
        <div class="welcome-card">
            <div class="assistant-label">
                🤖 AI Sales Assistant
            </div>
            <div>
                Hello <b>{customer_name}</b>! How can I help you today?
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )

    # -----------------------------
    # DISPLAY CHAT HISTORY
    # -----------------------------

    for message in st.session_state["customer_messages"]:

        with st.chat_message(message["role"]):
            st.write(message["content"])

    # -----------------------------
    # CUSTOMER MESSAGE
    # -----------------------------

    customer_message = st.chat_input(
        "Type your message here..."
    )


    if customer_message:

        # FIND ACTIVE LEAD
        # ----------------------------

        lead = st.session_state.get("customer_lead")

        if not lead:
            st.session_state["customer_messages"].append({
                "role": "user",
                "content": customer_message
            })

            st.session_state["customer_messages"].append({
                "role": "assistant",
                "content": (
                    "I couldn't match your details to a customer record. "
                    "Please start the conversation again with your "
                    "registered name."
                )
            })

            st.rerun()

        # -----------------------------------------
        # STORE CUSTOMER MESSAGE
        # -----------------------------------------

        st.session_state["customer_messages"].append({
            "role": "user",
            "content": customer_message
        })

        # -----------------------------------------
        # CALL REQUEST
        # -----------------------------------------

        call_request_phrases = [
            "call me",
            "call me now",
            "give me a call",
            "can you call me",
            "please call me",
            "i want a call",
            "phone me",
            "call my number"
        ]

        is_call_request = any(
            phrase in customer_message.lower()
            for phrase in call_request_phrases
        )

        if is_call_request:

            scheduled_time = parse_call_time(
                customer_message
            )

            if scheduled_time:

                success, result = schedule_demo_call(
                    lead["Name"],
                    scheduled_time
                )

                if success:

                    formatted_time = scheduled_time.strftime(
                        "%I:%M %p"
                    ).lstrip("0")

                    update_lead_record(
                        customer_message,
                        "Call Scheduled",
                        "Schedule Call",
                        lead["Name"],
                        None,
                        "Call Request",
                        "Positive",
                    )

                    record_action(
                        "Call scheduled.",
                        lead=lead,
                        intent="Call Request",
                        sentiment="Positive",
                        priority=""
                    )

                    st.session_state["customer_messages"].append({
                        "role": "assistant",
                        "content": (
                            f"Absolutely, {lead['Name']}. "
                            f"I'll call you at {formatted_time}. "
                            "Please keep your phone available."
                        )
                    })

                else:

                    st.session_state["customer_messages"].append({
                        "role": "assistant",
                        "content": (
                            "I'm sorry, I couldn't schedule the call "
                            "right now. Please try again."
                        )
                    })

            else:

                success, result = initiate_demo_call(
                    lead["Name"]
                )

                if success:

                    update_lead_record(
                        customer_message,
                        "Call Initiated",
                        "Initiate Call",
                        lead["Name"],
                        None,
                        "Call Request",
                        "Positive",
                    )

                    record_action(
                        "Call initiated.",
                        lead=lead,
                        intent="Call Request",
                        sentiment="Positive",
                        priority=""
                    )

                    st.session_state["customer_messages"].append({
                        "role": "assistant",
                        "content": (
                            f"Absolutely, {lead['Name']}. "
                            "I'll call you now. "
                            "Please answer your phone."
                        )
                    })

                else:

                    st.session_state["customer_messages"].append({
                        "role": "assistant",
                        "content": (
                            "I'm sorry, I couldn't initiate the call "
                            "right now. Please try again."
                        )
                    })

            st.rerun()

        # -----------------------------------------
        # BUILD CONVERSATION CONTEXT
        # -----------------------------------------

        conversation_text = ""

        for message in st.session_state["customer_messages"]:

            conversation_text += (
                f"{message['role'].upper()}: "
                f"{message['content']}\n"
            )

        # -----------------------------------------
        # AI SALES ANALYSIS
        # -----------------------------------------

        analysis = analyze_customer_message(
            customer_message,
            conversation_text,
            lead
        )

        intent = analysis["intent"]
        sentiment = analysis["sentiment"]
        priority = analysis["priority"]
        recommended_action = analysis["recommended_action"]

        # -----------------------------------------
        # EXECUTE SALES ACTION + UPDATE CRM
        # -----------------------------------------

        sales_result = execute_and_update_sales_action(
            intent=intent,
            recommended_action=recommended_action,
            customer_message=customer_message,
            selected_lead=lead,
            priority=priority,
            sentiment=sentiment,
        )

        # -----------------------------------------
        # STORE CUSTOMER-FACING RESPONSE
        # -----------------------------------------

        st.session_state["customer_messages"].append({
            "role": "assistant",
            "content": sales_result["customer_message"]
        })

        st.rerun()


    # -----------------------------
    # NEW CONVERSATION
    # -----------------------------

    st.divider()

    col1, col2 = st.columns(2)

    with col1:

        if st.button(
            "🔄 New Conversation",
            width="stretch"
        ):

            st.session_state["customer_name"] = ""
            st.session_state["customer_messages"] = []
            st.session_state["customer_lead"] = None

            st.rerun()


    # ============================================================
    # SALES DASHBOARD
    # ============================================================

if developer_mode and st.session_state.get("developer_authenticated", False):

    st.divider()

    st.markdown(
        '<div class="developer-console-header"><h1>Sales workspace</h1>'
        '<p>Lead management, customer activity and sales insights</p></div>',
        unsafe_allow_html=True,
    )

    # -----------------------------------------
    # LOAD CURRENT CRM DATA
    # -----------------------------------------

    try:
        dashboard_leads = pd.read_csv(crm_path("leads.csv"))
        if "Last Sentiment" not in dashboard_leads.columns:
            dashboard_leads["Last Sentiment"] = ""
    except Exception:
        dashboard_leads = pd.DataFrame()

    # -----------------------------------------
    # LOAD SALES ACTIVITY
    # -----------------------------------------

    if os.path.exists(crm_path("sales_activity.csv")):

        try:
            dashboard_activity = pd.read_csv(
                crm_path("sales_activity.csv")
            )
        except Exception:
            dashboard_activity = pd.DataFrame()

    else:
        dashboard_activity = pd.DataFrame()

    with st.expander("Restore local CRM data", expanded=False):
        st.caption(
            "Import CSVs from your previous workspace. Existing lead values are kept; "
            "missing values and new leads are added. Activity rows are appended without duplicates."
        )
        leads_upload = st.file_uploader(
            "Leads CSV", type=["csv"], key="crm_leads_restore"
        )
        activity_upload = st.file_uploader(
            "Sales activity CSV", type=["csv"], key="crm_activity_restore"
        )
        if st.button("Import selected files", key="crm_restore_button"):
            imported_counts = []
            try:
                if leads_upload is not None:
                    imported_counts.append(
                        f"{merge_crm_upload(leads_upload, 'leads.csv')} new lead(s)"
                    )
                if activity_upload is not None:
                    imported_counts.append(
                        f"{merge_crm_upload(activity_upload, 'sales_activity.csv')} new activity row(s)"
                    )
                if imported_counts:
                    st.success("Imported " + " and ".join(imported_counts) + ".")
                    st.rerun()
                st.info("Choose at least one CSV file to import.")
            except Exception as exc:
                st.error(f"Import failed: {exc}")

    if not os.getenv("CRM_DATA_DIR"):
        st.warning(
            "CRM files are stored on this service's local filesystem. Configure "
            "CRM_DATA_DIR to point to persistent storage before relying on redeploy-safe data."
        )

    # -----------------------------------------
    # KPI METRICS
    # -----------------------------------------

    total_leads = len(dashboard_leads)

    if not dashboard_leads.empty and "Status" in dashboard_leads.columns:

        status_series = (
            dashboard_leads["Status"]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.lower()
        )

        active_leads = (
            total_leads
            - status_series.isin(
                ["closed", "not interested"]
            ).sum()
        )

        closed_leads = status_series.eq("closed").sum()

    else:

        active_leads = total_leads
        closed_leads = 0

    total_activities = len(dashboard_activity)

    col1, col2, col3, col4 = st.columns(4)

    col1.metric(
        "Total Leads",
        total_leads
    )

    col2.metric(
        "Active Leads",
        active_leads
    )

    col3.metric(
        "Closed Leads",
        closed_leads
    )

    col4.metric(
        "Sales Activities",
        total_activities
    )

    # -----------------------------------------
    # SCHEDULED CALLS
    # -----------------------------------------

    st.subheader("📅 Scheduled Calls")

    if scheduled_calls:

        scheduled_call_rows = []

        for lead_name, call_data in scheduled_calls.items():

            scheduled_call_rows.append({
                "Lead": lead_name,
                "Scheduled Time": call_data.get(
                    "scheduled_time",
                    ""
                ).strftime("%d %B %Y at %I:%M %p")
                if call_data.get("scheduled_time")
                else "",
                "Status": call_data.get(
                    "status",
                    ""
                ),
                "Result": call_data.get(
                    "result",
                    ""
                )
            })

        scheduled_calls_df = pd.DataFrame(
            scheduled_call_rows
        )

        st.dataframe(
            scheduled_calls_df,
            hide_index=True,
            width="stretch"
        )

    else:

        st.info("No calls are currently scheduled.")

    st.divider()

    st.subheader("👥 Lead Database")

    # -----------------------------------------
    # LEAD SEARCH & FILTERS
    # -----------------------------------------
    
    def clear_dashboard_filters():
        st.session_state["dashboard_search"] = ""
        st.session_state["dashboard_status_filter"] = "All"
        st.session_state["dashboard_priority_filter"] = "All"
        st.session_state["dashboard_intent_filter"] = "All"
        st.session_state["dashboard_sentiment_filter"] = "All"
        st.session_state["dashboard_date_filter"] = ()

    st.button(
        "🧹 Clear Filters",
        on_click=clear_dashboard_filters
    )
    
    filter_col1, filter_col2, filter_col3 = st.columns(3)

    with filter_col1:

        search_leads = st.text_input(
            "🔎 Search Leads",
            placeholder="Name, company or email...",
            key="dashboard_search"
        )
    with filter_col2:

        if "Status" in dashboard_leads.columns:

            status_options = ["All"] + sorted(
                dashboard_leads["Status"]
                .fillna("")
                .astype(str)
                .str.strip()
                .unique()
                .tolist()
            )

        else:

            status_options = ["All"]

        selected_status = st.selectbox(
            "📌 Status",
            status_options,
            key="dashboard_status_filter"
        )

    with filter_col3:

        if "Priority" in dashboard_leads.columns:

            priority_options = ["All"] + sorted(
                dashboard_leads["Priority"]
                .fillna("")
                .astype(str)
                .str.strip()
                .unique()
                .tolist()
            )

        else:

            priority_options = ["All"]

        selected_priority = st.selectbox(
            "⭐ Priority",
            priority_options,
            key="dashboard_priority_filter"
        )


    # -----------------------------------------
    # INTENT FILTER
    # -----------------------------------------

    if "Last Intent" in dashboard_leads.columns:

        intent_options = ["All"] + sorted(
            dashboard_leads["Last Intent"]
            .fillna("")
            .astype(str)
            .str.strip()
            .unique()
            .tolist()
        )

    else:

        intent_options = ["All"]

    selected_intent = st.selectbox(
        "🧠 Intent",
        intent_options,
        key="dashboard_intent_filter"
    )

    selected_sentiment = st.selectbox(
        "😊 Sentiment",
        ["All"] + sorted(
            [
                str(x).strip()
                for x in dashboard_leads["Last Sentiment"].dropna().unique()
                if str(x).strip()
            ]
        ),
        key="dashboard_sentiment_filter"
    )

    selected_date_range = st.date_input(
        "📅 Last Interaction",
        value=(),
        key="dashboard_date_filter"
    )
    # -----------------------------------------
    # APPLY FILTERS
    # -----------------------------------------

    filtered_leads = dashboard_leads.copy()


    # SEARCH
    if search_leads.strip():

        search_text = search_leads.strip().lower()

        searchable_columns = [
            column
            for column in ["Name", "Company", "Email"]
            if column in filtered_leads.columns
        ]

        if searchable_columns:

            search_mask = pd.Series(
                False,
                index=filtered_leads.index
            )

            for column in searchable_columns:

                search_mask = (
                    search_mask
                    | filtered_leads[column]
                    .fillna("")
                    .astype(str)
                    .str.lower()
                    .str.contains(
                        search_text,
                        regex=False
                    )
                )

            filtered_leads = filtered_leads[search_mask]


    # STATUS
    if (
        selected_status != "All"
        and "Status" in filtered_leads.columns
    ):

        filtered_leads = filtered_leads[
            filtered_leads["Status"]
            .fillna("")
            .astype(str)
            .str.strip()
            == selected_status
        ]


    # PRIORITY
    if (
        selected_priority != "All"
        and "Priority" in filtered_leads.columns
    ):

        filtered_leads = filtered_leads[
            filtered_leads["Priority"]
            .fillna("")
            .astype(str)
            .str.strip()
            == selected_priority
        ]


    # INTENT
    if (
        selected_intent != "All"
        and "Last Intent" in filtered_leads.columns
    ):

        filtered_leads = filtered_leads[
            filtered_leads["Last Intent"]
            .fillna("")
            .astype(str)
            .str.strip()
            == selected_intent
        ]
    # SENTIMENT
    if (
        selected_sentiment != "All"
        and "Last Sentiment" in filtered_leads.columns
    ):
        filtered_leads = filtered_leads[
            filtered_leads["Last Sentiment"]
            .fillna("")
            .astype(str)
            .str.strip()
            == selected_sentiment
        ]

    # LAST INTERACTION DATE
    if len(selected_date_range) == 2:
        start_date, end_date = selected_date_range

        interaction_dates = pd.to_datetime(
            filtered_leads["Last Interaction"],
            errors="coerce"
        ).dt.date

        filtered_leads = filtered_leads[
            interaction_dates.between(start_date, end_date)
        ]

    # -----------------------------------------
    # RESULT COUNT
    # -----------------------------------------

    st.caption(
        f"Showing {len(filtered_leads)} of "
        f"{len(dashboard_leads)} leads"
    )

    csv_data = filtered_leads.to_csv(index=False).encode("utf-8")

    st.download_button(
        "⬇️ Export Filtered Leads",
        data=csv_data,
        file_name="filtered_leads.csv",
        mime="text/csv",
        width="stretch"
    )

    # -----------------------------------------
    # DISPLAY FILTERED LEADS
    # -----------------------------------------

    if filtered_leads.empty:

        st.info(
            "No leads match the selected filters."
        )

    else:

        # -----------------------------------------
        # SELECT LEAD
        # -----------------------------------------

        lead_table = st.dataframe(
            filtered_leads,
            hide_index=True,
            width="stretch",
            on_select="rerun",
            selection_mode="single-row",
            key="lead_selector"
        )


        # -----------------------------------------
        # SELECTED LEAD DETAILS
        # -----------------------------------------

        selected_rows = lead_table.selection.rows

        if selected_rows:

            selected_index = selected_rows[0]

            selected_lead = filtered_leads.iloc[
                selected_index
            ]

            selected_lead_name = str(
                selected_lead.get("Name", "")
            )

            st.divider()

            st.subheader(
                f"👤 Lead Profile — {selected_lead_name}"
            )

            profile_col1, profile_col2 = st.columns(2)

            with profile_col1:

                st.write(
                    f"**Company:** "
                    f"{selected_lead.get('Company', '')}"
                )

                st.write(
                    f"**Industry:** "
                    f"{selected_lead.get('Industry', '')}"
                )

                st.write(
                    f"**Job Role:** "
                    f"{selected_lead.get('Job Role', '')}"
                )

                st.write(
                    f"**Email:** "
                    f"{selected_lead.get('Email', '')}"
                )

            with profile_col2:

                st.write(
                    f"**Product:** "
                    f"{selected_lead.get('Product', '')}"
                )

                st.write(
                    f"**Status:** "
                    f"{selected_lead.get('Status', '')}"
                )

                st.write(
                    f"**Priority:** "
                    f"{selected_lead.get('Priority', '')}"
                )

                st.write(
                    f"**Last Intent:** "
                    f"{selected_lead.get('Last Intent', '')}"
                )
            lead_notes = str(
                selected_lead.get("Notes", "")
            ).strip()

            st.markdown("### 📝 Notes")

            if lead_notes:
                st.info(lead_notes)
            else:
                st.caption("No notes added for this lead.")
            # -----------------------------------------
            # LEAD ACTIVITY HISTORY
            # -----------------------------------------
            # ============================================================
            # CRM ACTIONS
            # ============================================================

            st.subheader("⚡ Lead Actions")

            action_col1, action_col2, action_col3 = st.columns(3)

            with action_col1:
                if st.button(
                    "📧 Send Email",
                    width="stretch",
                    key="crm_send_email"
                ):
                    st.session_state["crm_action"] = "email"
                    st.rerun()

            with action_col2:
                if st.button(
                    "📞 Call Lead",
                    width="stretch",
                    key="crm_call_lead"
                ):
                    st.session_state["crm_action"] = "call"
                    st.rerun()

            with action_col3:
                if st.button(
                    "📅 Schedule Follow-up",
                    width="stretch",
                    key="crm_schedule_followup"
                ):
                    st.session_state["crm_action"] = "followup"
                    st.rerun()

            # -----------------------------
            # SELECTED CRM ACTION
            # -----------------------------

            crm_action = st.session_state.get("crm_action")

            if crm_action == "email":

                st.markdown("### 📧 Send Email")

                lead_email = str(selected_lead.get("Email", "")).strip()
                lead_company = str(selected_lead.get("Company", "")).strip()
                lead_product = str(selected_lead.get("Product", "")).strip()

                if not lead_email:
                    st.error("No email address is available for this lead.")

                else:

                    st.caption(f"Recipient: {lead_email}")

                    with st.form("crm_email_form"):

                        email_subject = st.text_input(
                            "Subject",
                            value=f"Regarding {lead_product}"
                        )

                        email_body = st.text_area(
                            "Email Message",
                            value=(
                                f"Hello {selected_lead_name},\n\n"
                                f"I am reaching out regarding {lead_product}"
                                f"{' at ' + lead_company if lead_company else ''}.\n\n"
                                "Please let me know if you would like to discuss this further."
                            ),
                            height=180
                        )

                        send_email_button = st.form_submit_button(
                            "📤 Send Email",
                            type="primary",
                            width="stretch"
                        )

                    if send_email_button:

                        if not email_subject.strip() or not email_body.strip():

                            st.warning(
                                "Please enter both a subject and email message."
                            )

                        else:

                            email_result = send_email(
                                lead_email,
                                email_subject.strip(),
                                email_body.strip()
                            )

                            if email_result == "Email sent successfully.":

                                record_action(
                                    "Email sent.",
                                    lead=selected_lead,
                                    intent="CRM Email",
                                    sentiment=selected_lead.get(
                                        "Last Sentiment", ""
                                    ),
                                    priority=selected_lead.get(
                                        "Priority", ""
                                    )
                                )

                                update_lead_record(
                                    email_body,
                                    "Email Sent",
                                    "Send Email",
                                    selected_lead_name,
                                    selected_lead.get("Priority", ""),
                                    "CRM Email",
                                    selected_lead.get("Last Sentiment", "")
                                )

                                st.success(
                                    f"✅ Email sent successfully to {lead_email}"
                                )

                                st.session_state["crm_action"] = None

                            else:

                                st.error(email_result)

            elif crm_action == "call":

                st.markdown("### 📞 Call Lead")

                st.warning(
                    f"You are about to initiate a call for **{selected_lead_name}**."
                )

                call_col1, call_col2 = st.columns(2)

                with call_col1:
                    initiate_call_button = st.button(
                        "📞 Initiate Call Now",
                        type="primary",
                        width="stretch",
                        key="crm_initiate_call"
                    )

                with call_col2:
                    cancel_call_button = st.button(
                        "❌ Cancel",
                        width="stretch",
                        key="crm_cancel_call"
                    )

                if cancel_call_button:

                    st.session_state["crm_action"] = None
                    st.rerun()

                if initiate_call_button:

                    success, result = initiate_demo_call(
                        selected_lead_name
                    )

                    if success:

                        record_action(
                            "Call initiated.",
                            lead=selected_lead,
                            intent="CRM Call",
                            sentiment=selected_lead.get(
                                "Last Sentiment",
                                ""
                            ),
                            priority=selected_lead.get(
                                "Priority",
                                ""
                            )
                        )
                        
                        update_lead_record(
                            "CRM initiated call",
                            "Call Initiated",
                            "Initiate Call",
                            selected_lead_name,
                            selected_lead.get("Priority", ""),
                            "CRM Call",
                            selected_lead.get("Last Sentiment", "")
                        )

                        st.success(
                            f"📞 Call initiated successfully for {selected_lead_name}."
                        )

                        st.session_state["crm_action"] = None

                    else:

                        st.error(
                            f"❌ Call could not be initiated: {result}"
                        )

            elif crm_action == "followup":

                st.markdown("### 📅 Schedule Follow-up")

                st.info(
                    f"Schedule a follow-up call for **{selected_lead_name}**."
                )

                with st.form("crm_followup_form"):

                    followup_date = st.date_input(
                        "📅 Follow-up Date"
                    )

                    followup_time = st.time_input(
                        "⏰ Follow-up Time"
                    )

                    schedule_button = st.form_submit_button(
                        "📅 Schedule Follow-up Call",
                        type="primary",
                        width="stretch"
                    )

                if schedule_button:

                    scheduled_datetime = datetime.combine(
                        followup_date,
                        followup_time
                    )

                    if scheduled_datetime <= datetime.now():

                        st.error(
                            "Please select a future date and time."
                        )

                    else:

                        success, result = schedule_demo_call(
                            selected_lead_name,
                            scheduled_datetime
                        )

                        if success:

                            record_action(
                                "Call scheduled.",
                                lead=selected_lead,
                                intent="CRM Follow-up",
                                sentiment=selected_lead.get(
                                    "Last Sentiment",
                                    ""
                                ),
                                priority=selected_lead.get(
                                    "Priority",
                                    ""
                                )
                            )
                            # -----------------------------------------
                            # SEND CALL CONFIRMATION EMAIL
                            # -----------------------------------------

                            if selected_lead.get("Email"):

                                confirmation_subject = (
                                    "Call Confirmation - IITG AI Sales Agent"
                                )

                                confirmation_body = (
                                    f"Hello {selected_lead_name},\n\n"
                                    f"This is a confirmation that your call has been "
                                    f"scheduled for "
                                    f"{scheduled_datetime.strftime('%d %B %Y at %I:%M %p')}.\n\n"
                                    "Please keep your phone available around the "
                                    "scheduled time.\n\n"
                                    "We look forward to speaking with you.\n\n"
                                    "Regards,\n"
                                    "IITG AI Sales Agent"
                                )

                                confirmation_email_status = send_email(
                                    selected_lead["Email"],
                                    confirmation_subject,
                                    confirmation_body
                                )

                            else:

                                confirmation_email_status = (
                                    "Lead email not found."
                                )

                            if confirmation_email_status == "Email sent successfully.":

                                st.success(
                                    "📧 Call confirmation email sent successfully."
                                )

                            else:

                                st.warning(
                                    f"📧 Confirmation email status: "
                                    f"{confirmation_email_status}"
                                )

                            update_lead_record(
                                "CRM follow-up scheduled",
                                "Call Scheduled",
                                "Schedule Call",
                                selected_lead_name,
                                selected_lead.get(
                                    "Priority",
                                    ""
                                ),
                                "CRM Follow-up",
                                selected_lead.get(
                                    "Last Sentiment",
                                    ""
                                )
                            )

                            formatted_datetime = (
                                scheduled_datetime.strftime(
                                    "%d %B %Y at %I:%M %p"
                                ).lstrip("0")
                            )

                            st.success(
                                f"✅ Follow-up call scheduled for "
                                f"{formatted_datetime}."
                            )

                            st.session_state["crm_action"] = None

                        else:

                            st.error(
                                f"❌ Could not schedule the call: {result}"
                            )
            # ============================================================
            # MANUAL LEAD MANAGEMENT
            # ============================================================

            st.subheader("✏️ Manage Lead")

            status_options = [
                "New",
                "Interested",
                "Pricing Requested",
                "Information Requested",
                "Demo Requested",
                "Follow-up Required",
                "Ready to Buy",
                "Call Initiated",
                "Call Scheduled",
                "Email Sent",
                "Escalated",
                "Not Interested",
                "Closed",
            ]

            current_status = str(
                selected_lead.get("Status", "")
            ).strip()

            if current_status and current_status not in status_options:
                status_options.append(current_status)

            priority_options = [
                "High",
                "Medium",
                "Low",
            ]

            current_priority = str(
                selected_lead.get("Priority", "Medium")
            ).strip()

            if current_priority not in priority_options:
                current_priority = "Medium"

            manage_col1, manage_col2 = st.columns(2)

            with manage_col1:

                manual_status = st.selectbox(
                    "📌 Lead Status",
                    status_options,
                    index=status_options.index(current_status)
                    if current_status in status_options
                    else 0,
                    key="manual_lead_status"
                )

            with manage_col2:

                manual_priority = st.selectbox(
                    "🎯 Lead Priority",
                    priority_options,
                    index=priority_options.index(current_priority),
                    key="manual_lead_priority"
                )

            current_notes = str(
                selected_lead.get("Notes", "")
            )

            manual_notes = st.text_area(
                "📝 Lead Notes",
                value=current_notes,
                height=120,
                placeholder="Add notes about this lead...",
                key=f"manual_lead_notes_{selected_lead_name}"
            )

            if st.button(
                "💾 Save Lead Changes",
                type="primary",
                width="stretch",
                key="save_lead_changes"
            ):

                update_success = update_lead_record(
                    "Manual CRM update",
                    manual_status,
                    "Manual Lead Update",
                    selected_lead_name,
                    manual_priority,
                    selected_lead.get(
                        "Last Intent",
                        ""
                    ),
                    selected_lead.get(
                        "Last Sentiment",
                        ""
                    ),
                    manual_notes
                )

                if update_success:

                    record_action(
                        "Lead updated manually.",
                        lead=selected_lead,
                        intent="CRM Update",
                        sentiment=selected_lead.get(
                            "Last Sentiment",
                            ""
                        ),
                        priority=manual_priority
                    )

                    st.success(
                        f"✅ {selected_lead_name}'s lead record was updated."
                    )

                    st.rerun()

                else:

                    st.error(
                        "❌ Could not update the lead record."
                    )
            st.subheader(
                f"📜 Activity History — {selected_lead_name}"
            )

            if not dashboard_activity.empty:

                lead_activity = dashboard_activity[
                    dashboard_activity["lead"]
                    .fillna("")
                    .astype(str)
                    .str.strip()
                    .str.lower()
                    == selected_lead_name.strip().lower()
                ].copy()

                if not lead_activity.empty:

                    st.dataframe(
                        lead_activity.sort_values(
                            "timestamp",
                            ascending=False
                        ),
                        hide_index=True,
                        width="stretch"
                    )

                else:

                    st.info(
                        "No sales activity recorded for this lead yet."
                    )

            else:

                st.info(
                    "No sales activity recorded yet."
                )
            

        st.divider()

    # -----------------------------------------
    # SALES ACTIVITY HISTORY
    # -----------------------------------------

    st.subheader("📈 Sales Activity History")

    if dashboard_activity.empty:

        st.info("No sales activity recorded yet.")

    else:

        st.dataframe(
            dashboard_activity.sort_values(
                "timestamp",
                ascending=False
            ),
            hide_index=True,
            width="stretch"
        )

        # -----------------------------------------
        # SALES ANALYTICS
        # -----------------------------------------

        st.divider()

        st.subheader("📊 Sales Analytics")

        if dashboard_activity.empty:

            st.info(
                "Analytics will appear after sales activities are recorded."
            )

        else:

            # -----------------------------------------
            # ACTIVITY COUNTS
            # -----------------------------------------

            activity_actions = (
                dashboard_activity["action"]
                .fillna("")
                .astype(str)
                .str.strip()
                .str.lower()
            )

            calls_initiated = activity_actions.str.contains(
                "call initiated",
                regex=False
            ).sum()

            calls_scheduled = activity_actions.str.contains(
                "call scheduled",
                regex=False
            ).sum()

            total_sales_actions = len(
                dashboard_activity
            )

            analytics_col1, analytics_col2, analytics_col3 = st.columns(3)

            analytics_col1.metric(
                "📞 Calls Initiated",
                int(calls_initiated)
            )

            analytics_col2.metric(
                "📅 Calls Scheduled",
                int(calls_scheduled)
            )

            analytics_col3.metric(
                "⚡ Total Activities",
                int(total_sales_actions)
            )

            st.divider()

            # -----------------------------------------
            # INTENT ANALYSIS
            # -----------------------------------------

            chart_col1, chart_col2 = st.columns(2)

            with chart_col1:

                st.markdown("### 🧠 Customer Intent")

                if "intent" in dashboard_activity.columns:

                    intent_counts = (
                        dashboard_activity["intent"]
                        .fillna("Unknown")
                        .astype(str)
                        .value_counts()
                    )

                    st.bar_chart(
                        intent_counts
                    )

            # -----------------------------------------
            # PRIORITY ANALYSIS
            # -----------------------------------------

            with chart_col2:

                st.markdown("### 🎯 Lead Priority")

                if "priority" in dashboard_activity.columns:

                    priority_counts = (
                        dashboard_activity["priority"]
                        .fillna("Unknown")
                        .astype(str)
                        .value_counts()
                    )

                    st.bar_chart(
                        priority_counts
                    )

            st.divider()

            # -----------------------------------------
            # SENTIMENT ANALYSIS
            # -----------------------------------------

            st.markdown("### 😊 Customer Sentiment")

            if "sentiment" in dashboard_activity.columns:

                sentiment_counts = (
                    dashboard_activity["sentiment"]
                    .fillna("Unknown")
                    .astype(str)
                    .value_counts()
                )

                st.bar_chart(
                    sentiment_counts
                )

    st.divider()

    # -----------------------------------------
    # CLOSE DASHBOARD
    # -----------------------------------------

    if st.button(
        "⬅️ Back to Conversation",
        width="stretch"
    ):

        st.session_state["show_dashboard"] = False
        st.rerun()
