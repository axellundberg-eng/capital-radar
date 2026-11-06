# Granskning 2026-11-06: baslinje, åtgärder och jämförelse

Underlag: produktionsdatabasen på servern (468 bolag med runway, 474 bolag med rapporter).
Kontrollerna finns i `src/radar/review.py` och körs utan AI. Ett urval av flaggorna har verifierats
mot den lagrade rapporttexten (`report_pages`) och MFN.

## 1. Baslinje före åtgärder

Första körningen gav 574 flaggor hos 293 bolag:

| Kontroll | Antal | Bedömning efter verifiering |
|---|---|---|
| duplicate_event | 241 | Riktigt fel i datan: samma pressmeddelande på svenska och engelska ger två händelser |
| flow_column | 85 | Falsklarm: helårs- och niomånaderssiffror står legitimt i senare kolumner |
| missing_runway | 62 | Känd lucka: balansräkningen saknas eller rapporten är kort |
| exhausted_unexplained | 33 | Behöver granskas: kassan borde vara slut men ingen finansiering hittad |
| positive_but_burning | 31 | Riktigt metodfel för förlustbolag (se AcouSort nedan) |
| stale_report | 30 | Riktigt fel: nyare rapporter har klassats som nyheter och aldrig lästs |
| burn_jump | 28 | Behöver granskas |
| runway_extreme | 24 | Mest lönsamma bolag med liten burn; låg prioritet |
| unit_suspect | 19 | Falsklarm i verifierade fall: Fluicell (30 KSEK) och Avsalt (12 KSEK) har så lite kassa |
| currency_mix | 12 | Riktigt fel: finansiering i annan valuta än rapporten räknas inte alls |
| cash_column | 8 | Falsklarm: notnummer före beloppet ("Kassa och bank 11 20 630 ...") |
| odd_period | 1 | Behöver granskas |

### Verifierade fel

1. **Kvartalsrapporter klassas som nyheter (störst påverkan).** Rubriker i nyhetsform som
   "Agtiras starkaste kvartal – ...", "Alligator Bioscience AB reports financial results for the
   period 1 January – 30 September 2026" och "Adverty når positiv EBITDA i Q1" känns inte igen som
   rapporter. De sparas som nyheter och extraheras aldrig. Runway bygger då på 7–10 månader gamla
   siffror. Bilagornas filnamn visar däremot tydligt att det är rapporter
   (`agtira-kvartalsrapport-q2-2026.pdf`, `2026-q3-en-vf.pdf`, `adverty-q1-2026-sve.pdf`).
   Verifierat för Agtira, Alligator Bioscience och Adverty.
2. **Kassaflödespositiv trots burn.** AcouSort: första halvåret +3 965 KSEK i löpande kassaflöde,
   andra kvartalet −4 642 KSEK. Runway använder den längsta perioden och markerar bolaget som
   kassaflödespositivt. För ett förlustbolag är det fel; för lönsamma bolag med säsongsvariation
   (konsultbolag) kan det vara rätt. Kräver bedömning per bolag.
3. **Dubbla händelser från tvåspråkiga pressmeddelanden.** Exempel: Aira (AI Revenue Assistant
   Software), riktad emission 25 MSEK den 2026-10-20 både från den svenska och den engelska versionen.
   Runway-beräkningen slår redan ihop dem, men listan i prospektvyn visar båda.
4. **Finansiering i annan valuta ignoreras.** Exempel: GiG Software rapporterar i EUR, men en riktad
   emission i SEK räknas inte med. Arctic Blue Beverages har lån i EUR mot en rapport i SEK.

### Lucka i källan, inte i koden

Bambuser har inga kvartalsrapporter för 2026 på sin MFN-sida. Det kan inte rättas genom insamlingen.

## 2. Åtgärder

(fylls i efter bygget)

## 3. Jämförelse efter en granskningsrunda

(fylls i efter körningen)
