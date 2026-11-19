"""Isolated integration checks. No live DB, credentials, AI, email, or calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from integration_api import create_app
from integration_chat import (
    prepare_chat_entry, reconcile_saved_contacts, reconcile_saved_history, save_shared_message,
    share_contact_preferences, shared_history_rows, shared_visitor_profiles,
)
from shared_integration import (
    Conflict, IntegrationError, NotFound, SharedStore, build_chat_url,
    get_store, publish_crm_change,
)


class SharedIntegrationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "integration.sqlite3"
        self.clock = [2000000000]
        self.store = SharedStore(sqlite_path=self.path, clock=lambda: self.clock[0])
        self.store.initialize()
        self.keys = {"discovery": "d" * 32, "outreach": "o" * 32, "admin": "a" * 32}
        self.snapshot = {"offerings": [{"Offering Name": "Sample Workshop", "Price": "499"}],
                         "outreach_policy": {"enabled": False, "automatic_delivery": False}}
        self.client = create_app(self.store, self.keys, lambda: deepcopy(self.snapshot), lambda *_: None).test_client()

    def auth(self, role):
        return {"Authorization": "Bearer " + self.keys[role]}

    def prospect(self, external="lead-1", **fields):
        return self.store.upsert_contact("discovery", external, {
            "name": "Sample Customer", "email": "sample@example.com", **fields})

    def token(self, contact=None, ttl=120):
        result = self.store.create_link((contact or self.prospect())["contact_id"], "campaign-1", "https://example.com/", ttl)
        return parse_qs(urlsplit(result["chat_url"]).query)["chat_ref"][0], result

    def test_contact_replay_and_cross_source_identity_match_do_not_duplicate(self):
        first = self.prospect()
        cursor = self.store.changes()["next_cursor"]
        self.assertEqual(self.prospect(), first)
        self.assertEqual(self.store.changes(cursor)["changes"], [])
        matched = self.store.upsert_contact("another-source", "42", {"email": "SAMPLE@EXAMPLE.COM", "name": ""})
        self.assertEqual(matched["contact_id"], first["contact_id"])
        self.assertEqual(matched["name"], "Sample Customer")
        self.assertEqual(len(self.store.contacts()), 1)

    def test_same_name_is_not_identity_and_conflicting_channels_require_review(self):
        a = self.prospect(phone="+919876543210")
        b = self.prospect("lead-2", email="other@example.com", phone="+919876543211")
        before = deepcopy(self.store.contacts())
        with self.assertRaises(Conflict):
            self.store.upsert_contact("source", "conflict", {"email": a["email"], "phone": b["phone"]})
        self.assertEqual(self.store.contacts(), before)
        self.assertEqual(len(before), 2)

    def test_concurrent_same_contact_intake_is_serialized_across_connections(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(lambda i: self.store.upsert_contact("discovery", str(i), {"email": "same@example.com"})["contact_id"], range(12)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(len(self.store.contacts()), 1)

    def test_version_conflict_and_validation_are_not_partial_updates(self):
        contact = self.prospect()
        self.store.upsert_contact("discovery", "lead-1", {"company": "Updated"}, expected_version=contact["version"])
        with self.assertRaises(Conflict):
            self.store.upsert_contact("discovery", "lead-1", {"name": "Lost update"}, expected_version=contact["version"])
        for fields in ({"email": "bad"}, {"phone": "9876543210"}, {"qualification": "ready"}, {"preferences": {"consent": True}}):
            with self.assertRaises(IntegrationError):
                self.store.upsert_contact("bad", "id", fields)

    def test_personalised_link_contains_no_contact_details_and_token_is_hashed_at_rest(self):
        token, link = self.token()
        self.assertNotIn("sample@example.com", link["chat_url"])
        self.assertNotIn(link["contact_id"], link["chat_url"])
        with closing(sqlite3.connect(self.path)) as conn:
            stored = " ".join(str(row) for row in conn.execute("SELECT * FROM integration_records"))
        self.assertNotIn(token, stored)
        session = self.store.open_session("VIS-ABCDEF1234", token)
        self.assertEqual(session["association"], "email_link_matched")
        self.assertFalse(self.store.contact(link["contact_id"])["preferences"]["consent"])

    def test_expired_invalid_revoked_links_and_redirect_validation(self):
        token, link = self.token()
        self.store.revoke_link(link["link_id"])
        with self.assertRaises(NotFound):
            self.store.open_session("VIS-ABCDEF1234", token)
        token, _ = self.token()
        self.clock[0] += 121
        with self.assertRaises(NotFound):
            self.store.open_session("VIS-ABCDEF1234", token)
        with self.assertRaises(NotFound):
            self.store.open_session("VIS-ABCDEF1234", "tampered")
        for url in ("http://public.example/", "javascript:alert(1)", "https://u:p@example.com/", "https://example.com/?email=private"):
            with self.assertRaises(IntegrationError):
                build_chat_url(url, "opaque")
        self.assertIn("localhost", build_chat_url("http://localhost:8501/", "opaque"))

    def test_entry_is_provisional_and_does_not_expose_contact_or_load_old_history(self):
        token, link = self.token()
        state = {"customer_name": "Old session", "customer_messages": [{"role": "assistant", "content": "Old private conversation"}]}
        params = {"chat_ref": token}
        session = prepare_chat_entry(params, state, self.store)
        self.assertEqual(params, {})
        self.assertEqual(state["customer_name"], "")
        self.assertIsNone(state["customer_lead"])
        self.assertEqual(state["customer_messages"], [])
        self.assertNotIn("sample@example.com", json.dumps(state))
        self.assertEqual(session["contact_id"], link["contact_id"])
        self.assertEqual(shared_visitor_profiles(self.store)[session["visitor_id"]]["Email"], "sample@example.com")

    def test_forwarded_link_has_separate_conversations_and_cannot_rebind_existing_visitor(self):
        token, _ = self.token()
        a = self.store.open_session("VIS-VISITOR111", token)
        b = self.store.open_session("VIS-VISITOR222", token)
        self.assertNotEqual(a["conversation_id"], b["conversation_id"])
        contact = self.prospect("second", email="second@example.com")
        other_token, _ = self.token(contact)
        with self.assertRaises(Conflict):
            self.store.open_session(a["visitor_id"], other_token)

    def test_message_retry_preserves_content_and_no_duplicates_after_contact_is_shared(self):
        token, link = self.token()
        visitor = "VIS-VISITOR111"
        self.store.open_session(visitor, token)
        message = {"message_id": "message-1", "role": "user", "content": "  indented\n  code  "}
        first = self.store.save_message(visitor, message)
        self.assertEqual(self.store.save_message(visitor, message), first)
        shared = share_contact_preferences(visitor, "Actual Visitor", "actual@example.com", "", "", ["Email"], self.store)
        self.assertNotEqual(shared["contact_id"], link["contact_id"])
        self.assertEqual(shared["association"], "self_reported")
        self.assertEqual(self.store.save_message(visitor, message), first)
        self.assertEqual(self.store.messages()[0]["content"], message["content"])
        with self.assertRaises(Conflict):
            self.store.save_message(visitor, dict(message, content="Changed"))

    def test_api_role_permissions_and_malformed_payloads(self):
        self.assertEqual(self.client.get("/v1/contacts").status_code, 401)
        self.assertEqual(self.client.post("/v1/contacts", headers=self.auth("outreach"), json={}).status_code, 403)
        self.assertEqual(self.client.get("/v1/conversations", headers=self.auth("outreach")).status_code, 403)
        self.assertEqual(self.client.post("/v1/contacts", headers=self.auth("discovery"), json={"source": "admin"}).status_code, 400)
        contact = self.prospect()
        response = self.client.put(f"/v1/contacts/{contact['contact_id']}/preferences", headers=self.auth("discovery"), json={})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get("/v1/changes?after=bad", headers=self.auth("admin")).status_code, 400)
        self.assertEqual(self.client.get("/v1/changes?limit=501", headers=self.auth("admin")).status_code, 400)
        self.assertEqual(self.client.get("/health").headers["Cache-Control"], "no-store")

    def test_end_to_end_discovery_outreach_link_chat_and_private_crm(self):
        response = self.client.post("/v1/contacts", headers=self.auth("discovery"), json={"external_id": "sheet-row-stable-id", "contact": {
            "name": "Prospect", "email": "prospect@example.com", "qualification": "qualified"}})
        self.assertEqual(response.status_code, 200)
        contact_id = response.json["contact_id"]
        outreach = self.client.get("/v1/contacts?qualification=qualified", headers=self.auth("outreach"))
        self.assertEqual(outreach.json["contacts"][0]["contact_id"], contact_id)
        self.assertFalse(outreach.json["contacts"][0]["preferences"]["consent"])
        link = self.client.post("/v1/chat-links", headers=self.auth("outreach"), json={"contact_id": contact_id, "campaign_id": "launch"})
        self.assertEqual(link.status_code, 201)
        state = {}
        prepare_chat_entry({"chat_ref": parse_qs(urlsplit(link.json["chat_url"]).query)["chat_ref"][0]}, state, self.store)
        visitor_id = state["visitor_id"]
        save_shared_message({"visitor_id": visitor_id, "message_id": "sample-user", "role": "user", "content": "Please tell me more", "timestamp": "2026-11-05 10:00:00"}, self.store)
        save_shared_message({"visitor_id": visitor_id, "message_id": "sample-reply", "role": "assistant", "content": "Here are the details.", "timestamp": "2026-11-05 10:00:01"}, self.store)
        crm = self.client.get("/v1/conversations", headers=self.auth("admin")).json
        self.assertEqual(len(crm["messages"]), 2)
        self.assertTrue(all(row["contact_id"] == contact_id for row in crm["messages"]))
        self.assertEqual(shared_history_rows(self.store)[0]["timestamp"], "2026-11-05 10:00:00")
        self.assertEqual(shared_visitor_profiles(self.store)[visitor_id]["Name"], "Prospect")

    def test_communication_idempotency_and_change_cursor_survive_restart(self):
        contact = self.prospect()
        data = {"external_id": "provider-message-1-sent", "outcome": {"contact_id": contact["contact_id"], "channel": "email", "status": "sent", "provider_id": "provider-1"}}
        a = self.client.post("/v1/communications", headers=self.auth("outreach"), json=data)
        b = self.client.post("/v1/communications", headers=self.auth("outreach"), json=data)
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.json, b.json)
        data["outcome"]["status"] = "failed"
        self.assertEqual(self.client.post("/v1/communications", headers=self.auth("outreach"), json=data).status_code, 409)
        initial = self.store.changes(0, 1)
        restarted = SharedStore(sqlite_path=self.path)
        rest = restarted.changes(initial["next_cursor"])
        revisions = [initial["changes"][0]["revision"]] + [c["revision"] for c in rest["changes"]]
        self.assertEqual(revisions, sorted(set(revisions)))
        self.assertNotIn("sample@example.com", json.dumps(initial) + json.dumps(rest))

    def test_snapshot_reads_current_catalog_and_policy_without_stale_cache(self):
        initial = self.client.get("/v1/snapshot", headers=self.auth("outreach")).json
        self.snapshot["offerings"][0]["Price"] = "799"
        self.snapshot["outreach_policy"]["enabled"] = True
        updated = self.client.get("/v1/snapshot", headers=self.auth("outreach")).json
        self.assertNotEqual(initial["version"], updated["version"])
        self.assertEqual(updated["offerings"][0]["Price"], "799")

    def delivery_setup(self):
        self.clock[0] = datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc).timestamp()
        contact = self.prospect(qualification="qualified")
        self.store.set_preferences(contact["contact_id"], ["email"], True, "Synthetic test opt-in")
        self.snapshot["outreach_policy"] = {"enabled": True, "channels": {"email": True},
            "max_attempts": 3, "cooldown_days": 2, "quiet_start_hour": 9, "quiet_end_hour": 18}
        return contact

    def reservation_request(self, contact, campaign="campaign-1", **updates):
        version = self.client.get("/v1/snapshot", headers=self.auth("outreach")).json["version"]
        return {"contact_id": contact["contact_id"], "campaign_id": campaign, "event_id": "mail-1",
                "snapshot_version": version, "reviewed": True, **updates}

    def test_reservation_checks_policy_review_suppression_and_snapshot_before_send(self):
        contact = self.delivery_setup()
        request = self.reservation_request(contact)
        self.assertEqual(self.client.post("/v1/email-reservations", json=request, headers=self.auth("discovery")).status_code, 403)
        for update in ({"reviewed": False}, {"snapshot_version": "old"}):
            response = self.client.post("/v1/email-reservations", json={**request, **update}, headers=self.auth("outreach"))
            self.assertIn(response.status_code, {400, 409})
        client = create_app(self.store, self.keys, lambda: deepcopy(self.snapshot), lambda *_: {"reason": "Opt out"}).test_client()
        self.assertEqual(client.post("/v1/email-reservations", json=request, headers=self.auth("outreach")).status_code, 409)
        self.assertEqual(self.client.post("/v1/email-reservations", json=request, headers=self.auth("outreach")).status_code, 201)
        # A lost response never grants a second send authorization.
        self.assertEqual(self.client.post("/v1/email-reservations", json=request, headers=self.auth("outreach")).status_code, 409)

    def test_concurrent_reservations_and_contact_limits_cannot_be_bypassed_by_retry(self):
        contact = self.delivery_setup()
        policy = self.snapshot["outreach_policy"]
        def reserve(i):
            try:
                self.store.reserve_email(contact["contact_id"], "campaign-1", f"mail-{i}", policy, True)
                return True
            except Conflict:
                return False
        with ThreadPoolExecutor(max_workers=5) as pool:
            self.assertEqual(sum(pool.map(reserve, range(10))), 1)
        with self.assertRaises(Conflict):
            self.store.reserve_email(contact["contact_id"], "other", "mail-other", policy, True)
        for i in (2, 3):
            self.clock[0] += 2 * 86400
            self.store.reserve_email(contact["contact_id"], f"campaign-{i}", f"mail-{i}", policy, True)
        self.clock[0] += 2 * 86400
        with self.assertRaises(Conflict):
            self.store.reserve_email(contact["contact_id"], "campaign-4", "mail-4", policy, True)

    def test_paused_unqualified_or_opted_out_contacts_cannot_reserve_delivery(self):
        contact = self.delivery_setup()
        policy = self.snapshot["outreach_policy"]
        for changed in ({**policy, "enabled": False}, {**policy, "quiet_start_hour": 15}):
            with self.assertRaises(Conflict):
                self.store.reserve_email(contact["contact_id"], "campaign-1", "mail-1", changed, True)
        self.store.set_preferences(contact["contact_id"], [], False, "Synthetic withdrawal")
        with self.assertRaises(Conflict):
            self.store.reserve_email(contact["contact_id"], "campaign-1", "mail-1", policy, True)

    def test_new_email_link_clears_old_contact_form_and_pending_message(self):
        token, _ = self.token()
        state = {"visitor_contact_email": "previous@example.com", "visitor_contact_methods": ["Email"],
                 "pending_customer_message": "Previous person's question", "chat_compare_selection": ["old"]}
        prepare_chat_entry({"chat_ref": token}, state, self.store)
        self.assertFalse(set(state) & {"visitor_contact_email", "visitor_contact_methods", "pending_customer_message", "chat_compare_selection"})

    def test_communication_activity_exposes_outcomes_but_not_private_transcripts(self):
        contact = self.prospect()
        self.store.record_communication("outreach", "mail-1:sent", {"contact_id": contact["contact_id"], "channel": "email", "status": "sent"})
        activity = self.client.get(f"/v1/contacts/{contact['contact_id']}/activity", headers=self.auth("outreach")).json
        self.assertEqual(activity["communications"][0]["data"]["status"], "sent")
        self.assertNotIn("messages", activity)

    def test_existing_crm_change_event_commits_or_rolls_back_with_the_edit(self):
        with patch.dict(os.environ, {"INTEGRATION_ENABLED": "true"}):
            conn = self.store.connect()
            publish_crm_change(conn, "catalog.updated", "offerings")
            conn.rollback()
            self.assertEqual(self.store.changes()["changes"], [])
            publish_crm_change(conn, "catalog.updated", "offerings")
            conn.commit()
            conn.close()
        self.assertEqual(self.store.changes()["changes"][0]["topic"], "catalog.updated")

    def test_reconciliation_is_repeatable_and_skips_unidentified_legacy_messages(self):
        rows = [{"visitor_id": "VIS-VISITOR111", "message_id": "saved-1", "role": "user", "content": "Recover me", "timestamp": "2026-11-05 10:00:00"},
                {"visitor_id": "", "message_id": "old", "role": "user", "content": "Unidentified"}]
        self.assertEqual(reconcile_saved_history(rows, self.store), {"synced": 1, "skipped": 1})
        self.assertEqual(reconcile_saved_history(rows, self.store), {"synced": 1, "skipped": 1})
        self.assertEqual(len(self.store.messages()), 1)

    def test_database_failure_never_silently_uses_local_sqlite_and_errors_hide_secrets(self):
        get_store.cache_clear()
        self.addCleanup(get_store.cache_clear)
        with patch.dict(os.environ, {"INTEGRATION_ENABLED": "true", "DATABASE_URL": "postgresql://hidden-secret", "INTEGRATION_SQLITE_PATH": str(self.path)}), patch("psycopg2.connect", side_effect=RuntimeError("hidden-secret")):
            with self.assertRaises(RuntimeError):
                get_store()
        with patch.object(self.store, "contacts", side_effect=RuntimeError("hidden-secret")):
            response = self.client.get("/v1/contacts", headers=self.auth("admin"))
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("hidden-secret", response.text)

    def test_shared_sales_insight_and_preference_reconciliation_preserve_identity_limits(self):
        contact = self.prospect()
        token, _ = self.token(contact)
        visitor = "VIS-VISITOR111"
        self.store.open_session(visitor, token)
        self.store.save_insight(visitor, {"intent": "Pricing Question", "sentiment": "Positive", "priority": "High", "status": "Pricing Requested"})
        activity = self.client.get(f"/v1/contacts/{contact['contact_id']}/activity", headers=self.auth("outreach"))
        self.assertEqual(activity.json["conversations"][0]["insight"]["priority"], "High")
        self.assertEqual(activity.json["conversations"][0]["association"], "email_link_matched")
        self.assertFalse(self.store.contact(contact["contact_id"])["preferences"]["consent"])
        rows = [{"Visitor ID": visitor, "Contact Name": "Shared Name", "Email": "sample@example.com",
                 "Contact Consent": "Yes", "Preferred Contact Methods": "Email"},
                {"Visitor ID": "VIS-NOCONSENT1", "Email": "no@example.com", "Contact Consent": "No"}]
        self.assertEqual(reconcile_saved_contacts(rows, self.store), {"synced": 1, "skipped": 1})
        cursor = self.store.changes()["next_cursor"]
        reconcile_saved_contacts(rows, self.store)
        self.assertEqual(self.store.changes(cursor)["changes"], [])
        self.assertEqual(self.store.sessions()[0]["association"], "self_reported")

    def test_native_developer_panel_lists_contacts_and_creates_link_locally(self):
        from streamlit.testing.v1 import AppTest
        self.prospect()
        code = f'''
from shared_integration import SharedStore
from integration_chat import render_integration_panel
render_integration_panel(SharedStore(sqlite_path={str(self.path)!r}))
'''
        app = AppTest.from_string(code).run()
        self.assertFalse(app.exception)
        self.assertEqual(app.metric[0].value, "1")
        next(button for button in app.button if button.label == "Create chat link").click().run()
        self.assertFalse(app.exception)
        self.assertIn("chat_ref=", app.code[0].value)
        self.assertNotIn("sample@example.com", app.code[0].value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
