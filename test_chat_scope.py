"""Offline routing tests: no live model, CRM, messages, calls, or credentials."""

import ast
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from google.genai import types

from chat_performance import CHAT_RESPONSE_SCHEMA, INTENTS
from chat_scope import (
    CHAT_SCOPE_POLICY, CURRENT_INFORMATION_REPLY, PLAYFUL_REDIRECT,
    is_catalog_link_request, normalize_chat_result, schema_with_scope,
)
from offering_facts import build_grounded_chat_reply


def handlers(*names, **scope):
    path = Path(__file__).with_name("app.py")
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
    return scope


def model_reply(kind, reply, **values):
    return dict(intent="Other", sentiment="Neutral", priority="Low",
                recommended_action="No Action", response_kind=kind, reply=reply, **values)


class ChatScopeChecks(unittest.TestCase):
    def analyze(self, message, response):
        models = MagicMock()
        models.generate_content.return_value.text = json.dumps(response)
        scope = handlers("analyze_customer_message", re=re, json=json, types=types,
            CHAT_RESPONSE_SCHEMA=CHAT_RESPONSE_SCHEMA, INTENTS=INTENTS,
            client=SimpleNamespace(models=models),
            is_catalog_listing_question=lambda _: False,
            is_catalog_comparison_question=lambda _: False,
            get_verified_offering_context=lambda *args: "NO VERIFIED OFFERING INFORMATION IS AVAILABLE.")
        answer = scope["analyze_customer_message"](message, "USER: Earlier product question", include_reply=True)
        return answer, models

    def test_general_examples_return_answer_in_single_request_even_without_catalog(self):
        examples = (
            ("What is the capital of USA?", "The capital of the United States is Washington, D.C."),
            ("How to reach Mumbai from Delhi?", "You can travel by air, rail, or road. Check current schedules and fares with the operator."),
            ("Why is the sky blue?", "Air molecules scatter shorter blue wavelengths of sunlight more strongly."),
            ("Compare Python and Java", "Python uses dynamic typing; Java uses static typing."),
        )
        for question, reply in examples:
            with self.subTest(question=question):
                result, models = self.analyze(question, model_reply("general", reply))
                self.assertEqual(result["reply"], reply)
                self.assertEqual(result["recommended_action"], "No Action")
                self.assertEqual(result["intent"], "Other")
                models.generate_content.assert_called_once()
                config = models.generate_content.call_args.kwargs["config"]
                self.assertEqual(config.system_instruction, CHAT_SCOPE_POLICY)
                self.assertIn("response_kind", config.response_schema["required"])
                prompt = models.generate_content.call_args.kwargs["contents"]
                self.assertIn("For timeless general questions, answer from general knowledge", prompt)
                self.assertNotIn("For Other, explain politely that you help", prompt)

    def test_policy_distinguishes_curiosity_complaints_and_sensitive_requests_from_bait(self):
        for phrase in ("Default to good faith", "poor grammar", "fictional hypothetical",
                       "repeating an unanswered question", "Product complaints", "must never receive a roast",
                       "Never shame, threaten, intimidate", "private CRM records",
                       "General knowledge must never fill a missing company fact"):
            self.assertIn(phrase, CHAT_SCOPE_POLICY)

    def test_current_affairs_replaces_a_model_guess_with_clear_limitation(self):
        result, _ = self.analyze("Who is the current president?", model_reply("current_affairs", "A guessed name"))
        self.assertEqual(result["reply"], CURRENT_INFORMATION_REPLY)
        self.assertNotIn("guessed", result["reply"])

    def test_bait_gets_fixed_gentle_comeback_never_model_insult_or_escalating_roast(self):
        for _ in range(3):
            result, _ = self.analyze("You useless idiot", model_reply("banter", "An insulting model reply"))
            self.assertEqual(result["reply"], PLAYFUL_REDIRECT)
            self.assertNotIn("idiot", result["reply"])
            self.assertEqual(result["sentiment"], "Neutral")

    def test_non_sales_cannot_trigger_outreach_or_close_customer(self):
        for kind in ("general", "current_affairs", "banter", "support", "unknown"):
            response = dict(intent="Ready to Buy", sentiment="Positive", priority="High",
                            recommended_action="Escalate to Human", response_kind=kind, reply="Response")
            result, _ = self.analyze("Question", response)
            self.assertEqual(result["intent"], "Other")
            self.assertEqual(result["recommended_action"], "No Action")
            self.assertEqual(result["priority"], "Low")

    def test_sales_still_uses_verified_facts_and_existing_action_map(self):
        response = dict(intent="Pricing Question", sentiment="Neutral", priority="Medium",
                        recommended_action="Send Pricing", response_kind="sales", reply="The saved fee is 499 INR.")
        result, _ = self.analyze("What is the fee for Design Lab?", response)
        self.assertEqual(result, response)
        response.update(intent="Not Interested", recommended_action="Close Lead", reply="Understood.")
        result, _ = self.analyze("Please stop contacting me", response)
        self.assertEqual(result["recommended_action"], "Close Lead")

    def test_support_and_mixed_general_live_responses_are_preserved_without_roasting(self):
        for kind, reply in (("support", "I’m sorry you’re going through this. Let’s focus on getting you support."),
                            ("general", "Weather describes atmospheric conditions. I can’t check live weather here.")):
            result, _ = self.analyze("Question", model_reply(kind, reply))
            self.assertEqual(result["reply"], reply)

    def test_schema_and_result_normalization_do_not_mutate_shared_inputs(self):
        original = json.dumps(CHAT_RESPONSE_SCHEMA, sort_keys=True)
        scoped = schema_with_scope(CHAT_RESPONSE_SCHEMA)
        self.assertIn("response_kind", scoped["properties"])
        self.assertEqual(json.dumps(CHAT_RESPONSE_SCHEMA, sort_keys=True), original)
        response = model_reply("banter", "Bad reply")
        normalize_chat_result(response)
        self.assertEqual(response["reply"], "Bad reply")
        self.assertEqual(normalize_chat_result(dict(intent="Other", reply="Answer"))["response_kind"], "general")

    def test_general_chat_is_logged_and_shown_without_rewriting_sales_record_or_email(self):
        lead = {"Name": "Test Customer", "Visitor ID": "VIS-TEST", "Status": "Ready to Buy",
                "Priority": "High", "Last Intent": "Ready to Buy", "Interest Score": 9,
                "Email": "example@example.com"}
        original = dict(lead)
        update, email, log, publish = MagicMock(), MagicMock(), MagicMock(), MagicMock()
        scope = handlers("execute_and_update_sales_action",
            execute_sales_action=MagicMock(side_effect=AssertionError("No second answer request")),
            update_lead_record=update, send_email=email, record_action=log,
            st=SimpleNamespace(session_state={}))
        result = scope["execute_and_update_sales_action"](
            "Other", "No Action", "What is the capital of USA?", selected_lead=lead,
            priority="Low", sentiment="Neutral", prepared_reply="Washington, D.C.", on_reply_ready=publish)
        self.assertEqual(result["customer_message"], "Washington, D.C.")
        self.assertEqual(result["status"], "Ready to Buy")
        self.assertEqual(lead, original)
        update.assert_not_called()
        email.assert_not_called()
        publish.assert_called_once_with("Washington, D.C.")
        log.assert_called_once()

    def test_general_questions_are_not_hijacked_by_sales_fast_paths(self):
        rows = [{"Offering ID": "A", "Offering Name": "Design Lab", "Knowledge Status": "Published", "AI Active": True}]
        history = "USER: What do you offer?\nASSISTANT: Design Lab\n"
        for question in ("What is the capital of USA?", "How to reach Mumbai from Delhi?",
                         "Compare Python and Java", "What is the current price of gold?",
                         "What is the difference between products and services?"):
            self.assertIsNone(build_grounded_chat_reply(question, history, rows), question)
        self.assertIsNotNone(build_grounded_chat_reply("Compare the options", history, rows))
        self.assertFalse(is_catalog_link_request("Give me the website link for NASA", history, rows))
        self.assertTrue(is_catalog_link_request("Give the registration link for each", history, rows))
        self.assertTrue(is_catalog_link_request("Give me the link for Design Lab", history, rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
