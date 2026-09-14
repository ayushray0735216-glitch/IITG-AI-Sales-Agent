import os
import streamlit as st
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from google import genai
import smtplib
from email.message import EmailMessage

if "leads" not in st.session_state:
    st.session_state["leads"] = pd.read_csv("leads.csv")

if "using_uploaded_leads" not in st.session_state:
    st.session_state["using_uploaded_leads"] = False

if "uploaded_file_name" not in st.session_state:
    st.session_state["uploaded_file_name"] = None

load_dotenv()

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)
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
    
def record_action(action):
    if "action_log" not in st.session_state:
        st.session_state["action_log"] = []

    st.session_state["action_log"].append(action)

def update_lead_record(customer_message, status, last_action, lead_name=None, priority=None):
    """Update the active lead database and persist default CSV changes."""

    leads = load_leads().copy()

    leads["Status"] = leads["Status"].astype("object")
    leads["Last Action"] = leads["Last Action"].astype("object")
    leads["Last Interaction"] = leads["Last Interaction"].astype("object")

    if lead_name:
        for index, lead in leads.iterrows():
            if str(lead["Name"]).strip().lower() == str(lead_name).strip().lower():
                leads.at[index, "Status"] = status
                leads.at[index, "Last Action"] = last_action
                leads.at[index, "Priority"] = priority
                leads.at[index, "Last Interaction"] = datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                # Persist changes to the built-in database.
                # Uploaded databases are kept in session state only.
                if not st.session_state.get("using_uploaded_leads", False):
                    leads.to_csv("leads.csv", index=False)

                st.session_state["leads"] = leads.copy()
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
    """Load the current active lead database."""
    if "leads" not in st.session_state:
        st.session_state["leads"] = pd.read_csv("leads.csv")

    if "Priority" not in st.session_state["leads"].columns:
        st.session_state["leads"]["Priority"] = ""

    return st.session_state["leads"]


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

def execute_and_update_sales_action(intent, recommended_action, customer_message, selected_lead=None, priority=None):
    """Execute the sales action and update the matched lead."""

    action_result = execute_sales_action(
        intent,
        recommended_action,
        customer_message
    )

    if recommended_action == "Close Lead":
        customer_message_generated = "Understood. We won’t follow up further regarding this request. Wishing you all the best."
    else:
        customer_message_generated = generate_customer_message(
            customer_message,
            action_result
        )
    status_map = {
        "Schedule Demo": "Demo Requested",
        "Send Pricing": "Pricing Requested",
        "Send Information": "Information Requested",
        "Schedule Follow-up": "Follow-up Required",
        "Escalate to Human": "Escalated",
        "Close Lead": "Closed"
    }

    status = status_map.get(
        recommended_action,
        "No Change"
    )

    log_messages = {
        "Schedule Demo": "Demo scheduled/follow-up prepared.",
        "Send Pricing": "Pricing information prepared.",
        "Send Information": "Product information prepared.",
        "Schedule Follow-up": "Follow-up prepared.",
        "Escalate to Human": "Lead escalated to human representative.",
        "Close Lead": "Lead closed.",
        "No Action": "No sales action required."
    }

    record_action(
        log_messages.get(
            recommended_action,
            "Sales action completed."
        )
    )

    lead = selected_lead

    if recommended_action == "No Action":
        lead_updated = False
    else:
        lead_updated = update_lead_record(
        customer_message,
        status,
        recommended_action,
        lead.get("Name") if lead else None,
        priority
    )

    email_actions = {
        "Send Pricing",
        "Schedule Demo",
        "Send Information",
        "Schedule Follow-up",
        "Escalate to Human",
    }

    if recommended_action in email_actions and lead and lead.get("Email"):

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

    action_messages = {
    "Schedule Demo": "A demo follow-up has been prepared and the lead has been updated.",
    "Send Pricing": "Pricing information has been prepared and the lead has been updated.",
    "Send Information": "Product information has been prepared and the lead has been updated.",
    "Schedule Follow-up": "A follow-up has been prepared and the lead has been updated.",
    "Escalate to Human": "The request has been flagged for human follow-up and the lead has been updated.",
    "Close Lead": "The lead has been closed and no follow-up email was sent."
    }

    if lead_updated:
        action_message = action_messages.get(
            recommended_action,
            "The sales action has been completed and the lead has been updated."
        )
    else:
        if recommended_action == "No Action":
            action_message = "No sales action was required. The lead was not changed."
        else:
            action_message = (
                f"The action '{recommended_action}' was completed, "
                "but no matching lead was found in the database."
            )

    return action_message
