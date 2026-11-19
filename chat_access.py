"""Verified chat invitations and short-lived email challenges."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import smtplib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from urllib.parse import urlencode

import crm_storage

EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
CHALLENGE_MINUTES = 10
INVITE_DAYS = 30


def clean_email(value):
    address = str(value or "").strip().casefold()
    return address if EMAIL.fullmatch(address) and len(address) <= 254 else ""


def prospect_key(lead):
    """Keep the invitation tied to the imported lead after chat ID assignment."""
    for field in ("CRM Record ID", "Lead Import Key"):
        value = crm_storage.lead_cell(lead.get(field))
        if value:
            return field + ":" + value
    return "email:" + clean_email(lead.get("Email"))


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc)


@contextmanager
def _db():
    if crm_storage.database_enabled():
        conn = crm_storage._borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS chat_access (
                    access_key TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL,
                    lead_key TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, issued_at TEXT NOT NULL)""")
            conn.commit()
            yield conn, "%s"
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            crm_storage._release_connection(conn)
    else:
        conn = sqlite3.connect(crm_storage.crm_path("chat_access.sqlite3"), timeout=15)
        try:
            conn.execute("""CREATE TABLE IF NOT EXISTS chat_access (
                access_key TEXT PRIMARY KEY, kind TEXT NOT NULL, email TEXT NOT NULL,
                lead_key TEXT NOT NULL DEFAULT '', expires_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, issued_at TEXT NOT NULL)""")
            yield conn, "?"
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _put(key, kind, email, lead_key, expires, issued=None):
    with _db() as (conn, p):
        with conn.cursor() if p == "%s" else _sqlite_cursor(conn) as cur:
            cur.execute(f"DELETE FROM chat_access WHERE access_key = {p}", (key,))
            cur.execute(f"INSERT INTO chat_access VALUES ({p},{p},{p},{p},{p},0,{p})",
                        (key, kind, email, lead_key, expires.isoformat(), (issued or _now()).isoformat()))


@contextmanager
def _sqlite_cursor(conn):
    cur = conn.cursor()
    try:
        yield cur
    finally:
        cur.close()


def _get(key):
    with _db() as (conn, p):
        with conn.cursor() if p == "%s" else _sqlite_cursor(conn) as cur:
            cur.execute(f"SELECT kind,email,lead_key,expires_at,attempts,issued_at FROM chat_access WHERE access_key = {p}", (key,))
            return cur.fetchone()


def create_prospect_link(lead, base_url):
    email = clean_email(lead.get("Email"))
    if not email:
        raise ValueError("This lead needs a valid email address.")
    identity = prospect_key(lead)
    if not identity:
        raise ValueError("This lead needs a stable CRM identity.")
    token = secrets.token_urlsafe(32)
    _put("invite:" + _digest(token), "invite", email, identity, _now() + timedelta(days=INVITE_DAYS))
    separator = "&" if "?" in base_url else "?"
    return base_url.rstrip("/") + "/" + separator + urlencode({"chat": "prospect", "invite": token})


def prospect_invitation(token):
    if not token or len(token) > 128:
        return None
    row = _get("invite:" + _digest(token))
    if not row or row[0] != "invite" or datetime.fromisoformat(row[3]) <= _now():
        return None
    return {"email": row[1], "lead_key": row[2], "claimed": row[4] > 0}


def claim_invitation(token):
    """Allow the first deliberate chat start; later sessions verify the mailbox."""
    if not token or len(token) > 128:
        return None
    key = "invite:" + _digest(token)
    with _db() as (conn, p):
        with conn.cursor() if p == "%s" else _sqlite_cursor(conn) as cur:
            if p == "?":
                cur.execute("BEGIN IMMEDIATE")
            lock = " FOR UPDATE" if p == "%s" else ""
            cur.execute(f"SELECT kind,email,lead_key,expires_at,attempts FROM chat_access WHERE access_key = {p}{lock}", (key,))
            row = cur.fetchone()
            if not row or row[0] != "invite" or datetime.fromisoformat(row[3]) <= _now():
                return None
            if row[4] == 0:
                cur.execute(f"UPDATE chat_access SET attempts = 1 WHERE access_key = {p}", (key,))
                return {"email": row[1], "lead_key": row[2]}
            return None


