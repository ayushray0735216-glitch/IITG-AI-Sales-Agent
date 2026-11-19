import unittest

import pandas as pd

from crm_profile import build_activity_timeline, filter_conversation_history


class CrmProfileTests(unittest.TestCase):
    def test_conversation_matches_stable_visitor_id_after_name_changes(self):
        history = pd.DataFrame([
            {"timestamp": "2026-10-01 10:00:00", "lead": "Anonymous Visitor A1", "visitor_id": "A1", "role": "user", "content": "Need pricing"},
            {"timestamp": "2026-10-01 10:01:00", "lead": "Other", "visitor_id": "B2", "role": "user", "content": "Ignore"},
        ])
        lead = {"Name": "Asha", "Visitor ID": "A1"}

        result = filter_conversation_history(history, lead)

        self.assertEqual(result["content"].tolist(), ["Need pricing"])

    def test_timeline_merges_and_sorts_messages_and_sales_actions(self):
        history = pd.DataFrame([
            {"timestamp": "2026-10-01 10:02:00", "lead": "Mina", "visitor_id": "", "role": "assistant", "content": "Here is pricing"},
            {"timestamp": "2026-10-01 10:00:00", "lead": "Mina", "visitor_id": "", "role": "user", "content": "Pricing?"},
        ])
        activity = pd.DataFrame([
            {"timestamp": "2026-10-01 10:01:00", "lead": "Mina", "action": "Follow-up set", "intent": "Pricing Question", "priority": "High"},
        ])

        result = build_activity_timeline(history, activity, {"Name": "Mina", "Visitor ID": ""})

        self.assertEqual(result["Type"].tolist(), ["Customer message", "Sales activity", "Assistant reply"])
        self.assertIn("Pricing Question", result.loc[1, "Details"])


if __name__ == "__main__":
    unittest.main()
