# Arbetsrutin för timeout

- En enskild operation får aldrig lämnas utan kontroll längre än tre minuter.
- Använd explicita timeouter på högst 180 sekunder; välj högst 60 sekunder för
  diagnostik, serverkontroller och normala nätverksanrop.
- Om en operation når timeout: avbryt den exakta processen, kontrollera status
  med ett kort skrivskyddat anrop och rapportera vad som hann genomföras.
- Gör inte ett identiskt nytt försök utan att först ha identifierat orsaken eller
  valt en säkrare metod.

## Prioriterad garantigranskning

- Vid finansierings- och garantigranskning: läs `agents/guarantee-reviewer.md`
  och det aktuella livekontraktet. Använd befintligt abonnemangsflöde, ingen ny
  timer/worker eller direkt modell-API-koppling.
- Prioritera garantiberikning när granskbara kandidater finns. Reservera minst
  en garantifokuserad batch inom körbudgeten utan att tränga bort kontraktets
  financing-, backlog- och discovery-banor. Högst20claims totalt och giltig
  lease gäller; respektera deferrals och andra aktiva körningar.
  Målet är att lägga huvuddelen av den återstående granskningsbudgeten på
  garantier när underlaget tillåter det; redovisa faktisk täckning och hinder.
- Varje garanti behöver en källbunden inventering av villkor, tranch/instrument,
  tilldelning, ersättning, betalning/säljbarhet, historik och kvarvarande gaps.
  Äldre, outnyttjade, negativa och dataglesa fall får inte väljas bort.
- Ny information kräver jämförelse med tidigare granskning och aktuell
  dokumentversion. Historiskt minne och en publicerad agentprofil är inte
  bevis för en aktiv körning eller aktuell finansiell verifiering.
