
"""Mailbox views and controls for the CRM's customer correspondence."""
from hashlib import sha256
from html import escape
import re

import pandas as pd
import streamlit as st

import crm_storage as crm


PAGE_SIZE = 15
FOLDERS = ("Inbox", "Needs review", "Starred", "Drafts", "Sent", "Archived")
FOLDER_ICONS = {
    "Inbox": "inbox", "Needs review": "pending_actions", "Starred": "star",
    "Drafts": "draft", "Sent": "send", "Archived": "archive",
}


def mailbox_state_id(message_id):
    return "mailbox-state:" + sha256(message_id.encode()).hexdigest()


def plain(value):
    return crm.lead_cell(value)


def message_text(message):
    """Keep quoted history available without leading the reading pane with it."""
    match = re.search(r"(?mi)^(?:On .{1,400}wrote:\s*$|>+ ?|-----Original Message-----)", message)
    if match and message[:match.start()].strip():
        return message[:match.start()].strip(), message[match.start():].strip()
    return message, ""


def show_message(record, outbound=False):
    title = plain(record.get("subject")) or "(No subject)"
    address = plain(record.get("destination" if outbound else "sender_email"))
    sender = ("To " + address) if outbound else (plain(record.get("sender_name")) or address or "Customer")
    initial = escape((sender.lstrip() or "?")[0].upper())
    date = plain(record.get("updated_at" if outbound else "received_at"))
    st.html(
        f'<div class="crm-mail-message-head"><h3>{escape(title)}</h3>'
        f'<div class="crm-mail-sender"><span class="crm-mail-avatar">{initial}</span>'
        f'<div><strong>{escape(sender)}</strong>'
        f'<small>{escape(address) if not outbound and address != sender else ""}</small></div>'
        f'<time>{escape(date)}</time></div></div>'
    )
    status = "Accepted by mail server" if outbound else plain(record.get("status")) or "Needs review"
    st.badge(status, color="green" if outbound or status == "Replied" else "gray" if status == "Opted out" else "orange")
    body, quote = message_text(plain(record.get("body")))
    st.html(f'<div class="crm-mail-body">{escape(body or "Message body unavailable.")}</div>')
    if quote:
        with st.expander("Quoted message history"):
            st.html(f'<div class="crm-mail-body">{escape(quote)}</div>')
    if outbound:
        names = record.get("attachment_names")
        if isinstance(names, list) and names:
            st.caption("Attachments")
            for name in names:
                st.badge(plain(name), icon=":material/attach_file:")
    else:
        fields = (("Lead", "lead_name"), ("Intent", "intent"), ("Sentiment", "sentiment"),
                  ("Priority", "priority"), ("Lead status", "lead_status"))
        details = "".join(
            f'<div><dt>{label}</dt><dd>{escape(value)}</dd></div>'
            for label, key in fields if (value := plain(record.get(key)))
        )
        if details:
            st.html(f'<dl class="crm-mail-details">{details}</dl>')


def matches_folder(row, folder):
    flags = row["_flags"]
    if row.get("_compose"):
        return folder == "Drafts"
    if folder == "Archived":
        return bool(flags.get("archived"))
    if flags.get("archived"):
        return False
    if folder == "Starred":
        return bool(flags.get("starred"))
    if folder == "Drafts":
        return bool(flags.get("draft_body") or flags.get("draft_subject")) and row["status"] not in {"Replied", "Opted out"}
    if folder == "Needs review":
        return row["status"] not in {"Replied", "Opted out"}
    return True


def filtered_messages(records, folder, query):
    result = []
    terms = query.casefold().split()
    for record in records:
        if folder != "Sent" and not matches_folder(record, folder):
            continue
        haystack = " ".join(plain(record.get(field)) for field in (
            "sender_name", "sender_email", "destination", "lead_name", "subject", "body",
        )).casefold()
        if all(term in haystack for term in terms):
            result.append(record)
    return result


def render_message_list(records, folder, query):
    """Render one page of selectable rows and return the selected record."""
    if not records:
        st.info("No messages match this search." if query else f"No messages in {folder.lower()}.")
        return None
    signature = sha256(repr((folder, query)).encode()).hexdigest()[:12]
    page_key = f"crm_mail_page_{signature}"
    page_count = (len(records) + PAGE_SIZE - 1) // PAGE_SIZE
    page = min(st.session_state.get(page_key, 0), page_count - 1)
    offset = page * PAGE_SIZE
    shown = records[offset:offset + PAGE_SIZE]
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"{offset + 1}–{offset + len(shown)} of {len(records)}")
        if st.button("Previous", icon=":material/chevron_left:", disabled=page == 0, key=f"mail_prev_{signature}"):
            st.session_state[page_key] = page - 1
            st.rerun()
        if st.button("Next", icon=":material/chevron_right:", disabled=page + 1 >= page_count, key=f"mail_next_{signature}"):
            st.session_state[page_key] = page + 1
            st.rerun()

    rows = []
    for record in shown:
        outbound = folder == "Sent"
        address = plain(record.get("destination" if outbound else "sender_email"))
        sender = address if outbound else plain(record.get("sender_name")) or address or "Customer"
        flags = record.get("_flags", {})
        status = "Sent" if outbound else plain(record.get("status")) or "Needs review"
        if not outbound and not flags.get("read") and status != "Replied":
            status = "● " + status
        snippet = " ".join(message_text(plain(record.get("body")))[0].split())[:100]
        subject = plain(record.get("subject")) or "(No subject)"
        rows.append({
            "From" if not outbound else "To": ("★ " if flags.get("starred") else "") + sender,
            "Subject": subject + (f"  ·  {snippet}" if snippet else ""),
            "Date": plain(record.get("updated_at" if outbound else "received_at"))[:16].replace("T", " "),
            "Status": status,
        })
    view = pd.DataFrame(rows).style.map(
        lambda status: (
            "color: #176550; background-color: #dff5ed" if status in {"Replied", "Sent"}
            else "color: #81510c; background-color: #fff1d7" if "review" in status.casefold() or status.startswith("●")
            else ""
        ), subset=["Status"],
    )
    identity = sha256(repr([row["_id"] for row in shown]).encode()).hexdigest()[:12]
    grid = st.dataframe(
        view, key=f"crm_mail_rows_{signature}_{identity}", hide_index=True, on_select="rerun",
        selection_mode="single-row", row_height=43, height=min(360, 46 + 43 * len(rows)), width="stretch",
        column_config={
            "From": st.column_config.TextColumn(width=120),
            "To": st.column_config.TextColumn(width=145),
            "Subject": st.column_config.TextColumn(width="medium"),
            "Date": st.column_config.TextColumn(width=132),
            "Status": st.column_config.TextColumn(width=118),
        },
    )
    return shown[grid.selection.rows[0]] if grid.selection.rows and grid.selection.rows[0] < len(shown) else None

