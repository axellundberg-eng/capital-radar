# Granskning 2026-11-11: felrapport

Underlag: produktionsdatabasen (version ce1b6fa). Kontrollerna i `src/radar/review.py` körs utan AI
(`python -m radar review checks`). Alla fel nedan är verifierade mot lagrad rapporttext
(`report_pages`) eller nyhetsrubriker. Inget är rättat än; åtgärdsförslagen står sist.

## Sammanfattning

545 flaggor hos 238 bolag (förra granskningen: 553). Efter verifiering:

| Kontroll | Antal | Bedömning |
|---|---|---|
| duplicate_event | 319 | Kosmetiskt: svensk och engelsk version slås redan ihop i beräkningen |
| missing_runway | 61 | Känd lucka: balansräkning saknas eller rapporten är kort |
| burn_jump | 40 | Mest metod: säsong och engångsposter. Gold Town Games är ett riktigt problem (fel 4) |
| runway_extreme | 32 | Mest lönsamma bolag. Riktigt fel hos Summa Defence (fel 2) |
| positive_but_burning | 28 | Lönsamma bolag med ett svagt kvartal; rullande tolv månader hanterar det |
| exhausted_unexplained | 21 | Mest riktiga. Riktiga fel hos H100, K33 (fel 3) och Eurobattery (fel 5) |
| stale_report | 15 | **Riktigt systemfel** (fel 1) |
| unit_suspect | 12 | Riktiga fel hos Summa Defence och Genovis (fel 2); övriga har så lite kassa |
| currency_mix | 11 | **Falsklarm**: händelserna räknas redan om till rapportvalutan (fel 6) |
| cash_column | 4 | Falsklarm: Ferronordic, NextCell, Ranplan, Seafire |
| flow_same_value | 1 | Falsklarm: MSAB hade 3,1 MSEK båda åren |
| odd_period | 1 | Falsklarm: Aira bildades 2026-02-23 |

## Verifierade fel, efter påverkan

### 1. Nyare rapporter läses inte (14 bolag, störst påverkan)

Runway bygger på 7–13 månader gamla siffror trots att nyare rapporter finns. Fyra orsaker:

**a) Rapporter sparade som nyheter med rubriker i nyhetsform.** Klassningen på bilagans filnamn
gäller bara nya poster, inte de som redan låg som nyheter.

| Bolag | Rapport som ligger som nyhet |
|---|---|
| Balco Group | Q3 2026-10-26 "Fortsatt fokus på högre marginaler, ökad effektivitet …" |
| Bulten | Q3 2026-10-21 "En starkare verksamhet, redo för nästa kapitel" |
| Eniro Group | Q3 2026-11-05 "Första gången på mer än ett decennium - organisk tillväxt!" |
| Hedera Group | Q1, Q2 och Q3 2026 "Rapport tredje kvartalet 2026 - Hedera Group" |
| Promimic | Q1 och Q2 2026 "Tillväxt i kärnaffären i det andra kvartalet 2026" |
| Irisity | Q2 2026-08-27 "Q2: Positiv EBITDA." |
| GRANGEX | Q1 2026 "Kvartrapport Q1" (stavfel i rubriken) |

**b) Riktiga rapporter har status `skipped`.** New Nordic Healthbrands (Q2 och Q3 2026),
Spotr Group (Q2 2026) och MTI Investment (halvår 2026). Orsaken är inte utredd.

**c) Rapporter som aldrig lästs (`new`) eller misslyckats (`failed`).** Heimstaden (Q2 och Q3 2026),
Nicoccino (Q2 2026; Q3 publicerad 2026-11-10) och PION Group (halvår `new`, Q3 `failed`).

**d) Kända sedan tidigare.** Safe Lane Gaming (inskannad rapport, kräver OCR) och Thinc.

### 2. Enhetsfel: tusental lagrade som enheter

