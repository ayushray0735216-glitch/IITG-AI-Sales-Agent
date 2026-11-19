"""Grounded facts, lifecycle and comparison UI checks with no live services."""

import ast
import os
import re
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pandas as pd
from streamlit.testing.v1 import AppTest

from offering_facts import (
    build_grounded_chat_reply, clean_value, comparison_reply, is_visible_offering,
    offering_dates, offering_lifecycle, offering_price, parse_offering_date,
    registration_detail, saved_web_url,
)


def row(identity, name, **fields):
    return {"Offering ID": identity, "Offering Name": name, "Knowledge Status": "Published",
            "AI Active": "True", "Offering Type": "Workshop", **fields}


def scope_functions(*names, **scope):
    source = Path(__file__).with_name("app.py")
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
    return scope


class OfferingFactChecks(unittest.TestCase):
    def setUp(self):
        self.a = row("A", "Design Lab", Price="499", Currency="INR", **{
            "Start Date": "12 December 2099", "End Date": "12 December 2099",
            "Start Time": "10:00 AM IST", "End Time": "1:00 PM IST",
            "Duration": "3 hours", "Target Audience": "Beginners",
            "Registration Link": "https://example.com/design",
        })
        self.b = row("B", "Business Analytics", Price="0", **{
            "Start Date": "20 December 2099", "FAQs": "Bring your laptop.",
            "Policies": "Cancellation terms must be confirmed by the team.",
        })
        self.rows = [self.a, self.b, row("C", "Canvas Tote", Price="250", **{"Offering Type": "Accessories"})]
        self.now = datetime(2026, 11, 3, 10, tzinfo=ZoneInfo("Asia/Kolkata"))

    def test_blank_price_is_not_free_and_missing_currency_is_explicit(self):
        for value in (None, float("nan"), "N/A", "null", "TBD", "Not provided"):
            self.assertEqual(offering_price({"Price": value}), "Not provided")
        for value in ("0", "0.00", "Free"):
            self.assertEqual(offering_price({"Price": value}), "Free")
        self.assertEqual(offering_price({"Price": "499"}), "499 (currency not provided)")
        self.assertEqual(offering_price(self.a), "499 INR")

    def test_date_parsing_iso_ranges_and_invalid_or_incomplete_dates(self):
        self.assertEqual(parse_offering_date("2026-12-05"), pd.Timestamp(2026, 12, 5))
        self.assertEqual(parse_offering_date("5-6 December 2026"), pd.Timestamp(2026, 12, 5))
        self.assertEqual(parse_offering_date("5-6 December 2026", True), pd.Timestamp(2026, 12, 6))
        for value in ("next month", "31 February 2026", "12 December", "2026-99-30", "soon"):
            self.assertTrue(pd.isna(parse_offering_date(value)))

    def test_unconfirmed_schedules_and_real_product_availability(self):
        self.assertEqual(offering_lifecycle(row("A", "Event"), self.now), "Schedule not confirmed")
        self.assertEqual(offering_lifecycle(row("A", "Tote", **{"Offering Type": "Accessories"}), self.now), "Available")
        self.assertEqual(offering_lifecycle(row("A", "Event", **{"Start Date": "soon"}), self.now), "Schedule not confirmed")
        invalid = dict(self.a, **{"Start Date": "2030-12-20", "End Date": "2030-12-10"})
        self.assertEqual(offering_lifecycle(invalid, self.now), "Schedule not confirmed")
        self.assertEqual(offering_dates(invalid), "Schedule not confirmed")
        self.assertIn("end date not provided", offering_dates(self.b))

    def test_archived_or_cancelled_not_revived_and_rescheduled_event_recomputed(self):
        for status in ("Archived", "Cancelled", "Expired"):
            item = dict(self.a, **{"Lifecycle Status": status})
            self.assertEqual(offering_lifecycle(item, self.now), status)
            self.assertFalse(is_visible_offering(item, self.now))
        self.assertEqual(offering_lifecycle(dict(self.a, **{"Lifecycle Status": "Completed"}), self.now), "Upcoming")
        ended = dict(self.a, **{"Start Date": "2020-01-01", "End Date": "2020-01-02"})
        self.assertEqual(offering_lifecycle(ended, self.now), "Completed")
        self.assertFalse(is_visible_offering(ended, self.now))
        self.assertFalse(is_visible_offering(dict(self.a, **{"AI Active": "False"}), self.now))

    def test_missing_answer_reports_unknown_without_inventing_facts(self):
        blank = row("Z", "New Workshop", Price="", **{"Start Date": "next month"})
        answer = build_grounded_chat_reply("What is the price and schedule of New Workshop?", "", [blank])
        self.assertIn("**Price:** Not provided", answer["reply"])
        self.assertIn("Schedule not confirmed", answer["reply"])
        self.assertIn("follow-up", answer["reply"])
        self.assertNotIn("Free", answer["reply"])
        self.assertNotIn("Upcoming", answer["reply"])

    def test_comparison_uses_only_named_rows_including_missing_and_free_values(self):
        result = build_grounded_chat_reply("Compare Design Lab and Business Analytics", "", self.rows)
        self.assertEqual(result["offering_ids"], ["A", "B"])
        self.assertEqual(result["presentation"], "offering_comparison")
        self.assertIn("| Detail | Design Lab | Business Analytics |", result["reply"])
        self.assertIn("| Price | 499 INR | Free |", result["reply"])
        self.assertIn("| Duration | 3 hours | Not provided |", result["reply"])
        self.assertIn("https://example.com/design", result["reply"])
        self.assertIn("No registration link listed", result["reply"])
        self.assertNotIn("Canvas Tote", result["reply"])

    def test_unavailable_details_and_links_are_not_exposed_or_substituted(self):
        private = row("D", "Private Course", Price="SECRET", **{
            "Knowledge Status": "Draft", "Registration Link": "https://example.com/private",
        })
        result = build_grounded_chat_reply("Compare Design Lab and Private Course", "", self.rows + [private])
        self.assertNotIn("SECRET", result["reply"])
        self.assertNotIn("https://example.com/private", result["reply"])
        self.assertIn("aren’t available", result["reply"])
        self.assertEqual(result["offering_ids"], [])

    def test_ambiguous_reference_requires_choice_and_comparison_opens_picker(self):
        history = "USER: options?\nASSISTANT: Design Lab and Business Analytics.\nUSER: its price?"
        reply = build_grounded_chat_reply("its price?", history, self.rows)
        self.assertIn("Which offering", reply["reply"])
        self.assertNotIn("499", reply["reply"])
        choice = build_grounded_chat_reply("Compare options", "", self.rows)
        self.assertEqual(choice["presentation"], "comparison_picker")
        three = build_grounded_chat_reply("Compare Design Lab and Business Analytics and Canvas Tote", "", self.rows)
        self.assertEqual(three["presentation"], "comparison_picker")

    def test_extended_queries_keep_existing_model_or_action_path(self):
        for question in ("Why is Design Lab worth the price?", "What is Design Lab's price and can you call me?",
                         "What is Design Lab's price and refund policy?", "Register me for Design Lab", "Tell me a joke",
                         "I am excited to join Design Lab, how much is it?"):
            self.assertIsNone(build_grounded_chat_reply(question, "", self.rows), question)
        result = build_grounded_chat_reply("What are the FAQs and policies for Business Analytics?", "", self.rows)
        self.assertIn("Bring your laptop", result["reply"])
        self.assertIn("Cancellation terms", result["reply"])

    def test_actual_chat_branch_persists_exact_table_and_preserves_status_for_clarification(self):
        source = Path(__file__).with_name("app.py")
        tree = ast.parse(source.read_text(encoding="utf-8-sig"))
        branch = next(node for node in ast.walk(tree) if isinstance(node, ast.If)
                      and ast.unparse(node.test) == "factual_reply is not None")
        with tempfile.TemporaryDirectory() as tmp:
            state = {}
            lead = {"Name": "Visitor", "Visitor ID": "VIS-TEST", "Status": "Conversation Started"}
            upsert, update = MagicMock(), MagicMock()
            st = SimpleNamespace(session_state=state, rerun=MagicMock())
            scope = scope_functions("record_conversation_message", st=st, os=os, uuid=uuid, datetime=datetime,
                pd=pd, IST=ZoneInfo("Asia/Kolkata"), crm_path=lambda name: str(Path(tmp)/name), upsert_crm_rows=upsert)
            result = build_grounded_chat_reply("Compare Design Lab and Business Analytics", "", self.rows)
            scope.update(factual_reply=result, lead=lead, chat_display=MagicMock(), record_action=MagicMock(),
                         update_lead_record=update, customer_message="Compare Design Lab and Business Analytics",
                         clean_fact_value=clean_value)
            exec(compile(ast.Module(body=[branch], type_ignores=[]), str(source), "exec"), scope)
            saved = pd.read_csv(Path(tmp)/"conversation_history.csv")
            self.assertEqual(saved["content"].tolist(), [result["reply"]])
            self.assertEqual(state["customer_messages"][0]["presentation"], "offering_comparison")
            result["comparison_columns"][0]["facts"][2][1] = "Changed later"
            self.assertEqual(state["customer_messages"][0]["comparison_columns"][0]["facts"][2][1], "499 INR")
            self.assertEqual(len(upsert.call_args.args[1]), 1)
            scope["factual_reply"] = build_grounded_chat_reply("Compare options", "", self.rows)
            exec(compile(ast.Module(body=[branch], type_ignores=[]), str(source), "exec"), scope)
            self.assertEqual(update.call_args.args[1], "Conversation Started")
            self.assertEqual(update.call_args.kwargs["target_visitor_id"], "VIS-TEST")

    def test_links_are_web_only_and_website_fallback_is_saved_not_invented(self):
        for value in ("javascript:alert(1)", "https://u:p@example.com", "https://example.com:bad", "https://example.com/ bad"):
            self.assertEqual(saved_web_url(value), "")
            self.assertNotIn("](javascript", registration_detail({"Registration Link": value}))
        self.assertIn("[Visit website](https://example.com)", registration_detail({"Website": "example.com"}))
        self.assertNotIn("Visit website", registration_detail({}))
        table = comparison_reply([dict(self.a, **{"Offering Name": "A | B\n[odd]"}), self.b])
        self.assertIn(r"A \| B \[odd\]", table)

    def test_app_visibility_uses_same_status_and_preserves_archived_records(self):
        columns = list(set(self.a) | {"Lifecycle Status"})
        loader = MagicMock(return_value=pd.DataFrame([
            self.a, dict(self.b, **{"Lifecycle Status": "Archived"}),
        ]))
        scope = scope_functions("get_offering_lifecycle_status", "refresh_offering_lifecycle", "get_customer_visible_offerings",
            pd=pd, offering_lifecycle=offering_lifecycle, load_offerings=loader,
            normalize_ai_active=lambda value: str(value).lower() in {"true", "1", "yes"}, OFFERING_COLUMNS=columns)
        visible = scope["get_customer_visible_offerings"]()
        self.assertEqual(visible["Offering ID"].tolist(), ["A"])

    def test_ui_picker_selection_clear_and_submission_use_current_ids(self):
        at = AppTest.from_string('''
import streamlit as st
from customer_chat_ui import render_customer_chat
rows = [{"Offering ID": "A", "Offering Name": "Design Lab", "Knowledge Status": "Published", "AI Active": True},
        {"Offering ID": "B", "Offering Name": "Business Analytics", "Knowledge Status": "Published", "AI Active": True}]
messages = [{"role": "user", "content": "Compare options"},
            {"role": "assistant", "content": "Choose two below.", "presentation": "comparison_picker"}]
submitted = render_customer_chat(messages, "Welcome", offerings=rows)
if submitted:
    st.session_state["test_submission"] = submitted
''').run()
        self.assertFalse(at.exception)
        at.multiselect[0].set_value(["A", "B"]).run()
        next(b for b in at.button if b.label == "Clear comparison").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.multiselect[0].value, [])
        at.multiselect[0].set_value(["A", "B"]).run()
        next(b for b in at.button if b.label == "Compare selected offerings").click().run()
        self.assertFalse(at.exception)
        self.assertIn("Compare Design Lab and Business Analytics", at.session_state["test_submission"])

    def test_stale_expired_cards_have_no_registration_actions(self):
        at = AppTest.from_string('''
from customer_chat_ui import render_customer_chat
render_customer_chat([{"role": "assistant", "content": "Old event", "presentation": "offering_cards", "offering_ids": ["A"]}],
    "Welcome", offerings=[{"Offering ID": "A", "Offering Name": "Old event", "Knowledge Status": "Published", "AI Active": True,
    "Start Date": "2020-01-01", "End Date": "2020-01-02", "Registration Link": "https://example.com/old"}])
''').run()
        self.assertFalse(at.exception)
        self.assertFalse(at.get("link_button"))
        self.assertTrue(any("no longer available" in c.value for c in at.caption))
        self.assertTrue(any(b.label == "Refresh offering details" for b in at.button))


if __name__ == "__main__":
    unittest.main(verbosity=2)
