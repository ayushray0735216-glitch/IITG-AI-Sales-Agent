# IITG AI Sales Agent

An AI-powered sales automation prototype developed as part of the IITG research/project work.

## Overview

The IITG AI Sales Agent is designed to automate customer-response analysis and sales communication using Generative AI.

The system combines:

- Google Gemini for AI reasoning and response generation
- Streamlit for the sales dashboard
- Python for application logic
- Flask for voice webhook handling
- Twilio for voice communication
- ngrok for development-stage public webhook access
- CSV-based lead management
- Gmail SMTP for automated email actions

## Autopilot background scanner

The follow-up eligibility scan runs in a separate process every five minutes. It writes proposed actions to the
persistent queue; it does not send emails or place calls. Staff review and the existing consent, channel, quiet-hour,
attempt, and cooldown rules still apply.

### Local setup

Run the Streamlit app as usual, then start a second terminal in this project folder and run:

```text
python autopilot_worker.py
```

Without `DATABASE_URL`, the local queue is stored in SQLite and lead/activity records are read from CSV. The app and
worker must use the same `CRM_DATA_DIR` (the project folder by default).

### Hosted setup

Create a separate background worker for this repository with start command `python autopilot_worker.py`. Give it the
same `DATABASE_URL` and CRM-related environment variables as the web service so both processes use the same
PostgreSQL database. The worker records its heartbeat in CRM settings; the Autopilot panel warns when that heartbeat
is missing, overdue, or reports an error. Do not use the local SQLite fallback across separate hosted services.

The PostgreSQL action table enforces unique task IDs, and claims use database row locks. Local SQLite uses a primary
key plus transactional claims. Together these prevent duplicate queue rows and duplicate task claims across process
restarts. Interrupted work is left visible for staff review rather than silently retried.

## Core Capabilities

### 1. AI Customer Analysis

The system analyzes customer responses and identifies:

- Customer intent
- Sentiment
- Priority
- Recommended sales action
- AI-generated response

### 2. Intent Classification

The voice agent can classify customer messages into:

- Interested
- Pricing Question
- Product Question
- Request for Demo
- Request for More Information
- Not Interested
- Ready to Buy
- Objection
- Technical Issue / Support Request
- Other

### 3. Automated Sales Action Routing

Customer intent is mapped to an appropriate sales action.

Examples:

| Customer Intent | Sales Action |
|---|---|
| Pricing Question | Send Pricing |
| Request for Demo | Schedule Demo |
| Request for More Information | Send Information |
| Interested | Schedule Follow-up |
| Ready to Buy | Escalate to Human |
| Objection | Schedule Follow-up |
| Not Interested | Close Lead |
| Technical Issue / Support Request | Escalate to Human |
| Other | No Action |

### 4. Conversational Voice Agent

The voice module uses:

Customer Speech
→ Twilio
→ Flask
→ Speech Processing
→ Gemini Intent Classification
→ Conversation Memory
→ Gemini Response
→ Twilio Text-to-Speech

The system supports multi-turn conversations by maintaining conversation history using the call identifier.

### 5. Lead Management

The Streamlit dashboard provides:

- Lead database management
- Lead selection
- Customer-response analysis
- Lead status updates
- Last action tracking
- Interaction timestamps
- Priority tracking
- Sales activity logging

### 6. Email Automation

Selected sales actions can trigger automated email communication, including:

- Pricing information
- Product information
- Demo-related communication
- Follow-up
- Human escalation

## System Architecture

```text
                    Customer
                       |
                       v
                 Twilio Voice
                       |
                       v
                Flask Webhook
                       |
          +------------+------------+
          |                         |
          v                         v
   Intent Classification     Conversation Memory
          |                         |
          +------------+------------+
                       |
                       v
                  Gemini AI
                       |
                       v
              Sales Action Router
                       |
       +---------------+---------------+
       |               |               |
       v               v               v
    Pricing          Demo        Information
       |               |               |
       +---------------+---------------+
                       |
                       v
                AI Voice Response
                       |
                       v
                    Customer
