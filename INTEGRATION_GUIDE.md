# Shared CRM integration: local foundation

This is an additive integration layer for prospect discovery, outreach, and the
IITG customer chatbot. It is implemented locally and disabled by default. The
teammate's live Apps Script is NOT yet connected or changed. No live messages
have been sent while building or testing this integration.

## What exists now

- `shared_integration.py`: stable contacts, cross-source identity matching,
  qualifications, explicit contact preferences, expiring/revocable referral
  links, distinct visitor conversations, immutable messages, sales insights,
  idempotent communication outcomes and a durable change cursor.
- `integration_api.py`: authenticated server-to-server REST API. Existing app
  dependencies already include Flask and psycopg2; no new package is required.
- `integration_chat.py` and app.py hooks: personalised links open fresh chats,
  known contacts are associated only inside the developer CRM, transcripts and
  intent/sentiment/priority/status are synchronized, and saved visitor records
  can be reconciled after a secondary sync failure.
- `shared_crm_adapter.gs`: server-side Apps Script helper ready for the owner's
  project. It installs no triggers, grants no permissions, and sends no email.
- `shared_outreach_hooks.gs` and `apps_script_sender_patch.gs`: local connection
  for the inspected sender, retaining its HTML/plain-text builders and attachments.
  Requires explicit installation, configuration and per-recipient staff review.
- The developer console has a Shared module connections expander with contacts,
  visitor associations, link creation and reconciliation. Existing chat-history
  rows show the shared contact and identity-association label.

## Confirmed teammate interface, 5 November 2026

User supplied a combined prospect/outreach deployment:
https://script.google.com/macros/s/AKfycbxl5W2rREeG_2aEOJihBKNnVSjrrMJBEl2tPJyqk6Mi_OCdf7PPhbrC3ph4ERhnYLyi/exec

That URL currently requires Google authorization for Personalised message. No
permission was granted and no discovery, sending or reply-detection function ran.

The public library documentation (Personalised message, version 61) is readable:
https://script.google.com/macros/library/d/1GQ-2EM33dKGIjhHH8e9vihFoFx5PecoX_HKRi-CrodJi3lPr8QPpNZwS/61

The actual editor was subsequently readable at:
https://script.google.com/home/projects/1GQ-2EM33dKGIjhHH8e9vihFoFx5PecoX_HKRi-CrodJi3lPr8QPpNZwS/edit

Inspected the current getProspectSheet, getCampaignSettings, processNextProspect,
processProspectBatch, buildEmailHtml and buildPlainTextEmail implementations.
No function was run and no script was edited. This is the editor's current source,
not proof that it exactly matches the pinned library or running deployment.

The shared Campaign spreadsheet is view-only:
https://docs.google.com/spreadsheets/d/1gmUQqJTdW01v25ZkNUFdTHYGrcwfp6iz0Ff4kpOWFRs/edit

Observed Prospects headings A1:F1: Name, Company, Job title, Email, Status,
Subject. Source additionally requires Message and appends Error if absent. Tabs include
Prospects, Campaign, Reply History, Products, Campaigns, Knowledge_Base,
Campaign_Instructions, Assets and AI_Usage_Log. No sheet data was edited or
imported. A send Status such as Sent or Duplicate - Not Sent is NOT a
qualification result or proof of consent.

The sender selects Pending rows, checks whether another row with the same email
is Sent, drafts a message, builds HTML/plain text from a campaign object, calls
MailApp.sendEmail, then marks Sent. Its campaign.chatbotUrl is one global value;
both builders use it. The patch supplies one link per recipient, leaving the
global Campaign sheet unchanged. Qualification/consent were not present in this
sending path and must be supplied explicitly, never inferred from Pending/Sent.

## Configure and test locally

1. Keep the existing DATABASE_URL for hosted shared data. Every backend service
   must use the SAME database. A connection failure must not silently switch to
   a local CSV/SQLite store.
