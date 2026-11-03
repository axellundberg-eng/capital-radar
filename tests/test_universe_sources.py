from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.sources.nasdaq import SEGMENTS, parse_rows as parse_nasdaq_rows
from capital_radar.sources.ngm import parse_rows as parse_ngm_rows


class UniverseSourceTests(unittest.TestCase):
    def test_nasdaq_parser_keeps_reference_fields_only(self) -> None:
        payload = {
            "data": {
                "instrumentListing": {
                    "rows": [
                        {
                            "fullName": "Acrinova A",
                            "currency": "SEK",
                            "orderbookId": "TX3238630",
                            "assetClass": "SHARES",
                            "symbol": "ACRI A",
                            "sector": "Real Estate",
                            "isin": "SE0015660014",
                            "lastSalePrice": "8.00",
                        }
                    ]
                }
            }
        }
        item = parse_nasdaq_rows(payload, segment=SEGMENTS[0])[0]
        self.assertEqual(item.isin, "SE0015660014")
        self.assertEqual(item.segment, "Small Cap")
        self.assertEqual(item.mic, "XSTO")
        self.assertNotIn("lastSalePrice", item.metadata)

    def test_ngm_parser_filters_wrong_segment_and_instrument_type(self) -> None:
        payload = {
            "data": [
                {
                    "insref": 89945,
                    "name": "ECOMB",
                    "symbol": "ECOMB",
                    "type": "Equity",
                    "isin": "SE0023950555",
                    "issuer": "ECOMB AB",
                    "marketSegment": "NGM Growth Market",
                },
                {
                    "insref": 1,
                    "name": "Wrong",
                    "symbol": "WRONG",
                    "type": "Equity",
                    "isin": "SE0000000001",
                    "issuer": "Wrong AB",
                    "marketSegment": "NGM Main Market",
                },
            ]
        }
        items = parse_ngm_rows(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].external_id, "89945")
        self.assertEqual(items[0].mic, "NSME")


if __name__ == "__main__":
    unittest.main()

