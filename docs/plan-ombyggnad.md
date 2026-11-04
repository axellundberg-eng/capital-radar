# Plan: ombyggnad av Capital Radar

Utgår från granskningen i `granskning-runway-och-flode.md`. Målet är ett
system där varje siffra går att förklara och spåra till en källa, och där
ett fel i ett bolag aldrig fäller hela vyn.

## Princip

Den gamla koden används som kravlista, inte som grund. Varje del skrivs om
som ny, liten och testad kod med egna kopplingar. De gamla modulerna får
leva kvar bara tills den nya delen har ersatt dem och visat rätt resultat.

## Upptäckt 2026-11-04: rapportkedjan tappar data

- Den gamla driften raderade text och PDF:er efter extraktion för att spara
  disk. 5 557 dokumentinnehåll är tomma, och 1 694 av 5 774 rapporter kan
  inte extraheras om.
- Följden är att 80 bolag står kvar på rapporter som är 9–22 månader gamla,
  trots att nyare rapporter finns nedladdade.
- Radering är nu avstängd. Den nya rapportkedjan (steg 1b) måste hämta om
  saknade PDF:er.

## Redan åtgärdat (2026-11-04)

- Runway räknas från idag: rapportkassan rullas fram med burn.
- Endast genomförd finansiering räknas som kassa som standard.
- Gissningen om kSEK-skala är borttagen.
- Gränssnittet visar runway vid rapport, runway idag och rapportens ålder.

## Steg 1: Runway-motor (en enda beräkning)

- Ny modul `runway_engine` ersätter de två parallella spåren i
  `runway.py` och `radar_analysis.py`.
- Indata kontrolleras uttryckligen: perioden normaliseras (kvartal, H1,
  YTD, år), fakta måste höra till rapportperioden, och valutan är explicit.
- Burn = operativt kassaflöde + investeringar i immateriella och materiella
  tillgångar, per månad. Förfallande skuld visas separat.
- Utdata: kassa idag (uppskattad), säker runway, förväntad runway
  (intervall) och en lista över antaganden. Saknad data blir status
  "saknas", inte tyst bortfall.
- Testas med ett facitset av 20–30 verkliga bolag och kända svar.

Status: `runway_engine.py` (v1) och `runway_compare.py` finns och körs i
skuggläge mot produktionsdata.

## Steg 1b: Ny rapportkedja

- Ny, fristående kedja: hitta rapport → hämta PDF → extrahera text →
  DeepSeek med JSON-schema (kassa, kassaflöden per period, capex, skuld,
  periodens start och slut) → spara fakta med sida och utdrag.
- Hämtar om rapporter vars text saknas.
- Varje steg har en status per dokument, så att ett fel syns och kan köras
  om, i stället för att fastna som "content_ready".

## Steg 2: Affärsmodell med stabila ID:n

- Nya tabeller `deals` och `deal_events` med stabilt ID (uuid).
  Dokument kopplas explicit till en affär.
- Separata fält: annonserat, brutto, netto, utfall och kassa mottagen,
  var och en med källa.
- Vikter och korrigeringar knyts till `deals.id` och tappas inte när
  dokument tillkommer.
- Migrering: befintliga grupper och korrigeringar förs över en gång, med
  rapport över avvikelser.

## Steg 3: Strukturerad extraktion

- DeepSeek extraherar typ, status, belopp, valuta och instrument som
  JSON-schema per dokument. Regex på titlar blir bara reserv.
- Haiku-granskaren flaggar osäkra extraktioner (redan förberett).
- Grupperingen blir några få regler (bolag, round key, serie,
  tidsfönster) plus en granskningskö i stället för heuristik.

## Steg 4: Nytt gränssnitt

- Mindre komponenter i stället för en 95 kB `workspace.js`.
- Varje belopp visas med sin basis (brutto/netto/utfall).
- Runway visas som säker och förväntad, med förklaring.
- Ett fel i ett bolag eller en affär visas lokalt, inte som tom sida.

## Ordning och arbetssätt

Steg 1 först, eftersom runway är systemets kärna. Varje steg levereras
bakom en flagga och körs parallellt med den gamla beräkningen tills
facitsetet stämmer, sedan byts den gamla ut.
