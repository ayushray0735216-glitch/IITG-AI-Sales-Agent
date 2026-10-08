"""Shared CRM storage helpers for CSV fallback + Render PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import Json


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.getenv("CRM_DATA_DIR", str(PROJECT_DIR))
).expanduser()

DATABASE_URL = os.getenv("DATABASE_URL")


def crm_path(filename: str) -> str:
    """Return an absolute path for a CRM file."""
    if Path(filename).name != filename:
        raise ValueError("CRM filenames must not include directory components")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return str(DATA_DIR / filename)


def ensure_seed_file(filename: str) -> str:
    """Copy bundled seed file only when configured data file is absent."""
    target = Path(crm_path(filename))
    seed = PROJECT_DIR / filename

    if (
        not target.exists()
        and seed.exists()
        and target.resolve() != seed.resolve()
    ):
        shutil.copy2(seed, target)

    return str(target)


def database_enabled() -> bool:
    return bool(DATABASE_URL)


def get_connection():
    """Open a PostgreSQL connection when DATABASE_URL is configured."""
    if not DATABASE_URL:
        return None

    return psycopg2.connect(DATABASE_URL)


def initialize_database() -> None:
    """Create CRM tables and synchronize existing CSV seed data once."""
    if not database_enabled():
        return

    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_leads (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sales_activity (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_history (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS email_inbox (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_offerings (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_settings (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_automation_actions (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_communication_events (
                    event_id TEXT PRIMARY KEY,
                    provider_id TEXT,
                    data JSONB NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS crm_communication_events_provider_idx
                ON crm_communication_events (provider_id)
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_contact_suppressions (
                    suppression_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS crm_automation_actions_status_idx
                ON crm_automation_actions ((lower(data ->> 'status')))
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS crm_automation_actions_lead_idx
                ON crm_automation_actions ((data ->> 'lead_key'))
                """
            )

        conn.commit()

        # Seed empty tables from bundled/local CSV files.
        sync_csv_to_database("leads.csv", only_if_empty=True)
        sync_csv_to_database(
            "sales_activity.csv",
            only_if_empty=True,
        )
        sync_csv_to_database(
            "conversation_history.csv",
            only_if_empty=True,
        )
        sync_csv_to_database("email_inbox.csv", only_if_empty=True)
        sync_csv_to_database("offerings.csv", only_if_empty=True)

        # Once PostgreSQL contains data, recreate the local CSV cache.
        sync_database_to_csv("leads.csv")
        sync_database_to_csv("sales_activity.csv")
        sync_database_to_csv("conversation_history.csv")
        sync_database_to_csv("email_inbox.csv")
        sync_database_to_csv("offerings.csv")

    finally:
        conn.close()


def _lead_key(row: dict) -> str:
    email = row.get("Email")

    if email is not None and str(email).strip():
        return "email:" + str(email).strip().casefold()

    name = row.get("Name")

    if name is not None and str(name).strip():
        return "name:" + str(name).strip().casefold()

    return _generic_key(row)


