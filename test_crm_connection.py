"""Connection recovery checks without contacting the live CRM."""

import unittest
from unittest.mock import patch

import psycopg2

import crm_storage


class ConnectionRecoveryTests(unittest.TestCase):
    def test_retries_dns_failure_then_connects(self):
        dns_failure = psycopg2.OperationalError("could not translate host name")
        connection = object()
        with patch.object(crm_storage, "DATABASE_URL", "postgresql://example.invalid/db"), \
             patch.object(crm_storage.psycopg2, "connect", side_effect=[dns_failure, connection]) as connect, \
             patch.object(crm_storage.time, "sleep"):
            self.assertIs(crm_storage.get_connection(), connection)
            self.assertEqual(connect.call_count, 2)

    def test_reports_persistent_failure_without_fallback(self):
        dns_failure = psycopg2.OperationalError("could not translate host name")
        with patch.object(crm_storage, "DATABASE_URL", "postgresql://example.invalid/db"), \
             patch.object(crm_storage.psycopg2, "connect", side_effect=dns_failure) as connect, \
             patch.object(crm_storage.time, "sleep"):
            with self.assertRaises(psycopg2.OperationalError):
                crm_storage.get_connection()
            self.assertEqual(connect.call_count, 3)
        self.assertIn("DNS", crm_storage.connection_failure_message(dns_failure))


if __name__ == "__main__":
    unittest.main()
