"""Shared CRM storage helpers for CSV fallback + Render PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from time import monotonic
from uuid import uuid4

import pandas as pd
import psycopg2
from psycopg2.extensions import TRANSACTION_STATUS_IDLE, connection as PostgreSQLConnection
from psycopg2.extras import Json, execute_values


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(
    os.getenv("CRM_DATA_DIR", str(PROJECT_DIR))
).expanduser()

DATABASE_URL = os.getenv("DATABASE_URL")
_lead_write_lock = Lock()
_connection_lock = Lock()
_idle_connections = []
_CONNECTION_IDLE_SECONDS = 30
_MAX_IDLE_CONNECTIONS = 4
LEAD_EDIT_FIELDS = ("Name", "Email", "Phone", "WhatsApp Number", "Company", "Job Role", "Status", "Priority")
LEAD_PHONE_DTYPES = {"Phone": str, "WhatsApp Number": str}


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

    # A sleeping Neon compute or a brief DNS failure can make the first
    # connection fail. Retry only connection failures; never switch storage.
    for attempt in range(3):
        try:
            return psycopg2.connect(DATABASE_URL, connect_timeout=6)
        except psycopg2.OperationalError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def connection_failure_message(error: Exception) -> str:
    """Give a useful, credential-free explanation for a failed connection."""
    detail = str(error).casefold()
    if "could not translate host name" in detail or "name or service not known" in detail:
        return ("The CRM server address could not be found. Check your internet connection and "
                "DNS, then confirm the DATABASE_URL host in Neon is still current.")
    if "password authentication failed" in detail or "authentication failed" in detail:
        return "The CRM rejected its login details. Copy a fresh connection string from Neon into .env and restart Sharngon."
    if "timeout" in detail or "could not connect to server" in detail or "connection refused" in detail:
        return "The CRM server did not respond. Check the internet connection and Neon project status, then retry."
    return "The CRM connection failed. Check the Neon project and connection string, then retry."



def _borrow_connection():
    """Reuse a recently returned connection without sharing a live transaction."""
    now = monotonic()
    with _connection_lock:
        while _idle_connections:
            returned_at, dsn, conn = _idle_connections.pop()
            if dsn == DATABASE_URL and not conn.closed and now - returned_at < _CONNECTION_IDLE_SECONDS:
                return conn
            conn.close()
    return get_connection()


def _release_connection(conn):
    """Roll back unfinished work before retaining an idle database connection."""
    # Tests and external callers can supply their own connection implementation.
    if not isinstance(conn, PostgreSQLConnection):
        conn.close()
        return
    try:
        if not conn.closed and conn.get_transaction_status() != TRANSACTION_STATUS_IDLE:
            conn.rollback()
    except psycopg2.Error:
        conn.close()
        return
    with _connection_lock:
        if not conn.closed and len(_idle_connections) < _MAX_IDLE_CONNECTIONS:
            _idle_connections.append((monotonic(), DATABASE_URL, conn))
            return
    conn.close()

def upsert_crm_rows(filename: str, rows: list[dict]) -> None:
    """Persist just changed rows; callers keep the local CSV copy."""
    if not database_enabled() or not rows:
        return
    if filename not in {"leads.csv", "conversation_history.csv", "sales_activity.csv", "email_inbox.csv"}:
        raise ValueError("Row updates are only supported for CRM records.")
    values = {}
    for row in rows:
        # Match blank-cell normalization used by the existing CSV sync path.
        data = {str(key): None if pd.isna(value) or value == "" else value for key, value in row.items()}
        values[_row_key(filename, data)] = Json(data)
    conn = _borrow_connection()
    try:
        with conn.cursor() as cur:
            execute_values(
                cur,
                f"INSERT INTO {_table_name(filename)} (row_key, data) VALUES %s "
                "ON CONFLICT (row_key) DO UPDATE SET data = EXCLUDED.data",
                list(values.items()), page_size=500,
            )
        conn.commit()
    finally:
        _release_connection(conn)


def initialize_database() -> None:
    """Create CRM tables and synchronize existing CSV seed data once."""
    if not database_enabled():
        return

    conn = _borrow_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crm_leads (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sales_activity (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS conversation_history (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS email_inbox (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crm_offerings (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crm_settings (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crm_automation_actions (
                    row_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crm_communication_events (
                    event_id TEXT PRIMARY KEY,
                    provider_id TEXT,
                    data JSONB NOT NULL
                );

                CREATE INDEX IF NOT EXISTS crm_communication_events_provider_idx
                    ON crm_communication_events (provider_id);

                CREATE TABLE IF NOT EXISTS crm_contact_suppressions (
                    suppression_key TEXT PRIMARY KEY,
                    data JSONB NOT NULL
                );

                CREATE INDEX IF NOT EXISTS crm_automation_actions_status_idx
                    ON crm_automation_actions ((lower(data ->> 'status')));

                CREATE INDEX IF NOT EXISTS crm_automation_actions_lead_idx
                    ON crm_automation_actions ((data ->> 'lead_key'));
                """
            )

        conn.commit()

    finally:
        _release_connection(conn)

    for filename in (
        "leads.csv", "sales_activity.csv", "conversation_history.csv",
        "email_inbox.csv", "offerings.csv",
    ):
        sync_csv_to_database(filename, only_if_empty=True)
        sync_database_to_csv(filename)


