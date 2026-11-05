"""Additive cross-module records. PostgreSQL in production, explicit SQLite in tests.

Referral links are attribution only, never authentication or outreach consent.
No network services, migrations, workers, or delivery run on module import.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
import uuid


class IntegrationError(ValueError):
    pass


class Conflict(IntegrationError):
    pass


class NotFound(IntegrationError):
    pass


def enabled():
    return os.getenv("INTEGRATION_ENABLED", "").casefold() in {"true", "1", "yes"}


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value, limit=500):
    if not isinstance(value, str) or len(value) > limit:
        raise IntegrationError("Expected a text value within the allowed length.")
    return value.strip()


def _email(value):
    value = _text(value, 254).casefold()
    if value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        raise IntegrationError("Enter a valid email address.")
    return value


def _phone(value):
    value = re.sub(r"[\s().-]", "", _text(value, 40))
    if value and not re.fullmatch(r"\+[1-9]\d{7,14}", value):
        raise IntegrationError("Phone numbers must include the country code, for example +919876543210.")
    return value


def build_chat_url(base_url, token):
    parts = urlsplit(base_url)
    local = parts.hostname in {"localhost", "127.0.0.1", "::1"}
    if (parts.scheme != "https" and not (local and parts.scheme == "http")) or not parts.hostname:
        raise IntegrationError("The chat URL must use HTTPS, or HTTP on localhost.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise IntegrationError("Use the base customer chat URL without credentials or query parameters.")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode({"chat_ref": token}), ""))


def _execute(conn, sql, params=()):
    cursor = conn.cursor()
    cursor.execute(sql.replace("%s", "?") if isinstance(conn, sqlite3.Connection) else sql, params)
    return cursor


def _schema(conn):
    if not isinstance(conn, sqlite3.Connection):
        _execute(conn, "SELECT pg_advisory_xact_lock(738214051)").close()
    statements = (
        "CREATE TABLE IF NOT EXISTS integration_records (kind TEXT NOT NULL, record_id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (kind, record_id))",
        "CREATE TABLE IF NOT EXISTS integration_clock (clock_id INTEGER PRIMARY KEY, revision BIGINT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS integration_changes (revision BIGINT PRIMARY KEY, topic TEXT NOT NULL, entity_id TEXT NOT NULL, occurred_at TEXT NOT NULL)",
        "INSERT INTO integration_clock (clock_id, revision) VALUES (1, 0) ON CONFLICT (clock_id) DO NOTHING",
    )
    for sql in statements:
        _execute(conn, sql).close()


def _next_revision(conn):
    cursor = _execute(conn, "UPDATE integration_clock SET revision = revision + 1 WHERE clock_id = 1 RETURNING revision")
    revision = cursor.fetchone()[0]
    cursor.close()
    return revision


def _event(conn, revision, topic, entity_id):
    _execute(conn, "INSERT INTO integration_changes (revision, topic, entity_id, occurred_at) VALUES (%s, %s, %s, %s)",
             (revision, topic, entity_id, _now())).close()


def publish_crm_change(connection, topic, entity_id):
    """Append a durable notification in the SAME PostgreSQL transaction as a CRM edit."""
    if enabled():
        _schema(connection)
        _event(connection, _next_revision(connection), topic, entity_id)


class SharedStore:
    def __init__(self, database_url=None, sqlite_path=None, clock=time.time):
        if bool(database_url) == bool(sqlite_path):
            raise IntegrationError("Configure one PostgreSQL database or an explicit local test database.")
        self.database_url = database_url
        self.sqlite_path = str(sqlite_path) if sqlite_path else None
        self.clock = clock

    def connect(self):
        if self.database_url:
            import psycopg2
            return psycopg2.connect(self.database_url, connect_timeout=8)
        conn = sqlite3.connect(self.sqlite_path, timeout=15)
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def initialize(self):
        with self.transaction() as (conn, _):
            _schema(conn)

    @contextmanager
    def transaction(self, write=False):
        conn = self.connect()
        try:
            # Lock the shared revision row before EVERY integration mutation.
            # Revision order therefore follows commit order, preventing cursor
            # consumers missing an earlier transaction that commits later.
            revision = _next_revision(conn) if write else None
            yield conn, revision
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _get(conn, kind, identity):
        cursor = _execute(conn, "SELECT body FROM integration_records WHERE kind = %s AND record_id = %s", (kind, identity))
        row = cursor.fetchone()
        cursor.close()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _put(conn, kind, identity, body):
        _execute(conn, "INSERT INTO integration_records (kind, record_id, body) VALUES (%s, %s, %s) ON CONFLICT (kind, record_id) DO UPDATE SET body = EXCLUDED.body",
                 (kind, identity, _json(body))).close()

    @staticmethod
    def _all(conn, kind):
        cursor = _execute(conn, "SELECT body FROM integration_records WHERE kind = %s ORDER BY record_id", (kind,))
        result = [json.loads(row[0]) for row in cursor.fetchall()]
        cursor.close()
        return result

    def contacts(self):
        with self.transaction() as (conn, _):
            return self._all(conn, "contact")

    def contact(self, contact_id):
        with self.transaction() as (conn, _):
            record = self._get(conn, "contact", contact_id)
            if not record:
                raise NotFound("Contact not found.")
            return record

    def upsert_contact(self, source, external_id, fields, expected_version=None):
        source, external_id = _text(source, 80), _text(external_id, 200)
        if not source or not external_id or not isinstance(fields, dict):
            raise IntegrationError("Source, external_id and contact fields are required.")
        if set(fields) - {"name", "email", "phone", "whatsapp", "company", "job_title", "qualification"}:
            raise IntegrationError("Unsupported contact fields; contact preferences require a separate update.")
        clean = {key: (_email(value) if key == "email" else _phone(value) if key in {"phone", "whatsapp"} else _text(value))
                 for key, value in fields.items()}
        if clean.get("qualification", "pending") not in {"pending", "qualified", "unqualified"}:
            raise IntegrationError("Qualification must be pending, qualified, or unqualified.")
        source_key = _json([source, external_id])
        with self.transaction(write=True) as (conn, revision):
            mapping = self._get(conn, "source", source_key)
            matches = {mapping["contact_id"]} if mapping else set()
            for key in ("email", "phone", "whatsapp"):
                if clean.get(key):
                    identity = self._get(conn, "identity", _json([key, clean[key]]))
                    if identity:
                        matches.add(identity["contact_id"])
            if len(matches) > 1:
                raise Conflict("The email and phone match different contacts; staff must review them.")
            contact_id = next(iter(matches), "CON-" + uuid.uuid4().hex)
            prior = self._get(conn, "contact", contact_id)
            if not prior and not (clean.get("email") or clean.get("phone") or clean.get("whatsapp")):
                raise IntegrationError("A new prospect needs an email address or international phone number.")
            if expected_version is not None and (prior or {}).get("version", 0) != expected_version:
                raise Conflict("The contact changed. Reload it before saving.")
            record = deepcopy(prior) if prior else {
                "contact_id": contact_id, "name": "", "email": "", "phone": "", "whatsapp": "", "company": "", "job_title": "",
                "qualification": "pending", "preferences": {"channels": [], "consent": False},
                "version": 0, "created_at": _now(),
            }
            for key, value in clean.items():
                # Blank imports never erase an existing contact field.
                if value:
                    record[key] = value
            for key in ("email", "phone", "whatsapp"):
                old = (prior or {}).get(key)
                if old and old != record.get(key):
                    _execute(conn, "DELETE FROM integration_records WHERE kind = %s AND record_id = %s", ("identity", _json([key, old]))).close()
                if record.get(key):
                    self._put(conn, "identity", _json([key, record[key]]), {"contact_id": contact_id})
            self._put(conn, "source", source_key, {"contact_id": contact_id})
            if prior != record:
                record.update(version=record["version"] + 1, updated_at=_now())
                self._put(conn, "contact", contact_id, record)
                _event(conn, revision, "contact.updated", contact_id)
            return record

    def set_preferences(self, contact_id, channels, consent, evidence):
        allowed = {"email", "phone", "sms", "whatsapp"}
        if not isinstance(channels, list) or not all(isinstance(c, str) and c in allowed for c in channels):
            raise IntegrationError("Choose supported contact channels.")
        if not isinstance(consent, bool) or not isinstance(evidence, str) or not evidence.strip():
            raise IntegrationError("A boolean consent decision and its source/evidence are required.")
        with self.transaction(write=True) as (conn, revision):
            contact = self._get(conn, "contact", contact_id)
            if not contact:
                raise NotFound("Contact not found.")
            channels = sorted(set(channels)) if consent else []
            if "email" in channels and not contact.get("email"):
                raise IntegrationError("Email follow-up requires an email address.")
            if set(channels) & {"phone", "sms"} and not contact.get("phone"):
                raise IntegrationError("Phone and SMS follow-up require an international phone number.")
            if "whatsapp" in channels and not contact.get("whatsapp"):
                raise IntegrationError("WhatsApp follow-up requires a WhatsApp number.")
            prior_preferences = contact["preferences"]
            if all(prior_preferences.get(key) == value for key, value in {
                "channels": channels, "consent": consent, "evidence": evidence.strip(),
            }.items()):
                return contact
            contact.update(preferences={"channels": channels, "consent": consent, "evidence": _text(evidence, 1000), "recorded_at": _now()},
                           version=contact["version"] + 1, updated_at=_now())
            self._put(conn, "contact", contact_id, contact)
            _event(conn, revision, "contact.preferences_changed", contact_id)
            return contact

    def create_link(self, contact_id, campaign_id, base_url, ttl_seconds=172800):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 60 <= ttl_seconds <= 604800:
            raise IntegrationError("Link lifetime must be between one minute and seven days.")
        campaign_id = _text(campaign_id, 200)
        token = secrets.token_urlsafe(32)
        link_id = hashlib.sha256(token.encode()).hexdigest()
        url = build_chat_url(base_url, token)
        with self.transaction(write=True) as (conn, revision):
            if not self._get(conn, "contact", contact_id):
                raise NotFound("Contact not found.")
            record = {"link_id": link_id, "contact_id": contact_id, "campaign_id": campaign_id,
                      "expires_at": int(self.clock()) + ttl_seconds, "revoked": False, "created_at": _now()}
            self._put(conn, "link", link_id, record)
            _event(conn, revision, "chat_link.created", contact_id)
        return {**record, "chat_url": url}

    def revoke_link(self, link_id):
        with self.transaction(write=True) as (conn, revision):
            record = self._get(conn, "link", link_id)
            if not record:
                raise NotFound("Link not found.")
            record["revoked"] = True
            self._put(conn, "link", link_id, record)
            _event(conn, revision, "chat_link.revoked", record["contact_id"])

    def open_session(self, visitor_id, referral=None):
        visitor_id = _text(visitor_id, 100)
        if not re.fullmatch(r"VIS-[A-Za-z0-9-]{6,80}", visitor_id):
            raise IntegrationError("A valid visitor ID is required.")
        with self.transaction(write=True) as (conn, revision):
            existing = self._get(conn, "session", visitor_id)
            link = None
            if referral is not None:
                if not isinstance(referral, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", referral):
                    raise NotFound("This chat link is invalid or expired.")
                link = self._get(conn, "link", hashlib.sha256(referral.encode()).hexdigest())
                if not link or link["revoked"] or link["expires_at"] <= self.clock():
                    raise NotFound("This chat link is invalid or expired.")
                if existing and existing.get("contact_id") != link["contact_id"]:
                    raise Conflict("Start a new visitor session for a different personalised link.")
            if existing:
                return existing
            record = {"conversation_id": "CHAT-" + uuid.uuid4().hex, "visitor_id": visitor_id,
                      "contact_id": link["contact_id"] if link else None,
                      "association": "email_link_matched" if link else "anonymous",
                      "campaign_id": link["campaign_id"] if link else None, "started_at": _now()}
            self._put(conn, "session", visitor_id, record)
            _event(conn, revision, "conversation.started", record["conversation_id"])
            return record

    def save_message(self, visitor_id, message):
        if not isinstance(message, dict) or set(message) - {"message_id", "role", "content", "timestamp", "lead"}:
            raise IntegrationError("Unsupported message fields.")
        message_id = _text(message.get("message_id", ""), 100)
        if not message_id or message.get("role") not in {"user", "assistant"}:
            raise IntegrationError("A message ID and user/assistant role are required.")
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip() or len(content) > 50000:
            raise IntegrationError("A message cannot be empty.")
        with self.transaction(write=True) as (conn, revision):
            session = self._get(conn, "session", visitor_id)
            if not session:
                raise NotFound("Conversation not found.")
            prior = self._get(conn, "message", message_id)
            record = {**message, "content": content, "visitor_id": visitor_id,
                      "conversation_id": session["conversation_id"], "contact_id": session["contact_id"],
                      "association": session["association"], "campaign_id": session["campaign_id"]}
            record.setdefault("timestamp", prior["timestamp"] if prior else _now())
            if prior:
                # A contact may have been shared after an earlier provisional
                # attribution. Retries preserve the original immutable message.
                if any(prior.get(key) != record.get(key) for key in (*message.keys(), "visitor_id", "conversation_id")):
                    raise Conflict("This message ID already belongs to another message.")
                return prior
            self._put(conn, "message", message_id, record)
            _event(conn, revision, "conversation.message_added", session["conversation_id"])
            return record

    def sessions(self):
        with self.transaction() as (conn, _):
            return self._all(conn, "session")

    def bind_self_reported_contact(self, visitor_id, contact_id):
        """Sharing details can replace a provisional match; this is not verified login."""
        with self.transaction(write=True) as (conn, revision):
            session = self._get(conn, "session", visitor_id)
            if not session or not self._get(conn, "contact", contact_id):
                raise NotFound("Conversation or contact not found.")
            if session.get("contact_id") == contact_id and session["association"] == "self_reported":
                return session
            session.update(contact_id=contact_id, association="self_reported")
            self._put(conn, "session", visitor_id, session)
            _event(conn, revision, "conversation.contact_shared", session["conversation_id"])
            return session

    def save_insight(self, visitor_id, insight):
        if not isinstance(insight, dict) or set(insight) - {"intent", "sentiment", "priority", "status"}:
            raise IntegrationError("Unsupported sales insight fields.")
        clean = {key: _text(value, 100) for key, value in insight.items() if value is not None}
        with self.transaction(write=True) as (conn, revision):
            session = self._get(conn, "session", visitor_id)
            if not session:
                raise NotFound("Conversation not found.")
            if session.get("insight") == clean:
                return session
            session.update(insight=clean, updated_at=_now())
            self._put(conn, "session", visitor_id, session)
            _event(conn, revision, "conversation.insight_updated", session["conversation_id"])
            return session

    def messages(self):
        with self.transaction() as (conn, _):
            return sorted(self._all(conn, "message"), key=lambda row: (row["timestamp"], row["message_id"]))

    def record_communication(self, actor, external_id, data):
        if not isinstance(data, dict) or set(data) - {"contact_id", "channel", "status", "provider_id", "campaign_id"}:
            raise IntegrationError("Unsupported communication fields.")
        if data.get("channel") not in {"email", "phone", "sms", "whatsapp"} or data.get("status") not in {"sent", "delivered", "failed", "uncertain", "replied", "bounced"}:
            raise IntegrationError("Invalid communication channel or outcome.")
        clean = {key: _text(value, 200) for key, value in data.items()}
        actor, external_id = _text(actor, 80), _text(external_id, 200)
        if not actor or not external_id:
            raise IntegrationError("A source module and unique communication event ID are required.")
        identity = _json([actor, external_id])
        with self.transaction(write=True) as (conn, revision):
            prior = self._get(conn, "communication", identity)
            if prior:
                if prior["data"] != clean:
                    raise Conflict("This event ID was already used with different details.")
                return prior
            if not self._get(conn, "contact", clean.get("contact_id", "")):
                raise NotFound("Contact not found.")
            record = {"event_id": identity, "data": clean, "recorded_at": _now()}
            self._put(conn, "communication", identity, record)
            _event(conn, revision, "communication.recorded", clean["contact_id"])
            return record

    def communications(self, contact_id=None):
        with self.transaction() as (conn, _):
            rows = self._all(conn, "communication")
            return sorted([row for row in rows if contact_id is None or row["data"]["contact_id"] == contact_id],
                          key=lambda row: row["recorded_at"])

    def reserve_email(self, contact_id, campaign_id, event_id, policy, reviewed=False):
        """One durable dispatch slot; uncertain sends never become automatic retries.

        This coordinates connected senders only. Legacy/manual providers must
        also adopt the reservation contract before claiming global deduplication.
        """
        campaign_id, event_id = _text(campaign_id, 200), _text(event_id, 200)
        if not campaign_id or not event_id or reviewed is not True:
            raise IntegrationError("A stable campaign, event ID and staff review are required.")
        if policy.get("enabled") is not True or policy.get("channels", {}).get("email") is not True:
            raise Conflict("Email outreach is paused in the shared policy.")
        from zoneinfo import ZoneInfo
        hour = datetime.fromtimestamp(self.clock(), timezone.utc).astimezone(ZoneInfo("Asia/Kolkata")).hour
        if not int(policy.get("quiet_start_hour", 9)) <= hour < int(policy.get("quiet_end_hour", 18)):
            raise Conflict("The shared contact window is closed.")
        with self.transaction(write=True) as (conn, revision):
            contact = self._get(conn, "contact", contact_id)
            if not contact:
                raise NotFound("Contact not found.")
            prefs = contact["preferences"]
            if contact["qualification"] != "qualified" or not contact.get("email") or prefs.get("consent") is not True or "email" not in prefs.get("channels", []):
                raise Conflict("This prospect is not qualified with recorded email contact permission.")
            identity = _json([contact_id, campaign_id, "email"])
            if self._get(conn, "delivery", identity):
                raise Conflict("This recipient and campaign already have a dispatch reservation. Reconcile before retrying.")
            attempts = [row for row in self._all(conn, "delivery") if row["contact_id"] == contact_id]
            recent = [row for row in attempts if row["reserved_at_epoch"] > self.clock() - 30 * 86400]
            if len(recent) >= int(policy.get("max_attempts", 3)):
                raise Conflict("The shared outreach attempt limit has been reached.")
            if attempts and self.clock() - max(row["reserved_at_epoch"] for row in attempts) < int(policy.get("cooldown_days", 2)) * 86400:
                raise Conflict("The shared outreach cooldown has not finished.")
            record = {"reservation_id": "SEND-" + uuid.uuid4().hex, "contact_id": contact_id,
                      "campaign_id": campaign_id, "event_id": event_id, "channel": "email",
                      "reserved_at_epoch": self.clock(), "reserved_at": _now()}
            self._put(conn, "delivery", identity, record)
            _event(conn, revision, "delivery.reserved", contact_id)
            return record

    def changes(self, after=0, limit=100):
        if isinstance(after, bool) or not isinstance(after, int) or after < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise IntegrationError("Invalid change cursor or page size.")
        with self.transaction() as (conn, _):
            cursor = _execute(conn, "SELECT revision, topic, entity_id, occurred_at FROM integration_changes WHERE revision > %s ORDER BY revision LIMIT %s", (after, limit))
            rows = [dict(zip(("revision", "topic", "entity_id", "occurred_at"), row)) for row in cursor.fetchall()]
            cursor.close()
            return {"changes": rows, "next_cursor": rows[-1]["revision"] if rows else after}

    def recent_changes(self, limit=25):
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise IntegrationError("Invalid change page size.")
        with self.transaction() as (conn, _):
            cursor = _execute(conn, "SELECT revision, topic, entity_id, occurred_at FROM integration_changes ORDER BY revision DESC LIMIT %s", (limit,))
            rows = [dict(zip(("revision", "topic", "entity_id", "occurred_at"), row)) for row in cursor.fetchall()]
            cursor.close()
            return rows


@lru_cache(maxsize=1)
def get_store():
    if not enabled():
        raise IntegrationError("Shared integration is not enabled.")
    database_url = os.getenv("DATABASE_URL", "").strip()
    local_path = os.getenv("INTEGRATION_SQLITE_PATH", "").strip()
    if database_url:
        store = SharedStore(database_url=database_url)
    elif local_path:
        store = SharedStore(sqlite_path=Path(local_path).expanduser())
    else:
        raise IntegrationError("Set DATABASE_URL for shared hosting or INTEGRATION_SQLITE_PATH for local testing.")
    # A PostgreSQL failure never silently switches to local storage.
    store.initialize()
    return store
