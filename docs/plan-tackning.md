# Plan: täckningsluckor (OCR, saknade MFN-slugar, andra distributörer)

Datum: 2026-11-07. Status: utredning, ingen kod ändrad. "Verifierat" = riktigt anrop idag,
"antaget" = uppskattning från kunskap, ej mätt.

## 1. Runway saknas (~62 bolag, bildsidor utan textlager)

Problem: `pdftext.pages()` ger tom text för skannade sidor, och `select_pages` (nyckelord, max 8 sidor)
kan då inte välja sidor. OCR måste alltså först *hitta* sidorna, sedan *läsa* dem.

| Alternativ | Bildstorlek | Tid/sida (CX23) | Tabellnoggrannhet | Kostnad |
|---|---|---|---|---|
| Tesseract swe+eng (`tesseract-ocr`, `-swe`, `-eng`, pymupdf för rendering) | +~60-80 MB (antaget) | ~1-3 s vid 150-200 dpi, ~3-5 s vid 300 dpi (antaget) | Bra på ren text, tabellkolumner kan flyta ihop; siffror/minustecken ibland fel | 0 kr |
| ocrmypdf | +~150 MB (ghostscript, qpdf, unpaper) | som ovan + overhead | Samma motor | 0 kr, tyngre än nödvändigt |
| PaddleOCR / docTR | +1-2 GB (torch/paddle), 1-2 GB RAM | 5-15 s/sida på CPU (antaget) | Bättre layout, men överdimensionerat och trycker mot 4 GB | 0 kr, avråds |
| Claude Haiku 5.5 vision ($0.10/$0.50 per Mtok) | 0 | ~2-4 s API-svar | Bäst på tabeller; kan returnera färdig JSON | se nedan |
| DeepSeek | - | - | Saknar syn, kan inte användas för bilder | - |

Rekommendation (2-stegs, bara när textlagret är tomt):
1. Rendera alla sidor lågupplöst (150 dpi) och kör Tesseract för att få text till `select_pages`
   (en rapport på ~30 sidor ≈ 30-90 s).
2. De upp till 8 valda sidorna: skicka som bild till Haiku 5.5 vision med samma extraktionsprompt
   (en bild-sida ≈ 1 500-1 800 tokens in, ~400 ut, antaget; verifiera med `usage` på 2-3 sidor).
   Faller Haiku bort kan Tesseract-texten (300 dpi) gå till DeepSeek som i dag.
3. Märk resultatet `källa=ocr` och sänk konfidens/kräv granskning när OCR-siffror inte summerar.

Kostnad (62 bolag x ~8 sidor = ~500 sidor):
- Engång: Tesseract-flaggning 62 x 30 sidor x ~1,5 s ≈ 45 min CPU; Haiku 500 x (1 700 in + 400 ut)
  = 0,85 M in + 0,2 M ut ≈ 0,09 + 0,10 = ca 0,2 USD. Allt-Tesseract: 500 x 4 s ≈ 35 min, 0 USD.
- Månad: antag ~10 nya bildrapporter/månad x 8 sidor = 80 sidor ≈ 0,03 USD + ~10 min CPU.
- Serverkapacitet: kör OCR i en process i taget (RAM < 500 MB).

## 2. 35 bolag utan MFN-slug

Verifierat (riktiga anrop, 1 s paus, robots.txt tillåter sidor; endast `*.json/*.xml/*.rss` är avstängda):
- MFN:s egen sökruta anropar `GET https://www.mfn.se/search/companies?limit=5&query=<namn>` (hittad i
  `/assets/js/search_*.js`). Svarar JSON med `slug`, `slugs[]`, `name`, `isins[]`, `local_refs`
  (`SE:<orgnr>`), `tickers`. Ingen autentisering, ingen filändelse.
- Träffar: Bambuser (SE0027099326, slug `bambuser`), Hexagon, Sivers Semiconductors (slugs
  `sivers-ima-holding` + `sivers-semiconductors`), Cortus Energy, Neonode (`neonode-inc`), Climeon,
  Smart Eye. Tomma svar: Anoto Group, Kancera (finns inte på MFN, hamnar i fallback, avsnitt 3).
