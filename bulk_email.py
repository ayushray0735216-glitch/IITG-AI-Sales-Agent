
"""Durable CRM bulk-email batches and recipient delivery."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import json
import logging
import os
import re
from string import Formatter
from threading import Lock, Thread

import autopilot
import crm_storage as crm
import chat_access
from crm_email_delivery import send_email
from urllib.parse import urlparse


MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_TOTAL_BYTES = 17 * 1024 * 1024
FIELDS = {"name": "Name", "company": "Company", "job_title": "Job Role", "product": "Product"}
OUTCOMES = {
    "accepted_by_smtp": ("Sent", "Bulk email sent"),
    "blocked": ("Skipped", "Bulk email skipped"),
    "failed": ("Failed", "Bulk email failed"),
    "delivery_unknown": ("Check delivery", "Bulk email delivery uncertain"),
}
LOGGER = logging.getLogger(__name__)
_runner_lock = Lock()
_runner = None
_schema_lock = Lock()
_ready_stores = set()


def personalize(template, lead, chat_link=""):
    """Substitute only the supported CRM fields."""
    result = []
    for literal, field, spec, conversion in Formatter().parse(template):
        result.append(literal)
        if field is None:
            continue
        if field not in FIELDS and field != "chat_link" or spec or conversion:
            raise ValueError("Use only {name}, {company}, {job_title}, {product}, or {chat_link} for personalization.")
        value = chat_link if field == "chat_link" else crm.lead_cell(lead.get(FIELDS[field]))
        if field == "name":
            value = crm.lead_cell(lead.get("Contact Name")) or value
            if not value or value.casefold().startswith("anonymous visitor"):
                value = "there"
        result.append(value)
    return "".join(result).strip()


def review_recipients(selected, subject, body):
    """Freeze the reviewed text and explain why any selected leads are excluded."""
    leads = crm.load_crm_table("leads.csv").to_dict("records")
    by_identity = {}
    by_address = {}
    for lead in leads:
        # An older anonymous chat row can retain the same Visitor ID after a
        # named CRM lead is created. Use the lead's own record key for review.
        by_identity.setdefault(crm._lead_key(lead), []).append(lead)
        email = crm.lead_cell(lead.get("Email")).casefold()
        by_address[email] = by_address.get(email, 0) + 1

    activity = crm.load_crm_table("sales_activity.csv")
    settings = crm.load_autopilot_settings()
    suppressions = crm.load_contact_suppressions()
    uses_chat_link = any(field == "chat_link" for _, field, _, _ in Formatter().parse(body))
    chat_base = os.getenv("CHAT_PUBLIC_BASE_URL", "")
    if uses_chat_link and (urlparse(chat_base).scheme != "https" or
                           urlparse(chat_base).hostname in {None, "localhost", "127.0.0.1"}):
        raise ValueError("Set CHAT_PUBLIC_BASE_URL to your public HTTPS chat address before reviewing chat invitations.")
    reviewed = []
    seen = set()
    for selected_lead in selected:
        matches = by_identity.get(crm._lead_key(selected_lead), [])
        lead = matches[0] if len(matches) == 1 else selected_lead
        identity = crm.lead_identity(lead)
        email = crm.lead_cell(lead.get("Email"))
        address = email.casefold()
        reason = ""
        if len(matches) != 1 or address != crm.lead_cell(selected_lead.get("Email")).casefold():
            reason = "Lead or email changed. Refresh the lead list."
        elif not email:
            reason = "Missing email address."
        elif by_address[address] != 1 or address in seen:
            reason = "Duplicate email address in the CRM."
        else:
            reason = autopilot.contact_action_block_reason(
                lead, "email", email, activity=activity, settings=settings, suppressions=suppressions,
            )
        chat_link = chat_access.create_prospect_link(lead, chat_base) if uses_chat_link and not reason else ""
        personalized_subject = personalize(subject, lead)
        personalized_body = personalize(body, lead, chat_link=chat_link)
        if not personalized_subject or "\r" in personalized_subject or "\n" in personalized_subject or not personalized_body:
            raise ValueError("Every personalized email needs a single-line subject and a message.")
        reviewed.append({
            "lead_key": identity, "name": crm.lead_cell(lead.get("Name")), "email": email,
            "subject": personalized_subject, "body": personalized_body, "reason": reason,
            "import_key": crm.lead_cell(lead.get("Lead Import Key")),
        })
        if not reason:
            seen.add(address)
    return reviewed


@contextmanager
def _store():
    """Use shared PostgreSQL in hosted mode and the local CRM queue otherwise."""
    postgres = crm.database_enabled()
    store_key = crm.DATABASE_URL if postgres else str(crm.DATA_DIR)
    conn = crm._borrow_connection() if postgres else crm._sqlite_autopilot_connection()
    try:
        with conn:
            cursor = conn.cursor()
            try:
                def execute(sql, args=()):
                    cursor.execute(sql.replace("?", "%s") if postgres else sql, args)
                    return cursor

                with _schema_lock:
                    if store_key not in _ready_stores:
                        if postgres:
                            execute("SELECT pg_advisory_xact_lock(94520761)")
                        execute("""CREATE TABLE IF NOT EXISTS crm_email_batches (
                            batch_id TEXT PRIMARY KEY, subject TEXT NOT NULL, created_at TEXT NOT NULL,
                            attachment_names TEXT NOT NULL, attachments TEXT NOT NULL
                        )""")
                        execute("""CREATE TABLE IF NOT EXISTS crm_email_batch_recipients (
                            event_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, data TEXT NOT NULL,
                            status TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL
                        )""")
                        execute("""CREATE INDEX IF NOT EXISTS crm_email_batch_status_idx
                            ON crm_email_batch_recipients (status, updated_at)""")
                        execute("""CREATE INDEX IF NOT EXISTS crm_email_batch_recipients_batch_idx
                            ON crm_email_batch_recipients (batch_id)""")
                        conn.commit()
                        _ready_stores.add(store_key)
                yield execute
            finally:
                cursor.close()
    finally:
        if postgres:
            crm._release_connection(conn)
        else:
            conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat()


def validate_attachments(attachments):
    filenames = [name for name, _, _ in attachments]
    if len(set(name.casefold() for name in filenames)) != len(filenames):
        raise ValueError("Attachment names must be unique.")
    if any(not data or len(data) > MAX_FILE_BYTES for _, data, _ in attachments):
        raise ValueError("Each attachment must be nonempty and no larger than 15 MB.")
    if sum(len(data) for _, data, _ in attachments) > MAX_TOTAL_BYTES:
        raise ValueError("Attachments must total 17 MB or less.")


def queue_batch(batch_id, subject, recipients, attachments):
    if not any(not recipient["reason"] for recipient in recipients):
        raise ValueError("This review has no eligible recipients.")
    validate_attachments(attachments)
    filenames = [name for name, _, _ in attachments]
    encoded = [[name, base64.b64encode(data).decode("ascii"), mime] for name, data, mime in attachments]
    timestamp = _now()
    with _store() as execute:
        inserted = execute(
            """INSERT INTO crm_email_batches VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (batch_id) DO NOTHING""",
            (batch_id, subject, timestamp, json.dumps(filenames), json.dumps(encoded)),
        ).rowcount
        if inserted:
            for index, row in enumerate(recipients):
                execute(
                    "INSERT INTO crm_email_batch_recipients VALUES (?, ?, ?, ?, ?, ?)",
                    (f"bulk-email:{batch_id}:{index}", batch_id, json.dumps(row, ensure_ascii=False),
                     "Skipped" if row["reason"] else "Queued", row["reason"], timestamp),
                )
    return bool(inserted)


def queued_work_exists():
    with _store() as execute:
        return execute(
            "SELECT 1 FROM crm_email_batch_recipients WHERE status IN ('Queued', 'Sending') LIMIT 1"
        ).fetchone() is not None


def recent_batches():
    with _store() as execute:
        return execute("""SELECT batch_id, subject, created_at, attachment_names FROM crm_email_batches
                          ORDER BY created_at DESC LIMIT 30""").fetchall()


def batch_recipients(batch_id):
    with _store() as execute:
        rows = execute(
            """SELECT data, status, detail FROM crm_email_batch_recipients
               WHERE batch_id = ? ORDER BY event_id""", (batch_id,),
        ).fetchall()
    return [
        {"Lead": data["name"], "Email ID": data["email"], "Status": status, "Details": detail}
        for payload, status, detail in rows for data in [json.loads(payload)]
    ]


def _claim_recipient():
    with _store() as execute:
        abandoned = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        execute(
            """UPDATE crm_email_batch_recipients SET status = 'Check delivery', detail = ?
               WHERE status = 'Sending' AND updated_at < ?""",
            ("The sender stopped before recording a result. Check the sending mailbox.", abandoned),
        )
        row = execute(
            """SELECT event_id, batch_id, data FROM crm_email_batch_recipients
               WHERE status = 'Queued' ORDER BY updated_at, event_id LIMIT 1""",
        ).fetchone()
        if row and execute(
            """UPDATE crm_email_batch_recipients SET status = 'Sending', updated_at = ?
               WHERE event_id = ? AND status = 'Queued'""", (_now(), row[0]),
        ).rowcount == 1:
            return tuple(row)
        return None


def _batch_attachments(batch_id):
    with _store() as execute:
        row = execute("SELECT attachments FROM crm_email_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return [(name, base64.b64decode(encoded), mime) for name, encoded, mime in json.loads(row[0])]


def _record_outcome(recipient, event_id, event, label, action):
    timestamp = event.get("updated_at", _now())
    # Record the reviewed identity even when its address changed after approval.
    crm.update_lead_email_outcome(
        recipient["email"], label, action, timestamp, event_id, expected_lead_key=recipient["lead_key"],
    )
    names = ", ".join(event.get("attachment_names", []))
    activity = {
        "activity_id": event_id, "timestamp": timestamp, "lead": recipient["name"],
        "intent": "CRM Email" if label == "Sent" else "CRM Email Review",
        "action": f"{action} · {recipient['subject']}" + (f" · Attached {names}" if names else ""),
    }
    if recipient.get("import_key"):
        activity["lead_import_key"] = recipient["import_key"]
    crm.append_sales_activity(activity)


def process_queue(limit=None):
    """Deliver approved recipients outside the request thread."""
    if not (os.getenv("EMAIL_ADDRESS") and os.getenv("EMAIL_PASSWORD")):
        return
    previous_batch = None
    attachments = []
    processed = 0
    while limit is None or processed < limit:
        claim = _claim_recipient()
        if claim is None:
            return
        event_id, batch_id, payload = claim
        recipient = json.loads(payload)
        try:
            if batch_id != previous_batch:
                attachments = _batch_attachments(batch_id)
                previous_batch = batch_id
            result = send_email(
                recipient["email"], recipient["subject"], recipient["body"], attachments,
                review_id=event_id, expected_lead_key=recipient["lead_key"], bulk=True,
            )
            event = crm.get_communication_events([event_id]).get(event_id, {})
            label, action = OUTCOMES.get(event.get("status"), ("Check delivery", "Bulk email delivery uncertain"))
            detail = event.get("outcome") or result
            if not event:
                event = {
                    "event_id": event_id, "source": "Bulk CRM email", "channel": "email",
                    "direction": "outbound", "destination": recipient["email"],
                    "lead_name": recipient["name"], "subject": recipient["subject"],
                    "body": recipient["body"], "attachment_names": [name for name, _, _ in attachments],
                    "status": "delivery_unknown", "outcome": detail, "updated_at": _now(),
                }
                crm.record_communication_event(event)
            try:
                _record_outcome(recipient, event_id, event, label, action)
            except Exception:
                LOGGER.exception("Bulk email CRM summary update failed")
                detail += " The CRM lead summary needs review."
        except Exception:
            LOGGER.exception("Bulk email recipient could not be finalized")
            label = "Check delivery"
            detail = "Check the sending mailbox before sending to this recipient again."
        with _store() as execute:
            execute(
                "UPDATE crm_email_batch_recipients SET status = ?, detail = ?, updated_at = ? WHERE event_id = ?",
                (label, detail, _now(), event_id),
            )
            # Attachment bytes are needed only until every recipient has a final outcome.
            execute(
                """UPDATE crm_email_batches SET attachments = '[]'
                   WHERE batch_id = ? AND NOT EXISTS (
                       SELECT 1 FROM crm_email_batch_recipients WHERE batch_id = ? AND status IN ('Queued', 'Sending')
                   )""",
                (batch_id, batch_id),
            )
        processed += 1


def start_sender():
    """Start one sender in this app process. The separate CRM worker can resume queued work."""
    global _runner
    with _runner_lock:
        if _runner is None or not _runner.is_alive():
            def run():
                try:
                    process_queue()
                except Exception:
                    LOGGER.exception("Bulk email worker stopped with pending work")
            _runner = Thread(target=run, name="crm-bulk-email", daemon=True)
            _runner.start()

