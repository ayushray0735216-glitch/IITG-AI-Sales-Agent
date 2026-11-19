
"""Developer console controls for reviewing and tracking bulk email."""
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
import json
import mimetypes
import os

from psycopg2 import Error as PostgreSQLError
import streamlit as st

import bulk_email
import crm_storage as crm


def render_bulk_email(filtered_leads, library_dir, material_types, display_name, generate_draft, *, expanded=False):
    panel = st.expander("Bulk email", expanded=expanded, key="bulk_email", icon=":material/mail:", on_change="rerun")
    if not panel.open:
        return

    with panel:
        section = st.radio("Bulk email view", ["Compose batch", "Batch history"], horizontal=True,
                           key="bulk_email_section", label_visibility="collapsed")
        if section == "Compose batch":
            _compose(filtered_leads, library_dir, material_types, display_name, generate_draft)
        else:
            _render_results()


def _compose(filtered_leads, library_dir, material_types, display_name, generate_draft):
    scope = st.radio("Recipients", ["Choose leads", "All matching leads"], horizontal=True, key="bulk_email_scope")
    rows = filtered_leads.to_dict("records")
    if not rows:
        st.info("Change the Lead Database filters to find recipients.")
        return
    labels = [
        " · ".join(filter(None, (crm.lead_cell(lead.get("Name")), crm.lead_cell(lead.get("Email")),
                                crm.lead_cell(lead.get("Company"))))) or "Unnamed lead"
        for lead in rows
    ]
    options = list(range(len(rows)))
    selection_key = sha256(repr([(crm.lead_identity(lead), label) for lead, label in zip(rows, labels)]).encode()).hexdigest()[:16]
    selected = options if scope == "All matching leads" else st.multiselect(
        "Select leads", options, format_func=labels.__getitem__, key=f"bulk_email_selection_{selection_key}",
    )
    st.caption(f"{len(selected)} selected from {len(rows)} matching leads")
    subject_key = "bulk_email_subject"
    body_key = "bulk_email_body"
    with st.popover("Draft with AI", icon=":material/auto_awesome:"):
        purpose = st.text_input("Message purpose", placeholder="For example, invite leads to the next workshop")
        if st.button("Generate template", disabled=not purpose.strip(), key="bulk_email_generate"):
            try:
                with st.spinner("Drafting a reusable template…"):
                    subject, body = generate_draft(purpose.strip())
                st.session_state[subject_key] = subject
                st.session_state[body_key] = body
            except Exception:
                st.warning("A template could not be generated. You can enter the message directly.")

    subject = st.text_input("Subject", key=subject_key).strip()
    body = st.text_area("Message", key=body_key, height=170).strip()
    st.caption("Personalize with {name}, {company}, {job_title}, or {product}.")
    include_chat_link = st.checkbox(
        "Include each prospect's Chat with us link", value=True, key="bulk_email_include_chat_link",
        help="Each eligible recipient gets a different link to their own CRM chat.",
    )
    if include_chat_link:
        st.caption("The link will be added to the end of the email unless you already placed {chat_link} in the message.")
        if not os.getenv("CHAT_PUBLIC_BASE_URL"):
            st.info("A public HTTPS chat address is required before a batch with chat links can be reviewed or sent.")
    elif "{chat_link}" in body:
        st.warning("The message still contains {chat_link}. Remove it or turn on chat links before reviewing.")

    folder = Path(library_dir)
    library = {
        path.name: path for path in sorted(folder.iterdir())
        if path.is_file() and path.suffix.lower().lstrip(".") in material_types
    } if folder.is_dir() else {}
    selected_materials = st.multiselect("Approved library attachments", list(library), format_func=display_name)
    uploads = st.file_uploader("Upload shared attachments", type=material_types, accept_multiple_files=True,
                               help="Up to 15 MB per file and 17 MB combined.", key="bulk_email_uploads")
    if st.button("Review batch", type="primary", disabled=not (selected and subject and body), key="bulk_email_review"):
        st.session_state.pop("bulk_email_approved_review", None)
        try:
            attachments = []
            for name in selected_materials:
                path = library[name]
                if path.stat().st_size > bulk_email.MAX_FILE_BYTES:
                    raise ValueError(f"{display_name(name)} exceeds 15 MB.")
                attachments.append((display_name(name), path.read_bytes()))
            attachments.extend((Path(item.name.replace(chr(92), "/")).name, item.getvalue()) for item in (uploads or []))
            payload = [(name, data, mimetypes.guess_type(name)[0] or "application/octet-stream") for name, data in attachments]
            bulk_email.validate_attachments(payload)
            if not include_chat_link and "{chat_link}" in body:
                raise ValueError("Turn on chat links or remove {chat_link} from the message.")
            reviewed_body = body
            if include_chat_link and "{chat_link}" not in body:
                reviewed_body = body.rstrip() + "\n\nQuestions about this opportunity? [Chat with us]({chat_link})"
            reviewed = bulk_email.review_recipients([rows[i] for i in selected], subject, reviewed_body)
            st.session_state["bulk_email_approved_review"] = (uuid4().hex, subject, reviewed, payload)
        except ValueError as exc:
            st.error(str(exc))
        except (OSError, PostgreSQLError):
            st.error("The CRM or attachment library could not be read. Try again.")

    review = st.session_state.get("bulk_email_approved_review")
    if not review:
        return
    batch_id, reviewed_subject, reviewed, payload = review
    eligible = [row for row in reviewed if not row["reason"]]
    with st.container(border=True):
        st.subheader("Review snapshot")
        st.caption("Approval uses the recipients, message and files shown here. Review again to include edits.")
        st.write(f"**{len(eligible)} eligible · {len(reviewed) - len(eligible)} skipped**")
        st.dataframe([
            {"Lead": row["name"], "Email ID": row["email"], "Review": row["reason"] or "Ready"}
            for row in reviewed
        ], hide_index=True)
        if eligible:
            index = st.selectbox(
                "Preview recipient", list(range(len(eligible))),
                format_func=lambda i: eligible[i]["email"], key=f"bulk_preview_{batch_id}",
            )
            st.text(eligible[index]["subject"])
            st.text(eligible[index]["body"])
        for index, (name, data, mime) in enumerate(payload):
            st.download_button(f"Review {name}", data, file_name=name, mime=mime, on_click="ignore",
                               key=f"bulk_review_attachment_{batch_id}_{index}")
        configured = bool(os.getenv("EMAIL_ADDRESS") and os.getenv("EMAIL_PASSWORD"))
        if not configured:
            st.warning("Connect the sending mailbox before approving this batch.")
        if st.button("Approve and queue", icon=":material/send:", type="primary",
                     disabled=not (eligible and configured), key=f"bulk_email_approve_{batch_id}"):
            try:
                bulk_email.queue_batch(batch_id, reviewed_subject, reviewed, payload)
            except ValueError as exc:
                st.error(str(exc))
            except (OSError, PostgreSQLError):
                st.error("The batch could not be saved. Try again.")
            else:
                st.session_state.pop("bulk_email_approved_review", None)
                bulk_email.start_sender()
                st.success("Batch queued. Open Batch history to track each recipient.")


