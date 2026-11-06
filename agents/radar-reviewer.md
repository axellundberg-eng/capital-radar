# Radar-granskning varje måndag

Instruktion för en Claude-chattsession som granskar och rättar radarns data. Målet är färre fel och
mer information per bolag. Källhänvisning per rättelse krävs inte, men varje rättelse ska ha en
motivering i klartext och datan ska hänga ihop.

## Förutsättningar

- Servern nås med `ssh -i ~/.ssh/capital-radar deploy@2.31.136.11`. Kommandon körs i containern:
  `cd /srv/capital-radar && docker compose exec -T radar python -m radar <kommando>`.
- Följ 95 %-regeln i AGENTS.md: läs `get_usage` före rundan och efter varje större steg.
- Läsning per bolag kan delegeras till Haiku-subagenter. Rättelserna görs av huvudsessionen.

## Arbetsordning

1. **Baslinje:** `review checks` och notera antalet flaggor (`findings`).
2. **Kö:** `review queue --limit 40`. Kön är sorterad efter allvar (3 först) och om bolaget har ny
   data sedan förra granskningen. Varje post har runway, fakta med sida och citat, och händelser.
3. **Granska varje bolag i kön.** Jämför flaggan med fakta, citat och vid behov rapporttexten
   (`report_pages`) eller nyhetstexten (`news.text`). Avgör om det är ett fel.
4. **Rätta** med kommandona nedan. Kommandot räknar om bolagets runway direkt och skriver ut resultatet.
   Kontrollera att det nya resultatet är rimligt.
5. **Tolka om** när extraktionen är fel i grunden: `run --document <id> --reextract`.
6. **Logga rundan:** `review log --checked <antal> --corrections <antal> --before <flaggor före>
   --note "kort sammanfattning"`.

## Kommandon

| Vad | Kommando |
|---|---|
| Ändra ett värde | `correct fact --document D --kind cash --period-end 2026-09-30 --value 8612000 --reason "..."` |
| Ta bort ett felaktigt faktum | `correct fact --document D --kind operating_cash_flow --period-start 2026-01-01 --period-end 2026-06-30 --delete --reason "..."` |
| Rätta rapportens periodslut | `correct period --document D --period-end 2026-09-30 --reason "..."` |
| Dölj en felaktig händelse | `correct event --id N --hide --reason "..."` |
| Ändra en händelse | `correct event --id N --status completed --amount 25000000 --currency SEK --reason "..."` |
| Lägg till rådgivare | `correct event --id N --advisers "Carnegie; Redeye" --reason "..."` |
| Lista rättelser | `corrections list --company ID` |
| Ångra en rättelse | `corrections revert ID` |

Belopp anges i hela kronor (eller rapportens valuta), inte i TSEK eller MSEK.

## Vanliga fel och vad som gäller

- **positive_but_burning:** halvåret eller året är positivt, men senaste kvartalet bränner. För
  förlustbolag med en engångsinbetalning: ta bort den längre periodens kassaflöde, så räknas burn på
  senaste kvartalet. För lönsamma bolag med säsongsvariation (konsultbolag, industri): låt det vara.
- **currency_mix:** finansiering i annan valuta räknas inte. Räkna om till rapportens valuta och rätta
  händelsens belopp och valuta, med växelkursen i motiveringen.
- **cash_column / flow_same_value:** jämför med rapporttexten. Rätta värdet om fel kolumn lästs.
- **exhausted_unexplained:** leta efter finansiering i nyheterna efter rapporten. Hittas en emission
  eller ett lån som inte registrerats, kör om nyhetssteget eller rätta en befintlig händelse.
- **stale_report:** kontrollera på MFN om en nyare rapport finns. Insamlingen känner nu igen rapporter
  på bilagans filnamn; saknas rapporten helt är det en lucka i källan.
- **duplicate_event:** svensk och engelsk version av samma pressmeddelande. Slås ihop automatiskt i
  beräkningen och i listan; ingen rättelse behövs.

## Gränser

- Ta aldrig bort bolag eller rapporter. Händelser döljs, de raderas inte.
- Rätta inte marknadsvärde, prospektpoäng eller band: de räknas om automatiskt.
- Högst 20 rättelser per runda om inget annat sagts, så att rundan går att följa upp.
