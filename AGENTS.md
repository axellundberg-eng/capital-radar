# Arbetsrutin för timeout

- En enskild operation får aldrig lämnas utan kontroll längre än tre minuter.
- Använd explicita timeouter på högst 180 sekunder; välj högst 60 sekunder för
  diagnostik, serverkontroller och normala nätverksanrop.
- Om en operation når timeout: avbryt den exakta processen, kontrollera status
  med ett kort skrivskyddat anrop och rapportera vad som hann genomföras.
- Gör inte ett identiskt nytt försök utan att först ha identifierat orsaken eller
  valt en säkrare metod.

## Användningsgräns: sluta koda vid 95 %

- Läs användningen med verktyget `get_usage` (`mcp__ccd_session_mgmt__get_usage`) före varje
  ny kodningsomgång, före varje agent, före varje push eller deploy och när en agent
  rapporterat klart.
- Når **5-timmarsgränsen eller veckogränsen 95 %**: slutför bara det atomiska steg som är
  påbörjat (lämna aldrig något halvgjort, till exempel mitt mellan push och verifiering),
  och sluta sedan koda. Inga nya ändringar, agenter, pushar eller deployer.
- Säg i en eller två rader vilken gräns, hur många procent, när den nollställs och vad som
  återstår. Därefter besvaras bara frågor i vanlig chatt, så att användaren kan fortsätta
  samtala. Fortsätt först när användaren säger till eller gränsen nollställts under 95 %.
- Agenter som redan körs kan inte stoppas av regeln. Starta inga nya.

## Ny kod och gammal kod

- `src/radar/` är den nya kärnan, skriven från grunden.
- `legacy/` är den gamla koden. Den körs i produktion tills den nya har
  ersatt den, och raderas sedan i sin helhet. Den används bara som karta
  över vad systemet ska klara, aldrig som grund.
- Ny kod får **aldrig importera** något från `legacy/` eller `capital_radar`.
- Från den gamla koden får ny kod **endast använda API-inloggningarna** i
  `src/radar/credentials.py` (`DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`,
  `SCREENER_ADMIN_API_TOKEN`). Alla andra inställningar får egna `RADAR_`-namn.
- Reglerna kontrolleras av `tests/test_boundaries.py` i CI. Testet får inte
  försvagas eller kringgås.
- Ändra inte i `legacy/` utom för att hålla produktionen igång.
