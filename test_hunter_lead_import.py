"""Hunter imports must preserve CRM identity and never invent contact consent."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import crm_storage


class HunterLeadImportTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / "leads.csv"
        pd.DataFrame([{
            "Name": "Nilesh Patil", "Email": "", "Company": "Mastercard",
            "Job Role": "Senior Product Manager - Technical",
            "Lead Import Key": "import:linkedin:existing", "Lead Source": "Lead File",
            "Contact Consent": "No", "Workbook Profile": json.dumps({"Experience (Years)": "9.6"}),
        }]).to_csv(self.path, index=False)
        self.data_patch = patch.object(crm_storage, "DATA_DIR", Path(self.folder.name))
        self.db_patch = patch.object(crm_storage, "DATABASE_URL", None)
        self.data_patch.start()
        self.db_patch.start()
        self.addCleanup(self.data_patch.stop)
        self.addCleanup(self.db_patch.stop)

    def lead_rows(self):
        return pd.read_csv(self.path, dtype=str, keep_default_na=False).to_dict("records")

    def candidate(self, **changes):
        candidate = {"handle": "opaque-1", "name": "Navin P.", "title": "Bank Manager",
                     "company": "HDFC Bank", "domain": "hdfcbank.com", "verification": "valid",
                     "name_status": "Hunter masked", "email": "", "experience": ""}
        candidate.update(changes)
        return candidate

    def test_masked_import_is_idempotent_and_reveal_updates_same_lead(self):
        first, ready = crm_storage.import_hunter_prospects([self.candidate()])
        self.assertTrue(ready)
        self.assertEqual(first["added"], 1)
        self.assertEqual(len(self.lead_rows()), 2)
        self.assertEqual(self.lead_rows()[1]["Email"], "")
        self.assertEqual(self.lead_rows()[1]["Contact Consent"], "No")
        again, _ = crm_storage.import_hunter_prospects([self.candidate()])
        self.assertEqual(again["already_present"], 1)
        updated, _ = crm_storage.import_hunter_prospects([self.candidate(
            name="Navin Patil", name_status="Hunter revealed", email="navin@example.com",
            experience="12.5")])
        self.assertEqual(updated["updated"], 1)
        rows = self.lead_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["Name"], "Navin Patil")
        self.assertEqual(rows[1]["Email"], "navin@example.com")
        self.assertEqual(json.loads(rows[1]["Workbook Profile"])["Experience (Years)"], "12.5")

    def test_full_name_and_company_fill_existing_blank_email(self):
        result, _ = crm_storage.import_hunter_prospects([self.candidate(
            name="Nilesh Patil", company="Mastercard", domain="mastercard.com",
            title="Senior Product Manager - Technical", name_status="Staff supplied",
            email="nilesh@example.com")])
        self.assertEqual(result["updated"], 1)
        rows = self.lead_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Email"], "nilesh@example.com")
        self.assertEqual(rows[0]["Lead Source"], "Lead File")
        self.assertEqual(rows[0]["Contact Consent"], "No")
        self.assertEqual(json.loads(rows[0]["Workbook Profile"])["Experience (Years)"], "9.6")

    def test_masked_possible_match_needs_review(self):
        result, _ = crm_storage.import_hunter_prospects([self.candidate(
            name="Nilesh P.", company="Mastercard")])
        self.assertEqual(result["needs_review"], 1)
        self.assertEqual(len(self.lead_rows()), 1)


if __name__ == "__main__":
    unittest.main()