| Bolag | Lagrat | Rätt | Följd |
|---|---|---|---|
| Summa Defence Oyj | kassa 2 855 EUR, burn 1 878 EUR/mån | 2,86 MEUR och 1,88 MEUR/mån | Runway visas som 4 575 mån; rätt är cirka 1,5 mån vid rapporten (juni) |
| Genovis | kassa 163 012 SEK | 163 MSEK | Lönsamt bolag, runway påverkas inte, men kassa och kapitalbelopp blir fel |

Rapporternas tabeller anger "EUR thousand" respektive "SEK thousand". Rättas med
`correct scale --factor 1000`.

### 3. Bitcoinköp räknas som burn (H100 Group, K33)

Bitcoin bokförs som immateriell eller anläggningstillgång, så köpen hamnar i investeringarna och
räknas som rörelseinvesteringar:

- **H100:** investeringar 2025 på −1 190,9 MSEK (bitcoin) ger en burn på 98 MSEK/mån och bandet
  "Kassa slut". Utan bitcoinköpen var det löpande kassaflödet positivt på tolv månader. Första
  halvåret 2026 brände bolaget 2,1 MSEK/mån, vilket med kassan 18,1 MSEK ger cirka 4 månader idag.
- **K33:** "Acquisition of fixed assets" −153 MSEK 2025 ger en burn på 15,1 MSEK/mån. Utan köpen
  är den 2,2 MSEK/mån (första halvåret 2026). Kassan 3,3 MSEK räcker ändå inte, så bandet blir
  troligen detsamma, men burn är femfaldigt överskattad.

Båda äger bitcoin som kan säljas, vilket runway inte tar hänsyn till.

### 4. Gold Town Games: rullande tolv månader ger missvisande runway

Siffrorna är rätt lästa, men metoden slår fel. Runway idag visas som **1,2 mån**:

- Andra halvåret 2025 förbrukades ett förskott: löpande kassaflöde −7,5 MSEK på sex månader.
- Första halvåret 2026: löpande +1,56 MSEK, investeringar −1,94 MSEK, alltså cirka 65 000 kr/mån.
- Andra kvartalet 2026: cirka 109 000 kr/mån.
- Med kassan 0,98 MSEK den 30 juni ger kvartalstakten **cirka 4,5 mån idag** och halvårstakten
  cirka 10 mån.

Q3-rapporten väntas omkring 2026-11-20 (Q3 2025 publicerades 2025-11-20).

### 5. Avnotering och brottsutredning syns inte (Eurobattery Minerals) — *felbedömt, se Utfall*

Bolaget ligger i "Kassa slut" utan varningar, trots följande:

- Styrelseordföranden avgick 2026-09-21.
- Revisorn avgick 2026-10-15.
- NGM avser att begära avnotering (2026-10-20).
- VD och en styrelseledamot är häktade (2026-11-07).

Alla 32 nyheter efter rapporten har status `new`. AI-granskningen tar bara nyheter vars rubrik ser
ut som finansiering, så signalerna avnotering och strukturell förändring missas helt för sådana
rubriker.

### 6. Valutakontrollen är föråldrad (11 falsklarm)

`currency_mix` flaggar händelser i annan valuta än rapporten, men sedan valutaomräkningen byggdes
räknas de om till rapportvalutan (Sivers USD-konvertibel, Hexicon och Arctic Blue i EUR). Kontrollen
bör bara flagga händelser som saknar kurs (`unconverted`).

### 7. Orimliga runway-tal för bolag nära noll i burn

Trainimal (1 225 mån) och G5 Entertainment (1 339 mån) bränner nästan inget. Det är rätt räknat,
men talen ser ut som fel i listan. Visa "över 60 mån" i stället.

## Verifierade falsklarm

- **cash_column:** Ferronordic (110 MSEK enligt texten), NextCell (21,1 MSEK) och Seafire (25 MSEK)
  har rätt kassa; kontrollen jämförde mot en annan tabell. Ranplans balansräkning har äldsta datum
  först (30 jun 2025, 31 dec 2025, 30 jun 2026), så tredje kolumnen är rätt.
