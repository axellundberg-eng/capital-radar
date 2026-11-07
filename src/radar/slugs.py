"""MFN-slugar för bolag som saknar `companies.mfn_slug`.

MFN:s egen sökruta anropar `GET https://www.mfn.se/search/companies?limit=5&query=<namn>`
och svarar med en JSON-lista. Varje träff har bland annat `entity_id`, `slug`, `slugs`,
`name`, `isins`, `local_refs` (t.ex. `SE:556731-3126`) och `tickers`.

Flödet är avsiktligt uppdelat i två steg: `dry_run` läser databasen och frågar MFN men
skriver ingenting, och `apply` skriver bara de matchtyper som uttryckligen valts.
Slugen skrivs aldrig över om bolaget redan har en.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .settings import Settings

SEARCH_URL = "https://www.mfn.se/search/companies"
SEARCH_LIMIT = 5
DELAY_SECONDS = 1.5            # paus mellan sökningar, så MFN inte belastas
MAX_TIMEOUT_SECONDS = 60.0     # översta gräns för nätverksanrop enligt AGENTS.md
CLOSE_THRESHOLD = 0.9          # difflib-likhet som krävs för "name_close"
MAX_BYTES = 1_000_000

METHOD_ISIN = "isin"
METHOD_NAME_EXACT = "name_exact"
METHOD_NAME_CLOSE = "name_close"
DEFAULT_APPLY_METHODS = (METHOD_ISIN, METHOD_NAME_EXACT)

Fetch = Callable[[str], Any]

_PUBL = re.compile(r"\(publ\.?\)", re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(name: str) -> str:
    """Normalisera ett bolagsnamn för jämförelse: gemener, utan "(publ)", "AB" och skiljetecken.

    "holding" behålls medvetet, eftersom det kan vara en del av bolagets riktiga namn.
    """
    text = _PUBL.sub(" ", name or "").lower()
    text = _PUNCT.sub(" ", text).replace("_", " ")
    tokens = [token for token in _SPACES.split(text) if token and token != "ab"]
    return " ".join(tokens)


def _fetch_json(url: str, settings: Settings) -> Any:
    """Hämta och tolka JSON från en URL med User-Agent och timeout från inställningarna."""
    request = Request(url, headers={"User-Agent": settings.user_agent, "Accept": "application/json"})
    timeout = min(float(settings.http_timeout), MAX_TIMEOUT_SECONDS)
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 (värden är fast)
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("MFN-svaret är för stort")
    return json.loads(body.decode("utf-8"))


def candidates(name: str, settings: Settings, *, fetch: Fetch | None = None) -> list[dict[str, Any]]:
    """Sök MFN efter ett bolagsnamn och returnera träffarna som listan från endpointen.

    `fetch` tar en URL och returnerar tolkad JSON; utan den används nätverket.
    Ogiltiga poster (som inte är dict) filtreras bort.
    """
    url = f"{SEARCH_URL}?{urlencode({'limit': SEARCH_LIMIT, 'query': name})}"
    payload = fetch(url) if fetch is not None else _fetch_json(url, settings)
    if not isinstance(payload, list):
        return []
    return [hit for hit in payload if isinstance(hit, dict) and hit.get("slug")]


def _identity(hit: dict[str, Any]) -> str:
    """Entitetens identitet, så att flera slugar för samma bolag inte räknas som en tvetydighet."""
    return str(hit.get("entity_id") or hit.get("slug"))


def _isins(hit: dict[str, Any]) -> set[str]:
    return {str(value).strip().upper() for value in hit.get("isins") or [] if value}


def match(company_row: Any, results: Sequence[dict[str, Any]]) -> tuple[str, str, float] | None:
    """Välj en MFN-slug för ett bolag, eller None om inget entydigt val finns.

    Ordning: exakt ISIN ("isin", poäng 1.0), exakta normaliserade namn ("name_exact", 1.0),
    sedan difflib-likhet >= 0,9 ("name_close"). Om två olika entiteter får samma bästa
    resultat i ett steg väljs ingen, oavsett vilket steg det gäller.
    """
    company_isin = str(company_row["isin"] or "").strip().upper()
    if company_isin:
        isin_hits = [hit for hit in results if company_isin in _isins(hit)]
        picked = _unique_entity(isin_hits)
        if picked is not None:
            return str(picked["slug"]), METHOD_ISIN, 1.0
        if len({_identity(hit) for hit in isin_hits}) > 1:
            return None   # flera bolag med samma ISIN: tvetydigt

    company_name = normalize(str(company_row["name"]))
    if not company_name:
        return None

    exact_hits = [hit for hit in results if normalize(str(hit.get("name") or "")) == company_name]
    picked = _unique_entity(exact_hits)
    if picked is not None:
        return str(picked["slug"]), METHOD_NAME_EXACT, 1.0
    if len({_identity(hit) for hit in exact_hits}) > 1:
        return None

    scored = []
    for hit in results:
        ratio = SequenceMatcher(None, company_name, normalize(str(hit.get("name") or ""))).ratio()
        scored.append((round(ratio, 4), hit))
    if not scored:
        return None
    best_score = max(score for score, _ in scored)
    if best_score < CLOSE_THRESHOLD:
        return None
    best_hits = [hit for score, hit in scored if score == best_score]
    picked = _unique_entity(best_hits)
    if picked is None:
        return None   # oavgjort mellan olika bolag
    return str(picked["slug"]), METHOD_NAME_CLOSE, best_score


def _unique_entity(hits: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    """Returnera träffen om alla träffar gäller exakt en entitet, annars None."""
    hits = list(hits)
    if not hits or len({_identity(hit) for hit in hits}) != 1:
        return None
    # Samma bolag kan ha flera slugar; den första är den primära slugen från MFN.
    return hits[0]


def dry_run(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    fetch: Fetch | None = None,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
    delay: float = DELAY_SECONDS,
) -> list[dict[str, Any]]:
    """Föreslå MFN-slugar för alla bolag där `mfn_slug` saknas. Skriver ingenting i databasen.

    Returnerar en lista med ett förslag per bolag: id, name, isin, slug, method, score,
    candidate_names och error. Bolag utan entydig matchning får `method` None.
    """
    rows = connection.execute(
        "SELECT id, name, isin FROM companies WHERE mfn_slug IS NULL ORDER BY id").fetchall()
    proposals: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if index:
            sleep(delay)
        proposal: dict[str, Any] = {
            "id": row["id"], "name": row["name"], "isin": row["isin"],
            "slug": None, "method": None, "score": None, "candidate_names": [], "error": None,
        }
        try:
            hits = candidates(str(row["name"]), settings, fetch=fetch)
        except (OSError, ValueError) as error:
            proposal["error"] = str(error) or type(error).__name__
            hits = []
        proposal["candidate_names"] = [str(hit.get("name") or "") for hit in hits]
        found = match(row, hits) if hits else None
        if found is not None:
            proposal["slug"], proposal["method"], proposal["score"] = found
        log(f"{row['id']} {row['name']}: {proposal['method'] or 'ingen'} {proposal['slug'] or ''}".rstrip())
        proposals.append(proposal)
    return proposals


def apply(
    connection: sqlite3.Connection,
    proposals: Iterable[dict[str, Any]],
    *,
    methods: Sequence[str] = DEFAULT_APPLY_METHODS,
) -> int:
    """Skriv slugar för förslag med en av `methods`. Skriver aldrig över en befintlig slug.

    Returnerar antalet bolag som faktiskt uppdaterades.
    """
    allowed = set(methods)
    updated = 0
    with connection:
        for proposal in proposals:
            if proposal.get("method") not in allowed or not proposal.get("slug"):
                continue
            cursor = connection.execute(
                "UPDATE companies SET mfn_slug = ? WHERE id = ? AND mfn_slug IS NULL",
                (proposal["slug"], proposal["id"]))
            updated += cursor.rowcount
    return updated


CSV_FIELDS = ("id", "name", "isin", "slug", "method", "score", "candidate_names", "error")


def write_csv(proposals: Iterable[dict[str, Any]], path: Path | str) -> Path:
    """Skriv förslagen till en CSV för manuell granskning. Kandidatnamnen separeras med " | "."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for proposal in proposals:
            row = dict(proposal)
            row["candidate_names"] = " | ".join(proposal.get("candidate_names") or [])
            writer.writerow(row)
    return target