def _generic_key(row: dict) -> str:
    payload = json.dumps(
        row,
        sort_keys=True,
        default=str,
        ensure_ascii=False,
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _activity_key(row: dict) -> str:
    activity_id = row.get("activity_id")
    if activity_id is not None and str(activity_id).strip():
        return "activity:" + str(activity_id).strip().casefold()
    return _generic_key(row)


def _email_inbox_key(row: dict) -> str:
    message_id = row.get("message_id")
    if message_id is not None and str(message_id).strip():
        return "message:" + str(message_id).strip().casefold()
    return _generic_key(row)


def _offering_key(row: dict) -> str:
    offering_id = row.get("Offering ID")
    if offering_id is not None and str(offering_id).strip():
        return "offering:" + str(offering_id).strip().casefold()
    return _generic_key(row)


def _table_name(filename: str) -> str:
    if filename == "leads.csv":
        return "crm_leads"

    if filename == "sales_activity.csv":
        return "sales_activity"

    if filename == "conversation_history.csv":
        return "conversation_history"

    if filename == "email_inbox.csv":
        return "email_inbox"

    if filename == "offerings.csv":
        return "crm_offerings"

    raise ValueError(f"Unsupported CRM file: {filename}")


def _row_key(filename: str, row: dict) -> str:
    if filename == "leads.csv":
        return _lead_key(row)

    if filename == "email_inbox.csv":
        return _email_inbox_key(row)

    if filename == "offerings.csv":
        return _offering_key(row)

    return _activity_key(row)


def sync_csv_to_database(
    filename: str,
    only_if_empty: bool = False,
) -> None:
    """Copy CSV rows into PostgreSQL without deleting existing DB rows."""
    if not database_enabled():
        return

    path = Path(crm_path(filename))

    if not path.exists():
        return

    df = pd.read_csv(path)

    if df.empty:
        return

    table = _table_name(filename)
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            if only_if_empty:
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                if cur.fetchone()[0] > 0:
                    return

            # The offerings CSV is a complete catalog snapshot. Replacing the
            # table keeps deletions and deactivations in sync across services.
            if filename == "offerings.csv" and not only_if_empty:
                cur.execute("DELETE FROM crm_offerings")

            for _, row in df.iterrows():
                data = {
                    str(k): (
                        None if pd.isna(v) else v
                    )
                    for k, v in row.to_dict().items()
                }

                key = _row_key(filename, data)

                cur.execute(
                    f"""
                    INSERT INTO {table} (row_key, data)
                    VALUES (%s, %s)
                    ON CONFLICT (row_key)
                    DO UPDATE SET data = EXCLUDED.data
                    """,
                    (key, Json(data)),
                )

        conn.commit()

    finally:
        conn.close()


def sync_database_to_csv(filename: str) -> None:
    """Rebuild local CSV cache from PostgreSQL when DB has data."""
    if not database_enabled():
        return

    table = _table_name(filename)
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT data FROM {table} ORDER BY row_key"
            )

            rows = [row[0] for row in cur.fetchall()]

        if not rows:
            return

        pd.DataFrame(rows).to_csv(
            crm_path(filename),
            index=False,
        )

    finally:
        conn.close()


def sync_after_csv_write(filename: str) -> None:
    """Push a newly written CSV into PostgreSQL."""
    if not database_enabled():
        return

    sync_csv_to_database(filename)


def load_offerings_data() -> pd.DataFrame:
    """Read the latest offerings catalog from shared storage, or CSV fallback."""
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_offerings ORDER BY row_key")
                rows = [row[0] for row in cur.fetchall()]
            # An empty table is also authoritative: it may mean that every
            # offering was removed or deactivated. Never revive an old local cache.
            return pd.DataFrame(rows)
        finally:
            conn.close()

    path = Path(crm_path("offerings.csv"))
    if path.exists():
        try:
            return pd.read_csv(path)
        except (OSError, pd.errors.ParserError, UnicodeDecodeError):
            pass
    return pd.DataFrame()


def save_offerings_data(offerings: pd.DataFrame) -> None:
    """Persist an offerings catalog snapshot for both app and voice services."""
    path = crm_path("offerings.csv")
    offerings.to_csv(path, index=False)

    if not database_enabled():
        return

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crm_offerings")
            for _, row in offerings.iterrows():
                data = {
                    str(key): None if pd.isna(value) else value
                    for key, value in row.to_dict().items()
                }
                cur.execute(
                    """
                    INSERT INTO crm_offerings (row_key, data)
                    VALUES (%s, %s)
                    ON CONFLICT (row_key)
                    DO UPDATE SET data = EXCLUDED.data
                    """,
                    (_offering_key(data), Json(data)),
                )
        conn.commit()
    finally:
        conn.close()


AUTOPILOT_DEFAULTS = {
    "enabled": False,
    "channels": {"email": True, "phone": True, "sms": False, "whatsapp": False},
    "max_attempts": 3,
    "cooldown_days": 2,
    "quiet_start_hour": 9,
    "quiet_end_hour": 18,
    "require_contact_consent": True,
    "require_staff_review": True,
    "automatic_delivery": False,
}