def lead_cell(value) -> str:
    return "" if value is None or pd.isna(value) else str(value).strip()


def _lead_key(row: dict) -> str:
    record_id = lead_cell(row.get("CRM Record ID"))
    if record_id:
        return "lead:" + record_id

    import_key = row.get("Lead Import Key")
    if isinstance(import_key, str) and import_key.startswith("import:"):
        return import_key

    email = lead_cell(row.get("Email"))
    if email:
        return "email:" + email.casefold()

    name = lead_cell(row.get("Name"))
    if name:
        return "name:" + name.casefold()

    return _generic_key(row)



def lead_identity(row):
    """Identify a lead across review and delivery."""
    visitor_id = lead_cell(row.get("Visitor ID"))
    return "visitor:" + visitor_id if visitor_id else _lead_key(row)


def save_recorded_email_consent(original, source, recorded_on):
    """Record staff-confirmed email permission for the current lead address."""
    identity = lead_identity(original)
    address = lead_cell(original.get("Email"))
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) or not source.strip():
        raise ValueError("A valid email address and the source of consent are required.")
    if is_contact_suppressed(address, "email"):
        raise ValueError("This address has an active email suppression.")

    def apply_changes(current):
        matches = [
            key for key, row in current.items()
            if lead_identity(row) == identity and lead_cell(row.get("Email")).casefold() == address.casefold()
        ]
        if len(matches) != 1:
            return False, {}
        key = matches[0]
        row = current[key]
        if lead_cell(row.get("Status")).casefold() == "unsubscribed":
            raise ValueError("This lead is marked as unsubscribed.")
        methods = [item.strip() for item in lead_cell(row.get("Preferred Contact Methods")).split(",") if item.strip()]
        if not any(method.casefold() == "email" for method in methods):
            methods.append("Email")
        updated = {
            **row, "Contact Consent": "Yes", "Preferred Contact Methods": ", ".join(methods),
            "Email Consent Source": source.strip(), "Email Consent Date": recorded_on,
            "Email Consent Address": address,
        }
        current[key] = updated
        return True, {key: updated}

    return _write_leads(apply_changes)