SENDER_NAME = "Ayush Roy"
SENDER_TITLE = "AI Sales Assistant"
SENDER_COMPANY = "IITG AI Sales Agent"

# Page configuration
st.set_page_config(
    page_title="AI Sales Agent",
    page_icon="🤖",
    layout="wide"
)

# Main title
st.title("🤖 IITG AI Sales Agent")
st.caption(
    "AI-powered lead analysis, customer engagement, and automated sales workflow."
)

st.divider()

# Dashboard metrics

dashboard_leads = load_leads()

active_statuses = {
    "follow-up required",
    "pricing requested",
    "demo requested",
    "information requested",
    "escalated"
}

interested_statuses = {
    "follow-up required",
    "pricing requested",
    "demo requested",
    "information requested"
}

high_priority_count = sum(
    1
    for lead in dashboard_leads.to_dict("records")
    if str(lead.get("Priority", "")).strip().lower() == "high"
)

interested_count = sum(
    1
    for lead in dashboard_leads.to_dict("records")
    if str(lead.get("Status", "")).strip().lower() in interested_statuses
)

demo_count = sum(
    1
    for lead in dashboard_leads.to_dict("records")
    if str(lead.get("Status", "")).strip().lower() == "demo requested"
)

col1, col2, col3, col4 = st.columns(4)

with col1:
    st.metric(
        "Total Leads",
        len(dashboard_leads)
    )

with col2:
    st.metric(
        "High Priority Leads",
        high_priority_count
    )

with col3:
    st.metric(
        "Interested Leads",
        interested_count
    )

with col4:
    st.metric(
        "Demo Requests",
        demo_count
    )

st.divider()

# Main sections
st.header("📊 Lead Management")

st.caption(
    "Manage your customer database and select a lead for AI-powered sales analysis."
)

uploaded_file = st.file_uploader(
    "Upload Lead Database (Optional)",
    type=["csv", "xlsx"]
)

required_columns = {
    "Name",
    "Company",
    "Industry",
    "Job Role",
    "Email",
    "Product",
    "Status",
    "Last Action",
    "Last Interaction",
}

if uploaded_file is not None:
    if st.session_state.get("uploaded_file_name") != uploaded_file.name:
        if uploaded_file.name.lower().endswith(".csv"):
            uploaded_leads = pd.read_csv(uploaded_file)
        else:
            uploaded_leads = pd.read_excel(uploaded_file)

        missing_columns = required_columns - set(uploaded_leads.columns)

        if missing_columns:
            st.error(
                "The uploaded database is missing required columns: "
                + ", ".join(sorted(missing_columns))
            )
        else:
            st.session_state["leads"] = uploaded_leads.copy()
            st.session_state["using_uploaded_leads"] = True
            st.session_state["uploaded_file_name"] = uploaded_file.name
            st.success("Lead database uploaded successfully!")

current_leads = load_leads()

st.subheader("📋 Lead Database")

st.dataframe(
    current_leads,
    width="stretch"
)

st.caption(f"Total Leads: {len(current_leads)}")

st.divider()

st.subheader("👤 Select Lead")

lead_names = current_leads["Name"].tolist()

if not lead_names:
    st.warning("No leads are available in the current database.")
    st.stop()

selected_lead_name = st.selectbox(
    "Select a lead to analyze:",
    lead_names
)

selected_lead = current_leads[
    current_leads["Name"] == selected_lead_name
].iloc[0]

st.session_state["selected_lead"] = selected_lead.to_dict()

st.info(
    f"🏢 {selected_lead['Company']}  |  "
    f"💼 {selected_lead['Job Role']}  |  "
    f"📧 {selected_lead['Email']}"
)
st.header("📧 AI Outreach")

st.write(
    "The AI will generate personalized messages for potential customers."
)

leads = st.session_state["leads"]
if st.button("Generate AI Outreach"):
    st.subheader("🤖 AI-Generated Outreach")

    for index, lead in leads.iterrows():
        prompt = f"""
You are a professional B2B sales expert.

Analyze this sales lead:

Name: {lead['Name']}
Company: {lead['Company']}
Industry: {lead['Industry']}
Job Role: {lead['Job Role']}
Product: {lead['Product']}

Create a concise personalized first-contact sales message.
Mention the person's company and role naturally.
Keep it professional, helpful, and not overly promotional.
End with a simple call to action.

Do not include a signature or sender details. The application will add the signature automatically.
"""

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
                contents=prompt
            )

        st.markdown(
                f"### Lead {index + 1}: {lead['Name']} — {lead['Company']}"
            )
        st.write(response.text)
        st.markdown(
    f"Best regards,\n\n"
    f"**{SENDER_NAME}**  \n"
    f"{SENDER_TITLE}  \n"
    f"{SENDER_COMPANY}"
)
        st.divider()

