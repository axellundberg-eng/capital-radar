"""Regler för den nya koden (se AGENTS.md).

1. Ingen import från den gamla koden (`capital_radar`, `legacy`).
2. Från den gamla koden får bara API-inloggningarna i
   `radar.credentials.SHARED_CREDENTIALS` användas. Gamla
   inställningsnamn (SCREENER_*, DEEPSEEK_*, CAPITAL_RADAR_*, LCS_*) är
   förbjudna i övrigt; ny kod använder RADAR_*.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from radar.credentials import SHARED_CREDENTIALS

SRC = Path(__file__).resolve().parents[1] / "src" / "radar"
FORBIDDEN_MODULES = ("capital_radar", "legacy", "screener")
OLD_SETTING = re.compile(r"^(SCREENER|DEEPSEEK|CAPITAL_RADAR|LCS|ANTHROPIC|REVIEWER)_[A-Z0-9_]+$")


def _sources():
    files = sorted(SRC.rglob("*.py"))
    assert files, "inga källfiler hittades"
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in files]


def test_new_code_never_imports_old_code():
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            offenders += [f"{path.name}: {name}" for name in names
                          if name.split(".")[0] in FORBIDDEN_MODULES]
    assert not offenders, offenders


def test_only_api_logins_are_shared_with_old_code():
    offenders = []
    for path, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if OLD_SETTING.match(node.value) and node.value not in SHARED_CREDENTIALS:
                    offenders.append(f"{path.name}:{node.lineno}: {node.value}")
    assert not offenders, offenders


def test_credentials_repr_hides_values(monkeypatch):
    from radar.credentials import Credentials

    monkeypatch.setenv("DEEPSEEK_API_KEY", "hemlig-nyckel")
    credentials = Credentials.from_env()
    assert credentials.deepseek_api_key == "hemlig-nyckel"
    assert "hemlig-nyckel" not in repr(credentials)
