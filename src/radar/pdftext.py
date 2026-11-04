"""Text per sida ur en PDF."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader


def pages(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    return [(page.extract_text() or "").strip() for page in reader.pages]
