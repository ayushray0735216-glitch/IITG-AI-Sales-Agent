"""Durable, per-search Hunter workspaces for the developer console."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from psycopg2.extras import Json

import crm_storage


_LOCAL_LOCK = Lock()
_SELECTED_KEY = "prospect_progress:last_selected"


def _local_path(key: str) -> Path:
    root = Path(crm_storage.crm_path("prospect_searches.json")).parent
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return root / f"prospect_progress_{digest}.json"


def _read(key: str) -> dict:
    if crm_storage.database_enabled():
        conn = crm_storage.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT data FROM crm_settings WHERE row_key = %s", (key,))
                row = cur.fetchone()
            return row[0] if row and isinstance(row[0], dict) else {}
        finally:
            conn.close()
    path = _local_path(key)
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    return data if isinstance(data, dict) else {}


def _write(key: str, data: dict) -> None:
    if crm_storage.database_enabled():
        conn = crm_storage.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO crm_settings (row_key, data) VALUES (%s, %s) "
                    "ON CONFLICT (row_key) DO UPDATE SET data = EXCLUDED.data",
                    (key, Json(data)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return
    path = _local_path(key)
    with _LOCAL_LOCK:
        temporary = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                    prefix=".prospect-progress-", suffix=".json", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()


def load_search(profile_id: str) -> dict:
    return _read("prospect_progress:" + profile_id)


def save_search(profile_id: str, state: dict) -> None:
    _write("prospect_progress:" + profile_id, state)


def clear_search(profile_id: str) -> None:
    key = "prospect_progress:" + profile_id
    if crm_storage.database_enabled():
        conn = crm_storage.get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM crm_settings WHERE row_key = %s", (key,))
            conn.commit()
        finally:
            conn.close()
        return
    _local_path(key).unlink(missing_ok=True)


def load_last_selected() -> str:
    return str(_read(_SELECTED_KEY).get("profile_id") or "")


def save_last_selected(profile_id: str) -> None:
    _write(_SELECTED_KEY, {"profile_id": profile_id})