def load_autopilot_settings() -> dict:
    """Load the shared autopilot policy, falling back to local JSON storage."""
    data = None
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_settings WHERE row_key = %s", ("autopilot",))
                row = cur.fetchone()
            if row:
                data = row[0]
        finally:
            conn.close()
    else:
        settings_path = Path(crm_path("autopilot_settings.json"))
        if settings_path.exists():
            try:
                with settings_path.open("r", encoding="utf-8") as settings_file:
                    data = json.load(settings_file)
            except (OSError, json.JSONDecodeError):
                data = None

    settings = dict(AUTOPILOT_DEFAULTS)
    settings["channels"] = dict(AUTOPILOT_DEFAULTS["channels"])
    if isinstance(data, dict):
        settings.update({key: value for key, value in data.items() if key != "channels"})
        channels = data.get("channels")
        if isinstance(channels, dict):
            settings["channels"].update(channels)
    return _normalize_autopilot_settings(settings)


def _normalize_autopilot_settings(settings: dict) -> dict:
    normalized = dict(AUTOPILOT_DEFAULTS)
    normalized["channels"] = dict(AUTOPILOT_DEFAULTS["channels"])
    if isinstance(settings, dict):
        normalized.update({key: value for key, value in settings.items() if key != "channels"})
        if isinstance(settings.get("channels"), dict):
            normalized["channels"].update(settings["channels"])

    def as_bool(value, fallback=False):
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            if value.strip().casefold() in {"true", "yes", "1", "on"}:
                return True
            if value.strip().casefold() in {"false", "no", "0", "off", ""}:
                return False
        return fallback

    def bounded_int(value, fallback, lower, upper):
        try:
            result = int(value)
        except (TypeError, ValueError, OverflowError):
            return fallback
        return max(lower, min(upper, result))

    normalized["enabled"] = as_bool(normalized["enabled"])
    normalized["automatic_delivery"] = as_bool(normalized.get("automatic_delivery"))
    normalized["channels"] = {
        "email": as_bool(normalized["channels"].get("email"), True),
        "phone": as_bool(normalized["channels"].get("phone"), True),
        # SMS and WhatsApp remain unavailable until their integrations exist.
        "sms": False,
        "whatsapp": False,
    }
    normalized["max_attempts"] = bounded_int(normalized["max_attempts"], 3, 1, 5)
    normalized["cooldown_days"] = bounded_int(normalized["cooldown_days"], 2, 1, 30)
    normalized["quiet_start_hour"] = bounded_int(normalized["quiet_start_hour"], 9, 7, 12)
    normalized["quiet_end_hour"] = bounded_int(normalized["quiet_end_hour"], 18, 14, 21)
    if normalized["quiet_end_hour"] <= normalized["quiet_start_hour"]:
        normalized["quiet_start_hour"] = 9
        normalized["quiet_end_hour"] = 18
    # Contact consent is always mandatory. An admin may explicitly enable automatic delivery.
    normalized["require_contact_consent"] = True
    normalized["require_staff_review"] = not normalized["automatic_delivery"]
    return normalized


