"""Focused checks for bounded, explainable prospect discovery."""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import crm_storage
import hunter_prospect_source as hunter
import prospect_discovery as discovery


PROFILE = {"id": "test-profile", "name": "Test bank managers", "countries": ["India"],
           "states": [], "cities": [], "industries": [], "job_titles": ["Bank Manager"],
           "keywords": [], "exclusions": [], "size_min": None, "size_max": None}


class ProspectDiscoveryTests(unittest.TestCase):
    def test_threshold_suggestion_requires_clear_review_feedback(self):
        queue = {
            "a": {"status": "reviewed", "score": 90},
            "b": {"status": "imported", "score": 85},
            "c": {"status": "rejected", "score": 60},
            "d": {"status": "rejected", "score": 70},
        }
        self.assertEqual(discovery.suggested_threshold(queue), 78)
        queue["d"]["score"] = 90
        self.assertIsNone(discovery.suggested_threshold(queue))

    def test_company_filters_apply_saved_industry_city_and_keywords(self):
        profile = {**PROFILE, "cities": ["Mumbai"], "industries": ["Financial Services"],
                   "keywords": ["AI training"]}
        filters = hunter.discover_filters(profile)
        self.assertEqual(filters["headquarters_location"]["include"],
                         [{"country": "IN", "city": "Mumbai"}])
        self.assertEqual(filters["industry"]["include"], ["Financial Services"])
        self.assertEqual(filters["keywords"]["include"], ["AI training"])
        with self.assertRaisesRegex(ValueError, "only in the United States"):
            hunter.discover_filters({**profile, "states": ["Assam"]})

    def test_saved_industry_typing_variants_use_hunter_label(self):
        for typed in ("Financial services", "Financial Services.", "Financial Service"):
            with self.subTest(typed=typed):
                filters = hunter.discover_filters({**PROFILE, "industries": [typed]})
                self.assertEqual(filters["industry"]["include"], ["Financial Services"])
        with self.assertRaisesRegex(ValueError, "does not recognize the industry"):
            hunter.discover_filters({**PROFILE, "industries": ["not a real industry"]})

    def test_broad_search_restored_for_saved_advanced_criteria(self):
        profile = {**PROFILE, "industries": ["Financial Services"]}
        with patch.object(hunter, "discover_domains") as discover, \
             patch.object(hunter, "search_people", return_value={
                 "people": [], "total": 10, "next_cursor": "hunter-next"
             }) as search:
            result = hunter.search_profile(profile, "key", "saved-cursor")
            self.assertEqual(result["next_cursor"], "hunter-next")
            self.assertEqual(search.call_args.args[2], "saved-cursor")
            self.assertEqual(len(search.call_args.args), 6)
            discover.assert_not_called()

    def test_scan_keeps_queue_and_does_not_reveal_or_import(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(crm_storage, "database_enabled", return_value=False), \
                 patch.object(crm_storage, "crm_path", side_effect=lambda name: str(Path(directory) / name)), \
                 patch.object(crm_storage, "load_crm_table", return_value=pd.DataFrame()):
                discovery.save_settings(PROFILE["id"], {"enabled": True, "hour": 9})
                person = {"handle": "hunter-handle", "name": "Aman G.", "title": "Bank Manager",
                          "company": "Example Bank", "domain": "example.com", "verification": "valid",
                          "decision_maker": True, "linkedin_exists": False}
                calls = []

                def search(*args):
                    calls.append(args)
                    return {"people": [person], "total": 1, "next_cursor": "cursor-2"}

                now = datetime(2026, 11, 18, 10, tzinfo=discovery.IST)
                first = discovery.scan_once(PROFILE, "key", now=now, search=search)
                self.assertEqual(first["last_added"], 1)
                self.assertEqual(len(first["queue"]), 1)
                self.assertGreaterEqual(next(iter(first["queue"].values()))["score"], 70)
                second = discovery.scan_once(PROFILE, "key", now=now + timedelta(minutes=5), search=search)
                self.assertTrue(second["skipped"])
                self.assertEqual(len(calls), 1)
                later = discovery.scan_once(PROFILE, "key", now=now + timedelta(days=1, minutes=1), search=search)
                self.assertEqual(later["last_added"], 0)
                self.assertEqual(len(later["queue"]), 1)


if __name__ == "__main__":
    unittest.main()
