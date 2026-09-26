"""Shared CRM storage helpers for CSV fallback + Render PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
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

        conn.commit()

        # Seed empty tables from bundled/local CSV files.
        sync_csv_to_database("leads.csv", only_if_empty=True)
        sync_csv_to_database(
            "sales_activity.csv",
            only_if_empty=True,
        )

        # Once PostgreSQL contains data, recreate the local CSV cache.
        sync_database_to_csv("leads.csv")
        sync_database_to_csv("sales_activity.csv")

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
    return _generic_key(row)


def _table_name(filename: str) -> str:
    if filename == "leads.csv":
        return "crm_leads"

    if filename == "sales_activity.csv":
        return "sales_activity"

    raise ValueError(f"Unsupported CRM file: {filename}")


def _row_key(filename: str, row: dict) -> str:
    if filename == "leads.csv":
        return _lead_key(row)

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