- Gissning `/all/a/<slug>` är otillförlitlig: okänd slug ger HTTP 200 med tom sida (titel `MFN.se > `,
  0 poster). `/all/a/bambuser-ab` gav också 0 poster. Använd bara som kontroll: giltig slug har
  `<title>MFN.se > <Namn>` och >0 `item-link`.

Metod: `radar mfn-slugs --dry-run`
1. Normalisera namn (gemener, ta bort AB/(publ)/Group/Holding/Inc, å/ä/ö kvar i sökning, prova
   fullt namn, sedan namn utan suffix, sedan ticker).
2. Matcha primärt på ISIN (exakt), sekundärt orgnr (`local_refs`), sista på namn-likhet >= 0,9.
   Flera träffar eller bara namnmatch = "granska".
3. Skriv en CSV (bolag, föreslagen slug, matchtyp, ISIN-match ja/nej, antal poster på sidan) till
   `docs/` eller `data/`; slug skrivs till `companies.mfn_slug` först efter manuell OK. Ingen träff
   = markera `ingen_mfn` så att bolaget går till fallback.
4. Kostnad: 35 bolag x 1-3 sökningar x 1,2 s ≈ 2 min, 0 USD. Kör en gång, sedan veckovis för nya bolag.

## 3. Rapporter via andra distributörer

Kontrollerat:
- Cision: `news.cision.com/robots.txt` har inga Disallow; `https://news.cision.com/se/kancera` ger 200 med
  HTML och pressmeddelandelänkar (`/se/<bolag>/r/<titel>,c<id>`). RSS-adress ej verifierad (ingen
  `<link rel=alternate>` hittades); HTML-sidan går att tolka som MFN-sidan.
- GlobeNewswire (inkl. Notified): robots.txt och RSS gav 403 Access Denied (Akamai) mot vår klient. Avstå
  tills vidare, kräver ev. avtal/nyckel.
- Spotlight: robots.txt tillåter allt men `Crawl-delay: 10`. Bolagsnyheter ej testade.
- Nasdaq First North / NGM: robots-kontrollen gav bara HTML-sidor (ingen robots-fil returnerades), inget
  öppet bolagsflöde verifierat. Antaget: JS-baserat, dyrt att tolka.
- Modular Finance / bolagens IR-sidor: ej testat. Varierar per bolag, ej skalbart.

Minimal fallback (föreslås): körs bara för bolag där (a) `mfn_slug` saknas/`ingen_mfn`, eller (b) senaste
rapport på MFN är äldre än ~7 mån (så Bambuser 2026 fångas).
1. Lägg `companies.alt_source_url` (Cision-sida eller IR-sida), fylld manuellt/halvautomatiskt för de
   få bolag det gäller (uppskattat 10-20 st).
2. Hämta sidan med samma Throttle (>= 10 s för Spotlight), hitta länkar till PDF med ord som
   "delårsrapport|bokslutskommuniké|interim|year-end|report", ladda ner PDF, mata in i befintlig pipeline.
3. Kvittera i loggen vilket bolag som har äldre rapport än 7 mån trots att fallbacken kördes.

## 4. Kostnads- och tokentabell

| Post | Engång | Per månad | Antagande |
|---|---|---|---|
| Tesseract-flaggning (CPU) | ~45 min, 0 USD | ~10 min, 0 USD | 62 rapporter x 30 sidor, 1,5 s/sida |
| Haiku 5.5 vision, 8 sidor/bolag | ~0,85 Mtok in + 0,2 Mtok out = ~0,2 USD | ~0,03 USD | ~1 700 in + 400 ut per sida, 10 rapporter/mån |
| DeepSeek-extraktion (ersätts för OCR-bolag) | oförändrad | oförändrad | ingen skillnad i volym |
| Slugmatchning | ~2 min, 0 USD | ~1 min | 35 bolag, 1,2 s paus |
| Fallbackhämtning (Cision/IR) | ~5 min, 0 USD | ~5 min | 10-20 bolag, 10 s paus |
| Serverlagring | +60-80 MB bild | - | Tesseract + språkdata |
| Totalt | ca 0,2 USD + ~1 h CPU | < 0,1 USD | |

Ordning: (1) slugmatchning (snabbast, störst täckning per insats), (2) OCR, (3) fallback.
Osäkert tills mätt: tid per sida, Haiku-bildtokens (verifiera på 3 sidor), Cision-RSS, tillstånd för
GlobeNewswire.
