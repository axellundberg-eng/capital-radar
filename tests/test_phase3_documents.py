from __future__ import annotations

import sys
import unittest
from pathlib import Path
from urllib.error import URLError


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.document_quality import evaluate_document_quality
from capital_radar.documents import DocumentError, extract_document, validate_document_url
from capital_radar.http import HttpClient, HttpError
from capital_radar.text_selection import select_relevant_text


class Phase3DocumentTests(unittest.TestCase):
    def test_url_policy_rejects_http_and_unknown_hosts(self) -> None:
        allowed = ("storage.mfn.se",)
        with self.assertRaises(DocumentError):
            validate_document_url("http://storage.mfn.se/report.pdf", allowed)
        with self.assertRaises(DocumentError):
            validate_document_url("https://example.com/report.pdf", allowed)
        self.assertEqual(
            validate_document_url("https://storage.mfn.se/report.pdf", allowed),
            "storage.mfn.se",
        )

    def test_html_extraction_removes_scripts(self) -> None:
        body = b"""
        <html><script>ignore this</script><body>
        <h1>Interim report</h1><p>Cash and cash equivalents SEK 12m.</p>
        </body></html>
        """
        extracted = extract_document(body, "text/html", "https://www.mfn.se/a/x")
        self.assertIn("Cash and cash equivalents", extracted.text)
        self.assertNotIn("ignore this", extracted.text)
        self.assertEqual(extracted.page_count, 1)

    def test_relevant_page_is_selected_under_character_limit(self) -> None:
        pages = []
        for number in range(1, 9):
            content = "ordinary narrative " * 30
            if number == 6:
                content = (
                    "Cash and cash equivalents. Likvida medel. "
                    "Cash flow from operating activities. "
                ) * 12
            pages.append(f"[PAGE {number}]\n{content}")
        selected = select_relevant_text(
            "\n\n".join(pages),
            max_chars=2800,
            document_title="Interim report",
        )
        self.assertIn(6, selected.selected_pages)
        self.assertLessEqual(selected.selected_char_count, 2800)

    def test_broken_column_cash_flow_heading_is_still_complete(self) -> None:
        text = """
        Delårsrapport. Likvida medel 164 628 Tkr.
        Koncernens rapport över kassaflöden i sammandrag.
        KASSAFLÖDE FRÅN DEN
        UTGÅENDE LIKVIDA MEDEL 164 628
        LÖPANDE VERKSAMHETEN 16 850
        """
        quality = evaluate_document_quality(
            text,
            profile_category="report",
            page_count=40,
        )
        self.assertEqual(quality.status, "complete")
        self.assertIn("operating_cash_flow", quality.signals)

    def test_layout_split_cash_phrase_is_still_complete(self) -> None:
        # pypdf layout-lage bryter "Likvida medel" till "Lik vida medel"
        # (kolumnjustering). Kassa-signalen maste anda traffa.
        text = """
        Delårsrapport januari–juni 2026.
        Balansräkning. Lik vida medel 38 448 30 241 67 573
        Kassaflöde från den löpande verksamheten -53 193
        """
        quality = evaluate_document_quality(
            text,
            profile_category="report",
            page_count=17,
        )
        self.assertEqual(quality.status, "complete")
        self.assertIn("cash", quality.signals)

    def test_http_client_always_has_https_and_reports_error_type(self) -> None:
        client = HttpClient(
            user_agent="test",
            timeout=1,
            min_interval=0,
            retries=0,
        )
        self.assertIn(
            "HTTPSHandler",
            {type(handler).__name__ for handler in client._opener.handlers},
        )

        class BrokenOpener:
            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                raise URLError("")

        client._opener = BrokenOpener()
        with self.assertRaisesRegex(HttpError, "URLError"):
            client.get("https://www.mfn.se/all/a/test")


if __name__ == "__main__":
    unittest.main()