def _merge_workbook_leads(current, incoming):
    """Keep existing CRM values and attach the latest nonblank workbook fields."""
    by_import_key = {}
    by_email = {}
    for key, lead in current.items():
        for field in ("Lead Import Key", "Workbook Match Key"):
            if lead.get(field):
                by_import_key.setdefault(lead[field], key)
        if lead.get("Email"):
            by_email.setdefault(str(lead["Email"]).strip().casefold(), key)

    counts = dict(added=0, updated=0, unchanged=0, conflicts=0)
    changed = {}
    for lead in incoming:
        import_key = lead["Lead Import Key"]
        email = lead["Email"].casefold()
        key_match = by_import_key.get(import_key)
        email_match = by_email.get(email) if email else None
        if email_match is not None:
            email_record = current[email_match]
            email_key = email_record.get("Workbook Match Key") or email_record.get("Lead Import Key") or ""
            conflicting_linkedin = (
                email_key.startswith("import:linkedin:") and import_key.startswith("import:linkedin:")
                and email_key != import_key
            )
            if conflicting_linkedin or (key_match is not None and key_match != email_match):
                counts["conflicts"] += 1
                continue

        key = key_match if key_match is not None else email_match
        profile = json.loads(lead["Workbook Profile"])
        if key is None:
            key = import_key
            merged = {**lead, "Status": "New", "Lead Source": "Lead File", "Contact Consent": "No"}
            counts["added"] += 1
        else:
            existing = current[key]
            merged = existing.copy()
            stored_identity = existing.get("Workbook Match Key") or existing.get("Lead Import Key")
            if import_key != stored_identity and (
                import_key.startswith("import:linkedin:") or not stored_identity
            ):
                merged["Workbook Match Key"] = import_key

            for field, value in lead.items():
                if field not in {"Lead Import Key", "Workbook Profile"} and value:
                    if not str(merged.get(field) or "").strip():
                        merged[field] = value

            stored = workbook_profile(existing.get("Workbook Profile"))
            for field, value in profile.items():
                if value or field not in stored:
                    stored[field] = value
            merged["Workbook Profile"] = json.dumps(stored, ensure_ascii=False)
            if merged == existing:
                counts["unchanged"] += 1
            else:
                counts["updated"] += 1

        if current.get(key) != merged:
            current[key] = changed[key] = merged
        by_import_key[import_key] = key
        if merged.get("Email"):
            by_email[str(merged["Email"]).strip().casefold()] = key
    return counts, changed


def workbook_profile(payload):
    if not isinstance(payload, str) or not payload:
        return {}
    try:
        fields = json.loads(payload)
    except ValueError:
        return {}
    return fields if isinstance(fields, dict) else {}


def _replace_leads_csv(destination, records):
    with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=destination.parent,
                            suffix=".tmp", delete=False) as output:
        staged = Path(output.name)
        try:
            pd.DataFrame(records).to_csv(output, index=False)
            output.close()
            staged.replace(destination)
        finally:
            output.close()
            staged.unlink(missing_ok=True)


def _write_leads(apply_changes):
    """Apply a batch against the current CRM and rebuild the local lead cache."""
    with _lead_write_lock:
        destination = Path(ensure_seed_file("leads.csv"))
        if not database_enabled():
            records = (
                pd.read_csv(destination, dtype=str, keep_default_na=False).to_dict("records")
                if destination.exists() else []
            )
            # Positional keys retain all legacy CSV rows, even if names repeat.
            current = dict(enumerate(records))
            result, changed = apply_changes(current)
            if changed:
                _replace_leads_csv(destination, list(current.values()))
            return result, True

        conn = _borrow_connection()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute("LOCK TABLE crm_leads IN SHARE ROW EXCLUSIVE MODE")
                    cursor.execute("SELECT row_key, data FROM crm_leads ORDER BY row_key")
                    current = dict(cursor.fetchall())
                    result, changed = apply_changes(current)
                    if changed:
                        execute_values(
                            cursor,
                            "INSERT INTO crm_leads (row_key, data) VALUES %s "
                            "ON CONFLICT (row_key) DO UPDATE SET data = EXCLUDED.data",
                            [(key, Json(current[key])) for key in changed if key in current],
                        )
                        removed = [key for key in changed if key not in current]
                        if removed:
                            cursor.execute("DELETE FROM crm_leads WHERE row_key = ANY(%s)", (removed,))
        finally:
            _release_connection(conn)

        try:
            _replace_leads_csv(destination, list(current.values()))
        except OSError:
            return result, False
        return result, True


def import_workbook_leads(incoming):
    """Merge an uploaded workbook into the active CRM and refresh its local cache."""
    return _write_leads(lambda current: _merge_workbook_leads(current, incoming))


