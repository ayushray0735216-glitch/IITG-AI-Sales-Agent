"""Authenticated REST contract for the discovery and outreach modules.

Start explicitly with: python integration_api.py
Nothing is sent to prospects and no server starts when this module is imported.
"""
from __future__ import annotations

from functools import wraps
import hashlib
import hmac
import json
import os

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from shared_integration import Conflict, IntegrationError, NotFound, get_store


def read_snapshot():
    """Read authoritative organization facts on each request; never expose secrets."""
    from crm_storage import load_offerings_data, load_autopilot_settings
    from offering_facts import is_visible_offering
    import pandas as pd
    catalog = load_offerings_data()
    rows = catalog.astype(object).where(pd.notna(catalog), None).to_dict("records")
    return {"offerings": [row for row in rows if is_visible_offering(row)],
            "outreach_policy": load_autopilot_settings()}


def create_app(store=None, keys=None, snapshot_reader=None, suppression_reader=None):
    store = store or get_store()
    keys = keys if keys is not None else {
        role: os.getenv(f"INTEGRATION_{role.upper()}_KEY", "")
        for role in ("discovery", "outreach", "admin")
    }
    configured = {role: key for role, key in keys.items() if key}
    if any(role not in {"discovery", "outreach", "admin"} or not isinstance(key, str) or len(key) < 32 for role, key in configured.items()):
        raise IntegrationError("Use separate service keys of at least 32 characters for each module.")
    if len(set(configured.values())) != len(configured):
        raise IntegrationError("Each module must have its own service key.")
    snapshot_reader = snapshot_reader or read_snapshot
    if suppression_reader is None:
        from crm_storage import is_contact_suppressed
        suppression_reader = is_contact_suppressed
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

    def roles(*allowed):
        def decorate(handler):
            @wraps(handler)
            def authenticated(*args, **kwargs):
                authorization = request.headers.get("Authorization", "")
                scheme, _, value = authorization.partition(" ")
                role = next((role for role, key in configured.items()
                             if scheme.casefold() == "bearer" and hmac.compare_digest(value.encode(), key.encode())), None)
                if role is None:
                    return jsonify(error="A valid module service key is required."), 401
                if role not in allowed:
                    return jsonify(error="This module cannot perform that action."), 403
                request.integration_role = role
                return handler(*args, **kwargs)
            return authenticated
        return decorate

    def body(allowed):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) - set(allowed):
            raise IntegrationError("Send a JSON object with only the documented fields.")
        return data

    @app.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.errorhandler(Exception)
    def errors(exc):
        if isinstance(exc, HTTPException):
            return jsonify(error=exc.name), exc.code
        if isinstance(exc, (Conflict, NotFound, IntegrationError)):
            code = 409 if isinstance(exc, Conflict) else 404 if isinstance(exc, NotFound) else 400
            return jsonify(error=str(exc)), code
        # Database connection errors can contain credentials: don't echo them.
        app.logger.error("Shared integration request failed (%s)", type(exc).__name__)
        return jsonify(error="The shared data service is unavailable. Please retry later."), 503

    @app.get("/health")
    def health():
        return jsonify(status="ok", service="shared-integration")

    @app.post("/v1/contacts")
    @roles("discovery", "admin")
    def upsert_contact():
        data = body({"external_id", "contact", "expected_version"})
        version = data.get("expected_version")
        if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 0):
            raise IntegrationError("expected_version must be a non-negative integer.")
        return jsonify(store.upsert_contact(request.integration_role, data.get("external_id", ""), data.get("contact"), version))

    @app.get("/v1/contacts")
    @roles("discovery", "outreach", "admin")
    def contacts():
        qualification = request.args.get("qualification")
        if qualification and qualification not in {"pending", "qualified", "unqualified"}:
            raise IntegrationError("Invalid qualification filter.")
        rows = store.contacts()
        if qualification:
            rows = [row for row in rows if row["qualification"] == qualification]
        return jsonify(contacts=rows)

    @app.get("/v1/contacts/<contact_id>")
    @roles("discovery", "outreach", "admin")
    def contact(contact_id):
        return jsonify(store.contact(contact_id))

    @app.get("/v1/contacts/<contact_id>/activity")
    @roles("outreach", "admin")
    def activity(contact_id):
        store.contact(contact_id)
        rows = [row for row in store.sessions() if row.get("contact_id") == contact_id]
        return jsonify(conversations=rows, communications=store.communications(contact_id))

    @app.put("/v1/contacts/<contact_id>/preferences")
    @roles("admin")
    def preferences(contact_id):
        data = body({"channels", "consent", "evidence"})
        return jsonify(store.set_preferences(contact_id, data.get("channels"), data.get("consent"), data.get("evidence")))

    @app.post("/v1/chat-links")
    @roles("outreach", "admin")
    def chat_link():
        data = body({"contact_id", "campaign_id", "ttl_seconds"})
        return jsonify(store.create_link(data.get("contact_id"), data.get("campaign_id", ""),
            os.getenv("INTEGRATION_PUBLIC_CHAT_URL", "https://iitg-ai-sales-agent.streamlit.app/"),
            data.get("ttl_seconds", 172800))), 201

    @app.delete("/v1/chat-links/<link_id>")
    @roles("outreach", "admin")
    def revoke(link_id):
        store.revoke_link(link_id)
        return "", 204

    @app.post("/v1/communications")
    @roles("outreach", "admin")
    def communication():
        data = body({"external_id", "outcome"})
        return jsonify(store.record_communication(request.integration_role, data.get("external_id", ""), data.get("outcome")))

    @app.post("/v1/email-reservations")
    @roles("outreach", "admin")
    def reserve_email():
        data = body({"contact_id", "campaign_id", "event_id", "snapshot_version", "reviewed"})
        current = snapshot_reader()
        version = hashlib.sha256(json.dumps(current, sort_keys=True, default=str).encode()).hexdigest()
        if data.get("snapshot_version") != version:
            raise Conflict("Shared information changed. Review a fresh draft.")
        contact = store.contact(data.get("contact_id"))
        if suppression_reader(contact["email"], "email"):
            raise Conflict("This email address is suppressed from outreach.")
        return jsonify(store.reserve_email(contact["contact_id"], data.get("campaign_id", ""),
            data.get("event_id", ""), current.get("outreach_policy", {}), data.get("reviewed"))), 201

    @app.get("/v1/snapshot")
    @roles("discovery", "outreach", "admin")
    def snapshot():
        data = snapshot_reader()
        encoded = json.dumps(data, sort_keys=True, default=str).encode()
        return jsonify(version=hashlib.sha256(encoded).hexdigest(), **data)

    @app.get("/v1/changes")
    @roles("discovery", "outreach", "admin")
    def changes():
        try:
            after, limit = int(request.args.get("after", "0")), int(request.args.get("limit", "100"))
        except ValueError:
            raise IntegrationError("Invalid change cursor or page size.") from None
        return jsonify(store.changes(after, limit))

    @app.get("/v1/conversations")
    @roles("admin")
    def conversations():
        return jsonify(conversations=store.sessions(), messages=store.messages())

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    # Bind only to this computer for local development. Production uses gunicorn.
    create_app().run(host="127.0.0.1", port=int(os.getenv("INTEGRATION_PORT", "8800")), debug=False)
