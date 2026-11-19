"""Checks that links in sent HTML email retain their invitation tokens."""
import unittest
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

from crm_email_delivery import email_markdown_to_html


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for name, value in attrs if name == "href")


class EmailMarkupTests(unittest.TestCase):
    def test_prospect_invitation_link_retains_invite_parameter(self):
        token = "sample-invitation-token"
        body = f"[Chat with us](https://example.streamlit.app/?chat=prospect&invite={token})"
        markup = email_markdown_to_html(body)
        links = _Links()
        links.feed(markup)

        self.assertEqual(len(links.hrefs), 1)
        self.assertEqual(parse_qs(urlparse(links.hrefs[0]).query),
                         {"chat": ["prospect"], "invite": [token]})
        self.assertIn("&amp;invite=", markup)
        self.assertNotIn("&amp;amp;invite=", markup)


if __name__ == "__main__":
    unittest.main()