def import_hunter_prospects(prospects):
    """Save reviewed Hunter contacts, reusing unambiguous CRM records atomically."""
    if not prospects or len(prospects) > 50:
        raise ValueError("Choose between 1 and 50 reviewed prospects to import.")
    prepared = []
    seen_handles = set()
    for prospect in prospects:
        handle = str(prospect.get("handle") or "").strip()
        name = str(prospect.get("name") or "").strip()
        company = str(prospect.get("company") or "").strip()
        email = str(prospect.get("email") or "").strip().casefold()
        if not handle or handle in seen_handles or not name or not company:
            raise ValueError("Each selected prospect needs a unique Hunter result, name, and company.")
        if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
            raise ValueError("A revealed prospect email is invalid.")
        experience = str(prospect.get("experience") or "").strip()
        if experience:
            try:
                years = float(experience)
            except ValueError:
                raise ValueError("Experience must be a number of years or left blank.") from None
            if not 0 <= years <= 80:
                raise ValueError("Experience must be between 0 and 80 years.")
        seen_handles.add(handle)
        prepared.append({
            "hunter_key": "import:hunter:" + hashlib.sha256(handle.encode("utf-8")).hexdigest(),
            "name": name, "company": company,
            "title": str(prospect.get("title") or "").strip(),
            "domain": str(prospect.get("domain") or "").strip(),
            "email": email, "phone": str(prospect.get("phone") or "").strip(),
            "linkedin": str(prospect.get("linkedin") or "").strip(),
            "experience": experience,
            "verification": str(prospect.get("verification") or "").strip(),
            "name_status": str(prospect.get("name_status") or "Hunter masked").strip(),
        })

    def apply_changes(current):
        result = {"added": 0, "updated": 0, "already_present": 0, "needs_review": 0,
                  "details": [], "lead_keys": [], "stored": []}
        changed = {}
        for item in prepared:
            email = item["email"]
            name = item["name"].casefold()
            company = item["company"].casefold()
            key_matches = [key for key, row in current.items()
                           if lead_cell(row.get("Hunter Prospect Key")) == item["hunter_key"]
                           or lead_cell(row.get("Lead Import Key")) == item["hunter_key"]]
            email_matches = [key for key, row in current.items()
                             if email and lead_cell(row.get("Email")).casefold() == email]
            exact_matches = [key for key, row in current.items()
                             if lead_cell(row.get("Name")).casefold() == name
                             and lead_cell(row.get("Company")).casefold() == company]
            matches = set(key_matches + email_matches + exact_matches)
            masked = item["name_status"].casefold() == "hunter masked"
            possible_matches = [key for key, row in current.items()
                                if masked and not matches
                                and lead_cell(row.get("Company")).casefold() == company
                                and lead_cell(row.get("Name")).casefold().startswith(name.rstrip(".")[:1])
                                and lead_cell(row.get("Name")).split(" ")[0].casefold()
                                == item["name"].split(" ")[0].casefold()]
            if len(matches) > 1 or possible_matches:
                result["needs_review"] += 1
                result["details"].append(f"{item['name']} · {item['company']}: possible existing lead; review manually.")
                continue
            key = next(iter(matches)) if matches else None
            existing = current[key] if key is not None else {}
            if key is not None and email and lead_cell(existing.get("Email")) not in ("", email):
                result["needs_review"] += 1
                result["details"].append(f"{item['name']} · {item['company']}: existing lead has another email.")
                continue
            profile = workbook_profile(existing.get("Workbook Profile"))
            profile.update({
                "Hunter Domain": item["domain"],
                "Hunter Verification": item["verification"],
                "Hunter Name Status": item["name_status"],
            })
            for field, value in (("Name", item["name"]), ("Job Title", item["title"]),
                                 ("Company", item["company"]), ("Website", item["domain"])):
                if value and not profile.get(field):
                    profile[field] = value
            if item["linkedin"]:
                profile.setdefault("LinkedIn", item["linkedin"])
            if item["experience"] and not profile.get("Experience (Years)"):
                profile["Experience (Years)"] = item["experience"]
            if not profile.get("Experience (Years)"):
                profile["Experience (Years)"] = ""
            if item["email"] and not profile.get("Email"):
                profile["Email"] = item["email"]
            if key is None:
                record_id = uuid4().hex
                row = {
                    "CRM Record ID": record_id, "Lead Import Key": item["hunter_key"],
                    "Hunter Prospect Key": item["hunter_key"],
                    "Name": item["name"], "Email": item["email"],
                    "Phone": item["phone"], "Company": item["company"],
                    "Job Role": item["title"], "Status": "New",
                    "Lead Source": "Hunter Prospect", "Contact Consent": "No",
                    "Workbook Profile": json.dumps(profile, ensure_ascii=False),
                }
                key = (max(current, default=-1) + 1 if not database_enabled()
                       else _lead_key(row))
                current[key] = changed[key] = row
                result["added"] += 1
            else:
                row = existing.copy()
                for field, value in (("Email", item["email"]), ("Phone", item["phone"]),
                                     ("Job Role", item["title"]), ("Company", item["company"])):
                    if value and not lead_cell(row.get(field)):
                        row[field] = value
                if key_matches and not masked and item["name_status"] in {"Hunter revealed", "Staff supplied"}:
                    row["Name"] = item["name"]
                    profile["Name"] = item["name"]
                row["Hunter Prospect Key"] = item["hunter_key"]
                row["Workbook Profile"] = json.dumps(profile, ensure_ascii=False)
                if row != existing:
                    if _lead_key(row) != _lead_key(existing):
                        row["CRM Record ID"] = uuid4().hex
                        if isinstance(key, str):
                            del current[key]
                            changed[key] = None
                            key = _lead_key(row)
                    current[key] = changed[key] = row
                    result["updated"] += 1
                else:
                    result["already_present"] += 1
            result["lead_keys"].append(_lead_key(current[key]))
            result["stored"].append(item["hunter_key"])
        return result, changed

    return _write_leads(apply_changes)