- **Ranplan, runway 40,5 mån:** kommer från en outnyttjad kredit på 37,9 MSEK ("leaving a balance of
  SEK 37.9 million unutilised"). Det stämmer med rapporten.
- **MSAB:** löpande kassaflöde 3,1 MSEK i Q3 både 2026 och 2025.
- **Aira:** första perioden är 7 månader eftersom bolaget bildades 2026-02-23.

## Åtgärdsförslag i prioritetsordning

| # | Åtgärd | Löser | Insats |
|---|---|---|---|
| 1 | Klassa om befintliga nyheter som är rapporter (bilagans filnamn), läs om | Fel 1a: 7 bolag | Kod + omläsning |
| 2 | Utred `skipped` och `new`/`failed` och läs om rapporterna | Fel 1b–c: 6 bolag | Utredning + omläsning |
| 3 | `correct scale` för Summa Defence och Genovis | Fel 2 | Två kommandon |
| 4 | Rätta investeringarna för H100 och K33; regel för bitcoinbolag | Fel 3 | Rättelser + liten kod |
| 5 | Lägg avnotering, konkurs, rekonstruktion, häktning och revisor avgår i nyhetsfiltret | Fel 5 | Liten kod |
| 6 | Visa runway i senaste takt när den skiljer mycket från rullande tolv månader | Fel 4 | Liten kod |
| 7 | Uppdatera valutakontrollen; visa "över 60 mån" | Fel 6–7 | Liten kod |

## Utfall efter åtgärder (samma dag)

Version c530fd0. Kontrollerna efter åtgärderna: **519 flaggor hos 222 bolag** (före: 545 hos 238).
Gamla rapporter 15 → 4, valutalarm 11 → 0, "kassa slut utan förklaring" 21 → 17, enhetslarm 12 → 10.

| # | Åtgärd | Resultat |
|---|---|---|
| 1 | Rubrikmönster, PDF-prov för bolag med gammal rapport, alla bilagor sparas och provas | Nyare rapporter lästa för Balco, Bulten, Eniro, Hedera (Q3), New Nordic, PION (Q3), Promimic, Irisity, GRANGEX, Spotr och MTI (Q2/halvår) |
| 2 | `storage.googleapis.com` tillåten; överhoppade rapporter med fler PDF:er provas igen | PION Q3 läst; New Nordic och MTI läst ur rätt bilaga |
| 3 | `correct scale` ×1000 | Summa Defence: kassa 2,86 MEUR, runway 1,7 mån (var 4 575). Genovis: kassa 163 MSEK |
| 4 | Bitcoinköp satta till 0 i investeringarna | H100: burn 2,1 MSEK/mån, runway 4,1 mån (var "kassa slut"). K33: burn 4,9 MSEK/mån (var 15,1), fortfarande kassa slut |
| 5 | Felbedömt i rapporten: Eurobatterys avnotering var redan registrerad och bolaget ligger under "Strukturell förändring". Kontrollen `exhausted_unexplained` hoppar nu över bolag med strukturflaggor | Falsklarmet borta |
| 6 | Regel `ltm_distorted`: tolv månader mer än tre gånger högre burn än både årets snitt och senaste kvartalet | Huvudtalet är kvar på snittet (försiktigt); runway i senaste takt visas bredvid, även för akut och kassa slut. 7 bolag, bl.a. Gold Town Games: 1,2 mån i snitt, 37 mån i senaste takt (inklusive 3,5 MSEK som tagits in efter rapporten) |
| 7 | Valutakontrollen flaggar bara händelser utan växelkurs; runway över 60 mån visas som "över 60 mån" | 11 falsklarm borta |

**Kvar:** Heimstaden och Nicoccino publicerar rapporter utan PDF-bilaga på MFN; Safe Lane Gaming kräver
OCR; Thinc är känd sedan tidigare. Häktning och revisorsavgång fångas inte som egna händelser.