st.header("💬 Customer Conversation")

st.write(
    "The AI will analyze customer responses, identify customer intent, "
    "generate an appropriate reply, and recommend the next sales action."
)

customer_message = st.text_area(
    "Customer Response",
    placeholder="Paste the customer's response here..."
)

if st.button("Analyze Customer Response"):

    if customer_message.strip():

        st.session_state["current_lead"] = st.session_state.get("selected_lead")

        prompt = f"""
You are an AI Sales Agent.

Analyze the following customer response.

Customer response:
{customer_message}

IMPORTANT CLASSIFICATION RULE:
If the customer message is only a casual greeting, acknowledgement,
small talk, neutral statement, or does not express a clear sales intent,
classify it as Other and recommend No Action.
Do not classify such messages as Not Interested unless the customer
explicitly indicates that they do not want the product, service, or
further communication.

Perform these tasks:

1. Identify the customer's intent.

Choose exactly ONE of the following intents based on the customer's PRIMARY purpose:

- Interested
  Use when the customer explicitly expresses interest in the product,
  wants to explore it further, or shows willingness to continue the conversation,
  but does not primarily ask for a demo, pricing, or a specific product detail.

- Pricing Question
  Use when the customer asks about price, cost, plans, packages, subscription,
  budget, or pricing structure.

- Product Question
  Use when the customer asks a specific question about how the product works,
  its capabilities, features, functionality, integrations, or technical details.

- Technical Issue / Support Request
  Use when the customer reports a technical problem, error, malfunction,
  difficulty using the product, or explicitly asks for help from a human
representative or support team.

- Objection
  Use when the customer expresses a concern, hesitation, resistance, doubt,
  or reason for not proceeding, such as cost, complexity, implementation,
  risk, or switching concerns.

- Not Interested
  Use when the customer clearly says they are not interested,
  do not need the product, or do not want further communication.

- Request for Demo
  Use when the customer explicitly asks for a demo, demonstration,
  walkthrough, presentation, or product trial.

- Request for More Information
  Use when the customer explicitly asks for general information,
  documentation, brochure, product details, or an overview,
  WITHOUT primarily expressing interest or asking a specific product question.

- Ready to Buy
  Use when the customer indicates they are ready to purchase,
  proceed, sign up, start, or discuss the buying process.

- Other
  Use only when none of the above intents clearly applies.

IMPORTANT INTENT PRIORITY RULES:
1. If the customer explicitly asks for a demo, choose Request for Demo.
2. If the customer asks about price or cost, choose Pricing Question.
3. If the customer asks a specific product capability or feature question, choose Product Question.
4. If the customer expresses an objection or concern, choose Objection.
5. If the customer says they are ready to purchase, choose Ready to Buy.
6. If the customer clearly says they are not interested, choose Not Interested.
7. If the customer mainly expresses enthusiasm or interest in exploring the product,
   without asking for a specific feature, price, demo, or documentation, choose Interested.
8. Choose Request for More Information only when the PRIMARY purpose is to obtain
   general information or materials.
9. If the customer message is only a vague acknowledgment or does not clearly
   express a sales intent, such as "okay", "ok", "fine", "thanks", "sure", or
   a similarly unclear response, choose Other.
   Recommend No Action.
   Do not infer interest, purchase intent, or willingness to schedule a follow-up
   unless the customer explicitly expresses it.

2. Identify the customer's sentiment:
- Positive
- Neutral
- Negative

3. Determine the priority:
- High
- Medium
- Low

4. Generate a professional and concise reply.

The reply should:
- Directly address the customer's message.
- Be natural, helpful, and professional.
- Use only product information explicitly provided in the customer message or verified product information available in this prompt.
- Never assume that the product has a particular feature, integration, capability, compatibility, supported platform, pricing, guarantee, or technical specification.
- Never answer "yes" or "no" to a product capability question unless that capability is explicitly verified.
- If the requested product information is not available or cannot be verified, clearly acknowledge that it cannot be confirmed.
- When information cannot be confirmed, suggest that the sales team can review the customer's specific requirements.
- Never mention specific third-party platforms or companies unless they are explicitly provided as verified product information.
- Avoid unsupported claims about savings, ROI, performance, compatibility, or guarantees.
- Move the conversation toward an appropriate next step.

5. Recommend ONE next sales action based on the customer's intent:

- Technical Issue / Support Request → Escalate to Human
- Pricing Question → Send Pricing
- Interested → Schedule Follow-up
- Product Question → Escalate to Human
- Request for Demo → Schedule Demo
- Request for More Information → Send Information
- Not Interested → Close Lead
- Ready to Buy → Escalate to Human
- Objection → Schedule Follow-up
- If the intent does not clearly match any category → No Action

IMPORTANT:
For RECOMMENDED ACTION, return ONLY the action name after the arrow.
Never return the intent description, explanation, or the arrow.

For example:
Technical Issue / Support Request → Escalate to Human

RECOMMENDED ACTION: Escalate to Human

The recommended action must be exactly one of the actions listed above.
Do not choose an action that conflicts with the identified intent.

Return the result in this exact format:

INTENT:
SENTIMENT:
PRIORITY:
RECOMMENDED ACTION:
REPLY:
"""

        response = client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=prompt
        )

        ai_result = response.text
        st.session_state["ai_result"] = ai_result

        # -----------------------------
        # AUTOMATIC ACTION EXECUTION
        # -----------------------------

        parsed_result = parse_ai_result(ai_result)
        st.session_state["parsed_result"] = parsed_result

        intent = parsed_result["intent"]
        recommended_action = parsed_result["recommended_action"].strip()

        valid_actions = {
            "Schedule Demo",
            "Send Pricing",
            "Send Information",
            "Schedule Follow-up",
            "Escalate to Human",
            "Close Lead",
            "No Action",
        }

        if recommended_action not in valid_actions:
            recommended_action = "No Action"
            parsed_result["recommended_action"] = recommended_action
            st.session_state["parsed_result"] = parsed_result

        action_result = execute_and_update_sales_action(
            intent,
            recommended_action,
            customer_message,
            st.session_state.get("selected_lead"),
            parsed_result["priority"]
        )

        st.session_state["last_action"] = action_result
        st.rerun()


    else:
        st.session_state.pop("ai_result", None)
        st.session_state.pop("last_action", None)
        st.session_state.pop("email_status", None)
        st.warning("Please enter a customer response first.")