2. For an isolated local preview instead, set INTEGRATION_SQLITE_PATH to an
   absolute disposable file path; unset DATABASE_URL only in that test process.
   SQLite is not a database shared between different cloud computers.
3. Set INTEGRATION_ENABLED=true after selecting the intended data store. The
   integration tables are additive and created when the feature is first used.
   Existing CRM tables are not dropped or rewritten by these migrations.
4. Set separate INTEGRATION_DISCOVERY_KEY, INTEGRATION_OUTREACH_KEY and
   INTEGRATION_ADMIN_KEY values, each randomly generated and at least 32
   characters. With no keys configured, protected endpoints reject all callers.
   Store keys in server environment/secrets, never browser JavaScript or URLs.
5. Set INTEGRATION_PUBLIC_CHAT_URL=http://localhost:8501/ for local tests. For
   deployment, use https://iitg-ai-sales-agent.streamlit.app/.
6. Start the REST server explicitly: `python integration_api.py`. It binds only
   to 127.0.0.1:8800 for local development. It has no sending worker.
7. Start Streamlit separately in the same environment:
   `python -m streamlit run app.py --server.port 8501`.
8. The developer console's Shared module connections section can create a link
   after a contact has been supplied through the API. Opening that link creates
   a fresh visitor conversation. An email link never loads old private chats.

Do not activate the existing Autopilot merely to test this integration. Local
unit tests use temporary storage and no email/call/AI providers.

## REST contract

Use `Authorization: Bearer <module-key>` and JSON request bodies. The service is
for trusted server-side modules in this one organization. It is not a multi-tenant
public contact API. Health checks are public; all CRM routes require a service key.

| Method and route | Role | Purpose |
| --- | --- | --- |
| GET /health | Public | Process health; no prospect data |
| POST /v1/contacts | Discovery or admin | Upsert using external_id, contact, optional expected_version |
| GET /v1/contacts?qualification=qualified | Discovery/outreach/admin | Read shared contacts and their preferences |
| GET /v1/contacts/{contact_id} | Discovery/outreach/admin | Read one current contact |
| PUT /v1/contacts/{contact_id}/preferences | Admin | Record channels, consent and evidence |
| POST /v1/chat-links | Outreach or admin | Create an attribution link |
| DELETE /v1/chat-links/{link_id} | Outreach or admin | Revoke future use of a referral |
| POST /v1/communications | Outreach or admin | Record an immutable delivery/reply outcome |
| POST /v1/email-reservations | Outreach or admin | Reserve one reviewed dispatch; checks snapshot, policy, permission, suppression and shared attempt limits |
| GET /v1/contacts/{contact_id}/activity | Outreach or admin | Read associations, sales insight summaries and delivery outcomes; no transcripts |
| GET /v1/snapshot | All module roles | Fresh published/active catalog and current Autopilot policy |
| GET /v1/changes?after=0&limit=100 | All module roles | Durable ordered events and next_cursor |
| GET /v1/conversations | Admin | Private transcripts and visitor associations |

Contact intake example (synthetic):

```json
{
  "external_id": "stable-prospect-uuid",
  "contact": {
    "name": "Sample Prospect",
    "email": "sample@example.com",
    "phone": "+919876543210",
    "company": "Example Organization",
    "job_title": "Manager",
    "qualification": "qualified"
  }
}
```

Allowed contact fields: name, email, phone, whatsapp, company, job_title,
qualification. Phone/WhatsApp must use international country-code format.
Qualification values: pending, qualified, unqualified. Blank imports do not erase
existing values. Names alone never merge contacts. Conflicting email/phone
matches return 409 and require staff review. An existing source/external_id
mapping identifies the same record on replay. Use expected_version to detect
conflicting staff edits; do not use a movable spreadsheet row number as identity.

