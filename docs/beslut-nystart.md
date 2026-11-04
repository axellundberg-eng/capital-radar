# Beslut: börja om eller bygga vidare? (2026-11-04)

## Slutsats

**Börja om med ny kod. Behåll infrastrukturen, rådata och lärdomarna.**
Skriv inte om den gamla koden bit för bit.

## Underlag

### Koden

- 29 100 rader Python i 83 moduler, 15 800 rader tester och 85 kB JavaScript
  i en enda fil.
- 21 funktioner är längre än 150 rader. De största: `_financing_dossier`
  (470 rader), `_build_transaction_items` (423) och `_interpret_core` (304).
- Två parallella runway-beräkningar med olika regler. Affärs-ID byggs från
  `min(doc_id)` och är instabilt. Kategori och status gissas med regex på
  titlar.
- Testerna låser ofta in det gamla beteendet. När runway rättades fick sex
  tester skrivas om, eftersom de testade felet.

### Datan

| Del | Läge | Bedömning |
|---|---|---|
| Bolag och värdepapper (516 / 530) | Från börslistor | Behålls |
| Dokument med URL (25 789) och PDF-länkar (25 768) | Från MFN | Behålls; PDF:er hämtas om |
| Finansiella rapporter (5 774) | Endast 809 extraherade (14 %) | Görs om |
| Extraherade fakta | Facit: capex saknas, fel tecken, fel period | Görs om |
| Analyskörningar (51 062) | 46 % misslyckade eller "insufficient facts" | Kastas |
| Text till 5 557 dokument | Raderad av gamla driften | Hämtas om |
| 123 tabeller, 3,6 GB | Störst: 718 000 sync-rader och 287 000 progress-händelser | Kastas |

Det värdefulla i datan är **vilka bolag och vilka dokument som finns**.
Det som räknats fram ur dem bär de gamla felen och måste göras om ändå.

### Varför inte bygga vidare

Varje rättning hittills (runway, radering, läsmodellens tidsgräns,
insamlingen) krävde att man först förstod och kringgick gammal kod. Med en
ny kärna försvinner det arbetet, och resultatet blir testbart från början.

## Vad som behålls

- **Infrastruktur:** Hetzner-servern, Docker, CI med automatisk deploy,
  Cloudflare Tunnel och Access, backup. Allt är nytt och fungerar.
- **Rådata:** bolagsuniversumet, dokumentlistan och PDF-länkarna.
- **Kunskap:**
  - facitset och regler från `runway_engine` (v2),
  - MFN-tolkningen och beloppsparsern,
  - granskningen i `granskning-runway-och-flode.md`.
- **Gamla systemet:** körs kvar orört som referens tills det nya visar
  bättre siffror på facitsetet. Därefter stängs det av.

## Lärdomar som blir krav i det nya

1. **En sanning per begrepp:** en runway-beräkning och ett affärs-ID (stabilt
   uuid).
2. **Pipeline med status per dokument:** hitta → hämta → text → extrahera →
   fakta. Varje steg kan köras om och syns när det fastnar.
3. **Ingen radering av källdata.** Diskutrymme löses med lagring, inte genom
   att kasta det man behöver.
4. **Strukturerad AI-extraktion** med JSON-schema, period och källhänvisning
   per fakta. Ingen regex på titlar som primär källa.
5. **Facit först:** varje regel testas mot verkliga, granskade bolag, inte
   bara konstruerade exempel.
6. **Små moduler:** inga funktioner över cirka 60 rader och ingen fil över
   cirka 400. Ett fel i ett bolag fäller aldrig hela vyn.
7. **Fel syns:** saknad data blir status i gränssnittet, aldrig tyst
   bortfall.

## Förslag till ordning

1. Ny kärna i samma repo (nytt paket) med ren databas (~10 tabeller), som
   importerar bolag och dokumentlistan från den gamla.
2. Rapportkedjan: hämta PDF, extrahera text och DeepSeek-JSON, och spara
   fakta. Mät täckning och facit.
3. Runway-motorn ovanpå nya fakta.
4. Affärer: händelser per dokument, gruppering och stabila ID:n.
5. Nytt gränssnitt. Därefter stängs det gamla av.
