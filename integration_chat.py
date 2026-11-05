"""Streamlit bridge; shared contacts stay server-side until explicitly shared."""
from datetime import datetime
import uuid
from zoneinfo import ZoneInfo

from shared_integration import enabled, get_store


def prepare_chat_entry(params, state, store=None):
    """Consume an attribution reference without loading or exposing past chats."""
    reference = params.get("chat_ref")
    if not reference:
        return None
    del params["chat_ref"]
    visitor_id = "VIS-" + uuid.uuid4().hex[:10].upper()
    session = (store or get_store()).open_session(visitor_id, reference)
    # A referral starts a separate visitor: never carry a previous person's
    # contact form, queued message, or comparison selection into this chat.
    for key in list(state):
        if str(key).startswith(("visitor_contact_", "chat_comparison_view_")) or key in {
            "visitor_followup_preferences", "registration_contact_prompted",
            "pending_customer_message", "chat_compare_selection",
        }:
            state.pop(key, None)
    state.update(visitor_id=visitor_id, customer_name="", customer_messages=[],
                 customer_lead=None, customer_started=True, shared_chat_session=session)
    return session


def save_shared_message(row, store=None):
    store = store or get_store()
    visitor_id = row.get("visitor_id", "")
    store.open_session(visitor_id)
    message = {key: row[key] for key in ("message_id", "role", "content", "timestamp", "lead") if key in row}
    if message.get("timestamp"):
        parsed = datetime.fromisoformat(message["timestamp"])
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        message["timestamp"] = parsed.astimezone(ZoneInfo("UTC")).isoformat()
    return store.save_message(visitor_id, message)


def share_contact_preferences(visitor_id, name, email, phone, whatsapp, methods, store=None):
    """Called only after the existing explicit opt-in form succeeds."""
    store = store or get_store()
    fields = {"name": name}
    if "Email" in methods:
        fields["email"] = email
    if set(methods) & {"Phone call", "SMS"}:
        fields["phone"] = phone
    if "WhatsApp" in methods:
        fields["whatsapp"] = whatsapp
    contact = store.upsert_contact("chat", visitor_id, fields)
    channel_names = {"Email": "email", "Phone call": "phone", "SMS": "sms", "WhatsApp": "whatsapp"}
    store.set_preferences(contact["contact_id"], [channel_names[m] for m in methods], True,
                          "Customer submitted the chat contact-preferences opt-in form.")
    store.open_session(visitor_id)
    return store.bind_self_reported_contact(visitor_id, contact["contact_id"])


def shared_visitor_profiles(store=None):
    store = store or get_store()
    contacts = {row["contact_id"]: row for row in store.contacts()}
    profiles = {}
    for session in store.sessions():
        contact = contacts.get(session.get("contact_id"))
        if not contact:
            continue
        profiles[session["visitor_id"]] = {
            "Name": contact["name"], "Contact Name": contact["name"], "Email": contact["email"],
            "Phone": contact["phone"], "WhatsApp Number": contact.get("whatsapp", ""),
            "Contact ID": contact["contact_id"], "Identity association": session["association"],
        }
    return profiles


def shared_history_rows(store=None):
    """Display central and legacy transcripts without duplicate messages or mixed zones."""
    rows = []
    for row in (store or get_store()).messages():
        row = dict(row)
        row["timestamp"] = datetime.fromisoformat(row["timestamp"]).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
        rows.append(row)
    return rows


def reconcile_saved_history(rows, store=None):
    """Recover from a secondary sync failure using the original durable transcript."""
    store = store or get_store()
    synced, skipped = 0, 0
    for row in rows:
        if not row.get("visitor_id") or not row.get("message_id") or row.get("role") not in {"user", "assistant"}:
            skipped += 1
            continue
        try:
            save_shared_message(row, store)
            synced += 1
        except (ValueError, TypeError):
            skipped += 1
    return {"synced": synced, "skipped": skipped}