def save_autopilot_settings(settings: dict) -> dict:
    """Persist validated autopilot policy for all app services."""
    saved = _normalize_autopilot_settings(settings)

    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO crm_settings (row_key, data)
                    VALUES (%s, %s)
                    ON CONFLICT (row_key)
                    DO UPDATE SET data = EXCLUDED.data
                    """,
                    ("autopilot", Json(saved)),
                )
            conn.commit()
        finally:
            conn.close()
    else:
        settings_path = Path(crm_path("autopilot_settings.json"))
        with settings_path.open("w", encoding="utf-8") as settings_file:
            json.dump(saved, settings_file, indent=2)

    return saved


def _sqlite_autopilot_connection() -> sqlite3.Connection:
    """Open the durable local Autopilot store (used when PostgreSQL is absent)."""
    conn = sqlite3.connect(crm_path("autopilot_queue.sqlite3"), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS autopilot_actions (
            task_id TEXT PRIMARY KEY,
            lead_key TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Needs review',
            data TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS autopilot_actions_status_idx ON autopilot_actions(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS autopilot_actions_lead_idx ON autopilot_actions(lead_key)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS autopilot_local_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS communication_events (
            event_id TEXT PRIMARY KEY,
            provider_id TEXT,
            data TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS communication_events_provider_idx ON communication_events(provider_id)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS contact_suppressions (
            suppression_key TEXT PRIMARY KEY,
            data TEXT NOT NULL
        )"""
    )
    _migrate_legacy_autopilot_csv(conn)
    return conn


def _json_safe_row(row: dict) -> dict:
    safe = {}
    for key, value in row.items():
        try:
            missing = value is None or bool(pd.isna(value))
        except (TypeError, ValueError):
            missing = value is None
        safe[str(key)] = None if missing else value
    return safe


def _migrate_legacy_autopilot_csv(conn: sqlite3.Connection) -> None:
    """Import the former CSV queue once, preserving existing task decisions."""
    marker = conn.execute(
        "SELECT value FROM autopilot_local_meta WHERE key = 'csv_queue_migrated'"
    ).fetchone()
    if marker:
        return
    legacy_path = Path(crm_path("autopilot_actions.csv"))
    if legacy_path.exists():
        try:
            legacy = pd.read_csv(legacy_path)
        except (OSError, pd.errors.ParserError, UnicodeDecodeError):
            legacy = pd.DataFrame()
        for _, series in legacy.iterrows():
            task = _json_safe_row(series.to_dict())
            task_id = str(task.get("task_id") or "").strip()
            if not task_id:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO autopilot_actions(task_id, lead_key, status, data) VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    str(task.get("lead_key") or ""),
                    str(task.get("status") or "Needs review"),
                    json.dumps(task, ensure_ascii=False, default=str),
                ),
            )
    conn.execute(
        "INSERT OR REPLACE INTO autopilot_local_meta(key, value) VALUES ('csv_queue_migrated', 'yes')"
    )
    conn.commit()


def load_crm_table(filename: str) -> pd.DataFrame:
    """Read leads or sales activity from shared PostgreSQL or local CSV storage."""
    tables = {"leads.csv": "crm_leads", "sales_activity.csv": "sales_activity"}
    if filename not in tables:
        raise ValueError("Only leads.csv and sales_activity.csv can be loaded by the worker.")
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT data FROM {tables[filename]} ORDER BY row_key")
                return pd.DataFrame([row[0] for row in cur.fetchall()])
        finally:
            conn.close()
    path = Path(ensure_seed_file(filename))
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        return pd.DataFrame()


def append_sales_activity(activity: dict) -> bool:
    """Append one idempotent activity to the CRM timeline storage."""
    payload = _json_safe_row(activity)
    required = ("timestamp", "lead", "intent", "sentiment", "priority", "action")
    for column in required:
        payload.setdefault(column, "")
    payload.setdefault("activity_id", "")
    activity_id = str(payload.get("activity_id") or "").strip()

    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if activity_id:
                    cur.execute(
                        "SELECT 1 FROM sales_activity WHERE row_key = %s",
                        ("activity:" + activity_id.casefold(),),
                    )
                    if cur.fetchone():
                        return False
                row_key = _row_key("sales_activity.csv", payload)
                cur.execute(
                    "INSERT INTO sales_activity (row_key, data) VALUES (%s, %s) "
                    "ON CONFLICT (row_key) DO NOTHING",
                    (row_key, Json(payload)),
                )
                inserted = cur.rowcount > 0
            conn.commit()
        finally:
            conn.close()
        if inserted:
            sync_database_to_csv("sales_activity.csv")
        return inserted

    path = Path(ensure_seed_file("sales_activity.csv"))
    try:
        existing = pd.read_csv(path) if path.exists() else pd.DataFrame()
    except (OSError, pd.errors.ParserError, UnicodeDecodeError):
        existing = pd.DataFrame()
    if activity_id and "activity_id" in existing.columns:
        known_ids = existing["activity_id"].fillna("").astype(str).str.strip()
        if known_ids.eq(activity_id).any():
            return False
    for column in payload:
        if column not in existing.columns:
            existing[column] = ""
    for column in existing.columns:
        payload.setdefault(column, "")
    updated = pd.concat([existing, pd.DataFrame([payload], columns=existing.columns)], ignore_index=True)
    updated.to_csv(path, index=False)
    return True


