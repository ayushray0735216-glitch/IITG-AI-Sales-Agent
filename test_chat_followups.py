"""Isolated conversation checks: no credentials, live CRM writes or delivery."""

import ast
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlparse

import pandas as pd
from google.genai import types
from streamlit.testing.v1 import AppTest

from chat_followups import (
    DEFAULT_SUGGESTIONS, acknowledgment_reply, build_chat_suggestions,
    context_offerings, named_offerings, referenced_offerings,
)
from chat_performance import CHAT_RESPONSE_SCHEMA, INTENTS


def offering(identity, name, **fields):
    return {"Offering ID": identity, "Offering Name": name, "Knowledge Status": "Published",
            "AI Active": True, "Lifecycle Status": "Upcoming", **fields}


def isolated_functions(*names, **scope):
    """Run actual handlers without importing app.py or starting its workers."""
    source = Path(__file__).with_name("app.py")
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), scope)
    return scope


def turns(question, answer):
    return [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]


class FollowupChecks(unittest.TestCase):
    def setUp(self):
        self.rows = [
            offering("A", "Design Lab", Price="499", **{
                "Start Date": "12 December 2026", "Target Audience": "Beginners",
                "Registration Link": "https://example.com/design", "FAQs": "No experience required.",
                "Policies": "Refunds up to seven days before the event.",
            }),
            offering("B", "Business Analytics", Price="999", **{
                "Start Date": "20 December 2026", "Registration Link": "https://example.com/analytics",
            }),
            offering("C", "Canvas Tote", Price="250"),
        ]

    def test_initial_suggestions_and_current_topic_switch(self):
        self.assertEqual(build_chat_suggestions([], self.rows), DEFAULT_SUGGESTIONS)
        history = turns("Tell me about Design Lab", "Design Lab is for beginners.")
        history += turns("How much is Canvas Tote?", "Canvas Tote costs 250 INR.")
        choices = build_chat_suggestions(history, self.rows)
        self.assertLessEqual(len(choices), 3)
        self.assertTrue(all("Design Lab" not in q for _, _, q in choices))
        self.assertFalse(any(label in {"Registration link", "Dates & times"} for label, _, _ in choices))
        self.assertFalse(any(label == "Price" for label, _, _ in choices))

    def test_pronoun_resolves_recent_named_offering_and_avoids_old_topic(self):
        history = turns("Tell me about Design Lab", "Design Lab is for beginners.")
        history += turns("What is its price?", "The fee is 499 INR.")
        choices = build_chat_suggestions(history, self.rows)
        self.assertTrue(any("Design Lab" in q for _, _, q in choices))
        history += turns("What is the capital of India?", "I can help with our offerings.")
        self.assertEqual(build_chat_suggestions(history, self.rows), DEFAULT_SUGGESTIONS)

    def test_ambiguous_multiple_names_stay_multiple_and_card_ids_are_honored(self):
        history = turns("Which workshops are there?", "Design Lab and Business Analytics.")
        history += turns("When are those?", "Which schedule would you like?")
        self.assertEqual(len(context_offerings(history, self.rows)), 2)
        for _, _, prompt in build_chat_suggestions(history, self.rows)[:2]:
            self.assertIn("Design Lab", prompt)
            self.assertIn("Business Analytics", prompt)
        cards = [{"role": "assistant", "content": "Explore the catalog", "offering_ids": ["A", "B", "old"]}]
        self.assertEqual(len(context_offerings(cards, self.rows)), 2)

    def test_gratitude_optout_and_missing_facts(self):
        for closing in ("okay thank you", "thanks", "no thanks", "not interested", "Please stop contacting me", "bye"):
            self.assertEqual(build_chat_suggestions(turns(closing, "Understood."), self.rows), ())
        self.assertTrue(build_chat_suggestions(turns("Thanks, when is Design Lab?", "12 December."), self.rows))
        self.assertEqual(acknowledgment_reply("okay"), "Got it. I’m here if you need anything else.")
        self.assertEqual(acknowledgment_reply("thank you"), "You’re welcome!")
        self.assertEqual(build_chat_suggestions(turns("About Blank", "Blank"), [offering("Z", "Blank")]),
                         (DEFAULT_SUGGESTIONS[0],))

    def test_specific_title_does_not_also_match_parent(self):
        rows = [offering("P", "Analytics"), offering("S", "Analytics (Advanced)")]
        self.assertEqual([row["Offering ID"] for row in named_offerings("About Analytics (Advanced)", rows)], ["S"])
        self.assertEqual(len(named_offerings("Compare Analytics and Analytics (Advanced)", rows)), 2)

    def test_multiple_facts_retrieval_is_complete_and_excludes_private_records(self):
        private = offering("D", "Private Course", Price="SECRET", **{"Knowledge Status": "Draft"})
        scope = isolated_functions("get_verified_offering_context", re=re, pd=pd,
            load_offerings=lambda: pd.DataFrame(self.rows + [private]),
            refresh_offering_lifecycle=lambda frame: frame,
            named_offerings=named_offerings, referenced_offerings=referenced_offerings, normalize_ai_active=bool,
            is_catalog_comparison_question=lambda message: "compare" in message.lower())
        context = scope["get_verified_offering_context"]("Prices for Design Lab and Business Analytics and Private Course")
        self.assertIn("Price: 499", context)
        self.assertIn("Price: 999", context)
        self.assertIn("REQUESTED_OFFERING_UNAVAILABLE", context)
        self.assertNotIn("SECRET", context)
        self.assertNotIn("Canvas Tote", context)

    def test_followup_retrieval_uses_newest_topic_and_keeps_ambiguity(self):
        scope = isolated_functions("get_verified_offering_context", re=re, pd=pd,
            load_offerings=lambda: pd.DataFrame(self.rows),
            refresh_offering_lifecycle=lambda frame: frame,
            named_offerings=named_offerings, referenced_offerings=referenced_offerings,
            normalize_ai_active=bool, is_catalog_comparison_question=lambda message: False)
        history = "USER: Business Analytics\nASSISTANT: Business Analytics costs 999.\nUSER: Design Lab\nASSISTANT: Design Lab is for beginners.\nUSER: its price?"
        context = scope["get_verified_offering_context"]("its price?", history)
        self.assertIn("Design Lab", context)
        self.assertNotIn("Business Analytics", context)
        ambiguous = "USER: workshops?\nASSISTANT: Design Lab and Business Analytics.\nUSER: its price?"
        context = scope["get_verified_offering_context"]("its price?", ambiguous)
        self.assertIn("Design Lab", context)
        self.assertIn("Business Analytics", context)

    def test_link_buttons_answer_only_requested_subset(self):
        scope = isolated_functions("build_registration_link_reply", re=re, pd=pd,
            named_offerings=named_offerings, urlparse=urlparse,
            get_customer_visible_offerings=lambda: pd.DataFrame(self.rows))
        reply = scope["build_registration_link_reply"]("Give the registration link for each of these: Design Lab; Business Analytics")
        self.assertIn("https://example.com/design", reply)
        self.assertIn("https://example.com/analytics", reply)
        self.assertNotIn("Canvas Tote", reply)
        reply = scope["build_registration_link_reply"]("Give the links for all")
        self.assertIn("Canvas Tote", reply)
        self.assertIn("No registration link", reply)

    def test_reply_instructions_keep_single_request_and_factual_answer(self):
        models = MagicMock()
        models.generate_content.return_value.text = json.dumps({
            "intent": "Pricing Question", "sentiment": "Neutral", "priority": "Medium",
            "recommended_action": "Send Pricing", "reply": "Design Lab costs 499 INR.",
        })
        scope = isolated_functions("analyze_customer_message", re=re, json=json, types=types,
            CHAT_RESPONSE_SCHEMA=CHAT_RESPONSE_SCHEMA, INTENTS=INTENTS,
            client=SimpleNamespace(models=models),
            is_catalog_comparison_question=lambda message: False,
            is_catalog_listing_question=lambda message: False,
            get_verified_offering_context=lambda *args: "Offering Name: Design Lab\nPrice: 499 INR")
        result = scope["analyze_customer_message"]("its price?", "USER: Design Lab", include_reply=True)
        self.assertEqual(result["reply"], "Design Lab costs 499 INR.")
        self.assertEqual(models.generate_content.call_count, 1)
        prompt = models.generate_content.call_args.kwargs["contents"]
        self.assertIn("ask one focused clarification", prompt)
        self.assertIn("never omit facts", prompt)
        self.assertIn("not permission for an action", prompt)

    def test_native_suggestion_click_submits_explicit_question_and_retains_thinking(self):
        at = AppTest.from_string('''
import streamlit as st
from customer_chat_ui import render_customer_chat
messages = [{"role": "user", "content": "Tell me about Design Lab"},
            {"role": "assistant", "content": "Design Lab is for beginners."}]
rows = [{"Offering ID": "A", "Offering Name": "Design Lab", "Price": "499", "Knowledge Status": "Published", "AI Active": True,
         "Start Date": "12 December 2099", "Registration Link": "https://example.com/design"}]
submitted = render_customer_chat(messages, "Welcome", offerings=rows)
if submitted:
    st.session_state["test_submitted"] = submitted
''').run()
        self.assertFalse(at.exception)
        next(button for button in at.button if button.label == "Price").click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state["test_submitted"], "What is the price of Design Lab?")
        self.assertTrue(at.get("html"))
        self.assertEqual(at.chat_message[-2].markdown[0].value, "What is the price of Design Lab?")


if __name__ == "__main__":
    unittest.main(verbosity=2)
