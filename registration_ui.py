"""Customer event signup and developer registration overview."""

from __future__ import annotations

import os
from urllib.parse import urlencode

import streamlit as st

import chat_access
import crm_storage
import registration_workflow as registrations
from sharngon_brand import render_brand_header


def signup_url(offering_id):
    base = os.getenv("REGISTRATION_PUBLIC_BASE_URL") or os.getenv("CHAT_PUBLIC_BASE_URL") or "http://localhost:8501"
    return base.rstrip("/") + "/?" + urlencode({"signup": offering_id})


def _offering(offering_id):
    catalog = crm_storage.load_offerings_data()
    if catalog.empty or "Offering ID" not in catalog:
        return None
    matches = catalog.loc[catalog["Offering ID"].astype(str).eq(str(offering_id))]
    return matches.iloc[0].to_dict() if len(matches) == 1 else None


def render_signup(offering_id):
    render_brand_header("customer")
    offering = _offering(offering_id)
    if not offering or not registrations.eligible_offering(offering):
        st.error("This registration link is unavailable or the event has closed.")
        st.stop()
    st.title(f"Register for {offering['Offering Name']}")
    start = registrations.parse_start(offering)
    st.write(f"**When:** {start.strftime('%d %B %Y at %I:%M %p IST')}")
    description = str(offering.get("Description") or "").strip()
    if description and description.casefold() != "nan":
        st.write(description)
    st.caption("Your registration is confirmed only after you verify your email address.")
    name = st.text_input("Full name", key="event_signup_name")
    email = st.text_input("Email address", key="event_signup_email")
    reminder = st.checkbox("Email me a reminder before this event", value=True, key="event_signup_reminder")
    if st.button("Send verification code", type="primary", key="event_signup_send_code"):
        try:
            address = chat_access.clean_email(email)
            if not name.strip() or not address:
                raise ValueError("Enter your name and a valid email address.")
            challenge = chat_access.request_code(address, send=registrations.send_verification_code)
            st.session_state["event_signup_pending"] = {
                "offering_id": str(offering_id), "name": name.strip(),
                "email": address, "reminder": reminder, "challenge": challenge,
            }
            st.success("We sent a six-digit code. Check your inbox.")
        except ValueError as exc:
            st.error(str(exc))
        except Exception:
            st.error("The verification email could not be sent right now. Please try again later.")
    pending = st.session_state.get("event_signup_pending")
    if pending and pending["offering_id"] == str(offering_id):
        code = st.text_input("Verification code", max_chars=6, key="event_signup_code")
        if st.button("Verify and register", type="primary", key="event_signup_verify"):
            try:
                result = chat_access.verify_code(pending["challenge"], code)
            except Exception:
                st.error("Verification is temporarily unavailable. Please try again later.")
                st.stop()
            if not result or result["email"] != pending["email"]:
                st.error("The code is invalid or expired. Request a new code if needed.")
            else:
                del st.session_state["event_signup_pending"]
                try:
                    _, existing = registrations.register(offering, pending["name"], pending["email"], pending["reminder"])
                    st.session_state["event_signup_success"] = (str(offering_id), pending["email"], existing)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
                except Exception:
                    st.error("Your email was verified, but the confirmation could not be completed. Please contact the team before registering again.")
    success = st.session_state.get("event_signup_success")
    if success and success[0] == str(offering_id):
        st.success("You are registered. A confirmation email has been submitted to the sending mailbox.")
        if success[2]:
            st.caption("You were already registered for this event; no duplicate registration was created.")


def render_registration_admin(offerings):
    st.subheader("Automated event registrations")
    st.caption("Share a Sharngon signup link for a published future event. Verified signups receive a confirmation; opted-in reminders are sent in the 24 hours before the event.")
    notice = st.session_state.pop("event_reminder_notice", None)
    if notice:
        st.success(notice)
    eligible = [row.to_dict() for _, row in offerings.iterrows() if registrations.eligible_offering(row)]
    if not eligible:
        st.info("Publish a future webinar, workshop, seminar, course, program, or event with a start date to create a signup link.")
    for offering in eligible:
        with st.container(border=True):
            st.write(f"**{offering['Offering Name']}**")
            url = signup_url(str(offering["Offering ID"]))
            st.code(url, language=None)
            if url.startswith("http://localhost"):
                st.caption("Local demo link. Set REGISTRATION_PUBLIC_BASE_URL to your public HTTPS app address before sharing outside this computer.")
    try:
        records = registrations.list_registrations()
    except (OSError, RuntimeError) as exc:
        st.error(f"Registrations are temporarily unavailable: {exc}")
        return
    if records:
        st.dataframe([{
            "Name": row["customer_name"], "Email": row["email"], "Event": row["offering_name"],
            "Registered": row["registered_at"], "Confirmation": row["confirmation_state"],
            "Reminder opted in": bool(row["reminder_opt_in"]), "Reminder": row["reminder_state"],
        } for row in records], hide_index=True, width="stretch")
        if st.button("Check due reminders now", key="event_reminder_check"):
            sent = registrations.send_due_reminders()
            st.session_state["event_reminder_notice"] = (
                f"Reminder check complete. {sent} due reminder(s) submitted to the sending mailbox."
            )
            st.rerun()
    else:
        st.caption("No verified event signups yet.")
