"""Verified, event-triggered registrations and one-time event reminders."""

from __future__ import annotations

import os
import re
import smtplib
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from uuid import uuid4
from zoneinfo import ZoneInfo

import crm_storage
from chat_access import clean_email

IST = ZoneInfo("Asia/Kolkata")
EVENT_TYPES = {"webinar", "workshop", "seminar", "program", "course", "event", "events & experiences"}


@contextmanager
def _database():
    if crm_storage.database_enabled():
        conn = crm_storage._borrow_connection()
        placeholder = "%s"
    else:
        conn = sqlite3.connect(crm_storage.crm_path("event_registrations.sqlite3"), timeout=20)
        conn.execute("PRAGMA busy_timeout = 20000")
        placeholder = "?"
    try:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS event_registrations (
            registration_id TEXT PRIMARY KEY,
            offering_id TEXT NOT NULL,
            email TEXT NOT NULL,
            customer_name TEXT NOT NULL,
            offering_name TEXT NOT NULL,
            start_at TEXT NOT NULL,
            registered_at TEXT NOT NULL,
            reminder_opt_in INTEGER NOT NULL,
            confirmation_state TEXT NOT NULL DEFAULT 'pending',
            reminder_state TEXT NOT NULL DEFAULT 'pending',
            UNIQUE(offering_id, email)
        )""")
        conn.commit()
        yield conn, cur, placeholder
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        if crm_storage.database_enabled():
            crm_storage._release_connection(conn)
        else:
            conn.close()


def parse_start(offering):
    """Return an aware start time; a date-only event starts at 09:00 IST."""
    raw_date = str(offering.get("Start Date") or "").strip()
    raw_time = str(offering.get("Start Time") or "").strip()
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            date = datetime.strptime(raw_date, date_format).date()
            break
        except ValueError:
            continue
    else:
        return None
    hour, minute = 9, 0
    if raw_time:
        for time_format in ("%H:%M", "%I:%M %p", "%I %p"):
            try:
                parsed = datetime.strptime(raw_time, time_format)
                hour, minute = parsed.hour, parsed.minute
                break
            except ValueError:
                continue
        else:
            return None
    return datetime(date.year, date.month, date.day, hour, minute, tzinfo=IST)


def eligible_offering(offering):
    return (
        str(offering.get("Offering Type") or "").strip().casefold() in EVENT_TYPES
        and str(offering.get("Knowledge Status") or "").strip().casefold() == "published"
        and str(offering.get("AI Active") or "").strip().casefold() in {"true", "yes", "1"}
        and parse_start(offering) is not None
        and parse_start(offering) > datetime.now(IST)
    )


def _row(row):
    keys = ("registration_id", "offering_id", "email", "customer_name", "offering_name",
            "start_at", "registered_at", "reminder_opt_in", "confirmation_state", "reminder_state")
    return dict(zip(keys, row))


def list_registrations(offering_id=""):
    with _database() as (_, cur, p):
        query = "SELECT registration_id, offering_id, email, customer_name, offering_name, start_at, registered_at, reminder_opt_in, confirmation_state, reminder_state FROM event_registrations"
        if offering_id:
            cur.execute(query + f" WHERE offering_id = {p} ORDER BY registered_at DESC", (offering_id,))
        else:
            cur.execute(query + " ORDER BY registered_at DESC")
        return [_row(item) for item in cur.fetchall()]


def register(offering, name, email, reminder_opt_in):
    if not eligible_offering(offering):
        raise ValueError("This event is not open for Sharngon registration.")
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    email = clean_email(email)
    if not name or len(name) > 120 or not email:
        raise ValueError("Enter your name and a valid email address.")
    offering_id = str(offering.get("Offering ID") or "").strip()
    if not offering_id:
        raise ValueError("This event has no offering ID.")
    start = parse_start(offering)
    now = datetime.now(timezone.utc).isoformat()
    with _database() as (conn, cur, p):
        if p == "?":
            cur.execute("BEGIN IMMEDIATE")
        cur.execute(f"SELECT registration_id, confirmation_state FROM event_registrations WHERE offering_id = {p} AND email = {p}",
                    (offering_id, email))
        existing = cur.fetchone()
        if existing:
            registration_id = existing[0]
        else:
            registration_id = uuid4().hex
            cur.execute(f"INSERT INTO event_registrations VALUES ({','.join([p] * 10)})",
                        (registration_id, offering_id, email, name,
                         str(offering.get("Offering Name") or "Event").strip(), start.isoformat(), now,
                         int(bool(reminder_opt_in)), "pending", "pending"))
    if not existing or existing[1] in {"pending", "failed"}:
        send_registration_email(registration_id, "confirmation")
    return registration_id, bool(existing)


def _claim(registration_id, kind):
    field = "confirmation_state" if kind == "confirmation" else "reminder_state"
    with _database() as (conn, cur, p):
        if p == "?":
            cur.execute("BEGIN IMMEDIATE")
        cur.execute(f"UPDATE event_registrations SET {field} = 'sending' WHERE registration_id = {p} AND {field} IN ('pending', 'failed')",
                    (registration_id,))
        if not cur.rowcount:
            return None
        cur.execute(f"SELECT registration_id, offering_id, email, customer_name, offering_name, start_at, registered_at, reminder_opt_in, confirmation_state, reminder_state FROM event_registrations WHERE registration_id = {p}", (registration_id,))
        return _row(cur.fetchone())


def _state(registration_id, kind, value):
    field = "confirmation_state" if kind == "confirmation" else "reminder_state"
    with _database() as (_, cur, p):
        cur.execute(f"UPDATE event_registrations SET {field} = {p} WHERE registration_id = {p}",
                    (value, registration_id))


def _send_message(email, subject, body):
    sender, password = os.getenv("EMAIL_ADDRESS"), os.getenv("EMAIL_PASSWORD")
    if not sender or not password:
        raise RuntimeError("The sending mailbox is not configured.")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    message.set_content(body)
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465")), timeout=20) as smtp:
        smtp.login(sender, password)
        refused = smtp.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
    return message["Message-ID"]


def send_verification_code(email, code):
    _send_message(email, "Your Sharngon registration code",
                  f"Your registration code is {code}. It expires in 10 minutes.\n\nIf you did not request this, ignore this email.")


def send_registration_email(registration_id, kind):
    if kind not in {"confirmation", "reminder"}:
        raise ValueError("Unknown registration email type.")
    row = _claim(registration_id, kind)
    if row is None:
        return False
    when = datetime.fromisoformat(row["start_at"]).astimezone(IST)
    date_label = when.strftime("%d %B %Y at %I:%M %p IST")
    if kind == "confirmation":
        subject = f"Registration confirmed: {row['offering_name']}"
        body = (f"Hello {row['customer_name']},\n\nYour registration for {row['offering_name']} is confirmed.\n"
                f"Scheduled start: {date_label}.\n\nWe look forward to seeing you.\n\nSharngon")
    else:
        subject = f"Reminder: {row['offering_name']} starts soon"
        body = (f"Hello {row['customer_name']},\n\nThis is your requested reminder for {row['offering_name']}.\n"
                f"Scheduled start: {date_label}.\n\nSharngon")
    try:
        message_id = _send_message(row["email"], subject, body)
    except (OSError, RuntimeError, smtplib.SMTPException):
        # An ambiguous SMTP outcome is left in 'sending' to prevent duplicate mail.
        raise
    _state(registration_id, kind, "sent")
    crm_storage.record_communication_event({
        "event_id": f"registration:{registration_id}:{kind}", "provider_id": message_id,
        "channel": "email", "direction": "outbound", "destination": row["email"],
        "subject": subject, "source": "Sharngon event registration", "status": "accepted_by_smtp",
        "outcome": "SMTP accepted the message; inbox delivery is unconfirmed.",
        "updated_at": datetime.now(IST).isoformat(),
    })
    return True


def send_due_reminders(now=None):
    """Send each opted-in reminder once, within the 24 hours before event start."""
    now = (now or datetime.now(IST)).astimezone(IST)
    sent = 0
    for row in list_registrations():
        start = datetime.fromisoformat(row["start_at"]).astimezone(IST)
        if (row["reminder_opt_in"] and row["confirmation_state"] == "sent"
                and row["reminder_state"] == "pending"
                and now <= start <= now + timedelta(hours=24)):
            try:
                sent += int(send_registration_email(row["registration_id"], "reminder"))
            except (OSError, RuntimeError, smtplib.SMTPException):
                # A claimed send is never repeated automatically after an unclear SMTP result.
                continue
    return sent
