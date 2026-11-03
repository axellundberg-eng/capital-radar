from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from capital_radar.normalization import canonicalize_url, normalize_issuer_name


class NormalizationTests(unittest.TestCase):
    def test_share_classes_and_corporate_suffixes_resolve_to_same_issuer(self) -> None:
        values = {
            normalize_issuer_name("Acrinova A"),
            normalize_issuer_name("Acrinova B"),
            normalize_issuer_name("Acrinova AB (publ)"),
        }
        self.assertEqual(values, {"acrinova"})

    def test_url_tracking_parameters_are_removed(self) -> None:
        self.assertEqual(
            canonicalize_url(
                "HTTPS://Example.COM/a//b/?utm_source=x&lang=sv#section"
            ),
            "https://example.com/a/b?lang=sv",
        )


if __name__ == "__main__":
    unittest.main()

