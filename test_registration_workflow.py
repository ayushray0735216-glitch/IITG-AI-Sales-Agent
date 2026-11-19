"""Behavior checks for verified signup persistence and one-time reminders."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import crm_storage
import registration_workflow as workflow


class RegistrationWorkflowTests(unittest.TestCase):
    def test_confirmation_and_reminder_are_sent_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            start = datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(hours=12)
            offering = {
                "Offering ID": "demo-webinar", "Offering Name": "Demo webinar",
                "Offering Type": "Webinar", "Knowledge Status": "Published", "AI Active": True,
                "Start Date": start.strftime("%Y-%m-%d"), "Start Time": start.strftime("%H:%M"),
            }
            messages = []
            with patch.object(crm_storage, "DATABASE_URL", None), patch.object(crm_storage, "DATA_DIR", Path(temporary)), \
                 patch.object(workflow, "_send_message", side_effect=lambda to, subject, body: messages.append((to, subject, body)) or "<demo@local>"), \
                 patch.object(crm_storage, "record_communication_event"):
                first_id, existing = workflow.register(offering, "Demo Person", "demo@example.com", True)
                self.assertFalse(existing)
                second_id, existing = workflow.register(offering, "Demo Person", "demo@example.com", True)
                self.assertTrue(existing)
                self.assertEqual(first_id, second_id)
                self.assertEqual(len(messages), 1)
                self.assertEqual(workflow.send_due_reminders(), 1)
                self.assertEqual(workflow.send_due_reminders(), 0)
                self.assertEqual(len(messages), 2)
                self.assertEqual(workflow.list_registrations()[0]["reminder_state"], "sent")


if __name__ == "__main__":
    unittest.main()