def record_communication_event(event: dict) -> str:
    """Upsert a provider delivery/call event so asynchronous callbacks are auditable."""
    payload = _json_safe_row(event)
    provider_id = str(payload.get("provider_id") or "").strip()
    event_id = str(payload.get("event_id") or provider_id or "").strip()
    if not event_id:
        event_id = _generic_key(payload)
    payload["event_id"] = event_id
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO crm_communication_events(event_id, provider_id, data)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (event_id) DO UPDATE SET
                       provider_id = COALESCE(EXCLUDED.provider_id, crm_communication_events.provider_id),
                       data = crm_communication_events.data || EXCLUDED.data""",
                    (event_id, provider_id or None, Json(payload)),
                )
            conn.commit()
        finally:
            conn.close()
        return event_id
    conn = _sqlite_autopilot_connection()
    try:
        with conn:
            prior = conn.execute(
                "SELECT data FROM communication_events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if prior:
                merged = json.loads(prior["data"])
                merged.update(payload)
                payload = merged
            conn.execute(
                "INSERT OR REPLACE INTO communication_events(event_id, provider_id, data) VALUES (?, ?, ?)",
                (event_id, provider_id or None, json.dumps(payload, ensure_ascii=False, default=str)),
            )
        return event_id
    finally:
        conn.close()


def load_communication_events(limit: int = 500) -> pd.DataFrame:
    """Read recent outbound and provider status events for CRM audit views."""
    limit = max(1, min(int(limit), 5000))
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM crm_communication_events ORDER BY (data ->> 'updated_at') DESC LIMIT %s",
                    (limit,),
                )
                return pd.DataFrame([row[0] for row in cur.fetchall()])
        finally:
            conn.close()
    conn = _sqlite_autopilot_connection()
    try:
        rows = conn.execute(
            "SELECT data FROM communication_events ORDER BY rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return pd.DataFrame([json.loads(row["data"]) for row in rows])
    finally:
        conn.close()


def set_contact_suppression(destination: str, channel: str, reason: str, lead_key: str = "", source: str = "") -> dict:
    """Persist an opt-out, bounce, or invalid-address block for one contact channel."""
    normalized_destination = str(destination or "").strip().casefold()
    normalized_channel = str(channel or "").strip().casefold()
    if not normalized_destination or normalized_channel not in {"email", "phone", "whatsapp", "sms", "all"}:
        raise ValueError("A destination and supported channel are required for suppression.")
    key = f"{normalized_channel}:{normalized_destination}"
    payload = {
        "suppression_key": key,
        "destination": normalized_destination,
        "channel": normalized_channel,
        "reason": str(reason or "Contact suppressed").strip(),
        "lead_key": str(lead_key or "").strip(),
        "source": str(source or "").strip(),
        "active": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO crm_contact_suppressions(suppression_key, data) VALUES (%s, %s)
                       ON CONFLICT (suppression_key) DO UPDATE SET data = EXCLUDED.data""",
                    (key, Json(payload)),
                )
            conn.commit()
        finally:
            conn.close()
    else:
        conn = _sqlite_autopilot_connection()
        try:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO contact_suppressions(suppression_key, data) VALUES (?, ?)",
                    (key, json.dumps(payload, ensure_ascii=False)),
                )
        finally:
            conn.close()
    return payload


