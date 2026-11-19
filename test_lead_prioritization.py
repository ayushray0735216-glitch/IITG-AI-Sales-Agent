"""Focused unit tests for the Prioritize Today queue ranking."""

import unittest
from datetime import date, timedelta

import pandas as pd

from lead_prioritization import build_prioritize_today_queue


class PrioritizeTodayTests(unittest.TestCase):
    def test_due_dates_rank_first_and_closed_leads_are_excluded(self):
        today = date(2026, 10, 2)
        leads = pd.DataFrame(
            [
                {
                    "Name": "Due today",
                    "Status": "Interested",
                    "Last Intent": "Product Question",
                    "Interest Score": 3,
                    "Next Follow-up": today.isoformat(),
                },
                {
                    "Name": "Overdue",
                    "Status": "Follow-up Required",
                    "Last Intent": "Other",
                    "Interest Score": 1,
                    "Next Follow-up": (today - timedelta(days=2)).isoformat(),
                },
                {
                    "Name": "Closed",
                    "Status": "Closed",
                    "Last Intent": "Ready to Buy",
                    "Interest Score": 10,
                    "Next Follow-up": (today - timedelta(days=9)).isoformat(),
                },
            ]
        )

        queue = build_prioritize_today_queue(leads, today=today)

        self.assertEqual(queue["Name"].tolist(), ["Overdue", "Due today"])
        self.assertIn("overdue", queue.iloc[0]["Why This Rank"].lower())
        self.assertEqual(queue.iloc[1]["Follow-up Due"], "Due today")

    def test_same_due_date_uses_intent_score_then_recent_activity(self):
        today = date(2026, 10, 2)
        leads = pd.DataFrame(
            [
                {"Name": "Lower intent", "Status": "Interested", "Last Intent": "Interested", "Interest Score": 9, "Next Follow-up": today.isoformat()},
                {"Name": "High intent", "Status": "Interested", "Last Intent": "Ready to Buy", "Interest Score": 4, "Next Follow-up": today.isoformat()},
                {"Name": "Same intent, higher score, yesterday", "Status": "Interested", "Last Intent": "Ready to Buy", "Interest Score": 8, "Next Follow-up": today.isoformat()},
                {"Name": "Same intent, higher score, today", "Status": "Interested", "Last Intent": "Ready to Buy", "Interest Score": 8, "Next Follow-up": today.isoformat()},
            ]
        )
        activity = pd.DataFrame(
            [
                {"lead": "Same intent, higher score, yesterday", "timestamp": (today - timedelta(days=1)).isoformat()},
                {"lead": "Same intent, higher score, today", "timestamp": today.isoformat()},
                {"lead": "High intent", "timestamp": (today - timedelta(days=1)).isoformat()},
                {"lead": "Lower intent", "timestamp": today.isoformat()},
            ]
        )

        queue = build_prioritize_today_queue(leads, activity=activity, today=today)

        self.assertEqual(
            queue["Name"].tolist(),
            [
                "Same intent, higher score, today",
                "Same intent, higher score, yesterday",
                "High intent",
                "Lower intent",
            ],
        )
        self.assertEqual(queue.iloc[0]["Recent Activity"], "Today")

    def test_legacy_lead_gets_an_inferred_date_without_mutating_source(self):
        today = date(2026, 10, 2)
        leads = pd.DataFrame(
            [
                {
                    "Name": "Legacy lead",
                    "Status": "Interested",
                    "Last Intent": "Interested",
                    "Interest Score": 6,
                    "Last Interaction": (today - timedelta(days=5)).isoformat(),
                }
            ]
        )

        queue = build_prioritize_today_queue(leads, today=today)

        self.assertIn("estimated", queue.iloc[0]["Follow-up Due"])
        self.assertIn("last interaction + 3 days", queue.iloc[0]["Why This Rank"])
        self.assertNotIn("Next Follow-up", leads.columns)

    def test_missing_activity_is_explained(self):
        queue = build_prioritize_today_queue(
            pd.DataFrame([{"Name": "New lead", "Status": "New"}]),
            today=date(2026, 10, 2),
        )

        self.assertEqual(queue.iloc[0]["Recent Activity"], "No activity recorded")
        self.assertIn("intent not recorded", queue.iloc[0]["Why This Rank"].lower())


if __name__ == "__main__":
    unittest.main()
