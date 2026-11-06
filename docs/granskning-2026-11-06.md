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

Byggt och driftsatt i `f92a81c`:

1. **Rapporter känns igen på bilagans filnamn.** `classify` läser nu bilagornas filnamn
   (`kvartalsrapport`, `q2-2026`, `2026-q3`, `interim-report`, `arsredovisning` m.fl.). En rapport som
   tidigare sparats som nyhet läggs till som rapport vid nästa insamling. Stämmor och inbjudningar blir
   aldrig rapporter.
2. **Rättelser som överlever omextraktion.** Tabellen `corrections` och kommandona
   `radar correct fact|period|event` och `radar corrections list|revert`. Rättelserna läggs ovanpå
   extraherade fakta och händelser vid varje omräkning, och varje rättelse kräver en motivering.
3. **Granskningskö och kontroller.** `radar review checks` och `radar review queue` med kontrollerna i
   `src/radar/review.py`. Falsklarmen från baslinjen är åtgärdade: notnummer före beloppet räknas inte
   som kolumn, kassaflöden för hela året i senare kolumner flaggas inte, och enhet och dubbletter har
   lägre allvar.
4. **Dubbletter i prospektlistan.** Den svenska och den engelska versionen av samma händelse visas en gång.
5. **Instruktion för måndagssessionen** i `agents/radar-reviewer.md`.

Med de förbättrade kontrollerna gav samma data **488 flaggor hos 243 bolag** (tidigare 574 hos 293).
Skillnaden är falsklarm som tagits bort, inte rättade fel. Det är den siffran jämförelsen nedan utgår från.

## 3. Jämförelse efter en granskningsrunda

Rundan bestod av ny insamling och extraktion med den nya klassningen, plus två manuella rättelser.
Före och efter mäts med samma kontroller.

| | Före | Efter | Förändring |
|---|---|---|---|
| Flaggor totalt | 488 | 473 | −15 |
| Bolag med flaggor | 243 | 234 | −9 |
| stale_report (gammal rapport) | 30 | 14 | −16 (18 lösta, 2 nya) |
| missing_runway | 62 | 61 | −1 |
| unit_suspect | 19 | 17 | −2 |
| positive_but_burning | 31 | 30 | −1 (2 rättade, 1 ny) |
| cash_column | 7 | 6 | −1 |
| duplicate_event | 241 | 244 | +3 (nya pressmeddelanden; slås ihop i beräkningen) |
| runway_extreme | 24 | 26 | +2 (nya rapporter från lönsamma bolag) |

### Effekt på bolagsnivå

- **AcouSort:** från "kassaflödespositiv" till 1,8 månaders runway (rättelse: EU-finansiering i Q1).
- **Bioextrax:** från "kassaflödespositiv" till 1,1 månaders runway (rättelse: licensförskott i Q1).
- **Alligator Bioscience:** runway byggde på årsredovisningen 2025; nu Q3 2026 med 4,0 månader.
- **Agtira** och **Adverty:** nu med rapporten för andra kvartalet 2026 i stället för bokslutet 2025.

Två bolag som var dolda som kassaflödespositiva och ett med inaktuella siffror ligger nu i
målgruppen för garantimandat.

### Kvar till nästa runda

- 14 bolag med gammal rapport, bland dem Bambuser (inga 2026-rapporter på MFN), Bulten och Irisity.
- 33 bolag där kassan borde vara slut utan att finansiering hittats, varav tre nya (Irisity, Keo
  Capital, OODA AI).
- 30 kassaflödespositiva bolag med burn i senaste kvartalet: de flesta är lönsamma säsongsbolag; de
  förlustbolag som återstår behöver bedömas ett och ett.
- 12 händelser i annan valuta: kräver valutaomräkning i koden (se `docs/granskning-finansmodell.md`).
- De 6 kvarvarande kassaflaggorna ser ut att vara falsklarm (notnummer eller datumfragment som "202"
  ur "2026"); talavläsningen i kontrollen bör justeras.

Rundan är loggad i `review_runs` på servern.
