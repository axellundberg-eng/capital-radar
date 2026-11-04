# Granskning: runway och transaktionsflödet (2026-11-04)

Underlag inför nydesignen. Bygger på kodläsning. Punkterna 1, 2 och 4 under
runway är kontrollerade direkt i koden.

## Runway idag

- Senaste rapport med både kassa och operativt kassaflöde (CFO) väljs
  (`runway.py:79-175`). Burn = max(0, −CFO / periodens månader)
  (`runway.py:262`). Reported runway = kassa / burn.
- Adjusted runway finns i två varianter med olika regler: `runway.py:227-253`
  (genomfört 100 %, committed 70 %) och `radar_analysis.py:183-280`
  (pro forma i UI:t: bruttobelopp × vikt, standardvikt 1,0).

### Största riskerna

1. Kassan rullas aldrig fram från rapportdatum. En fem månader gammal rapport
   ger samma runway "från idag" som en färsk (`runway.py:263-266`,
   `radar_analysis.py:246`).
2. Annonserade men inte genomförda emissioner räknas som 100 % kassa i pro
   forma (standardvikt 1,0, `radar_analysis.py:233`).
3. Bruttobelopp används, emissionskostnader och garantiersättning dras inte av.
4. Beloppsskala gissas: SEK-belopp under 1 000 multipliceras med en miljon och
   under 500 000 med tusen (`runway.py:210-225`). 400 000 kr kan bli 400 MSEK.
5. Burn: kvartal kontra YTD-period, jämförelsekolumner och kapitaliserad FoU kan
   ge fel burn med faktor 2–3. Investeringskassaflöde och förfallande skuld
   ingår inte.
6. Dubbelräkning: annonsering och utfall av samma emission kan räknas båda
   (`runway.py:195-246`). Dedup i pro forma kräver exakt en round key.
7. Äldre rapport används tyst när den nyaste saknar fakta. Bolag utan data
   försvinner tyst.

## Från MFN-dokument till affär i flödet

- Kategori och status bestäms av regex på **titeln** (`transaction_feed.py:61-258`).
- Belopp tas med regex ur rubrik och brödtext (`_amount`, rad 361-444).
- Gruppering till en affär sker girigt med hundratals heuristikrader
  (`_related`, rad 627-770) plus manuella korrigeringar.
- DeepSeek skriver sammanfattningar som valideras mot fakta och cachas.

### Största problemen

1. Affärens ID är `min(doc_id)` (`transaction_feed.py:1700`). Ett äldre dokument
   som läggs till byter ID, och vikter och korrigeringar tappar sin koppling.
2. Ett enda fel i bygget kan fälla hela flödet (många `raise ValueError`).
3. Titelbaserad klassning ger felkategorier och filtrerar bort riktiga affärer.
4. UI:t visar belopp utan beloppsbasis (brutto, max, lån) och hårdkodar MSEK
   (`workspace.js:22-43`).
5. För bred sammanslagning: två företrädesemissioner inom 180 dagar kan bli en.
6. UI:t säger "ChatGPT" fast motorn är DeepSeek.

## Rekommendation inför nydesign

- **Runway:** ett enda beräkningsspår. Rulla fram kassan (burn × månader sedan
  periodslut). Räkna bara genomförd nettokassa som säker och visa förväntad
  finansiering separat som intervall. Normalisera perioder och visa saknad
  data öppet.
- **Affärer:** egen tabell med stabilt ID. Händelser kopplas explicit, med
  separata belopp för annonserat, brutto, netto och utfall. Använd DeepSeek
  för strukturerad extraktion (typ, status, belopp, valuta) och behåll regex
  som reserv.
- **Gruppering:** få tydliga regler och en granskningskö. Ett fel flaggar en
  affär i stället för att fälla flödet.
- **UI:** små komponenter, tydlig skillnad mellan annonserat och genomfört,
  alltid beloppsbasis, och rätt namn på AI-motorn.
