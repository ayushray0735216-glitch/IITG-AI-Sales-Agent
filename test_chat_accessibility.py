"""Native layout, labels, history and responsive comparison regression checks."""

import unittest
from html.parser import HTMLParser

from streamlit.testing.v1 import AppTest

from customer_chat_ui import THINKING_INDICATOR_HTML, split_chat_history
from offering_facts import build_grounded_chat_reply


class StatusMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.attributes.append((tag, dict(attrs)))


class ChatAccessibilityChecks(unittest.TestCase):
    def test_history_is_preserved_and_recent_view_starts_with_customer(self):
        messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(13)]
        earlier, recent, offset = split_chat_history(messages)
        self.assertEqual(earlier + recent, messages)
        self.assertEqual(offset, len(earlier))
        self.assertEqual(recent[0]["role"], "user")
        self.assertEqual(recent[-1]["content"], "12")
        self.assertEqual(len(messages), 13)
        self.assertEqual(split_chat_history([]), ([], [], 0))

    def test_composer_is_pinned_outside_panel_with_descriptive_placeholder(self):
        at = AppTest.from_string('''
import streamlit as st
from customer_chat_ui import render_customer_chat
submitted = render_customer_chat([], 'Welcome')
if submitted:
    st.session_state['received'] = submitted
''').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.chat_input), 1)
        self.assertEqual(len(at.main.get('chat_input')), 0)
        self.assertIn('Your message', at.chat_input[0].placeholder)
        at.chat_input[0].set_value('My question').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['received'], 'My question')

    def test_older_messages_remain_available_in_native_expander(self):
        at = AppTest.from_string('''
from customer_chat_ui import render_customer_chat
messages = [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': 'Message '+str(i)} for i in range(12)]
render_customer_chat(messages, 'Welcome')
''').run()
        self.assertFalse(at.exception)
        self.assertEqual(at.expander[0].label, 'Earlier messages (6)')
        self.assertEqual([message.markdown[0].value for message in at.chat_message], ['Message '+str(i) for i in range(12)])

    def test_contextual_form_labels_and_validation_then_send(self):
        at = AppTest.from_string('''
import streamlit as st
from customer_chat_ui import render_customer_chat
rows = [{'Offering ID': 'A', 'Offering Name': 'Design Lab', 'Knowledge Status': 'Published', 'AI Active': True,
         'Offering Type': 'Workshop', 'Start Date': '12 December 2099', 'Registration Link': 'https://example.com/design'}]
messages = [{'role': 'assistant', 'content': 'Design Lab', 'presentation': 'offering_cards', 'offering_ids': ['A']}]
submitted = render_customer_chat(messages, 'Welcome', offerings=rows)
if submitted:
    st.session_state['received'] = submitted
''').run()
        self.assertFalse(at.exception)
        self.assertIn('Design Lab', at.text_input[0].label)
        labels = [button.label for button in at.button]
        self.assertIn('Compare Design Lab', labels)
        self.assertTrue(any('call request for Design Lab' in label for label in labels))
        next(button for button in at.button if button.label == 'Send question about Design Lab').click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any('Enter a question about Design Lab' in warning.value for warning in at.warning))
        at.text_input[0].set_value('What is included?')
        next(button for button in at.button if button.label == 'Send question about Design Lab').click().run()
        self.assertFalse(at.exception)
        self.assertEqual(at.session_state['received'], 'About Design Lab: What is included?')

    def test_comparison_cards_use_saved_values_and_can_switch_to_original_table(self):
        at = AppTest.from_string('''
import streamlit as st
from customer_chat_ui import render_customer_chat
from offering_facts import build_grounded_chat_reply
rows = [{'Offering ID': 'A', 'Offering Name': 'Design Lab', 'Knowledge Status': 'Published', 'AI Active': True, 'Price': '499', 'Currency': 'INR'},
        {'Offering ID': 'B', 'Offering Name': 'Business Analytics', 'Knowledge Status': 'Published', 'AI Active': True, 'Price': '0'}]
if 'saved' not in st.session_state:
    result = build_grounded_chat_reply('Compare Design Lab and Business Analytics', '', rows)
    st.session_state['saved'] = {'role': 'assistant', 'content': result['reply'], 'presentation': result['presentation'],
                                 'offering_ids': result['offering_ids'], 'comparison_columns': result['comparison_columns']}
rows[0]['Price'] = '99999'
render_customer_chat([st.session_state['saved']], 'Welcome', offerings=rows)
''').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.columns), 2)
        text = '\n'.join(markdown.value for markdown in at.markdown)
        self.assertIn('499 INR', text)
        self.assertNotIn('99999', text)
        group = at.get('button_group')[0]
        group.set_value('Table').run()
        self.assertFalse(at.exception)
        self.assertEqual(len(at.columns), 0)
        self.assertTrue(any('| Price | 499 INR | Free |' in markdown.value for markdown in at.markdown))

    def test_legacy_comparison_without_snapshot_remains_readable(self):
        at = AppTest.from_string('''
from customer_chat_ui import render_customer_chat
render_customer_chat([{'role': 'assistant', 'presentation': 'offering_comparison',
                      'content': '| Detail | A | B |\\n| --- | --- | --- |\\n| Price | 499 | Free |'}], 'Welcome')
''').run()
        self.assertFalse(at.exception)
        self.assertTrue(any('| Price | 499 | Free |' in markdown.value for markdown in at.markdown))

    def test_thinking_status_announces_text_without_requiring_motion(self):
        parser = StatusMarkup()
        parser.feed(THINKING_INDICATOR_HTML)
        status = next(attrs for _, attrs in parser.attributes if attrs.get('role') == 'status')
        self.assertEqual(status['aria-live'], 'polite')
        self.assertEqual(status['aria-atomic'], 'true')
        self.assertTrue(any(attrs.get('aria-hidden') == 'true' for _, attrs in parser.attributes))
        self.assertIn('prefers-reduced-motion: reduce', THINKING_INDICATOR_HTML)
        self.assertIn('animation: none', THINKING_INDICATOR_HTML)
        self.assertIn('Sales assistant is preparing a reply', THINKING_INDICATOR_HTML)


if __name__ == '__main__':
    unittest.main(verbosity=2)