def _send_code(email, code):
    sender, password = os.getenv("EMAIL_ADDRESS"), os.getenv("EMAIL_PASSWORD")
    if not sender or not password:
        raise RuntimeError("The email mailbox is not configured for verification.")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = email
    message["Subject"] = "Your IITG chat verification code"
    message.set_content(f"Your chat verification code is {code}. It expires in {CHALLENGE_MINUTES} minutes.\n\nIf you did not request this, ignore this email.")
    with smtplib.SMTP_SSL(os.getenv("SMTP_HOST", "smtp.gmail.com"), int(os.getenv("SMTP_PORT", "465")), timeout=15) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)


def request_code(email, lead_key="", send=_send_code):
    email = clean_email(email)
    if not email:
        raise ValueError("Enter a valid email address.")
    rate_key = "rate:" + _digest(email)
    last = _get(rate_key)
    if last and (_now() - datetime.fromisoformat(last[5])).total_seconds() < 60:
        raise ValueError("Please wait a minute before requesting another code.")
    code = f"{secrets.randbelow(1000000):06d}"
    challenge = secrets.token_urlsafe(24)
    _put("code:" + _digest(challenge), "code", email,
         _digest(code + ":" + challenge) + ":" + lead_key,
         _now() + timedelta(minutes=CHALLENGE_MINUTES))
    send(email, code)
    _put(rate_key, "rate", email, "", _now() + timedelta(minutes=1))
    return challenge


def verify_code(challenge, code):
    if not challenge or not re.fullmatch(r"\d{6}", str(code or "")):
        return None
    key = "code:" + _digest(challenge)
    with _db() as (conn, p):
        with conn.cursor() if p == "%s" else _sqlite_cursor(conn) as cur:
            if p == "?":
                cur.execute("BEGIN IMMEDIATE")
            lock = " FOR UPDATE" if p == "%s" else ""
            cur.execute(f"SELECT kind,email,lead_key,expires_at,attempts FROM chat_access WHERE access_key = {p}{lock}", (key,))
            row = cur.fetchone()
            if not row or row[0] != "code" or datetime.fromisoformat(row[3]) <= _now() or row[4] >= 5:
                return None
            cur.execute(f"UPDATE chat_access SET attempts = attempts + 1 WHERE access_key = {p}", (key,))
            expected, _, identity = row[2].partition(":")
            if not hmac.compare_digest(expected, _digest(str(code) + ":" + challenge)):
                return None
            cur.execute(f"DELETE FROM chat_access WHERE access_key = {p}", (key,))
            return {"email": row[1], "lead_key": identity}


def match_lead(email, identity=""):
    """Return exactly one current CRM lead; a changed invite address fails closed."""
    leads = crm_storage.load_crm_table("leads.csv")
    if leads.empty or "Email" not in leads:
        return None
    matched = [row for row in leads.to_dict("records")
               if clean_email(row.get("Email")) == email
               and (not identity or prospect_key(row) == identity)]
    return matched[0] if len(matched) == 1 else None


def bind_chat_identity(lead):
    """Give an existing lead one stable internal chat ID without changing consent."""
    identity = prospect_key(lead)
    email = clean_email(lead.get("Email"))

    def change(current):
        matches = [(key, row) for key, row in current.items()
                   if prospect_key(row) == identity and clean_email(row.get("Email")) == email]
        if len(matches) != 1:
            return None, {}
        key, row = matches[0]
        if not crm_storage.lead_cell(row.get("Visitor ID")):
            row = {**row, "Visitor ID": "CHAT-" + secrets.token_hex(12).upper()}
            current[key] = row
            return row, {key: row}
        return row, {}

    result, _ = crm_storage._write_leads(change)
    return result


def bind_public_visitor(visitor, email):
    visitor_id = crm_storage.lead_cell(visitor.get("Visitor ID"))
    def change(current):
        matches = [(key, row) for key, row in current.items()
                   if crm_storage.lead_cell(row.get("Visitor ID")) == visitor_id]
        if len(matches) != 1:
            return None, {}
        key, row = matches[0]
        updated = {**row, "Email": email, "Lead Source": "AI Chat — Verified Email",
                   "Email Verified At": _now().isoformat()}
        current[key] = updated
        return updated, {key: updated}
    result, _ = crm_storage._write_leads(change)
    return result
