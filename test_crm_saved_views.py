import unittest
from datetime import date

import pandas as pd

from crm_saved_views import build_crm_saved_view_masks


class CrmSavedViewTests(unittest.TestCase):
    def setUp(self):
        self.leads = pd.DataFrame(
            [
                {
                    "Name": "Anonymous Visitor V-1",
                    "Status": "Conversation Started",
                    "Last Intent": "Product Question",
                    "Interest Score": 4,
                    "Lead Source": "AI Chat — Anonymous Visitor",
                    "Visitor ID": "V-1",
                    "Contact Name": "",
                    "Email": "",
                    "Phone": "",
                    "Last Interaction": "2026-10-02",
                    "Next Follow-up": "2026-10-02",
                },
                {
                    "Name": "High-intent lead",
                    "Status": "New",
                    "Last Intent": "Ready to Buy",
                    "Interest Score": 8,
                    "Lead Source": "Lead File",
                    "Visitor ID": "",
                    "Contact Name": "Alex",
                    "Email": "alex@example.test",
                    "Phone": "123",
                    "Last Interaction": "2026-09-10",
                    "Next Follow-up": "",
                },
                {
                    "Name": "Recent by activity",
                    "Status": "Information Requested",
                    "Last Intent": "Product Question",
                    "Interest Score": 5,
                    "Lead Source": "Lead File",
                    "Visitor ID": "",
                    "Contact Name": "Pat",
                    "Email": "pat@example.test",
                    "Phone": "456",
                    "Last Interaction": "2026-09-10",
                    "Next Follow-up": "2026-10-10",
                },
                {
                    "Name": "Closed lead",
                    "Status": "Closed",
                    "Last Intent": "Ready to Buy",
                    "Interest Score": 10,
                    "Lead Source": "AI Chat — Anonymous Visitor",
                    "Visitor ID": "V-2",
                    "Contact Name": "",
                    "Email": "",
                    "Phone": "",
                    "Last Interaction": "2026-10-01",
                    "Next Follow-up": "",
                },
            ]
        )
        self.activity = pd.DataFrame(
            [{"lead": "Recent by activity", "timestamp": "2026-10-01T12:00:00"}]
        )

    def test_saved_views_match_expected_records(self):
        masks = build_crm_saved_view_masks(
            self.leads, self.activity, today=date(2026, 10, 2)
        )
        expected_names = {
            "needs_follow_up": ["Anonymous Visitor V-1", "High-intent lead"],
            "high_intent": ["High-intent lead"],
            "anonymous_visitors": ["Anonymous Visitor V-1", "Closed lead"],
            "recently_active": ["Anonymous Visitor V-1", "Recent by activity"],
        }
        for view, expected in expected_names.items():
            with self.subTest(view=view):
                self.assertEqual(self.leads.loc[masks[view], "Name"].tolist(), expected)

    def test_legacy_follow_up_date_uses_existing_three_day_window(self):
        self.leads.loc[1, "Last Interaction"] = "2026-09-29"
        masks = build_crm_saved_view_masks(
            self.leads, self.activity, today=date(2026, 10, 2)
        )
        self.assertTrue(masks["needs_follow_up"].iloc[1])

    def test_view_masks_do_not_mutate_crm_records(self):
        before = self.leads.copy(deep=True)
        build_crm_saved_view_masks(self.leads, self.activity, today=date(2026, 10, 2))
        pd.testing.assert_frame_equal(self.leads, before)


if __name__ == "__main__":
    unittest.main()
