"""Small, testable helpers for the customer response path."""

INTENTS = {
    "Interested": "Schedule Follow-up",
    "Pricing Question": "Send Pricing",
    "Product Question": "Send Information",
    "Request for Demo": "Schedule Demo",
    "Request for More Information": "Send Information",
    "Not Interested": "Close Lead",
    "Ready to Buy": "Schedule Follow-up",
    "Objection": "Schedule Follow-up",
    "Other": "No Action",
}

CHAT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "sentiment": {"type": "string", "enum": ["Positive", "Neutral", "Negative"]},
        "priority": {"type": "string", "enum": ["High", "Medium", "Low"]},
        "recommended_action": {"type": "string", "enum": sorted(set(INTENTS.values()))},
        "reply": {"type": "string"},
    },
    "required": ["intent", "sentiment", "priority", "recommended_action", "reply"],
}


def recent_conversation_text(messages, max_messages=20, max_chars=20000):
    """Keep recent turns intact where possible and always keep the newest text."""
    parts = []
    remaining = max_chars
    for message in reversed(messages[-max_messages:]):
        part = f"{str(message.get('role', '')).upper()}: {message.get('content', '')}\n"
        if len(part) > remaining:
            if not parts:
                parts.append(part[-remaining:])
            break
        parts.append(part)
        remaining -= len(part)
        if remaining <= 0:
            break
    return "".join(reversed(parts))
