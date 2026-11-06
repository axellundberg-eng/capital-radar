# ADR: Haiku-granskare som hittar och rättar fel

**Status:** Accepted i ändrad form (se "Beslut 2026-11-06" nedan). Resten av dokumentet beskriver
den automatiska varianten, som är parkerad.
**Datum:** 2026-11-06
**Beslutsfattare:** Axel Lundberg

## Beslut 2026-11-06: veckovis granskning i en chattsession

Granskningen görs tills vidare av en Claude-chattsession som startas manuellt varje måndag. Ingen
schemaläggare, ingen ny API-nyckel och inget nytt löpande jobb på servern. Sessionen når servern via
SSH som i dag och arbetar genom kommandon i `radar`-CLI:t, inte genom att skriva direkt i databasen.

**Det som behöver byggas:**

1. **Granskningskö** `python -m radar review queue --since <datum>`: en kompakt JSON per bolag som
   fått ny rapport, nya nyheter eller nytt band sedan förra granskningen. Innehåller runway-resultatet,
   fakta med sida och citat, finansieringshändelser och flaggor, så att sessionen inte behöver läsa
   hela rapporter.
2. **Automatiska rimlighetskontroller** i kön (utan AI): kassa slut utan finansieringsnyhet, burn som
   skiljer mycket mellan perioder, periodlängd som inte stämmer, dubbla händelser, band som inte matchar
   flaggorna. Kontrollerna sorterar kön så att de troliga felen granskas först.
3. **Rättelser som överlever omextraktion**: tabellen `corrections` (gammalt värde, nytt värde,
   motivering, tid, `reverted`) och kommandona `radar correct fact|event ... --reason "..."`,
   `radar corrections list` och `radar corrections revert <id>`. Runway-beräkningen lägger rättelserna
   ovanpå extraherade fakta, så att en ny extraktion inte skriver över dem.
4. **Omtolkning på begäran**: `radar run --document <id> --force` för en rapport och
   `radar news --document <id> --force` för ett pressmeddelande, följt av omräkning av runway för
   bolaget.
5. **Granskningslogg** `review_runs` (datum, antal granskade bolag, antal rättelser), så att nästa
   måndag vet var förra granskningen slutade.
6. **Instruktion för sessionen** `agents/radar-reviewer.md`: arbetsordning, vilka fält som får ändras,
   att varje rättelse kräver motivering, att 95 %-regeln gäller och att läsningen per bolag delegeras
   till Haiku-subagenter för att spara plangränsen.

**Konsekvenser:** granskningen kostar inga API-pengar men tar av Pro-planens gränser. Den blir av bara
om någon startar den. Den automatiska varianten nedan kan byggas senare ovanpå samma CLI-kommandon.

## Context

Radar-kärnan extraherar siffror (kassa, burn, periodslut, finansieringshändelser) med DeepSeek och
räknar runway deterministiskt. Fel har hittats för hand: Hemply (balansräkning fel klassad), 4C Group
(runway), Xspray (amortering efter perioden), OXE Marine (0 i runway). Varje sådant fel har kostat
en manuell genomgång. Målet är att en granskare hittar och rättar fel automatiskt. Att varje
uppgift ska vara källbunden är inte ett krav. Det viktiga är att felen hittas och rättas och att
datan hänger ihop internt.

Tidigare plan (memory: `capital-radar-migration`) var en granskare som bara flaggar. Det ändras här.

## Decision

Lägg till en Haiku-agent (`src/radar/review.py`) som körs i schemaläggaren efter nyhetssteget, och
som får **skrivrätt** på en avgränsad uppsättning fält. Varje ändring loggas i en ny tabell
`corrections` med gammalt värde, nytt värde, motivering, modell, tidpunkt och ett `reverted`-fält.

**Vad agenten får ändra:**
- Runway-indata: kassa, burn, periodslut, månader i perioden, investeringar.
- Finansieringshändelser: typ, status, belopp, datum, rådgivare.
- Strukturella flaggor när texten tydligt visar att de saknas eller är fel.

**Vad agenten inte får göra:**
- Ta bort bolag eller händelser. Den kan bara markera en händelse som felaktig, och då döljs den
  men raderas inte.
- Ändra bolagets namn, identitet, marknadsvärde eller prospektpoäng. Dessa räknas om automatiskt.
- Göra fler ändringar per körning än budgeten tillåter (förslag: högst 20 per cykel).

**Kontext till agenten:** lagrade rapportsidor (`report_pages`), nyhetstexter (`news.text`) och det
aktuella runway-resultatet. Det är redan i databasen, så ingen extern källa behövs för att
kontrollera att siffrorna hänger ihop. Agenten behöver inte peka ut en källa för varje ändring,
men den ska ange motiveringen i klartext.

**Skyddsräcken:**
1. **Ångra:** varje ändring kan återställas med `python -m radar corrections revert <id>`.
2. **Kill-switch:** `RADAR_REVIEW_ENABLED=0` stänger av agenten utan omdeploy.
3. **Regressionsset:** de kända fallen (Hemply, 4C, Xspray, OXE) körs före varje ändring av
   prompten. Om agenten rättar fel där det redan är rätt, eller missar de kända felen, blockeras
   driftsättningen.
