"""Staff-reviewed sale close and delivery handover for one CRM lead."""

from datetime import datetime, timezone
from uuid import uuid4

import crm_storage


def save_step(original: dict, step: str, details: dict) -> dict:
    if step not in {"close", "handover", "delivered"}:
        raise ValueError("Choose a valid sale step.")
    name = crm_storage.lead_cell(original.get("Name"))
    if not name:
        raise ValueError("Open a named lead before closing a sale.")
    fields = {key: str(value or "").strip() for key, value in details.items()}
    if step == "close":
        required = {"offering": "offering", "agreement": "customer agreement or order reference",
                    "owner": "delivery owner", "next_step": "delivery next step"}
    elif step == "handover":
        required = {"handover_note": "handover note", "owner": "delivery owner"}
    else:
        required = {"delivery_evidence": "delivery confirmation or reference"}
    for key, label in required.items():
        if not fields.get(key):
            raise ValueError(f"Enter the {label} before continuing.")

    def update(current):
        matches = [(key, row) for key, row in current.items()
                   if crm_storage.lead_identity(row) == crm_storage.lead_identity(original)]
        if len(matches) != 1:
            raise ValueError("This lead could not be matched uniquely. Refresh and try again.")
        key, row = matches[0]
        prior = crm_storage.lead_cell(row.get("Sale Stage"))
        if step == "close" and prior:
            raise ValueError("This sale has already been closed. Refresh the lead profile.")
        if step == "handover" and prior != "Closed Won":
            raise ValueError("Close the sale before recording the handover.")
        if step == "delivered" and prior != "Handed Over":
            raise ValueError("Record the handover before marking delivery complete.")
        stage = {"close": "Closed Won", "handover": "Handed Over", "delivered": "Delivered"}[step]
        now = datetime.now(timezone.utc).isoformat()
        updates = {"Sale Stage": stage, "Status": stage,
                   "Sale Updated At": now}
        if step == "close":
            updates.update({"Sale Offering": fields["offering"],
                            "Sale Agreement Reference": fields["agreement"],
                            "Delivery Owner": fields["owner"],
                            "Delivery Next Step": fields["next_step"],
                            "Sale Closed At": now})
        elif step == "handover":
            updates.update({"Delivery Owner": fields["owner"],
                            "Delivery Handover Note": fields["handover_note"],
                            "Delivery Handed Over At": now})
        else:
            updates.update({"Delivery Confirmation": fields["delivery_evidence"],
                            "Delivery Completed At": now})
        current[key] = {**row, **updates}
        return current[key], {key: current[key]}

    saved, _ = crm_storage._write_leads(update)
    crm_storage.append_sales_activity({
        "activity_id": "sale:" + uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "lead": name, "intent": "Sale", "sentiment": "", "priority": "High",
        "action": {"close": "Sale closed", "handover": "Delivery handed over",
                   "delivered": "Delivery confirmed"}[step],
        "details": fields.get("offering") or fields.get("handover_note") or fields.get("delivery_evidence"),
    })
    return saved
