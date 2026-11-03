"""Customer conversation presentation using native Streamlit controls."""

import hashlib
import re
from contextlib import contextmanager

import streamlit as st
from chat_followups import build_chat_suggestions
from offering_facts import (
    EVENT_TYPES, MISSING, clean_value, is_visible_offering, offering_dates,
    markdown_text, offering_key, offering_lifecycle, offering_price, offering_times, saved_web_url,
)

CHAT_UI_VERSION = "2026-11-03-chat-bubble-alignment-v6"


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
<div class="sales-chat-thinking" role="status" aria-live="polite" aria-atomic="true" aria-busy="true">
    <span>Sales assistant is preparing a reply</span>
    <span class="sales-chat-thinking-dots" aria-hidden="true">
        <span class="sales-chat-thinking-dot"></span>
        <span class="sales-chat-thinking-dot"></span>
        <span class="sales-chat-thinking-dot"></span>
    </span>
</div>
"""


@contextmanager
def message_bubble(role):
    """Align the bubble itself while keeping its text naturally readable."""
    is_customer = role == "user"
    with st.container(
        horizontal=True,
        horizontal_alignment="right" if is_customer else "left",
        gap=None,
    ):
        with st.chat_message(
            "user" if is_customer else "assistant",
            avatar=":material/person:" if is_customer else ":material/support_agent:",
            width="content" if is_customer else "stretch",
        ):
            yield


def render_pending_turn(slot, message, reply=None):
    """Send the visual turn to the browser before any slow CRM or AI work."""
    with slot.container():
        with message_bubble("user"):
            st.caption("You")
            st.write(message)
        with message_bubble("assistant"):
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


def mentioned_offering_keys(text, offerings):
    normalized = " " + re.sub(r"[^\w]+", " ", text.casefold()).strip() + " "
    return [
        offering_key(row) for row in offerings
        if clean_value(row.get("Offering Name"))
        and " " + re.sub(r"[^\w]+", " ", clean_value(row.get("Offering Name")).casefold()).strip() + " " in normalized
    ]


def render_offering_actions(offering, key, can_call):
    if not is_visible_offering(offering):
        st.caption("This offering is no longer available. Refresh the offering list for current options.")
        return None
    name = clean_value(offering.get("Offering Name"))
    label_name = markdown_text(name)
    response = None
    with st.container(gap="small"):
        registration_url = saved_web_url(offering.get("Registration Link"))
        if registration_url:
            st.link_button(f"Register: {label_name}", registration_url, icon=":material/open_in_new:", width="stretch")
        else:
            website = saved_web_url(offering.get("Website") or offering.get("Website URL"))
            if website:
                st.link_button(f"Website: {label_name}", website, icon=":material/open_in_new:", width="stretch")

        if st.button(f"Compare {label_name}", icon=":material/compare_arrows:", key=f"{key}_compare", width="stretch"):
            selected = list(st.session_state.get("chat_compare_selection", []))
            identity = offering_key(offering)
            if identity not in selected:
                if len(selected) < 2:
                    st.session_state["chat_compare_selection"] = selected + [identity]
                    st.success(f"{name} added to comparison. Choose two offerings below.")
                else:
                    st.info("Two offerings are selected. Change your selection below.")

        with st.popover(f"Ask about {label_name}", icon=":material/chat:", key=f"{key}_ask_popover", width="stretch"):
            st.caption(f"Ask about {name}")
            with st.form(f"{key}_question_form", border=False):
                question = st.text_input(f"Your question about {name}", key=f"{key}_question")
                send_question = st.form_submit_button(f"Send question about {label_name}", width="stretch")
            if send_question:
                if question.strip():
                    response = f"About {name}: {question.strip()}"
                else:
                    st.warning(f"Enter a question about {name} before sending.")

        with st.popover(f"Request call about {label_name}", icon=":material/call:", key=f"{key}_call_popover", width="stretch"):
            st.write(f"Would you like a call about {name}?")
            st.caption(
                "This can start a call to your saved phone number."
                if can_call else
                "We’ll ask you to share your phone number and choose Phone call in the follow-up form."
            )
            if st.button(
                f"Call me now about {label_name}" if can_call else f"Continue with call request for {label_name}",
                key=f"{key}_call", type="primary", width="stretch",
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
            offering_lifecycle(offering),
        ))))
        st.write(f"**Price:** {offering_price(offering)}")
        dates = offering_dates(offering)
        times = offering_times(offering)
        is_event = clean_value(offering.get("Offering Type")).casefold() in EVENT_TYPES
        if dates != MISSING or is_event:
            st.write(f"**Dates:** {dates}")
        if times != MISSING or is_event:
            st.write(f"**Time:** {times}")
        for label, field in (("Duration", "Duration"), ("Certificate", "Certificate")):
            value = clean_value(offering.get(field))
            if value:
                st.write(f"**{label}:** {value}")
        with st.expander(f"Details, FAQs and policies: {markdown_text(clean_value(offering.get('Offering Name')))}"):
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


def clear_comparison_selection():
    st.session_state.pop("chat_compare_selection", None)


def render_comparison_picker(offerings, force=False):
    by_id = {offering_key(row): row for row in offerings}
    selected = [key for key in st.session_state.get("chat_compare_selection", []) if key in by_id]
    st.session_state["chat_compare_selection"] = selected[:2]
    if not selected and not force:
        return None
    if len(by_id) < 2:
        st.caption("At least two current offerings are needed for a comparison.")
        return None
    with st.container(border=True):
        chosen = st.multiselect(
            "Choose two offerings to compare", list(by_id),
            format_func=lambda identity: clean_value(by_id[identity].get("Offering Name")),
            max_selections=2, key="chat_compare_selection",
        )
        valid_chosen = [identity for identity in chosen if identity in by_id]
        if st.button("Compare selected offerings", disabled=len(valid_chosen) != 2):
            if len(valid_chosen) != 2:
                return None
            names = [clean_value(by_id[identity].get("Offering Name")) for identity in valid_chosen]
            return f"Compare {names[0]} and {names[1]}, including dates, duration, price, and who each is for."
        st.button("Clear comparison", on_click=clear_comparison_selection)
    return None


def split_chat_history(messages, visible_messages=6):
    """Keep recent exchanges readable without deleting or reordering history."""
    start = max(0, len(messages) - visible_messages)
    if start and messages[start].get("role") == "assistant":
        start -= 1
    return messages[:start], messages[start:], start


def render_saved_comparison(message, index):
    columns = message.get("comparison_columns", [])
    if not isinstance(columns, list) or len(columns) != 2:
        st.write(message["content"])
        return
    view = st.segmented_control(
        "Comparison display", ["Cards", "Table"], default="Cards", required=True,
        key=f"chat_comparison_view_{index}", width="stretch",
        help="Cards sit side by side on wide screens and stack on narrow screens. Table shows the saved answer.",
    )
    if view == "Table":
        st.write(message["content"])
        return
    st.write("Compare the saved offering details:")
    for slot, column in zip(st.columns(2, gap="small", vertical_alignment="top"), columns):
        with slot, st.container(border=True):
            st.markdown(f"**{markdown_text(column['name'])}**")
            for label, value in column["facts"]:
                st.markdown(f"**{label}:** {value}")
            st.markdown(f"**Registration:** {column['registration']}")
    st.write("“Not provided” means that detail hasn’t been confirmed here.")


def render_transcript_message(message, index, by_id, offerings, can_call, is_latest=False):
    is_customer = message.get("role") == "user"
    response = None
    with message_bubble("user" if is_customer else "assistant"):
        st.caption("You" if is_customer else "Sales assistant")
        presentation = message.get("presentation") if is_latest and not is_customer else None
        keys = message.get("offering_ids", []) if presentation == "offering_cards" else []
        if keys:
            st.write("Explore our current offerings. Open details or choose a next step.")
            for identity in keys:
                offering = by_id.get(identity)
                if offering:
                    key = f"offering_{index}_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
                    response = render_offering_card(offering, key, can_call) or response
                else:
                    st.caption("An offering from this answer is no longer available. Browse offerings for the current list.")
        else:
            if presentation == "offering_comparison":
                render_saved_comparison(message, index)
                st.caption("Saved comparison. Refresh offering details to check the latest availability.")
            else:
                st.write(message["content"])
            if is_latest and not is_customer:
                matches = mentioned_offering_keys(message["content"], offerings)
                if len(matches) == 1:
                    key = f"reply_{index}_" + hashlib.sha256(matches[0].encode()).hexdigest()[:16]
                    response = render_offering_actions(by_id[matches[0]], key, can_call) or response
    return response


def render_customer_chat(messages, greeting, offerings=None, can_call=False, queued_message=None, display=None):
    """Render the conversation and return a typed or suggested message."""
    suggested_message = None
    action_message = None
    offerings = [row for row in (offerings or []) if is_visible_offering(row)]
    by_id = {offering_key(row): row for row in offerings}
    show_cards = bool(messages and messages[-1].get("presentation") == "offering_cards")
    show_comparison = bool(messages and messages[-1].get("presentation") == "offering_comparison")
    earlier_messages, recent_messages, first_recent = split_chat_history(messages)
    with st.container(border=True, key="customer_chat_panel", gap="small"):
        st.badge("AI sales assistant", icon=":material/support_agent:", color="blue")
        st.caption(greeting)
        if earlier_messages:
            with st.expander(f"Earlier messages ({len(earlier_messages)})"):
                for index, message in enumerate(earlier_messages):
                    render_transcript_message(message, index, by_id, offerings, can_call)
            st.caption("Recent messages are shown below. Expand Earlier messages to read the full conversation.")

        with st.container(
            height="content",
            border=False,
            key="customer_chat_transcript",
        ):
            if not messages:
                with message_bubble("assistant"):
                    st.caption("Sales assistant")
                    st.write(
                        "Tell me what you’re looking for. I can help you explore "
                        "offerings, compare options, and check prices or dates."
                    )
            for offset, message in enumerate(recent_messages):
                index = first_recent + offset
                action_message = render_transcript_message(
                    message, index, by_id, offerings, can_call,
                    is_latest=index == len(messages) - 1,
                ) or action_message

            pending_turn = st.empty()

        force_picker = bool(messages and messages[-1].get("presentation") == "comparison_picker")
        action_message = render_comparison_picker(offerings, force=force_picker) or action_message
        if show_cards or show_comparison:
            if st.button("Refresh offering details", icon=":material/refresh:", key="refresh_chat_offerings"):
                action_message = "What products and services do you offer?"

        suggestions = build_chat_suggestions(messages, offerings)
        if suggestions:
            st.caption("You could ask")
        with st.container(horizontal=True, gap="small"):
            for label, icon, question in suggestions:
                if st.button(
                    label,
                    icon=icon,
                    help=question,
                    key="quick_question_" + hashlib.sha256(question.encode()).hexdigest()[:16],
                ):
                    suggested_message = question

        st.write("Type your own question or choose a suggestion. Sharing contact details is optional.")

    # At the page level, Streamlit pins this native composer to the bottom.
    # Avoid a second scrolling region or a fixed pixel height on small screens.
    typed_message = st.chat_input(
        "Your message — ask about offerings, prices, dates, or registration",
        key="customer_chat_composer", submit_mode="disable", width="stretch", height=68,
    )

    submitted_message = typed_message or action_message or suggested_message or queued_message
    if submitted_message:
        render_pending_turn(pending_turn, submitted_message)
        if display is not None:
            display.slot = pending_turn
    return submitted_message
