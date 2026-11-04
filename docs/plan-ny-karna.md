# Plan: ny kärna (`src/radar`)

Arbetssätt: sätt ett litet mål, få det att fungera på riktig data och bygg
sedan vidare. Varje steg avslutas med ett mätbart resultat.

## Steg 1: från rapport-PDF till runway (pågår)

**Mål:** för de 26 bolagen i facitsetet tar ny kod den senaste rapporten hela
vägen från PDF till runway. Burn ska ligga inom 20 % av facit för de
verifierade bolagen.

Kedja med status per dokument:

1. **Import:** bolag, rapportdokument och PDF-länkar läses en gång ur den
   gamla databasen (skrivskyddat). Det är data, inte kod.
2. **Hämta:** PDF:en laddas ned och sparas. Den raderas aldrig.
3. **Text:** text extraheras sida för sida.
4. **Extraktion:** DeepSeek får de relevanta sidorna och svarar med JSON
   (kassa, kassaflöden per period, capex, investeringar, med sida och citat).
   Svaret valideras innan det sparas.
5. **Runway:** beräknas med de kalibrerade reglerna (längsta period,
   försiktighet vid högre senaste period, capex eller investeringar).

Körs med `python -m radar import` och `python -m radar run --facit`.
Utelämnat i steg 1: finansiering efter rapporten, gränssnitt och bevakning.

## Steg 2: alla bolag

Kedjan körs för alla 516 bolag. Mät täckning (andel med runway), fel per
steg och kostnad. Rätta de vanligaste felen.

## Steg 3: löpande drift

Ny rapport från MFN gör att kedjan körs automatiskt. Enkel statussida.

## Steg 4: finansiering och affärer

Händelser per dokument med stabila affärs-ID:n. Säker och förväntad runway.

## Steg 5: gränssnitt

Det nya gränssnittet ersätter det gamla, och sedan raderas `legacy/`.