def reconcile_saved_contacts(rows, store=None):
    store = store or get_store()
    synced, skipped = 0, 0
    allowed = {"Email", "Phone call", "SMS", "WhatsApp"}
    for row in rows:
        methods = [value.strip() for value in str(row.get("Preferred Contact Methods", "")).split(",") if value.strip() in allowed]
        if not row.get("Visitor ID") or str(row.get("Contact Consent", "")).casefold() != "yes" or not methods:
            skipped += 1
            continue
        try:
            share_contact_preferences(row["Visitor ID"], row.get("Contact Name") or row.get("Name", ""),
                row.get("Email", ""), row.get("Phone", ""), row.get("WhatsApp Number", ""), methods, store)
            synced += 1
        except (ValueError, TypeError):
            skipped += 1
    return {"synced": synced, "skipped": skipped}


def render_integration_panel(store=None):
    import pandas as pd
    import streamlit as st
    with st.expander("Shared module connections", expanded=False):
        if not enabled() and store is None:
            st.info("Shared integration is not enabled yet. The teammate connection guide is in INTEGRATION_GUIDE.md.")
            return
        try:
            store = store or get_store()
            if st.button("Refresh shared records", key="shared_refresh"):
                st.rerun()
            contacts, sessions = store.contacts(), store.sessions()
            st.caption("Prospect discovery and messaging modules connect through the shared API. A configured connection is not proof that a teammate module has completed its setup.")
            st.metric("Shared contacts", len(contacts))
            if contacts:
                st.dataframe(pd.DataFrame([
                    {"Contact ID": c["contact_id"], "Name": c["name"], "Email": c["email"], "Phone": c["phone"],
                     "Qualification": c["qualification"], "Contact consent": c["preferences"]["consent"]}
                    for c in contacts
                ]), hide_index=True, width="stretch")
                lookup = {c["contact_id"]: c for c in contacts}
                chosen = st.selectbox("Contact for a personalised chat link", list(lookup),
                    format_func=lambda identity: lookup[identity]["name"] or lookup[identity]["email"] or lookup[identity]["phone"], key="shared_link_contact")
                campaign = st.text_input("Campaign reference", key="shared_link_campaign")
                if st.button("Create chat link", key="shared_create_link"):
                    import os
                    link = store.create_link(chosen, campaign, os.getenv("INTEGRATION_PUBLIC_CHAT_URL", "https://iitg-ai-sales-agent.streamlit.app/"))
                    st.code(link["chat_url"], language=None)
                    st.caption("This link associates a new chat with the email recipient’s record. It does not verify identity, disclose previous chats, or grant contact consent.")
            else:
                st.info("No shared prospects have been received yet.")
            if sessions:
                st.dataframe(pd.DataFrame(sessions), hide_index=True, width="stretch")
            outcomes = store.communications()
            if outcomes:
                st.caption("Recorded delivery outcomes from the connected messaging module")
                st.dataframe(pd.DataFrame([{**row["data"], "Recorded at": row["recorded_at"]}
                                            for row in outcomes]), hide_index=True, width="stretch")
            if st.button("Sync saved visitor details and chats", key="shared_reconcile"):
                from crm_storage import crm_path
                from pathlib import Path
                path = Path(crm_path("conversation_history.csv"))
                rows = pd.read_csv(path, dtype=str, keep_default_na=False).to_dict("records") if path.exists() else []
                result = reconcile_saved_history(rows, store)
                lead_path = Path(crm_path("leads.csv"))
                lead_rows = pd.read_csv(lead_path, dtype=str, keep_default_na=False).to_dict("records") if lead_path.exists() else []
                contact_result = reconcile_saved_contacts(lead_rows, store)
                st.success(f"Synchronized {result['synced']} messages; {result['skipped']} legacy or invalid rows need separate review.")
                st.caption(f"Synchronized {contact_result['synced']} opted-in visitor profiles. Other profiles were left unchanged.")
            with st.expander("Recent synchronization events"):
                st.dataframe(pd.DataFrame(store.recent_changes()), hide_index=True, width="stretch")
        except Exception as exc:
            st.error(f"Shared integration is unavailable ({type(exc).__name__}). Check the connection configuration.")
