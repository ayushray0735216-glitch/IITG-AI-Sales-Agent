"""Vercel entry point for the shared CRM service; no sending or local fallback."""
import os
from flask import Flask, jsonify

try:
    if not os.getenv("DATABASE_URL", "").strip():
        raise RuntimeError("Shared PostgreSQL configuration is required")
    if os.getenv("INTEGRATION_ENABLED", "").casefold() not in {"true", "yes", "1"}:
        raise RuntimeError("Shared integration must be explicitly enabled")
    if any(len(os.getenv("INTEGRATION_" + role + "_KEY", "")) < 32
           for role in ("DISCOVERY", "OUTREACH", "ADMIN")):
        raise RuntimeError("All three module keys are required")
    from integration_api import create_app
    app = create_app()
except Exception:
    # Do not echo database credentials or provider errors to public pages/logs.
    app = Flask(__name__)

    @app.get("/health")
    def unavailable():
        response = jsonify(status="unavailable", message="Shared service configuration or database connection needs attention.")
        response.headers["Cache-Control"] = "no-store"
        return response, 503