def save_lead_edits(edits):
    """Validate and save a batch of edits using the original values to detect conflicts."""
    def apply_changes(current):
        positions = {}
        for key, row in current.items():
            # PostgreSQL already provides the canonical row key. CSV rows use positions.
            identity = key if isinstance(key, str) else _lead_key(row)
            positions.setdefault(identity, []).append(key)

        changed = {}
        updated_count = 0
        changed_emails = set()
        for original, edits_for_row in edits:
            matches = positions.get(_lead_key(original), [])
            if len(matches) != 1 or matches[0] in changed:
                raise ValueError("A lead could not be identified uniquely. Refresh the lead records and try again.")
            key = matches[0]
            row = current[key]
            updates = {}
            for field, raw_value in edits_for_row.items():
                if field not in LEAD_EDIT_FIELDS:
                    raise ValueError("This column cannot be edited.")
                value = lead_cell(raw_value)
                existing = lead_cell(row.get(field))
                if value == existing:
                    continue
                if existing != lead_cell(original.get(field)):
                    raise ValueError("A lead changed while you were editing. Refresh the lead records and try again.")
                if field == "Name" and not value:
                    raise ValueError("Enter a name for each lead.")
                if field == "Email" and value:
                    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                        raise ValueError("Enter a valid email address or clear the email cell.")
                    if value.casefold() != existing.casefold():
                        changed_emails.add(value.casefold())
                if field in LEAD_PHONE_DTYPES and value:
                    digits = re.sub(r"[^0-9]", "", value)
                    if not re.fullmatch(r"\+?[0-9(). -]+", value) or not 7 <= len(digits) <= 15:
                        raise ValueError("Enter a phone or WhatsApp number with 7 to 15 digits, or clear the cell.")
                updates[field] = value
            if not updates:
                continue

            updated = {**row, **updates}
            if _lead_key(updated) != _lead_key(row):
                # Move a legacy email/name keyed row once, then keep its identity stable.
                updated["CRM Record ID"] = uuid4().hex
                if isinstance(key, str):
                    del current[key]
                    changed[key] = None
                    key = _lead_key(updated)
            current[key] = changed[key] = updated
            updated_count += 1

        seen_emails = set()
        for row in current.values():
            email = lead_cell(row.get("Email")).casefold()
            if email in changed_emails and email in seen_emails:
                raise ValueError("That email address is already assigned to another lead.")
            seen_emails.add(email)
        return updated_count, changed

    return _write_leads(apply_changes)


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

    df = pd.read_csv(path, dtype=LEAD_PHONE_DTYPES if filename == "leads.csv" else None)

    if df.empty:
        return

    table = _table_name(filename)
    conn = _borrow_connection()

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

            values = {}
            for _, row in df.iterrows():
                data = {
                    str(k): (
                        None if pd.isna(v) else v
                    )
                    for k, v in row.to_dict().items()
                }

                key = _row_key(filename, data)

                # Preserve the previous row-by-row behavior for duplicate IDs:
                # the last row wins, with one upsert for each key.
                values[key] = Json(data)
            execute_values(
                cur,
                f"INSERT INTO {table} (row_key, data) VALUES %s "
                "ON CONFLICT (row_key) DO UPDATE SET data = EXCLUDED.data",
                list(values.items()), page_size=500,
            )

        if filename == "offerings.csv":
            from shared_integration import publish_crm_change
            publish_crm_change(conn, "catalog.updated", "offerings")

        conn.commit()

    finally:
        _release_connection(conn)


