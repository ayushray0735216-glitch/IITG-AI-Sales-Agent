"""Shared CRM email transport and send-time contact checks."""
import html
import os
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import autopilot
import crm_storage
from crm_storage import load_autopilot_settings, load_contact_suppressions, record_communication_event, set_contact_suppression

IST = ZoneInfo("Asia/Kolkata")


def email_markdown_to_html(body):
    """Render a small, safe Markdown subset as readable email HTML."""
    def inline_markup(value):
        escaped = html.escape(str(value), quote=True)
        escaped = re.sub(
            r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
            lambda match: (
                f'<a href="{html.escape(match.group(2), quote=True)}">{match.group(1)}</a>'
                if urlparse(match.group(2)).scheme in {"http", "https"}
                else match.group(1)
            ),
            escaped,
        )
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
        escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
        return escaped

    blocks = []
    paragraph = []
    list_kind = None
    list_items = []

    def flush_paragraph():
        if paragraph:
            blocks.append("<p>" + "<br>\n".join(inline_markup(line) for line in paragraph) + "</p>")
            paragraph.clear()

    def flush_list():
        nonlocal list_kind, list_items
        if list_items:
            blocks.append(
                f"<{list_kind}>" + "".join(f"<li>{inline_markup(item)}</li>" for item in list_items)
                + f"</{list_kind}>"
            )
        list_kind = None
        list_items = []

    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush_paragraph()
            flush_list()
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.+)$", raw_line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.+)$", raw_line)
        if bullet or numbered:
            flush_paragraph()
            kind = "ul" if bullet else "ol"
            if list_kind and list_kind != kind:
                flush_list()
            list_kind = kind
            list_items.append((bullet or numbered).group(1))
        else:
            flush_list()
            paragraph.append(line)
    flush_paragraph()
    flush_list()
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:1.6;color:#202938">'
        + "\n".join(blocks)
        + "</div>"
    )


def email_markdown_to_text(body):
    """Create a readable plain-text alternative without visible Markdown syntax."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", str(body or ""))
    text = re.sub(r"(?<!\*)\*\*(.+?)\*\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    text = re.sub(r"(?m)^\s*(\d+)[.)]\s+", r"\1. ", text)
    return text.strip()


def contact_guard_for_destination(channel, destination, requested_reply=False):
    """Recheck CRM consent, destination, suppression, and outreach cadence at send time."""
    destination = str(destination or "").strip()
    try:
        leads = crm_storage.load_crm_table("leads.csv")
        # Replies to a synced inbound email are not scheduled outreach.
        activity = None if requested_reply else crm_storage.load_crm_table("sales_activity.csv")
        field = "Email" if channel == "email" else "Phone"
        if leads.empty or field not in leads.columns:
            return None, "No matching CRM lead was found; no message or call was started."
        values = leads[field].fillna("").astype(str).str.strip().str.casefold()
        matches = leads.loc[values.eq(destination.casefold())]
        if len(matches.index) != 1:
            return None, "The destination must match exactly one CRM lead; no message or call was started."
        lead = matches.iloc[0]
        reason = autopilot.contact_action_block_reason(
            lead,
            channel,
            destination,
            activity=activity,
            settings=load_autopilot_settings(),
            suppressions=load_contact_suppressions(),
            now=datetime.now(IST),
        )
        return (lead.to_dict(), reason)
    except Exception as exc:
        print(f"Contact eligibility check failed closed: {exc}")
        return None, "The CRM could not verify consent and contact eligibility; no message or call was started."


def send_email(
    to_email, subject, body, attachments=None, in_reply_to=None, references=None,
    review_id=None, expected_lead_key="", bulk=False,
):
    """Send through Gmail and record the provider outcome independently of CRM follow-up."""
    sender_email = os.getenv("EMAIL_ADDRESS")
    sender_password = os.getenv("EMAIL_PASSWORD")
    if not sender_email or not sender_password:
        return "Email credentials not configured."

    message_id = make_msgid(domain=sender_email.rsplit("@", 1)[-1])
    event_id = review_id or f"email:{message_id}"
    event = {
        "event_id": event_id, "provider": "gmail_smtp",
        "provider_id": message_id, "message_id": message_id,
        "channel": "email", "direction": "outbound", "destination": str(to_email),
        "subject": str(subject), "in_reply_to": str(in_reply_to or ""),
        "attachment_names": [item[0] for item in attachments or ()],
        "source": "Bulk CRM email" if bulk else "Reviewed CRM email" if review_id else "CRM email send",
    }
    if review_id:
        event["body"] = body
    lead_for_email, block_reason = contact_guard_for_destination(
        "email", to_email, requested_reply=bool(in_reply_to and review_id),
    )
    if expected_lead_key and (
        not lead_for_email or crm_storage.lead_identity(lead_for_email) != expected_lead_key
    ):
        block_reason = "The CRM recipient changed after batch approval."
    if lead_for_email:
        event["lead_name"] = crm_storage.lead_cell(lead_for_email.get("Name"))
        event["visitor_id"] = crm_storage.lead_cell(lead_for_email.get("Visitor ID"))

    def log_outcome(status, outcome):
        record_communication_event({
            **event, "status": status, "outcome": outcome, "updated_at": datetime.now(IST).isoformat(),
        })

    try:
        if review_id and not crm_storage.claim_reviewed_email({
            **event, "updated_at": datetime.now(IST).isoformat(),
        }):
            return "This message was already submitted. Check the CRM send status before sending again."
        if block_reason:
            log_outcome("blocked", block_reason)
            return f"Email blocked: {block_reason}"
    except Exception:
        return "The CRM could not reserve this email. Check its status before trying again."

    submitted = False
    accepted = False
    try:
        msg = EmailMessage()
        msg["Message-ID"] = message_id
        msg["From"] = sender_email
        msg["To"] = to_email
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references
        msg.set_content(email_markdown_to_text(body))
        msg.add_alternative(email_markdown_to_html(body), subtype="html")
        for filename, file_bytes, content_type in attachments or ():
            maintype, subtype = content_type.split("/", 1)
            msg.add_attachment(file_bytes, maintype=maintype, subtype=subtype, filename=filename)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(sender_email, sender_password)
            submitted = True
            refused = smtp.send_message(msg)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            accepted = True
    except Exception as exc:
        if not accepted:
            definite_failure = not submitted or isinstance(
                exc, (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError)
            )
            outcome = (
                "SMTP rejected the message or could not be reached. Review and retry."
                if definite_failure else
                "SMTP acceptance could not be confirmed. Check the provider before sending again."
            )
            try:
                log_outcome("failed" if definite_failure else "delivery_unknown", outcome)
                if isinstance(exc, smtplib.SMTPRecipientsRefused) and any(
                    response[0] in {550, 551, 553} for response in exc.recipients.values()
                ):
                    set_contact_suppression(
                        to_email, "email", "Recipient rejected as invalid by SMTP (5xx)",
                        source="SMTP permanent recipient rejection",
                    )
            except Exception:
                return "The CRM could not record the outcome. Check the provider before sending again."
            return f"Email {'failed' if definite_failure else 'needs review'}: {outcome}"

    try:
        log_outcome("accepted_by_smtp", "SMTP accepted the message; final inbox delivery is unconfirmed.")
    except Exception:
        return "SMTP accepted the message, but its CRM receipt could not be saved. Check the provider before sending again."
    return "Email sent successfully."



