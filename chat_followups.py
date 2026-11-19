"""Local, catalog-grounded follow-up choices; no model or database requests."""

import re


DEFAULT_SUGGESTIONS = (
    ("Browse offerings", ":material/storefront:", "What products and services do you offer?"),
    ("Compare options", ":material/compare_arrows:", "Can you help me compare the options?"),
    ("Help me choose", ":material/help_outline:", "What details should I know before choosing?"),
)


def _text(value):
    text = str(value).strip() if value is not None else ""
    return "" if text.casefold() in {"nan", "none", "nat", "<na>"} else text


def _normalized(value):
    return re.sub(r"[^\w]+", " ", _text(value).casefold()).strip()


def named_offerings(text, offerings):
    normalized = " " + _normalized(text) + " "
    matches = [row for row in offerings if _normalized(row.get("Offering Name"))
               and " " + _normalized(row.get("Offering Name")) + " " in normalized]
    # A specific title such as 'Analytics (Advanced)' must not also select
    # its shorter parent 'Analytics' unless it was independently named.
    return [row for row in matches if not any(
        _normalized(row.get("Offering Name")) != _normalized(other.get("Offering Name"))
        and " " + _normalized(row.get("Offering Name")) + " " in
            " " + _normalized(other.get("Offering Name")) + " "
        and " " + _normalized(row.get("Offering Name")) + " " not in
            normalized.replace(" " + _normalized(other.get("Offering Name")) + " ", " ")
        for other in matches
    )]


def is_conversation_closing(message):
    """Only standalone closings; 'thanks, what is the price?' is a question."""
    normalized = _normalized(message)
    return bool(re.fullmatch(
        r"(?:(?:ok|okay|great|perfect|got it|understood) )?(?:thanks|thank you)"
        r"(?: very much| so much| a lot)?|bye|goodbye|see you|that s all"
        r"|(?:i am |i m )?not interested(?: thanks| thank you)?"
        r"|ok|okay|got it|understood|no thanks"
        r"|(?:please )?(?:stop contacting me|do not contact me|don t contact me|unsubscribe)",
        normalized,
    ))


def context_offerings(messages, offerings):
    """Resolve the current turn first, never revive a topic after a topic switch."""
    offerings = [row for row in offerings if _text(row.get("Offering Name"))]
    latest_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    user_text = latest_user.get("content", "")
    explicit = named_offerings(user_text, offerings)
    if explicit:
        return explicit
    latest_reply = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})
    ids = latest_reply.get("offering_ids", [])
    named = named_offerings(latest_reply.get("content", ""), offerings)
    if ids:
        return [row for row in offerings if _text(row.get("Offering ID")) in ids
                or (not _text(row.get("Offering ID")) and _text(row.get("Offering Name")) in ids)]
    if named:
        return named
    # Only an explicit reference permits looking back beyond the latest answer.
    if re.search(r"\b(?:it|its|that|those|these|them|each|both)\b", _normalized(user_text)):
        for message in reversed(messages[:-1]):
            named = named_offerings(message.get("content", ""), offerings)
            if named:
                return named
    return []


def referenced_offerings(message, conversation_text, offerings):
    """Resolve pronouns from the most recent exchange, not the longest old title."""
    if not re.search(r"\b(?:it|its|that|those|these|them|each|both)\b", _normalized(message)):
        return []
    turns = [{"role": match.group(1).casefold(), "content": match.group(2).strip()}
             for match in re.finditer(
                 r"(?ms)^(USER|ASSISTANT):\s*(.*?)(?=^(?:USER|ASSISTANT):|\Z)",
                 conversation_text,
             )]
    if not turns or turns[-1] != {"role": "user", "content": message.strip()}:
        turns.append({"role": "user", "content": message})
    return context_offerings(turns, offerings)


def build_chat_suggestions(messages, offerings):
    """Offer at most three relevant, passive questions using the visible catalog."""
    latest_user = next((m.get("content", "") for m in reversed(messages)
                        if m.get("role") == "user"), "")
    if is_conversation_closing(latest_user):
        return ()
    focus = context_offerings(messages, offerings)
    if not focus:
        return DEFAULT_SUGGESTIONS
    names = "; ".join(_text(row.get("Offering Name")) for row in focus)
    multiple = len(focus) > 1
    subject = f"each of these offerings: {names}" if multiple else names
    asked = _normalized(latest_user)
    # Skip the topic just requested rather than repeat it as the next step.
    candidates = (
        ("Prices" if multiple else "Price", ":material/payments:",
         f"What is the price of {subject}?", ("Price",), r"\b(?:price|prices|cost|fee|fees|free|how much)\b"),
        ("Dates & times", ":material/calendar_month:",
         f"What are the dates and times for {subject}?", ("Start Date", "Start Time"),
         r"\b(?:date|dates|time|times|schedule|when)\b"),
        ("Registration links" if multiple else "Registration link", ":material/link:",
         f"Give me the registration link for {subject}.", ("Registration Link",),
         r"\b(?:registration|register|enroll|enrol|link|links)\b"),
        ("Who it’s for", ":material/groups:", f"Who is {subject} intended for?",
         ("Target Audience",), r"\b(?:audience|intended|eligible|eligibility|who)\b"),
        ("FAQs", ":material/help_outline:", f"What are the FAQs for {subject}?",
         ("FAQs",), r"\b(?:faq|faqs|frequently)\b"),
        ("Policies", ":material/policy:", f"What are the policies for {subject}?",
         ("Policies",), r"\b(?:policy|policies|refund|cancellation)\b"),
    )
    choices = []
    for label, icon, question, fields, asked_pattern in candidates:
        if any(_text(row.get(field)) for row in focus for field in fields) and not re.search(asked_pattern, asked):
            choices.append((label, icon, question))
    if len(focus) == 2 and not re.search(r"\b(?:compare|comparison|difference|versus|vs)\b", asked):
        choices.insert(0, ("Compare these two", ":material/compare_arrows:",
                           f"Compare {names}, using the recorded price, schedule, and audience."))
    choices.append(DEFAULT_SUGGESTIONS[0])
    return tuple(choices[:3])


def acknowledgment_reply(message):
    if re.search(r"\b(?:thanks|thank you)\b", _normalized(message)):
        return "You’re welcome!"
    return "Got it. I’m here if you need anything else."
