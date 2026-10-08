"""Durable, independent scanner for the Autopilot follow-up review queue.

Run as a separate long-lived process. It only creates staff-review tasks; it
never sends messages or initiates calls.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

import autopilot
from crm_storage import (
    database_enabled,
    initialize_database,
    load_autopilot_actions,
    load_autopilot_settings,
    load_crm_table,
    load_contact_suppressions,
    queue_autopilot_actions,
    save_autopilot_worker_status,
)


IST = ZoneInfo("Asia/Kolkata")
DEFAULT_SCAN_INTERVAL_SECONDS = 300
STOP_EVENT = threading.Event()

logging.basicConfig(
    level=os.getenv("AUTOPILOT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
LOGGER = logging.getLogger("autopilot_worker")


def _interval_seconds() -> int:
    try:
        configured = int(os.getenv("AUTOPILOT_SCAN_INTERVAL_SECONDS", DEFAULT_SCAN_INTERVAL_SECONDS))
    except (TypeError, ValueError, OverflowError):
        configured = DEFAULT_SCAN_INTERVAL_SECONDS
    return max(60, min(configured, 3600))


def scan_queue_once(settings: dict | None = None, now: datetime | None = None) -> dict:
    """Create due review tasks from current CRM data and persist a worker heartbeat."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    policy = settings if settings is not None else load_autopilot_settings()
    storage_mode = "PostgreSQL" if database_enabled() else "local SQLite queue + CRM CSV files"

    if policy.get("enabled", False):
        leads = load_crm_table("leads.csv")
        activity = load_crm_table("sales_activity.csv")
        existing = load_autopilot_actions()
        tasks, counts = autopilot.build_eligible_actions(
            leads,
            activity,
            policy,
            existing_actions=existing,
            now=now,
            suppressions=load_contact_suppressions(),
        )
        created = queue_autopilot_actions(tasks)
        scan_message = "Eligibility scan completed. New tasks remain in staff review."
    else:
        counts = {
            "scanned": 0,
            "not_due": 0,
            "estimated_due": 0,
            "terminal": 0,
            "no_consent_or_channel": 0,
            "attempt_limit": 0,
            "cooldown": 0,
            "already_queued": 0,
            "suppressed": 0,
            "invalid_contact": 0,
        }
        created = 0
        scan_message = "Autopilot policy is off; no leads were scanned or queued."

    heartbeat = {
        "status": "running",
        "storage_mode": storage_mode,
        "enabled": bool(policy.get("enabled", False)),
        "checked_at": now.isoformat(),
        "next_check_at": (now + timedelta(seconds=_interval_seconds())).isoformat(),
        "new_tasks": created,
        "counts": counts,
        "message": scan_message,
        "error": "",
    }
    save_autopilot_worker_status(heartbeat)
    return heartbeat


def _record_failure(error: Exception) -> None:
    now = datetime.now(IST)
    payload = {
        "status": "error",
        "storage_mode": "PostgreSQL" if database_enabled() else "local SQLite queue + CRM CSV files",
        "enabled": False,
        "checked_at": now.isoformat(),
        "next_check_at": (now + timedelta(seconds=_interval_seconds())).isoformat(),
        "new_tasks": 0,
        "counts": {},
        "message": "The last background scan failed. Check worker logs before taking action.",
        "error": f"{type(error).__name__}: {str(error)[:500]}",
    }
    try:
        save_autopilot_worker_status(payload)
    except Exception:
        LOGGER.exception("Could not persist worker error status")


def run_forever() -> None:
    """Run an immediate scan, then poll every five minutes until stopped."""
    initialize_database()
    if not database_enabled():
        LOGGER.warning(
            "DATABASE_URL is not configured. Local mode uses a SQLite queue and CSV CRM data; "
            "the web app and worker must share the same CRM_DATA_DIR. Hosted services need shared PostgreSQL."
        )
    interval = _interval_seconds()
    LOGGER.info("Autopilot background scanner started; interval=%s seconds", interval)
    while not STOP_EVENT.is_set():
        try:
            status = scan_queue_once()
            LOGGER.info(
                "%s Scanned %s lead(s); queued %s new task(s).",
                status["message"],
                status["counts"].get("scanned", 0),
                status["new_tasks"],
            )
        except Exception as error:
            LOGGER.exception("Autopilot scan failed")
            _record_failure(error)
        STOP_EVENT.wait(interval)
    LOGGER.info("Autopilot background scanner stopped")


def _request_stop(_signum, _frame) -> None:
    STOP_EVENT.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _request_stop)
    run_forever()