def sync_database_to_csv(filename: str) -> None:
    """Rebuild local CSV cache from PostgreSQL when DB has data."""
    if not database_enabled():
        return

    table = _table_name(filename)
    conn = _borrow_connection()

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
        _release_connection(conn)


def sync_after_csv_write(filename: str) -> None:
    """Push a newly written CSV into PostgreSQL."""
    if not database_enabled():
        return

    sync_csv_to_database(filename)


def load_offerings_data() -> pd.DataFrame:
    """Read the latest offerings catalog from shared storage, or CSV fallback."""
    if database_enabled():
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_offerings ORDER BY row_key")
                rows = [row[0] for row in cur.fetchall()]
            # An empty table is also authoritative: it may mean that every
            # offering was removed or deactivated. Never revive an old local cache.
            return pd.DataFrame(rows)
        finally:
            _release_connection(conn)

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

    conn = _borrow_connection()
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
        from shared_integration import publish_crm_change
        publish_crm_change(conn, "catalog.updated", "offerings")
        conn.commit()
    finally:
        _release_connection(conn)


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
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_settings WHERE row_key = %s", ("autopilot",))
                row = cur.fetchone()
            if row:
                data = row[0]
        finally:
            _release_connection(conn)
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
        conn = _borrow_connection()
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
            from shared_integration import publish_crm_change
            publish_crm_change(conn, "policy.updated", "autopilot")
            conn.commit()
        finally:
            _release_connection(conn)
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
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT data FROM {tables[filename]} ORDER BY row_key")
                return pd.DataFrame([row[0] for row in cur.fetchall()])
        finally:
            _release_connection(conn)
    path = Path(ensure_seed_file(filename))
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, dtype=LEAD_PHONE_DTYPES if filename == "leads.csv" else None)
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
        conn = _borrow_connection()
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
            _release_connection(conn)
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
        conn = _borrow_connection()
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
            _release_connection(conn)
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



def get_communication_events(event_ids) -> dict:
    """Load the exact send outcomes for the current review items."""
    if not event_ids:
        return {}
    if database_enabled():
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT event_id, data FROM crm_communication_events WHERE event_id = ANY(%s)",
                    (list(event_ids),),
                )
                return dict(cur.fetchall())
        finally:
            _release_connection(conn)
    conn = _sqlite_autopilot_connection()
    try:
        placeholders = ", ".join("?" for _ in event_ids)
        rows = conn.execute(
            f"SELECT event_id, data FROM communication_events WHERE event_id IN ({placeholders})",
            tuple(event_ids),
        )
        return {key: json.loads(data) for key, data in rows}
    finally:
        conn.close()


