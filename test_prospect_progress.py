"""Check that prospect search progress survives reloads and stays isolated."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import crm_storage
import prospect_progress
import prospect_search


class ProspectProgressTests(unittest.TestCase):
    def test_saved_searches_keep_separate_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            def crm_path(name):
                return str(Path(directory) / name)

            with patch.object(crm_storage, "database_enabled", return_value=False), \
                 patch.object(crm_storage, "crm_path", side_effect=crm_path):
                first = {"state": {"hunter_prospect_preview": {"history": [None, "page-2"]},
                                   "hunter_revealed_emails": {"person-1": "lead@example.com"}}}
                second = {"state": {"hunter_prospect_preview": {"history": [None]}}}
                prospect_progress.save_search("first", first)
                prospect_progress.save_search("second", second)
                prospect_progress.save_last_selected("second")

                self.assertEqual(prospect_progress.load_search("first"), first)
                self.assertEqual(prospect_progress.load_search("second"), second)
                self.assertEqual(prospect_progress.load_last_selected(), "second")

                prospect_progress.clear_search("first")
                self.assertEqual(prospect_progress.load_search("first"), {})
                self.assertEqual(prospect_progress.load_search("second"), second)

    def test_switching_searches_restores_each_page_and_review_list(self):
        with tempfile.TemporaryDirectory() as directory:
            def crm_path(name):
                return str(Path(directory) / name)

            profile_a = {"id": "first", "name": "First", "countries": ["India"]}
            profile_b = {"id": "second", "name": "Second", "countries": ["India"]}
            state = {}
            with patch.object(crm_storage, "database_enabled", return_value=False), \
                 patch.object(crm_storage, "crm_path", side_effect=crm_path), \
                 patch.object(prospect_search.st, "session_state", state):
                prospect_search._activate_progress(profile_a)
                state["hunter_prospect_preview"] = {
                    "profile_id": "first", "history": [None, "page-2"],
                    "fingerprint": prospect_search._preview_fingerprint(profile_a, True),
                }
                state["hunter_review_list"] = {"person-1": {"name": "A"}}
                prospect_search._save_progress(profile_a)
                prospect_search._activate_progress(profile_b)
                self.assertEqual(state["hunter_review_list"], {})
                prospect_search._activate_progress(profile_a)
                self.assertEqual(state["hunter_prospect_preview"]["history"], [None, "page-2"])
                self.assertIn("person-1", state["hunter_review_list"])


if __name__ == "__main__":
    unittest.main()