def load_contact_suppressions() -> pd.DataFrame:
    """Return active suppressed destinations for live send-time checks."""
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_contact_suppressions")
                rows = [row[0] for row in cur.fetchall()]
            return pd.DataFrame([row for row in rows if row.get("active", True)])
        finally:
            conn.close()
    conn = _sqlite_autopilot_connection()
    try:
        rows = conn.execute("SELECT data FROM contact_suppressions").fetchall()
        return pd.DataFrame(
            [row for row in (json.loads(item["data"]) for item in rows) if row.get("active", True)]
        )
    finally:
        conn.close()


def is_contact_suppressed(destination: str, channel: str) -> dict | None:
    """Return the matching block, including global blocks, if one is active."""
    normalized = str(destination or "").strip().casefold()
    if not normalized:
        return None
    records = load_contact_suppressions()
    if records.empty:
        return None
    matches = records[
        records["destination"].fillna("").astype(str).str.casefold().eq(normalized)
        & records["channel"].fillna("").astype(str).str.casefold().isin(
            {str(channel or "").strip().casefold(), "all"}
        )
    ]
    return matches.iloc[-1].to_dict() if not matches.empty else None


def load_autopilot_actions() -> pd.DataFrame:
    """Read the persistent Autopilot review queue from PostgreSQL or local SQLite."""
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_automation_actions ORDER BY row_key")
                rows = [row[0] for row in cur.fetchall()]
            return pd.DataFrame(rows)
        finally:
            conn.close()
    conn = _sqlite_autopilot_connection()
    try:
        rows = conn.execute(
            "SELECT data FROM autopilot_actions ORDER BY task_id"
        ).fetchall()
        return pd.DataFrame([json.loads(row["data"]) for row in rows])
    finally:
        conn.close()


def queue_autopilot_actions(actions: list[dict]) -> int:
    """Persist tasks idempotently using a database primary key as the dedupe guard."""
    if not actions:
        return 0
    if database_enabled():
        conn = get_connection()
        inserted = 0
        try:
            with conn.cursor() as cur:
                for action in actions:
                    task = _json_safe_row(action)
                    cur.execute(
                        """
                        INSERT INTO crm_automation_actions (row_key, data)
                        VALUES (%s, %s)
                        ON CONFLICT (row_key) DO NOTHING
                        """,
                        (str(task["task_id"]), Json(task)),
                    )
                    inserted += cur.rowcount
            conn.commit()
        finally:
            conn.close()
        return inserted
    conn = _sqlite_autopilot_connection()
    inserted = 0
    try:
        with conn:
            for action in actions:
                task = _json_safe_row(action)
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO autopilot_actions(task_id, lead_key, status, data) VALUES (?, ?, ?, ?)",
                    (
                        str(task["task_id"]),
                        str(task.get("lead_key") or ""),
                        str(task.get("status") or "Needs review"),
                        json.dumps(task, ensure_ascii=False, default=str),
                    ),
                )
                inserted += cursor.rowcount
        return inserted
    finally:
        conn.close()


def claim_autopilot_action(task_id: str) -> dict | None:
    """Atomically claim a pending queue row so separate processes cannot claim it twice."""
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    claimed_at = datetime.now(timezone.utc).isoformat()
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM crm_automation_actions WHERE row_key = %s FOR UPDATE",
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return None
                task = row[0]
                if str(task.get("status", "")).casefold() != "needs review":
                    conn.rollback()
                    return None
                if task.get("manual_retry_only"):
                    conn.rollback()
                    return None
                try:
                    attempts = int(float(task.get("delivery_attempts") or 0))
                except (TypeError, ValueError, OverflowError):
                    attempts = 1
                if attempts > 0:
                    conn.rollback()
                    return None
                task.update({"status": "Processing", "delivery_attempts": 1, "delivery_started_at": claimed_at})
                cur.execute(
                    "UPDATE crm_automation_actions SET data = %s WHERE row_key = %s",
                    (Json(task), task_id),
                )
            conn.commit()
            return task
        finally:
            conn.close()
    conn = _sqlite_autopilot_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT data FROM autopilot_actions WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        task = json.loads(row["data"])
        try:
            attempts = int(float(task.get("delivery_attempts") or 0))
        except (TypeError, ValueError, OverflowError):
            attempts = 1
        if (
            str(task.get("status", "")).casefold() != "needs review"
            or attempts > 0
            or task.get("manual_retry_only")
        ):
            conn.rollback()
            return None
        task.update({"status": "Processing", "delivery_attempts": 1, "delivery_started_at": claimed_at})
        conn.execute(
            "UPDATE autopilot_actions SET status = ?, data = ? WHERE task_id = ?",
            (task["status"], json.dumps(task, ensure_ascii=False, default=str), task_id),
        )
        conn.commit()
        return task
    finally:
        conn.close()