def claim_reviewed_email(event: dict) -> bool:
    """Reserve a send before contacting SMTP. Only definite failures are retryable."""
    event = _json_safe_row({**event, "status": "sending"})
    event_id = event["event_id"]
    if database_enabled():
        conn = _borrow_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO crm_communication_events(event_id, provider_id, data)
                           VALUES (%s, %s, %s)
                           ON CONFLICT (event_id) DO UPDATE SET
                           provider_id = EXCLUDED.provider_id, data = EXCLUDED.data
                           WHERE crm_communication_events.data ->> 'status' IN ('failed', 'blocked')
                           RETURNING event_id""",
                        (event_id, event["provider_id"], Json(event)),
                    )
                    return cur.fetchone() is not None
        finally:
            _release_connection(conn)
    conn = _sqlite_autopilot_connection()
    try:
        with conn:
            cursor = conn.execute(
                """INSERT INTO communication_events(event_id, provider_id, data) VALUES (?, ?, ?)
                   ON CONFLICT(event_id) DO UPDATE SET
                   provider_id = excluded.provider_id, data = excluded.data
                   WHERE json_extract(communication_events.data, '$.status') IN ('failed', 'blocked')""",
                (event_id, event["provider_id"], json.dumps(event, ensure_ascii=False)),
            )
            return cursor.rowcount == 1
    finally:
        conn.close()


def update_lead_email_outcome(destination, status, action, timestamp, event_id, expected_lead_key=""):
    """Update the matched lead without rewriting its other fields or sales stage."""
    def apply_changes(current):
        matches = [
            key for key, row in current.items()
            if lead_cell(row.get("Email")).casefold() == destination.casefold()
        ]
        if len(matches) != 1:
            return False, {}
        key = matches[0]
        row = current[key]
        if expected_lead_key and lead_identity(row) != expected_lead_key:
            return False, {}
        previous_email_at = pd.to_datetime(row.get("Last Email At"), utc=True, errors="coerce")
        event_time = pd.to_datetime(timestamp, utc=True, errors="coerce")
        if pd.notna(previous_email_at) and previous_email_at > event_time:
            return True, {}
        updated = {
            **row, "Email status": status, "Last Email Event": event_id,
            "Last Email At": timestamp,
        }
        if status in {"Response sent", "Sent"}:
            updated["Last Action"] = action
            updated["Last Interaction"] = timestamp
        current[key] = updated
        return True, {key: updated}

    return _write_leads(apply_changes)

def load_communication_events(limit: int = 500, *, sent_email_only=False) -> pd.DataFrame:
    """Read recent provider events or the CRM's accepted outbound mail."""
    limit = max(1, min(int(limit), 5000))
    if database_enabled():
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                condition = (
                    "data ->> 'channel' = 'email' AND data ->> 'direction' = 'outbound' "
                    "AND data ->> 'status' = 'accepted_by_smtp'"
                    if sent_email_only else "event_id NOT LIKE 'mailbox-state:%%'"
                )
                cur.execute(
                    f"SELECT data FROM crm_communication_events WHERE {condition} "
                    "ORDER BY (data ->> 'updated_at') DESC LIMIT %s", (limit,),
                )
                return pd.DataFrame([row[0] for row in cur.fetchall()])
        finally:
            _release_connection(conn)
    conn = _sqlite_autopilot_connection()
    try:
        condition = (
            "json_extract(data, '$.channel') = 'email' AND json_extract(data, '$.direction') = 'outbound' "
            "AND json_extract(data, '$.status') = 'accepted_by_smtp'"
            if sent_email_only else "event_id NOT LIKE 'mailbox-state:%'"
        )
        rows = conn.execute(
            f"SELECT data FROM communication_events WHERE {condition} "
            "ORDER BY json_extract(data, '$.updated_at') DESC LIMIT ?", (limit,),
        ).fetchall()
        return pd.DataFrame([json.loads(row["data"]) for row in rows])
    finally:
        conn.close()


