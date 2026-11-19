import unittest
from datetime import date

import pandas as pd

from crm_overview import build_crm_overview


class CrmOverviewTests(unittest.TestCase):
    def setUp(self):
        self.leads = pd.DataFrame(
            [
                {
                    "Name": "Chat visitor",
                    "Status": "Conversation Started",
                    "Priority": "High",
                    "Lead Source": "AI Chat — Anonymous Visitor",
                    "Visitor ID": "VIS-1",
                    "Last Interaction": "2026-10-01",
                    "Next Follow-up": "",
                },
                {
                    "Name": "Imported lead",
                    "Status": "New",
                    "Priority": "Medium",
                    "Lead Source": "Lead File",
                    "Visitor ID": "",
                    "Last Interaction": "2026-10-02",
                    "Next Follow-up": "2026-10-02",
                },
                {
                    "Name": "Converted lead",
                    "Status": "Won",
                    "Priority": "High",
                    "Lead Source": "Lead File",
                    "Visitor ID": "",
                    "Last Interaction": "2026-10-02",
                    "Next Follow-up": "",
                },
                {
                    "Name": "Closed, not converted",
                    "Status": "Closed",
                    "Priority": "High",
                    "Lead Source": "Lead File",
                    "Visitor ID": "",
                    "Last Interaction": "2026-10-01",
                    "Next Follow-up": "2026-10-01",
                },
            ]
        )

    def test_counts_and_masks_use_open_status_and_explicit_conversion(self):
        result = build_crm_overview(self.leads, today=date(2026, 10, 2))
        self.assertEqual(
            result["counts"],
            {
                "new": 2,
                "high_priority": 1,
                "follow_ups_due": 1,
                "walk_in": 1,
                "file_based": 3,
                "converted": 1,
            },
        )
        self.assertEqual(
            self.leads.loc[result["masks"]["follow_ups_due"], "Name"].tolist(),
            ["Imported lead"],
        )
        self.assertEqual(
            self.leads.loc[result["masks"]["converted"], "Name"].tolist(),
            ["Converted lead"],
        )

    def test_missing_legacy_date_uses_three_day_estimate_without_mutating_data(self):
        before = self.leads.copy(deep=True)
        result = build_crm_overview(self.leads, today=date(2026, 10, 4))
        self.assertTrue(result["masks"]["follow_ups_due"].iloc[0])
        pd.testing.assert_frame_equal(self.leads, before)


if __name__ == "__main__":
    unittest.main()
