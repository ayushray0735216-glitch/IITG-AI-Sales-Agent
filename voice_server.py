import os
import pandas as pd
from datetime import datetime
from flask import Flask, request
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

    if lead_name:
        call_leads[call_sid] = lead_name
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
        gather = Gather(
            input="speech",
            action="https://iitg-ai-sales-agent.onrender.com/process",
            method="POST",
            speech_timeout="auto",
            language="en-IN"
        )

        gather.say(
            "How else can I help you?"
        )

        response.append(gather)

    else:
        response.say(
            "I didn't hear a response. Thank you for calling. Goodbye."
        )
        response.hangup()

    return str(response)


if __name__ == "__main__":
    app.run(port=5000, debug=True)