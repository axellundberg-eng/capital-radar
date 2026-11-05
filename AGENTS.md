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

## Kodbasen

- All kod ligger i `src/radar/`, skriven från grunden. Den äldre koden är borttagen; den finns
  kvar i git-historiken under taggen `legacy-last` om något behöver slås upp.
- Den gamla databasen (`data/screener.db` på servern) är ett arkiv som bara läses av
  engångsimporten `python -m radar import` och jämförelsen `radar collect --compare-legacy`.
- Ny kod får **aldrig importera** `capital_radar` eller `legacy`. Från äldre inställningar delas
  endast API-inloggningarna i `src/radar/credentials.py`; alla andra inställningar heter
  `RADAR_*`. Reglerna kontrolleras av `tests/test_boundaries.py` i CI och får inte försvagas.
- Kör alltid testerna med både `pytest` (som CI) och `python -m pytest`, och använd aldrig
  `from tests.x import` (tests/ saknar `__init__.py`; använd `from test_x import`).
