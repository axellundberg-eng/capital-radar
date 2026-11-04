"""Tolka enheten som rapporten anger (TSEK, MSEK, kEUR ...)."""

from __future__ import annotations

import re

PREFIX = {"": 1.0, "T": 1e3, "K": 1e3, "M": 1e6, "MN": 1e6, "MD": 1e9, "B": 1e9}
CURRENCY_ALIASES = {"KR": "SEK", "KRONOR": "SEK", "SEK": "SEK", "EUR": "EUR", "USD": "USD",
                    "NOK": "NOK", "DKK": "DKK", "GBP": "GBP"}
WORDS = {"THOUSANDS": 1e3, "THOUSAND": 1e3, "TUSEN": 1e3, "MILLIONS": 1e6, "MILLION": 1e6, "MILJONER": 1e6,
         "M": 1e6, "MN": 1e6, "MLN": 1e6, "K": 1e3, "T": 1e3, "BN": 1e9}
SUFFIX = {"M": 1e6, "MN": 1e6, "K": 1e3, "T": 1e3, "BN": 1e9}


def parse_unit(unit: str | None, currency: str | None = None) -> tuple[float, str | None] | None:
    """Returnerar (multiplikator, valuta) eller None om enheten är okänd."""
    words = re.sub(r"[^A-Z]", " ", (unit or "").upper()).split()
    multiplier, found = 1.0, None
    for word in words:
        if word in WORDS:
            multiplier = WORDS[word]
            continue
        for prefix in ("MN", "MD", "T", "K", "M", "B", ""):
            if word.startswith(prefix) and word[len(prefix):] in CURRENCY_ALIASES:
                found = CURRENCY_ALIASES[word[len(prefix):]]
                if PREFIX[prefix] > 1:
                    multiplier = PREFIX[prefix]
                break
        else:
            # Engelsk form: SEKm, SEKbn, EURk
            for suffix in ("MN", "BN", "M", "K", "T"):
                if word.endswith(suffix) and word[:-len(suffix)] in CURRENCY_ALIASES:
                    found, multiplier = CURRENCY_ALIASES[word[:-len(suffix)]], SUFFIX[suffix]
                    break
    found = found or (currency.upper() if currency else None)
    if not words and not currency:
        return None
    return multiplier, found
