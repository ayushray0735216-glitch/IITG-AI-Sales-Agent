# IITG shared CRM connection service

API-only deployment package. The existing customer chatbot stays on Streamlit.
Contains no credentials, prospect records, email worker or phone service.

Deploy this folder as its own Vercel project/repository, using Flask.
Before deployment enter these Environment Variables privately in Vercel:

- DATABASE_URL: the SAME Neon PostgreSQL connection used by the Streamlit app.
- INTEGRATION_ENABLED: true
- INTEGRATION_PUBLIC_CHAT_URL: https://iitg-ai-sales-agent.streamlit.app/
- INTEGRATION_DISCOVERY_KEY: a separate random value of at least 32 characters.
- INTEGRATION_OUTREACH_KEY: a different random value of at least 32 characters.
- INTEGRATION_ADMIN_KEY: another different random value of at least 32 characters.

Do not put keys or the database URL in this repository or send them in chat.
Never configure SQLite or local CSV as the cloud shared data store.

Deployment process status alone does not establish a working database connection.
GET /health must return HTTP 200 with status ok. Missing/invalid configuration
or database startup failure exposes only a generic HTTP 503 health response.
Protected /v1 endpoints require a valid module Bearer key.

The Streamlit app must receive its integration update and configuration too.
The Apps Script owner must install the reviewed connector in a test copy and
configure it with this project's production URL and its discovery/outreach keys.
This service does not run periodic jobs or send email. Existing manual/legacy
senders need to adopt the reservation contract before global duplicate prevention
can be claimed. See the main INTEGRATION_GUIDE.md for the full testing contract.
