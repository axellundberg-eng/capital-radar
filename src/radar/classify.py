"""Rubrik (och MFN:s kategori) -> report, financing, calendar eller other.

Regelbaserat och medvetet litet. Ordningen är viktig:
1. calendar: inbjudningar, webbsändningar, publiceringsdatum och finanskalender
   (även "X publicerar delårsrapport den 25 oktober" är bara ett datumbesked).
2. report: själva rapporten (delårs-, halvårs-, bokslut, årsredovisning), men
   inte stämmor, även om årsredovisningen nämns där.
3. financing: emissioner, lån, konvertibler, kreditfaciliteter m.m.
4. other: allt annat.
"""

from __future__ import annotations

import re

CALENDAR = re.compile(
    r"inbjudan|invitation|invites?\b|bjuder in|webcast|webinar|audiocast|telefonkonferens|conference call"
    r"|earnings call|financial calendar|finansiell kalender|finanskalender|publiceringsdatum|publication date"
    r"|reporting date|rapportdatum|datum för (?:publicering|offentliggörande)|date for (?:publication|release)"
    r"|ändrat? (?:datum|publicering)|ändrar (?:datum|publicering)|(?:change|changes|changed) .{0,30}date"
    r"|flyttar fram|senarelägg|tidigarelägg|postpone|reschedul|presentation (?:av|of) .{0,30}(?:rapport|report)"
    r"|(?:kommer att|ska|avser att|will|to) (?:publicera|offentliggöra|lämna|publish|release|present|announce)\b"
    r".{0,60}(?:rapport|report)"
    r"|(?:publicerar|offentliggör|publishes|publish|releases)\b.{0,60}\b(?:den|on)\s+\d{1,2}\b",
    re.I)
MEETING = re.compile(r"stämma|general meeting|\bagm\b|\begm\b|notice of", re.I)
REPORT = re.compile(
    r"delårsrapport|halvårsrapport|kvartalsrapport|interimsrapport|bokslutskommunik[eé]|årsredovisning"
    r"|\bbokslut\s+20\d\d|\binterim (?:financial )?report|\bhalf[- ]?year(?:ly)? (?:financial )?report"
    r"|\b(?:full|half)[- ]year (?:report|results)|\byear[- ]end report|\bquarterly report|\bannual report"
    r"|\bannual and sustainability report|\bq[1-4]\s*20\d\d\b"
    r"|\bq[1-4]\s*(?:interim |quarterly )?(?:report|rapport|results)\b"
    r"|\b(?:first|second|third|fourth)\s+quarter\s+(?:20\d\d\s+)?(?:report|results)\b"
    r"|report for the (?:first|second|third|fourth) quarter"
    r"|rapporten? (?:för|från) (?:det )?(?:första|andra|tredje|fjärde) kvartalet"
    # Rubriker i nyhetsform: "Rapport tredje kvartalet 2026 - Hedera Group", "Tillväxt i kärnaffären i det
    # andra kvartalet 2026", "Q2: Positiv EBITDA." och stavfelet "Kvartrapport Q1".
    r"|\brapport (?:för )?(?:det )?(?:första|andra|tredje|fjärde) kvartalet"
    r"|\b(?:i|under) det (?:första|andra|tredje|fjärde) kvartalet 20\d\d|^\s*q[1-4]\s*:|kvartrapport",
    re.I)
FINANCING = re.compile(
    r"nyemission|företrädesemission|emission\b|emissionen|private placement|rights issue|(?:directed )?share issue"
    r"|brygglån|konvertibel|convertible|teckningsoption|teckningsrätt|warrants?\b|kreditfacilitet|credit facilit"
    r"|låneavtal|loan agreement|\blån(?:et|en|er|ens)?\b|\bloans?\b|finansiering|financing|kapitalanskaffning"
    r"|capital raise|revolving|kreditram|garantiåtagande|underwriting",
    re.I)


# Rapporter med rubrik i nyhetsform ("Agtiras starkaste kvartal ...") känns igen på bilagans filnamn,
# t.ex. agtira-kvartalsrapport-q2-2026.pdf, 2026-q3-en-vf.pdf eller adverty-q1-2026-sve.pdf.
REPORT_FILE = re.compile(
    r"kvartalsrapport|delarsrapport|delårsrapport|halvarsrapport|halvårsrapport|interim[-_ ]?report"
    r"|quarterly[-_ ]?report|half[-_ ]?year[-_ ]?report|bokslutskommunik|year[-_ ]?end[-_ ]?report"
    r"|arsredovisning|årsredovisning|annual[-_ ]?report"
    r"|(?<![a-z0-9])q[1-4][-_ ]?20\d\d(?![0-9])|(?<![0-9])20\d\d[-_ ]?q[1-4](?![0-9a-z])"
    r"|(?<![a-z0-9])q[1-4][-_ ]?(?:rapport|report)|(?:rapport|report)[-_ ]?q[1-4](?![0-9a-z])",   # bulten-q3-rapport-2026.pdf
    re.I)


def _report_attachment(attachments) -> bool:
    return any(REPORT_FILE.search(url.rsplit("/", 1)[-1]) for url in attachments or ())


def classify(title: str, category: str | None = None, attachments=()) -> str:
    """Returnerar report, financing, calendar eller other.

    `attachments` är bilagornas adresser; ett rapportlikt filnamn räcker för report när rubriken
    inte säger det (men aldrig för kallelser och stämmokommunikéer)."""
    text = f"{title} {category or ''}"
    if CALENDAR.search(text):
        return "calendar"
    if (REPORT.search(text) or _report_attachment(attachments)) and not MEETING.search(text):
        return "report"
    if FINANCING.search(text):
        return "financing"
    return "other"