lead = st.session_state.get("selected_lead")

if lead is not None:
    st.markdown("### 👤 Selected Lead")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.write("**Name**")
        st.write(lead["Name"])

    with col2:
        st.write("**Company**")
        st.write(lead["Company"])

    with col3:
        st.write("**Job Role**")
        st.write(lead["Job Role"])
        
if "ai_result" in st.session_state:
    st.subheader("🤖 AI Sales Agent Analysis")

    ai_result = st.session_state["ai_result"]

    st.markdown("### 📊 Customer Assessment")

    parsed_result = parse_ai_result(ai_result)

    intent = parsed_result["intent"]
    sentiment = parsed_result["sentiment"]
    priority = parsed_result["priority"]
    recommended_action = parsed_result["recommended_action"]
    reply = parsed_result["reply"]

    col1, col2, col3 = st.columns(3)

    with col1:
        st.metric("Intent", intent)

    with col2:
        st.metric("Sentiment", sentiment)

    with col3:
        st.metric("Priority", priority)

    st.markdown("### 🎯 Recommended Action")
    st.info(recommended_action)

    st.markdown("### 💬 AI-Generated Reply")
    st.write(reply)
    if "last_action" in st.session_state:
        st.markdown("### ⚡ Automatic Action Executed")
        st.success(st.session_state["last_action"])
        if "email_status" in st.session_state:
            st.info(f"📧 Email Status: {st.session_state['email_status']}")
# -----------------------------
# SALES ACTIVITY LOG
# -----------------------------

st.header("📋 Sales Activity Log")

if "action_log" in st.session_state and st.session_state["action_log"]:

    col1, col2 = st.columns([5, 1])

    with col2:
        if st.button("Clear Log"):
            st.session_state["action_log"] = []
            st.rerun()

    for index, action in enumerate(
        st.session_state["action_log"], start=1
    ):
        st.write(f"{index}. {action}")

else:
    st.info("No sales actions recorded yet.")