@st.fragment(run_every="10s")
def _render_results():
    try:
        batches = bulk_email.recent_batches()
        if not batches:
            st.caption("Approved batches will appear here.")
            return
        by_id = {row[0]: row for row in batches}
        batch_id = st.selectbox("Batch", list(by_id),
            format_func=lambda key: f"{by_id[key][1]} · {by_id[key][2][:16].replace('T', ' ')} UTC")
        filenames = json.loads(by_id[batch_id][3])
        if filenames:
            st.text("Attachments  " + ", ".join(filenames))
        rows = bulk_email.batch_recipients(batch_id)
        if bulk_email.queued_work_exists():
            bulk_email.start_sender()
        st.dataframe(rows, hide_index=True)
        st.caption("Sent means the mail server accepted the message.")
    except (OSError, PostgreSQLError):
        st.error("Batch history is temporarily unavailable.")


def render_record_email_consent(lead):
    email = crm.lead_cell(lead.get("Email"))
    if not email:
        return
    key = sha256(crm.lead_identity(lead).encode()).hexdigest()[:16]
    with st.popover("Record email opt-in"):
        with st.form(f"record_email_consent_{key}"):
            st.caption(f"Email address  {email}")
            source = st.text_input("Where did this person opt in?")
            recorded_on = st.date_input("Opt-in date", max_value=date.today())
            confirmed = st.checkbox("This person gave permission to email this address.")
            if st.form_submit_button("Save opt-in"):
                if not confirmed or not source.strip() or not recorded_on:
                    st.error("Confirm permission and enter its source and date.")
                    return
                try:
                    updated, cache_ready = crm.save_recorded_email_consent(lead, source, recorded_on.isoformat())
                except ValueError as exc:
                    st.error(str(exc))
                except (OSError, PostgreSQLError):
                    st.error("The CRM could not save this opt-in.")
                else:
                    if updated and cache_ready:
                        st.session_state.pop("leads", None)
                        st.session_state["bulk_email_consent_notice"] = True
                        st.rerun()
                    st.warning("The opt-in could not be reflected in the current lead view. Refresh the dashboard.")