4. **Dagligt underlag:** en lista över alla ändringar senaste dygnet, så att de kan granskas.

## Modellkanal

Granskaren ska inte dela API-kanal med den befintliga DeepSeek-extraktionen, så att de inte
konkurrerar om gränser och kvot. Tre sätt övervägdes:

| Kanal | Hur den körs | Kostnad och gränser | Passar för |
|---|---|---|---|
| 1. Claude-session (chatt) | Startas manuellt eller schemalagt som session | Räknas mot Pro-planen och 95 %-regeln i AGENTS.md. Körs inte när datorn är avstängd | Engångsgranskning, inte löpande drift |
| 2. Haiku via egen Anthropic-nyckel (valt) | Ny klient i schemaläggaren, egen nyckel | Egen API-fakturering, egna gränser | Löpande granskning |
| 3. DeepSeek via egen nyckel eller konto | Samma klient som i dag, men ett nytt konto | Billigast per token, men samma leverantör som extraktionen | Reserv om Haiku inte är tillgänglig |

**Beslut:** Haiku via en egen Anthropic-nyckel för löpande drift. DeepSeek via separat konto som
reserv, om Anthropic-nyckeln saknas eller ger fel. Chatten (kanal 1) används bara för engångsbedömningar,
inte i schemaläggaren, eftersom den tar av samma plangräns som ditt övriga arbete.

Konsekvens: två nycklar och två fakturor att hålla ordning på. `.env` får `ANTHROPIC_API_KEY` och
`REVIEW_MODEL`. Ingen befintlig nyckel byts ut.

## Options Considered

### Option A: Flagga endast (tidigare plan)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Låg |
| Cost | Låg: en Haiku-körning per bolag och cykel |
| Scalability | Hög |
| Team familiarity | Hög: samma mönster som övriga steg |

**Pros:** Inga oavsiktliga ändringar. Lätt att lita på.
**Cons:** Ingen rättning. Någon måste fortfarande göra jobbet för hand, vilket är just problemet.

### Option B: Rätta med loggning och ångra (valt)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medel: ny tabell, skrivvägar, regressionsset |
| Cost | Medel: en Haiku-körning per bolag och cykel, plus fler körningar vid prompt-ändringar |
| Scalability | Hög, med tak per cykel |
| Team familiarity | Medel: nytt mönster, men ångra-funktionen är enkel |

**Pros:** Felen rättas automatiskt. Alla ändringar är spårbara och kan ångras.
**Cons:** En felaktig rättning kan påverka band, prospektlista och poäng tills den upptäcks.

### Option C: Rätta efter godkännande i kön
| Dimension | Assessment |
|-----------|------------|
| Complexity | Hög: kö, godkännandegränssnitt |
| Cost | Medel |
| Scalability | Begränsad av hur snabbt du kan godkänna |
| Team familiarity | Medel |

**Pros:** Ingen felaktig ändring slinker igenom.
**Cons:** Fungerar inte som automatisk rättning, och det blir samma flaskhals som i dag.

## Trade-off Analysis

Option B ger den största nyttan mot det uttalade målet. Priset är risken för felaktiga rättningar,
som hanteras med tre saker: loggen, möjligheten att ångra och regressionssetet. Option C är säkrare
men löser inte problemet. Option A löser inte problemet alls.

Den viktigaste avvägningen är att agenten inte ska vara källbunden. Utan källa finns inget sätt att
i efterhand kontrollera en ändring mot ett dokument. Därför är loggen med motivering och möjligheten
att ångra det som ersätter källhänvisningen. Om en ändring visar sig fel måste den kunna
återställas i ett kommando.

Ordningen mot backup är viktig. En felaktig skrivning till databasen är just den typ av händelse
som Litestream-backupen skyddar mot. Därför bör backup vara på plats innan granskaren får skrivrätt.

## Consequences

**Blir enklare:**
- Fel av samma slag som Hemply och 4C rättas utan manuell genomgång.
- Konsistens mellan fält (kassa mot burn mot skuld) kontrolleras löpande.

**Blir svårare:**
- Felsökning: en siffra kan ha ändrats av agenten, så varje värde måste kunna spåras till en
  ändring eller en extraktion.
- Kostnad och tidsåtgång ökar per cykel.

**Behöver ses över senare:**
- Om regressionssetet är för litet för att fånga ändringar i prompten.
- Om tröskeln på 20 ändringar per cykel är rätt nivå efter några veckors drift.
- Om agenten bör få skrivrätt på fler fält, först när loggen visat att de rättningarna är riktiga.

## Action Items

1. [ ] Godkänn beslutet (Option B) och ordningen: backup först, sedan granskaren.
2. [ ] Skapa `corrections`-tabellen, `review.py` och kommandona `corrections list` och
       `corrections revert <id>`.
3. [ ] Bygg regressionssetet från de kända fallen, med verifierade förväntade värden.
4. [ ] Koppla agenten till schemaläggaren efter nyhetssteget, med `RADAR_REVIEW_ENABLED`.
5. [ ] Kör i skuggläge en vecka: agenten skriver loggen men rättar inget. Granska loggen.
6. [ ] Aktivera skrivrätt när loggen i skuggläget visar färre än några procent felaktiga förslag.
