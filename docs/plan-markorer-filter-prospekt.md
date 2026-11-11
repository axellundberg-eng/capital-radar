# Plan: fler markörer, ett andra filterlager och markörer i prospektpoängen

Status: utredning och plan, inget byggt. Underlag: produktionsdatabasen 2026-11-11 (494 bolag med runway,
varav 193 i målgruppen: under 18 månader, akut, kassa slut eller pågående kapitalanskaffning).

## 1. Fler markörer

I dag finns tre signaler om kommande kapitalbehov (plans.py): stämmobeslut om emission (17 aktiva),
uttalad plan (12) och utvärderar finansiering (18), plus banden pågående kapitalanskaffning och
strukturell förändring. Kandidater, räknade på produktionsdata:

| Markör | Bolag totalt | Varav i målgruppen | Källa (finns redan?) | Bedömning |
|---|---|---|---|---|
| **Kallelse till extra bolagsstämma** med emissionsärende på dagordningen | 86 kallelser senaste 120 dagar | 43 | Nyhetsrubriker finns; dagordningen kräver att kallelsens text hämtas (som för stämmokommunikéer) | **Starkast tidig signal**: kommer före stämmobeslutet. Behöver läsa dagordningen så att bara emission, bemyndigande, minskning av aktiekapital och ändrade aktiekapitalgränser räknas |
| **Väsentlig osäkerhet om fortsatt drift** (going concern) i senaste rapporten | 18 | 15 | Rapporttexten finns redan | Stark: revisor eller styrelse säger att finansiering saknas. Reguljärt uttryck, inga AI-anrop |
| **Lån som förfaller inom 12 månader är större än kassan** | 49 | 35 | Finns (`debt_due_12m`) | Stark: refinansieringsbehov, ofta via emission |
| **Svag senaste emission** (företrädesemission under 80 % tecknad senaste 12 mån) | 14 | 12 | Finns (teckningsgrad i finansieringshändelserna) | Mycket relevant för garanti: svag efterfrågan, garanterna tog en stor del |
| **Teckningsoptioner med lösenperiod inom 90 dagar** | 4 | 3 | Finns (lösenperiod i händelserna) | Relevant: en TO-period är en finansieringshändelse, ofta med garanti |
| **Utestående brygglån** (senaste 120 dagar) | 2 | 2 | Finns | Relevant: brygglån betalas oftast med en kommande emission |
| **Rapport inom 30 dagar** | – | – | Finns (finanskalendern) | Tidsmarkör för när bolaget sannolikt kommunicerar kapitalbehov |
| VD eller CFO lämnar | 10 | 4 | Nyhetsrubriker | Svag men användbar som varning |
| Likviditetsgaranti avslutas | 3 | 2 | Nyhetsrubriker | Svag varning |
| Kostnadsbesparing eller varsel | 4 | 1 | Nyhetsrubriker | Svag; för få träffar för en egen markör |
| Bemyndigande med riktad emission tillåten | 300 | 121 | Finns (authorizations) | För vanligt för att vara en markör. Visas som egenskap (riktad emission möjlig utan stämma) |
| Blankning registrerad | 15 | 8 | Finns | Risk snarare än behov. Visas på Garanti-sidan, inte som markör |
| Insynsköp senaste 90 dagar | 207 bolag med insynshandel | – | Finns | Tvetydig. Visas på Garanti-sidan, inte som markör |

**Förslag:** markörerna delas i två nivåer, så att listan inte drunknar:

- **Kapitalbehov** (driver bannern och poängen): stämmobeslut, kallelse med emissionsärende, uttalad plan,
  going concern, lån förfaller före kassan räcker, utestående brygglån, TO-period inom 90 dagar.
- **Sammanhang** (visas i detaljpanelen, påverkar inte poängen): utvärderar finansiering, svag senaste
  emission, rapport inom 30 dagar, ledningsbyte, likviditetsgaranti avslutad.

Teknik: utöka `plans.py` med `going_concern` och `egm_called` (reguljära uttryck; kallelsens text hämtas
en gång och sparas, som i authorizations.py). De övriga markörerna räknas fram i webblagret ur data som
redan finns. Ett gemensamt fält `markers` per bolag: nyckel, etikett, detalj, datum och källänk.

## 2. Andra filterlagret: undergrupper

Avsikten som jag förstår den: banden (runway) är första lagret. Under dem ska man kunna välja
**undergrupper** och kombinera dem, till exempel "medtech" och "stämmobeslut", eller "nämnt i
Q-rapporten att man ska ta in pengar", och få fram just de bolagen.

### Bransch: data finns delvis

