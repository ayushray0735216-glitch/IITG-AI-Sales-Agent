import os
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
IST = ZoneInfo("Asia/Kolkata")
from flask import Flask, request
from twilio.rest import Client as TwilioClient
import threading
import time
import re
import smtplib
from email.message import EmailMessage
from twilio.twiml.voice_response import VoiceResponse, Gather
from dotenv import load_dotenv
from google import genai

load_dotenv()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)

app = Flask(__name__)

conversation_history = {}
call_leads = {}
call_numbers = {}
def load_leads():
    """Load the lead database."""
    return pd.read_csv("leads.csv")


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

            leads.to_csv("leads.csv", index=False)

            print(
                f"Lead updated: {lead['Name']} | "
                f"Status: {status} | "
                f"Action: {last_action}"
            )

            return True

    print(f"Lead not found: {lead_name}")
    return False

def load_leads():
    """Load the lead database."""
    return pd.read_csv("leads.csv")


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

            leads.to_csv("leads.csv", index=False)

            print(
                f"Lead updated: {lead['Name']} | "
                f"Status: {status} | "
                f"Action: {last_action}"
            )

            return True

    print(f"Lead not found: {lead_name}")
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

            sender_email = os.getenv("EMAIL_ADDRESS")
            sender_password = os.getenv("EMAIL_PASSWORD")

            if not sender_email or not sender_password:
                return "Email credentials not configured."

            msg = EmailMessage()

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

                    smtp.send_message(msg)

                return "Email sent successfully."

            except Exception as e:

                return f"Email failed: {e}"

    return "Lead not found."


def initiate_scheduled_voice_call(lead_name, to_number):

    try:

        twilio_client = TwilioClient(
            os.getenv("TWILIO_ACCOUNT_SID"),
            os.getenv("TWILIO_AUTH_TOKEN")
        )

        from_number = os.getenv("TWILIO_PHONE_NUMBER")

        if not to_number or not from_number:
            return False, "Phone number is not configured."

        if not to_number or not from_number:
            return False, "Phone number is not configured."

        call = twilio_client.calls.create(
            to=to_number,
            from_=from_number,
            url=(
                "https://iitg-ai-sales-agent.onrender.com"
                f"/voice?lead={lead_name}"
            )
        )

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
    
def get_ai_response(customer_message, history=None):
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

    if lead_name:
        call_leads[call_sid] = lead_name

    if caller_number:
        call_numbers[call_sid] = caller_number
    gather = Gather(
        input="speech",
        action="https://iitg-ai-sales-agent.onrender.com/process",
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

    response = VoiceResponse()

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
            history
        )

        print(f"AI [{call_sid}]: {ai_reply}")

        # Step 4: Store conversation memory
        history.append({
            "customer": speech,
            "assistant": ai_reply,
            "intent": intent,
            "sales_action": sales_action
        })

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
            action="https://iitg-ai-sales-agent.onrender.com/process",
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