import unittest

import pandas as pd

from crm_lead_list import LEAD_LIST_COLUMNS, build_clean_lead_list


class CleanLeadListTests(unittest.TestCase):
    def setUp(self):
        self.leads = pd.DataFrame(
            [
                {
                    "Name": "",
                    "Visitor ID": "VIS-42",
                    "Company": "Private Company",
                    "Email": "person@example.test",
                    "Phone": "555-0100",
                    "Product": "CRM Suite",
                    "Last Intent": "Pricing Question",
                    "Priority": "High",
                    "Status": "Follow-up Required",
                    "Last Interaction": "2026-10-01 12:30:00",
                    "Next Follow-up": "",
                    "Notes": "Keep this in the profile only",
                },
                {
                    "Name": "Existing customer",
                    "Visitor ID": "",
                    "Company": "",
                    "Email": "",
                    "Phone": "",
                    "Product": "",
                    "Last Intent": "",
                    "Priority": "",
                    "Status": "Conversation Started",
                    "Last Interaction": "",
                    "Next Follow-up": "2026-10-09",
                    "Notes": "",
                },
            ]
        )

    def test_list_is_compact_and_uses_visitor_id_when_name_is_missing(self):
        displayed = build_clean_lead_list(self.leads)
        self.assertEqual(list(displayed.columns), LEAD_LIST_COLUMNS)
        self.assertEqual(displayed.iloc[0]["Name / Visitor ID"], "Visitor VIS-42")
        self.assertNotIn("Email", displayed.columns)
        self.assertNotIn("Phone", displayed.columns)
        self.assertNotIn("Notes", displayed.columns)

    def test_anonymous_row_shows_full_id_but_opted_in_contact_uses_contact_name(self):
        leads = pd.DataFrame([
            {"Name": "Anonymous Visitor A1", "Visitor ID": "VIS-123456", "Email": "", "Phone": "", "Contact Name": ""},
            {"Name": "Anonymous Visitor B2", "Visitor ID": "VIS-654321", "Email": "person@example.test", "Phone": "", "Contact Name": "Sam"},
        ])

        displayed = build_clean_lead_list(leads)

        self.assertEqual(displayed.iloc[0]["Name / Visitor ID"], "Anonymous visitor · VIS-123456")
        self.assertEqual(displayed.iloc[1]["Name / Visitor ID"], "Sam")

    def test_readable_status_and_follow_up_dates(self):
        displayed = build_clean_lead_list(self.leads)
        self.assertEqual(displayed.iloc[0]["Status"], "Follow-up required")
        self.assertEqual(displayed.iloc[0]["Priority"], "High")
        self.assertEqual(displayed.iloc[0]["Last activity"], "1 Oct 2026")
        self.assertEqual(displayed.iloc[0]["Next follow-up"], "4 Oct 2026 · estimated")
        self.assertEqual(displayed.iloc[1]["Next follow-up"], "9 Oct 2026")

    def test_recent_activity_uses_the_newer_activity_timestamp(self):
        activity = pd.DataFrame(
            [{"lead": "Existing customer", "timestamp": "2026-10-02T09:00:00"}]
        )
        displayed = build_clean_lead_list(self.leads, activity)
        self.assertEqual(displayed.iloc[1]["Last activity"], "2 Oct 2026")

    def test_source_lead_records_are_not_changed(self):
        before = self.leads.copy(deep=True)
        build_clean_lead_list(self.leads)
        pd.testing.assert_frame_equal(self.leads, before)


if __name__ == "__main__":
    unittest.main()