def finish_autopilot_action(task_id: str, status: str, outcome: str) -> bool:
    """Persist a processing task's outcome without silently resetting its attempt state."""
    task_id = str(task_id or "").strip()
    if not task_id:
        return False
    if status not in {"Completed", "Failed", "Blocked", "Needs review"}:
        raise ValueError("Unsupported Autopilot result status.")
    finished_at = datetime.now(timezone.utc).isoformat()
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM crm_automation_actions WHERE row_key = %s FOR UPDATE",
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return False
                task = row[0]
                if str(task.get("status", "")).casefold() != "processing":
                    conn.rollback()
                    return False
                task.update({"status": status, "outcome": outcome, "resolved_at": finished_at})
                cur.execute(
                    "UPDATE crm_automation_actions SET data = %s WHERE row_key = %s",
                    (Json(task), task_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    conn = _sqlite_autopilot_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT data FROM autopilot_actions WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        task = json.loads(row["data"])
        if str(task.get("status", "")).casefold() != "processing":
            conn.rollback()
            return False
        task.update({"status": status, "outcome": outcome, "resolved_at": finished_at})
        conn.execute(
            "UPDATE autopilot_actions SET status = ?, data = ? WHERE task_id = ?",
            (status, json.dumps(task, ensure_ascii=False, default=str), task_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def retry_autopilot_action(task_id: str, confirmed_no_contact: bool = False) -> tuple[bool, str]:
    """Return a failed action to staff review only after explicit no-contact confirmation.

    A manual retry is never eligible for background delivery. Staff must open the
    matched lead and use its normal, consent-checked email or call action.
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return False, "Select a queued action first."
    if not confirmed_no_contact:
        return False, "Confirm that no email was accepted and no call was started."

    reviewed_at = datetime.now(timezone.utc).isoformat()
    allowed_statuses = {"failed", "blocked", "needs review"}
    provider_fields = (
        "provider_id", "delivery_provider_id", "provider_reference", "call_sid",
        "accepted_at", "sent_at", "delivered_at",
    )

    def prepare(task):
        status = str(task.get("status", "")).strip().casefold()
        if status not in allowed_statuses:
            return None, "Only failed or review-required actions can be returned to review."
        if any(str(task.get(field, "") or "").strip() for field in provider_fields):
            return None, "A provider reference exists. Check the communication history before retrying."
        prior_outcome = str(task.get("outcome", "")).casefold()
        if any(marker in prior_outcome for marker in (
            "accepted by smtp", "email sent", "call initiated", "call started",
            "provider accepted", "delivery confirmed",
        )):
            return None, "The recorded outcome may indicate contact was started. Do not retry this action."
        history = task.get("retry_history", [])
        if not isinstance(history, list):
            history = []
        history.append({"at": reviewed_at, "confirmed_no_contact": True})
        task.update({
            "status": "Needs review",
            "delivery_attempts": 0,
            "manual_retry_only": True,
            "retry_history": history[-10:],
            "outcome": "Staff confirmed no email was accepted and no call was started. Returned to staff review; automatic delivery is disabled for this retry.",
            "resolved_at": "",
        })
        return task, "Returned to staff review. Use the lead's normal contact action to retry."

    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM crm_automation_actions WHERE row_key = %s FOR UPDATE",
                    (task_id,),
                )
                row = cur.fetchone()
                if not row:
                    conn.rollback()
                    return False, "That queued action no longer exists."
                task, message = prepare(row[0])
                if task is None:
                    conn.rollback()
                    return False, message
                cur.execute(
                    "UPDATE crm_automation_actions SET data = %s WHERE row_key = %s",
                    (Json(task), task_id),
                )
            conn.commit()
            return True, message
        finally:
            conn.close()

    conn = _sqlite_autopilot_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT data FROM autopilot_actions WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return False, "That queued action no longer exists."
        task, message = prepare(json.loads(row["data"]))
        if task is None:
            conn.rollback()
            return False, message
        conn.execute(
            "UPDATE autopilot_actions SET status = ?, data = ? WHERE task_id = ?",
            (task["status"], json.dumps(task, ensure_ascii=False, default=str), task_id),
        )
        conn.commit()
        return True, message
    finally:
        conn.close()


def resolve_autopilot_actions(lead_key: str, outcome: str) -> int:
    """Resolve open review tasks after staff completes or dismisses the action."""
    lead_key = str(lead_key or "").strip()
    if not lead_key:
        return 0
    resolved_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    new_status = "Completed" if outcome != "Dismissed" else "Dismissed"
    if database_enabled():
        conn = get_connection()
        changed = 0
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT row_key, data FROM crm_automation_actions WHERE data ->> 'lead_key' = %s FOR UPDATE",
                    (lead_key,),
                )
                for task_id, data in cur.fetchall():
                    if str(data.get("status", "")).casefold() != "needs review":
                        continue
                    data.update({"status": new_status, "outcome": outcome, "resolved_at": resolved_at})
                    cur.execute(
                        "UPDATE crm_automation_actions SET data = %s WHERE row_key = %s",
                        (Json(data), task_id),
                    )
                    changed += 1
            conn.commit()
        finally:
            conn.close()
        return changed
    conn = _sqlite_autopilot_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT task_id, data FROM autopilot_actions WHERE lead_key = ? AND lower(status) = 'needs review'",
            (lead_key,),
        ).fetchall()
        for row in rows:
            data = json.loads(row["data"])
            data.update({"status": new_status, "outcome": outcome, "resolved_at": resolved_at})
            conn.execute(
                "UPDATE autopilot_actions SET status = ?, data = ? WHERE task_id = ?",
                (new_status, json.dumps(data, ensure_ascii=False, default=str), row["task_id"]),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def save_autopilot_worker_status(status: dict) -> None:
    """Persist the latest worker heartbeat and scan summary for the CRM screen."""
    payload = _json_safe_row(status)
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO crm_settings (row_key, data) VALUES (%s, %s)
                    ON CONFLICT (row_key) DO UPDATE SET data = EXCLUDED.data
                    """,
                    ("autopilot_worker", Json(payload)),
                )
            conn.commit()
        finally:
            conn.close()
        return
    conn = _sqlite_autopilot_connection()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS autopilot_worker_status (row_key TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO autopilot_worker_status(row_key, data) VALUES ('current', ?)",
                (json.dumps(payload, ensure_ascii=False, default=str),),
            )
    finally:
        conn.close()


def load_autopilot_worker_status() -> dict:
    """Read the latest heartbeat and scan summary, if a worker has started."""
    if database_enabled():
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_settings WHERE row_key = %s", ("autopilot_worker",))
                row = cur.fetchone()
            return row[0] if row and isinstance(row[0], dict) else {}
        finally:
            conn.close()
    conn = _sqlite_autopilot_connection()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS autopilot_worker_status (row_key TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT data FROM autopilot_worker_status WHERE row_key = 'current'"
        ).fetchone()
        return json.loads(row["data"]) if row else {}
    finally:
        conn.close()
