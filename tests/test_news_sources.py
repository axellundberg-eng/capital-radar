from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.sources.issuer_feeds import IssuerFeed, parse_feed as parse_ir_feed
from capital_radar.sources.mfn import parse_feed as parse_mfn_feed
from capital_radar.sources.nasdaq_news import parse_items, parse_jsonp


class NewsSourceTests(unittest.TestCase):
    def test_mfn_feed_parser_uses_list_metadata_without_detail_fetch(self) -> None:
        html = """
        <div class="short-item compressible" id="46bf6b27">
          <span class="compressed-date">2026-07-23</span>
          <span class="compressed-time">09:00:00</span>
          <a class="title-link author-link author-preview"
             href="/all/a/netel-holding"
             author="netel-holding">Netel Holding AB (publ)</a>
          <a class="title-link item-link"
             href="/a/netel-holding/financing"
             title="Netel secures new financing">Netel secures new financing</a>
          <a class="attachment-icon"
             href="https://storage.mfn.se/id/financing.pdf"></a>
        </div>
        """
        item = parse_mfn_feed(html)[0]
        self.assertEqual(item.external_id, "46bf6b27")
        self.assertEqual(item.issuer_name, "Netel Holding AB (publ)")
        self.assertEqual(item.published_at, "2026-07-23T09:00:00+02:00")
        self.assertEqual(item.attachments[0].mime_type, "application/pdf")

    def test_mfn_company_page_uses_validated_default_issuer(self) -> None:
        html = """
        <div class="short-item compressible" id="issuer-item-1">
          <span class="compressed-date">2026-07-23</span>
          <span class="compressed-time">09:00:00</span>
          <a class="title-link item-link"
             href="/a/polygiene-group/interim-report"
             title="Polygiene Group Interim Report">Interim report</a>
        </div>
        """
        item = parse_mfn_feed(
            html,
            default_issuer_name="Polygiene Group",
            default_issuer_slug="polygiene-group",
            default_issuer_url="https://www.mfn.se/all/a/polygiene-group",
        )[0]
        self.assertEqual(item.issuer_name, "Polygiene Group")
        self.assertEqual(item.metadata["issuer_slug"], "polygiene-group")
        self.assertEqual(
            item.metadata["issuer_url"],
            "https://www.mfn.se/all/a/polygiene-group",
        )

    def test_nasdaq_jsonp_parser_filters_to_target_markets(self) -> None:
        body = """handleResponse({"results":{"item":[
          {"disclosureId":1,"headline":"Riktad emission","language":"sv",
           "languages":["sv"],"cnsCategory":"Investor News",
           "messageUrl":"https://view.news.eu.nasdaq.com/view?id=1",
           "published":"2026-07-23 09:00:00",
           "market":"First North Sweden","attachment":[],
           "company":"Exempel AB","categoryId":55,"cnsTypeId":"3"},
          {"disclosureId":2,"headline":"Other","language":"en",
           "messageUrl":"https://example.com/2",
           "published":"2026-07-23 09:00:00",
           "market":"Main Market, Copenhagen","attachment":[],
           "company":"Other A/S"}
        ]}});"""
        items = parse_items(parse_jsonp(body))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "1")
        self.assertEqual(items[0].published_at, "2026-07-23T09:00:00+02:00")

    def test_issuer_rss_parser(self) -> None:
        body = b"""<?xml version="1.0"?>
        <rss><channel><item>
          <guid>abc-1</guid>
          <title>Exempel publicerar delarsrapport</title>
          <link>https://example.com/report</link>
          <pubDate>Thu, 23 Jul 2026 07:00:00 GMT</pubDate>
          <enclosure url="https://example.com/report.pdf" type="application/pdf"/>
        </item></channel></rss>"""
        item = parse_ir_feed(
            body, feed=IssuerFeed("Exempel AB", "https://example.com/rss", "sv")
        )[0]
        self.assertEqual(item.external_id, "abc-1")
        self.assertEqual(item.attachments[0].url, "https://example.com/report.pdf")


if __name__ == "__main__":
    unittest.main()
