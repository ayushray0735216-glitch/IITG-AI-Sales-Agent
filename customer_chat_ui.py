"""Customer conversation presentation using native Streamlit controls."""

import hashlib
import re
from urllib.parse import urlparse

import streamlit as st


SUGGESTIONS = (
    ("Browse offerings", ":material/storefront:", "What products and services do you offer?"),
    ("Compare options", ":material/compare_arrows:", "Can you help me compare the options?"),
    ("Help me choose", ":material/help_outline:", "What details should I know before choosing?"),
)


THINKING_INDICATOR_HTML = """
<style>
.sales-chat-thinking { display: flex; align-items: center; gap: .6em; }
.sales-chat-thinking-dots { display: inline-flex; align-items: center; gap: .3em; min-height: 1.5em; }
.sales-chat-thinking-dot {
    display: inline-block; width: .4em; height: .4em; border-radius: 50%;
    background: currentColor; animation: sales-chat-bounce 1.2s ease-in-out infinite;
}
.sales-chat-thinking-dot:nth-child(2) { animation-delay: .15s; }
.sales-chat-thinking-dot:nth-child(3) { animation-delay: .3s; }
@keyframes sales-chat-bounce {
    0%, 60%, 100% { transform: translateY(0); opacity: .45; }
    30% { transform: translateY(-.35em); opacity: 1; }
}
@media (prefers-reduced-motion: reduce) {
    .sales-chat-thinking-dot { animation: none; }
}
</style>
<div class="sales-chat-thinking" role="status" aria-live="polite">
    <span>Thinking</span>
    <span class="sales-chat-thinking-dots" aria-hidden="true">
        <span class="sales-chat-thinking-dot"></span>
        <span class="sales-chat-thinking-dot"></span>
        <span class="sales-chat-thinking-dot"></span>
    </span>
</div>
"""


def render_pending_turn(slot, message, reply=None):
    """Send the visual turn to the browser before any slow CRM or AI work."""
    with slot.container():
        with st.chat_message("user", avatar=":material/person:", width="content"):
            st.caption("You")
            st.write(message)
        with st.chat_message("assistant", avatar=":material/support_agent:"):
            st.caption("Sales assistant")
            if reply is None:
                st.html(THINKING_INDICATOR_HTML)
            else:
                st.write(reply)


class ChatTurnDisplay:
    """Replace the transient thinking bubble before slower record keeping."""

    def __init__(self):
        self.slot = None

    def show_reply(self, message, reply):
        if self.slot is not None:
            render_pending_turn(self.slot, message, reply=reply)


def clean_value(value):
    text = str(value).strip() if value is not None else ""
    return "" if text.casefold() in {"", "nan", "none", "nat", "<na>"} else text


def offering_key(offering):
    return clean_value(offering.get("Offering ID")) or clean_value(offering.get("Offering Name"))