Preferences example: `{"channels":["email"],"consent":true,"evidence":"Customer opted in through form XYZ"}`.
Discovery/outreach keys cannot grant consent. A referral click never grants it.
The existing chat contact form continues to require explicit opt-in and records
self-reported details; this does not establish verified account ownership.

Link request: `{"contact_id":"CON-...","campaign_id":"campaign-uuid","ttl_seconds":172800}`.
Response contains chat_url, link_id and expires_at. Only a SHA-256 hash of the
random reference is stored. Lifetime: 60 seconds to 7 days. The link is an
attribution reference, NOT a login credential. Forwarded links create separate
unverified sessions, not permission to reveal the intended recipient's profile.

Outcome request:

```json
{
  "external_id": "provider-message-uuid:sent",
  "outcome": {
    "contact_id": "CON-...",
    "channel": "email",
    "status": "sent",
    "provider_id": "provider-message-uuid",
    "campaign_id": "campaign-uuid"
  }
}
```

Statuses: sent, delivered, failed, uncertain, replied, bounced. Replaying the
same event ID and data is safe; reusing the ID with different data returns 409.
Use a separate stable event ID for a later delivery transition. The log is NOT a
send authorization or a provider delivery mechanism.

Reservation body: `{"contact_id":"CON-...","campaign_id":"campaign-uuid","event_id":"MAIL-uuid","snapshot_version":"current-snapshot-version","reviewed":true}`.
Only a 201 response grants this connector one dispatch attempt. A repeated
reservation returns 409, including the same event ID. A lost response must be
reconciled, not retried as a send. Reservations count toward the 30-day outreach
limit/cooldown, including uncertain attempts. They do not automatically expire,
because expiry could allow duplicate delivery after a provider timeout.

## Wire the owner's Apps Script

The live script is unchanged. Use a COPY of its project/spreadsheet for synthetic
testing first; pause its triggers while installing the connector.

1. Add shared_crm_adapter.gs and shared_outreach_hooks.gs as new script files.
2. Replace the original getCampaignSettings, processNextProspect, buildEmailHtml
   and buildPlainTextEmail function blocks with apps_script_sender_patch.gs.
   Do not leave duplicate definitions. The two builders are unchanged copies;
   the first two functions become Legacy_ functions called by reviewed wrappers.
3. In Script Properties set SHARED_CRM_BASE_URL, SHARED_CRM_DISCOVERY_KEY,
   SHARED_CRM_OUTREACH_KEY, SHARED_CRM_OFFERING_ID and SHARED_CRM_CAMPAIGN_ID.
   Use stable offering/campaign IDs, not mutable names. The base URL must be the
   separately hosted HTTPS backend, NOT the Streamlit URL or existing /exec URL.
   Keep keys out of spreadsheet cells and browser code.
4. Add Qualification with explicit pending/qualified/unqualified and Shared Review.
   The connector requires exactly Approved after staff review. Record actual
   email opt-in through the customer form or admin preferences endpoint with its
   evidence. Do not invent permission for discovered addresses.
5. Set SHARED_CRM_ENABLED=true only in the test copy after the API and Streamlit
   share the intended database and synthetic contacts. Production remains
   unchanged until the owner installs/deploys the patch. Setting the flag false
   restores legacy behavior; it is NOT an emergency pause. Use the shared admin
   pause with the connector enabled and stop legacy triggers when needed.

The connector writes Prospect ID, Shared Contact ID, Shared Campaign ID,
Shared Event ID, Shared Chat URL, Shared Link Expires, Shared Send State and
Shared Sync State. Keep these out of public dashboards. First imports use
optimistic version 0: a collision requires staff review. Set the correct Shared
Contact ID only after checking the match. Later sends read that contact instead
of overwriting newer admin edits with a stale sheet row.

Connection contract used by those files:

1. After qualification, map the known Prospects headings through
   `SharedCRM.prospectFromSheetRow(headings, row, qualification)`, then call
   `SharedCRM.upsertProspect(stableProspectId, fields)`. Save the returned contact_id.