- **ICB-bransch från Nasdaq finns i den gamla databasen** för 427 av 517 bolag (Health Care 97,
  Technology 86, Industrials 74 …). Det saknas för de 89 bolagen på NGM.
- ICB skiljer inte medtech från bioteknik eller läkemedel, och inte mjukvara från hårdvara. Det behövs
  för undergrupper som "medtech".
- **Förslag:** en engångsklassning med DeepSeek av varje bolags verksamhet ur första sidorna i senaste
  rapporten, till en fast lista:
  - Life science: medtech, bioteknik/läkemedel, diagnostik.
  - Tech: mjukvara/SaaS, hårdvara/elektronik, gaming, fintech, övrig tech.
  - Övrigt: cleantech/energi, gruv/råvaror, industri, konsument, fastigheter, finans.

  Den täcker även NGM. Cirka 500 anrop med korta texter, sedan bara nya bolag. Resultatet sparas i en
  egen tabell (`company_sectors`: ICB, underbransch, källa, datum) och kan rättas för hand med
  `correct`-kommandot.

### Gränssnitt

- En rad med filterknappar under de godkända bandrutorna (banden ändras inte), i två grupper:
  - **Bransch**: underbranscherna ovan.
  - **Markörer**: kapitalbehovsmarkörerna i punkt 1.
- Flerval. **ELLER** inom en grupp (medtech eller diagnostik), **OCH** mellan grupper (medtech och
  stämmobeslut). Antal visas per knapp, för valt band.
- Gäller både Lista och Prospekt. Valet sparas i adressen (`#lista?bransch=medtech&markor=stamma`) så
  att en filtrerad vy kan delas och bokmärkas.
- På Prospekt visas dessutom en egen sektion överst: **"Uttalat kapitalbehov"** med bolag som har en
  kapitalbehovsmarkör, före den vanliga listan. Pågående och strukturell förändring ligger kvar längst ned
  som i dag.

## 3. Markörerna i prospektpoängen

I dag (qualify.py, 100 poäng): runway 20, ingen annonserad emission 15, storlek 15, andel av
marknadsvärdet 25, emissionshistorik 10, kvalitet 5, sektor 10. Uttalat kapitalbehov påverkar inte.

**Förslag:** en ny kontroll **"Uttalat kapitalbehov"** på 15 poäng:

- Full poäng: stämmobeslut, kallelse med emissionsärende, uttalad plan eller going concern.
- Halv poäng: lån förfaller före kassan räcker, brygglån eller TO-period inom 90 dagar.
- Flera markörer ger inte mer än 15.

Vikterna justeras så att summan blir 100: **ingen annonserad emission 15 → 10** (nästan alla prospekt
klarar den, så den skiljer dåligt) och **andel av marknadsvärdet 25 → 20**. Allt annat är oförändrat.

Två alternativ till ranking, att välja mellan:

- **A. Poäng (rekommenderas):** markören väger tungt, men ett bolag med uttalat behov och dåliga övriga
  villkor (för stort behov, för liten börsvärde) hamnar inte automatiskt överst.
- **B. Hård nivå:** alla bolag med en kapitalbehovsmarkör sorteras före alla utan, och poängen sorterar
  inom varje nivå. Enklare att förstå, men ett dåligt prospekt med en markör går före ett bra utan.

Med sektionen "Uttalat kapitalbehov" i punkt 2 får man i praktiken B visuellt och A i poängen.

## Ordning och storlek

| Steg | Innehåll | Storlek |
|---|---|---|
| 1 | Markörerna going concern, lån förfaller, svag emission, TO-period, brygglån, rapport inom 30 dagar (data finns) | Liten |
| 2 | Kallelse till extra stämma med dagordning (hämta text en gång, reguljära uttryck) | Mellan |
| 3 | Poängkontroll "Uttalat kapitalbehov" och nya vikter | Liten |
| 4 | Branschklassning: ICB från gamla databasen och DeepSeek-klassning till underbransch | Mellan |
| 5 | Filterknappar (bransch och markörer), sektionen "Uttalat kapitalbehov" och delbar adress | Mellan |

Steg 1–3 ger effekt på prospektsidan direkt. Steg 4 måste göras före branschfiltret i steg 5.

## Beslut som behövs

1. ~~Ranking~~ **Beslutat 2026-11-11: båda.** Poängkontrollen "Uttalat kapitalbehov" (A) och sektionen
   "Uttalat kapitalbehov" överst på prospektsidan (B).
2. Underbranscherna: räcker listan ovan, eller saknas någon?
3. Ska "utvärderar finansiering" räknas som kapitalbehov (poäng) eller bara som sammanhang?