def saved_web_url(value):
    """Allow only usable web links saved in the catalog."""
    text = clean_value(value)
    if not text or re.search(r"[\s\x00-\x1f]", text):
        return ""
    if ":" not in text and "." in text.split("/")[0]:
        text = "https://" + text
    try:
        parsed = urlparse(text)
        parsed.port  # Validate the port as well as the hostname.
        return text if (
            parsed.scheme in {"https", "http"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ) else ""
    except ValueError:
        return ""


def offering_price(offering):
    price = clean_value(offering.get("Price"))
    if not price:
        return "Price not provided"
    if price.casefold() in {"free", "free of cost", "no charge"}:
        return "Free"
    try:
        if float(price.replace(",", "")) == 0:
            return "Free"
    except ValueError:
        pass
    return " ".join(filter(None, (price, clean_value(offering.get("Currency")))))


def offering_dates(offering):
    start = clean_value(offering.get("Start Date"))
    end = clean_value(offering.get("End Date"))
    return " – ".join(dict.fromkeys(value for value in (start, end) if value))


def mentioned_offering_keys(text, offerings):
    normalized = " " + re.sub(r"[^\w]+", " ", text.casefold()).strip() + " "
    return [
        offering_key(row) for row in offerings
        if clean_value(row.get("Offering Name"))
        and " " + re.sub(r"[^\w]+", " ", clean_value(row.get("Offering Name")).casefold()).strip() + " " in normalized
    ]


def render_offering_actions(offering, key, can_call):
    name = clean_value(offering.get("Offering Name"))
    response = None
    with st.container(horizontal=True, gap="small"):
        registration_url = saved_web_url(offering.get("Registration Link"))
        if registration_url:
            st.link_button("View registration", registration_url, icon=":material/open_in_new:")
        else:
            website = saved_web_url(offering.get("Website") or offering.get("Website URL"))
            if website:
                st.link_button("Visit website", website, icon=":material/open_in_new:")

        if st.button("Compare", icon=":material/compare_arrows:", key=f"{key}_compare"):
            selected = list(st.session_state.get("chat_compare_selection", []))
            identity = offering_key(offering)
            if identity not in selected:
                if len(selected) < 2:
                    st.session_state["chat_compare_selection"] = selected + [identity]
                else:
                    st.info("Two offerings are selected. Change your selection below.")

        with st.popover("Ask a question", icon=":material/chat:", key=f"{key}_ask_popover"):
            st.caption(f"Ask about {name}")
            with st.form(f"{key}_question_form", border=False):
                question = st.text_input("Your question", key=f"{key}_question")
                send_question = st.form_submit_button("Send question")
            if send_question:
                if question.strip():
                    response = f"About {name}: {question.strip()}"
                else:
                    st.warning("Enter a question first.")

        with st.popover("Request a call", icon=":material/call:", key=f"{key}_call_popover"):
            st.write(f"Would you like a call about {name}?")
            st.caption(
                "This can start a call to your saved phone number."
                if can_call else
                "We’ll ask you to share your phone number and choose Phone call in the follow-up form."
            )
            if st.button(
                "Call me now" if can_call else "Continue with call request",
                key=f"{key}_call", type="primary",
            ):
                response = f"Please call me now about {name}."

    if not registration_url:
        st.caption("No registration link is available for this offering. You can ask a question or request a follow-up.")
    return response


def render_offering_card(offering, key, can_call):
    with st.container(border=True):
        st.markdown(f"**{clean_value(offering.get('Offering Name'))}**")
        st.caption(" · ".join(filter(None, (
            clean_value(offering.get("Offering Type")),
            clean_value(offering.get("Lifecycle Status")),
        ))))
        st.write(f"**Price:** {offering_price(offering)}")
        dates = offering_dates(offering)
        times = " – ".join(filter(None, (
            clean_value(offering.get("Start Time")), clean_value(offering.get("End Time")),
        )))
        if dates:
            st.write(f"**Dates:** {dates}")
        if times:
            st.write(f"**Time:** {times}")
        for label, field in (("Duration", "Duration"), ("Certificate", "Certificate")):
            value = clean_value(offering.get(field))
            if value:
                st.write(f"**{label}:** {value}")
        with st.expander("View details"):
            available = False
            for label, field in (
                ("Description", "Description"), ("Topics", "Topics"),
                ("Specifications", "Product Details"), ("Who it’s for", "Target Audience"),
                ("FAQs", "FAQs"), ("Policies", "Policies"),
            ):
                value = clean_value(offering.get(field))
                if value:
                    available = True
                    st.markdown(f"**{label}**")
                    st.write(value)
            if not available:
                st.caption("More details haven’t been provided yet. Ask our team for help.")
        return render_offering_actions(offering, key, can_call)


def render_comparison_picker(offerings):
    by_id = {offering_key(row): row for row in offerings}
    selected = [key for key in st.session_state.get("chat_compare_selection", []) if key in by_id]
    st.session_state["chat_compare_selection"] = selected[:2]
    if not selected:
        return None
    with st.container(border=True):
        chosen = st.multiselect(
            "Choose two offerings to compare", list(by_id),
            format_func=lambda identity: clean_value(by_id[identity].get("Offering Name")),
            max_selections=2, key="chat_compare_selection",
        )
        if st.button("Compare selected offerings", disabled=len(chosen) != 2):
            names = [clean_value(by_id[identity].get("Offering Name")) for identity in chosen]
            return f"Compare {names[0]} and {names[1]}, including dates, duration, price, and who each is for."
    return None


def render_customer_chat(messages, greeting, offerings=None, can_call=False, queued_message=None, display=None):
    """Render the conversation and return a typed or suggested message."""
    suggested_message = None
    action_message = None
    offerings = offerings or []
    by_id = {offering_key(row): row for row in offerings}
    show_cards = bool(messages and messages[-1].get("presentation") == "offering_cards")
    with st.container(border=True, key="customer_chat_panel", gap="small"):
        st.badge("AI sales assistant", icon=":material/support_agent:", color="blue")
        st.caption(greeting)

        with st.container(
            height="content" if show_cards else (320 if len(messages) < 3 else 420),
            border=False,
            autoscroll=not show_cards,
            key="customer_chat_transcript",
        ):
            if not messages:
                with st.chat_message("assistant", avatar=":material/support_agent:"):
                    st.caption("Sales assistant")
                    st.write(
                        "Tell me what you’re looking for. I can help you explore "
                        "offerings, compare options, and check prices or dates."
                    )
            for index, message in enumerate(messages):
                is_customer = message["role"] == "user"
                with st.chat_message(
                    "user" if is_customer else "assistant",
                    avatar=":material/person:" if is_customer else ":material/support_agent:",
                    width="content" if is_customer else "stretch",
                ):
                    st.caption("You" if is_customer else "Sales assistant")
                    is_latest = index == len(messages) - 1 and not is_customer
                    keys = message.get("offering_ids", []) if is_latest and show_cards else []
                    if keys:
                        st.write("Explore our current offerings below. Open a card’s details or choose a next step.")
                        for identity in keys:
                            offering = by_id.get(identity)
                            if offering:
                                key = f"offering_{index}_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
                                action_message = render_offering_card(offering, key, can_call) or action_message
                            else:
                                st.caption("An offering from this answer is no longer available. Browse offerings for the current list.")
                    else:
                        st.write(message["content"])
                        if is_latest:
                            matches = mentioned_offering_keys(message["content"], offerings)
                            if len(matches) == 1:
                                key = f"reply_{index}_" + hashlib.sha256(matches[0].encode()).hexdigest()[:16]
                                action_message = render_offering_actions(by_id[matches[0]], key, can_call) or action_message

            pending_turn = st.empty()

        action_message = render_comparison_picker(offerings) or action_message

        with st.container(horizontal=True, gap="small"):
            for index, (label, icon, question) in enumerate(SUGGESTIONS):
                if st.button(
                    label,
                    icon=icon,
                    help=question,
                    key=f"quick_question_{index}",
                ):
                    suggested_message = question

        typed_message = st.chat_input(
            "Ask about offerings, prices, dates, or registration…",
            key="customer_chat_composer",
            submit_mode="disable",
        )
        st.caption("Sharing your name and contact details is optional.")

    submitted_message = typed_message or action_message or suggested_message or queued_message
    if submitted_message:
        render_pending_turn(pending_turn, submitted_message)
        if display is not None:
            display.slot = pending_turn
    return submitted_message
