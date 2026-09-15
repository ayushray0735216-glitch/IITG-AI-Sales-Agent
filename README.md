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