2. Before a permitted email is built for that recipient, call
   `SharedCRM.createChatLink(contactId, campaignId)` and persist/reuse that link
   for retries of the same email. Put chat_url into THAT recipient's Chat with us
   button and plain-text email. A campaign-wide chatbotUrl is insufficient to
   identify different recipients. The sender patch now sets campaign.chatbotUrl
   only for that recipient and retains its poster/brochure handling.
3. Read `SharedCRM.currentSnapshot()` for current offerings and policy. Preserve
   the sender's actual qualification, consent, suppression, quiet-hour, cadence,
   deduplication and provider-result checks; this adapter does not replace them.
   A queued item is rechecked before delivery. This connector requires staff
   review and a durable dispatch reservation; it creates no worker or triggers.
4. After a known result, use `SharedCRM.recordOutcome(stableEventId, outcome)`.
   If provider acceptance is uncertain, record uncertain and reconcile with the
   provider instead of blindly sending again. Recording outcomes is idempotent;
   the sender persists Sending before reserving/calling MailApp and blocks
   Sending/Sent/Uncertain rows on replay, even if old Status is reset. A known
   send with failed CRM logging becomes Shared Sync State = Pending sent.
   SharedOutreach.reconcileOutcome(sheet, rowNumber) records it without resending.
   A crash leaving Sending requires provider verification; do not reset blindly.
5. Poll `SharedCRM.changesSince(cursor)`. Persist the returned cursor ONLY after
   processing the entire page successfully. Follow pages until caught up and
   periodically refresh the snapshot too. These notifications contain IDs/topics,
   not raw prospect contact details or chat messages.

The backend clock is locked inside each write transaction so cursor order follows
commit order. Catalog edits and Autopilot policy saves append change events in
the same PostgreSQL transaction. API snapshots always read current CRM facts.
Screens still need their refresh/subscription adapter; shared storage alone does
not make all open screens instantly redraw.

## Recovery, hosting and limitations

- Primary legacy chat writes remain durable before secondary integration sync.
  If secondary sync fails, the original transcript is not discarded. The admin
  reconciliation button replays saved visitor messages/explicit opt-ins using
  stable IDs. Unidentified or invalid legacy rows remain available in old CRM
  history and are skipped with a count, never guessed into another person's chat.
- The backend must be hosted separately with HTTPS and the shared PostgreSQL
  secrets. Production command: `gunicorn 'integration_api:create_app()'` on a host
  that supports it. Windows local development uses `python integration_api.py`.
  The existing Streamlit deployment does not automatically serve /v1 endpoints.
- Always-on automation requires an independently hosted worker. This foundation
  creates no background email/call worker and makes no always-on hosting promise.
- No live PostgreSQL migration or teammate deployment has been performed. Tests
  cover SQLite transactions, duplicate/conflict recovery, API permissions, source
  mapping, hashed/referral expiry, private CRM views, and chat/UI regressions.
  A hosted PostgreSQL staging run and an owner-approved Apps Script dry run with
  synthetic prospects remain required before enabling real cross-module sending.
- Reservations coordinate connected senders only. Existing manual email, legacy
  Autopilot actions and other Apps Script projects must also adopt the reservation
  contract before claiming organization-wide cadence/deduplication. Historical
  attempts are not silently imported into this new counter.
- The sender reads current catalog details before drafting and checks again
  before dispatch, stopping for review if facts/policy changed. Voice guidance
  uses the common catalog. Other dashboards still need change-cursor refresh
  wiring; shared storage does not instantly redraw open screens or sent emails.

Validation commands:

```text
python -m unittest test_shared_integration test_chat_scope test_chat_followups test_offering_facts test_chat_accessibility -q
node test_shared_crm_adapter.cjs
node test_apps_script_sender.cjs
```

Apps Script references:
- https://developers.google.com/apps-script/reference/url-fetch/url-fetch-app
- https://developers.google.com/apps-script/guides/properties