def load_mailbox_drafts() -> list[dict]:
    """Read saved new-message drafts from the same store as the review flags."""
    if database_enabled():
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT data FROM crm_communication_events
                       WHERE event_id LIKE 'mailbox-state:compose:%' AND data ->> 'draft_active' = 'true'"""
                )
                return [row[0] for row in cur.fetchall()]
        finally:
            _release_connection(conn)
    conn = _sqlite_autopilot_connection()
    try:
        rows = conn.execute(
            """SELECT data FROM communication_events
               WHERE event_id LIKE 'mailbox-state:compose:%' AND json_extract(data, '$.draft_active') = 1"""
        )
        return [json.loads(row[0]) for row in rows]
    finally:
        conn.close()


def update_mailbox_state(event_id, **changes):
    """Merge review flags and draft text without changing the original message."""
    if database_enabled():
        record_communication_event({"event_id": event_id, **changes})
        return
    conn = _sqlite_autopilot_connection()
    try:
        with conn:
            conn.execute(
                """INSERT INTO communication_events(event_id, data) VALUES (?, ?)
                   ON CONFLICT (event_id) DO UPDATE SET data = json_patch(communication_events.data, excluded.data)""",
                (event_id, json.dumps({"event_id": event_id, **changes}, ensure_ascii=False)),
            )
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
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO crm_contact_suppressions(suppression_key, data) VALUES (%s, %s)
                       ON CONFLICT (suppression_key) DO UPDATE SET data = EXCLUDED.data""",
                    (key, Json(payload)),
                )
            conn.commit()
        finally:
            _release_connection(conn)
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
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_contact_suppressions")
                rows = [row[0] for row in cur.fetchall()]
            return pd.DataFrame([row for row in rows if row.get("active", True)])
        finally:
            _release_connection(conn)
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
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_automation_actions ORDER BY row_key")
                rows = [row[0] for row in cur.fetchall()]
            return pd.DataFrame(rows)
        finally:
            _release_connection(conn)
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
        conn = _borrow_connection()
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
            _release_connection(conn)
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
        conn = _borrow_connection()
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
            _release_connection(conn)
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
        conn = _borrow_connection()
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
            _release_connection(conn)
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
        conn = _borrow_connection()
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
            _release_connection(conn)

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


def resolve_autopilot_actions(lead_key: str, outcome: str, channel: str = "") -> int:
    """Resolve open review tasks after staff completes or dismisses the action."""
    lead_key = str(lead_key or "").strip()
    if not lead_key:
        return 0
    resolved_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    new_status = "Completed" if outcome != "Dismissed" else "Dismissed"
    if database_enabled():
        conn = _borrow_connection()
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
                    if channel and data.get("channel") != channel:
                        continue
                    data.update({"status": new_status, "outcome": outcome, "resolved_at": resolved_at})
                    cur.execute(
                        "UPDATE crm_automation_actions SET data = %s WHERE row_key = %s",
                        (Json(data), task_id),
                    )
                    changed += 1
            conn.commit()
        finally:
            _release_connection(conn)
        return changed
    conn = _sqlite_autopilot_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT task_id, data FROM autopilot_actions WHERE lead_key = ? AND lower(status) = 'needs review'",
            (lead_key,),
        ).fetchall()
        changed = 0
        for row in rows:
            data = json.loads(row["data"])
            if channel and data.get("channel") != channel:
                continue
            changed += 1
            data.update({"status": new_status, "outcome": outcome, "resolved_at": resolved_at})
            conn.execute(
                "UPDATE autopilot_actions SET status = ?, data = ? WHERE task_id = ?",
                (new_status, json.dumps(data, ensure_ascii=False, default=str), row["task_id"]),
            )
        conn.commit()
        return changed
    finally:
        conn.close()


def save_autopilot_worker_status(status: dict) -> None:
    """Persist the latest worker heartbeat and scan summary for the CRM screen."""
    payload = _json_safe_row(status)
    if database_enabled():
        conn = _borrow_connection()
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
            _release_connection(conn)
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
        conn = _borrow_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_settings WHERE row_key = %s", ("autopilot_worker",))
                row = cur.fetchone()
            return row[0] if row and isinstance(row[0], dict) else {}
        finally:
            _release_connection(conn)
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
