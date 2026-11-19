"""Regression checks for the read-only sales reports summaries."""

import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from crm_workspace import lead_link_key
from sales_analytics import (
    chat_people_summary, intent_activity_details, match_chat_lead,
    offering_interest_summary, report_outcomes, within_days,
)


IST = ZoneInfo("Asia/Kolkata")


class SalesAnalyticsTests(unittest.TestCase):
    def test_rolling_period_and_malformed_dates(self):
        now = datetime(2026, 11, 16, 12, tzinfo=IST)
        frame = pd.DataFrame({"timestamp": [
            (now - timedelta(days=2)).isoformat(),
            (now - timedelta(days=20)).isoformat(),
            "not a date",
        ]})
        self.assertEqual(len(within_days(frame, 7, now)), 1)
        self.assertEqual(len(within_days(frame, 30, now)), 2)
        self.assertEqual(len(frame), 3)

    def test_people_messages_and_unanswered_thread(self):
        now = datetime(2026, 11, 16, 12, tzinfo=IST)
        history = pd.DataFrame([
            {"visitor_id": "v1", "lead": "Krishna", "role": "user", "timestamp": (now - timedelta(minutes=3)).isoformat()},
            {"visitor_id": "v1", "lead": "Krishna", "role": "assistant", "timestamp": (now - timedelta(minutes=2)).isoformat()},
            {"visitor_id": "v1", "lead": "Krishna", "role": "user", "timestamp": (now - timedelta(minutes=1)).isoformat()},
            {"visitor_id": "v2", "lead": "Other", "role": "user", "timestamp": now.isoformat()},
            {"visitor_id": "v3", "lead": "Bad", "role": "legacy date", "timestamp": now.isoformat()},
        ])
        people, messages, threads = chat_people_summary(within_days(history, 7, now))
        self.assertEqual((people, messages), (2, 3))
        self.assertEqual(next(t for t in threads if t["key"] == "v1")["last_role"], "user")

    def test_lead_link_is_distinct_for_colliding_visitor_ids(self):
        leads = pd.DataFrame([
            {"CRM Record ID": "record-1", "Name": "Samrat", "Visitor ID": "shared"},
            {"CRM Record ID": "record-2", "Name": "Anonymous", "Visitor ID": "shared"},
        ])
        self.assertNotEqual(lead_link_key(leads.iloc[0]), lead_link_key(leads.iloc[1]))
        matched = match_chat_lead({"name": "Samrat", "visitor_id": "shared"}, leads)
        self.assertEqual(matched["CRM Record ID"], "record-1")

    def test_offering_chart_counts_people_not_messages_and_keeps_unknowns(self):
        now = datetime(2026, 11, 16, 12, tzinfo=IST)
        stamp = now.isoformat()
        catalog = pd.DataFrame({"Offering Name": ["AI Workshop", "Design Course", "Unused Seminar"]})
        leads = pd.DataFrame([
            {"CRM Record ID": "k", "Visitor ID": "v1", "Name": "Krishna", "Interest Level": "High"},
            {"CRM Record ID": "a", "Visitor ID": "v2", "Name": "Asha", "Interest Level": "Low"},
        ])
        history = pd.DataFrame([
            {"visitor_id": "v1", "lead": "Krishna", "role": "user", "content": "Tell me about AI Workshop", "timestamp": stamp},
            {"visitor_id": "v1", "lead": "Krishna", "role": "user", "content": "What is the AI Workshop price?", "timestamp": stamp},
            {"visitor_id": "v2", "lead": "Asha", "role": "user", "content": "Compare AI Workshop and Design Course", "timestamp": stamp},
            {"visitor_id": "v3", "lead": "Visitor", "role": "user", "content": "What do you offer?", "timestamp": stamp},
            {"visitor_id": "v3", "lead": "Visitor", "role": "assistant", "content": "We have an AI Workshop", "timestamp": stamp},
        ])
        counts, people = offering_interest_summary(within_days(history, 7, now), leads, catalog)
        totals = counts.groupby("Offering")["People"].sum().to_dict()
        self.assertEqual(totals, {
            "AI Workshop": 2, "Design Course": 1, "Unused Seminar": 0, "Unclassified": 1,
        })
        self.assertEqual({p["interest"] for p in people["AI Workshop"]}, {"High", "Low"})

    def test_other_details_do_not_cross_shared_visitor_id(self):
        now = datetime(2026, 11, 16, 12, tzinfo=IST)
        leads = pd.DataFrame([
            {"CRM Record ID": "a", "Name": "Asha", "Visitor ID": "shared"},
            {"CRM Record ID": "b", "Name": "Bela", "Visitor ID": "shared"},
        ])
        activity = within_days(pd.DataFrame([{
            "lead": "Asha", "intent": "Other", "action": "No sales action required.",
            "timestamp": now.isoformat(),
        }]), 7, now)
        history = pd.DataFrame([{
            "lead": "Bela", "visitor_id": "shared", "role": "user",
            "content": "Private question", "timestamp": (now - timedelta(minutes=1)).isoformat(),
        }])
        details = intent_activity_details(activity, leads, history, "Other")
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["message"], "")

    def test_outcome_periods_use_saved_event_times(self):
        now = datetime(2026, 11, 16, 12, tzinfo=IST)
        current = (now - timedelta(days=2)).isoformat()
        previous = (now - timedelta(days=12)).isoformat()
        activity = pd.DataFrame([
            {"action": "Chat started from email link.", "timestamp": current},
            {"action": "Chat started from email link.", "timestamp": previous},
        ])
        inbox = pd.DataFrame([
            {"message_id": "m1", "received_at": current},
            {"message_id": "m1", "received_at": current},
            {"message_id": "m2", "received_at": previous},
        ])
        emails = pd.DataFrame([
            {"updated_at": current}, {"updated_at": current},
            {"updated_at": previous},
        ])
        leads = pd.DataFrame([
            {"Status": "New", "Priority": "High"},
            {"Status": "Converted", "Priority": "High"},
        ])
        result = report_outcomes(activity, inbox, emails, leads, 7, now)
        self.assertEqual(result["emails"], (2, 1))
        self.assertEqual(result["replies"], (1, 1))
        self.assertEqual(result["email_chats"], (1, 1))
        self.assertEqual((result["high_priority"], result["converted"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
