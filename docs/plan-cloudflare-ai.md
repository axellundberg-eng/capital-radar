# Utredning: Workers AI för OCR av bildsidor i finansiella rapporter

Datum: 2026-11-07. Status: utredning, ingen kod ändrad. Komplement till `docs/plan-tackning.md`.

Märkning: **[Verifierat]** = läst i Cloudflares dokumentation eller i källa som angivits, med sidans datum.
**[Antaget]** = uppskattning eller saknad källa. Inget av detta är testat mot våra egna rapporter.

## Sammanfattning

- Workers AI har en vision-modell som kan ta bilder: `@cf/meta/llama-3.2-11b-vision-instruct`. Den
  presterar dåligt på tabeller i ett oberoende benchmark (se avsnitt 4). Den är inte lämplig för
  balansräkning och kassaflöde utan test.
- Kostnaden är låg för båda alternativen. Haiku och Workers AI skiljer sig med ett par tiotal öre per
  engångskörning. Kostnad är därför inte skäl nog att byta. Kvalitet och verifierbarhet är det som avgör.
- Rekommendation: behåll Haiku för OCR av tabellsidor. Testa Workers AI endast parallellt på ~20 kända
  sidor. Byt inte förrän Workers AI klarar siffror och kolumner i samma nivå.

## 1. Vilka modeller kan läsa bilder på Workers AI

Modellkatalogen (sida daterad 2026-08-12) [Verifierat] listar följande med bildindata. Modell-ID:n
nedan är de som verifierats i detaljsidorna, inte alla i katalogen:

| Modell-ID | Notering | Status |
|---|---|---|
| `@cf/meta/llama-3.2-11b-vision-instruct` | Dedikerad vision-modell, 128k kontext | Verifierat: Vision Yes, kräver godkännande av Metas licens (skicka först `"prompt": "agree"`) |
| `@cf/meta/llama-4-scout-17b-16e-instruct` | "Natively multimodal", 131k kontext | Verifierat: Vision listad. Bildformat ej dokumenterat |
| `@cf/mistralai/mistral-small-3.1-24b-instruct` | "Vision understanding", 128k kontext | Verifierat: vision nämnd. Bildformat ej dokumenterat |
| `@cf/google/gemma-4-26b-a4b-it` | 256k kontext | Verifierat: "Vision: Yes". Bildformat ej dokumenterat |
| `@cf/moondream/moondream3.1-9B-A2B` | Bild-till-text, nämner OCR och strukturerad output | Verifierat (prissidan). Ej testad |
| `@cf/moonshotai/kimi-k2.6` (namn i katalog: kimi-k2.6) | "Vision inputs", 262k kontext | Verifierat i katalog. Namn/ID ej bekräftat |
| `@cf/llava-hf/llava-1.5-7b-hf` | Beta, svag | Verifierat i katalog. Ej aktuell |

Namnmönster: ingen gemma-3-modell med bild, ingen modell med "ocr" i namnet [Verifierat].
Qwen3-VL och Mistral Small 4 hittades inte i Workers AI-katalogen i våra sökningar [Antaget: finns inte].

**toMarkdown / dokumentkonvertering** (sida daterad 2026-04-21) [Verifierat]:
- Konverterar "dokument i flera format" till Markdown. Bilder kan köras via Workers AI-modeller för
  bl.a. "object detection and summarization".
- Sidan **nämner inte OCR** av bilder eller skannade PDF:er och anger inga storleks- eller sidgränser.
- "Gratis för de flesta konverteringar", men bildkonvertering använder Workers AI-modeller och kan
  kosta utöver gratisnivån.
- Slutsats: **förlita dig inte på toMarkdown för skannade balansräkningar**. Testa bara om vi vill.

## 2. Priser och neuron

Prissidan (daterad 2026-10-20) [Verifierat]:
- **0,011 USD per 1 000 neuron.** Gratis 10 000 neuron per dag (återställs 00:00 UTC) på Workers Free och Paid.
- Vision-modeller prissätts per token i sidan, med neuron-motsvarighet:

| Modell | USD per M in / M ut | Neuron per M in / M ut |
|---|---|---|
| llama-3.2-11b-vision-instruct | 0,049 / 0,676 | 4 410 / 61 493 |
| moondream3.1-9B-A2B | 0,300 / 1,000 | 27 273 / 90 909 |
| mistral-small-3.1-24b-instruct | 0,351 / 0,555 | ej angivet i sidan |
| llama-4-scout-17b-16e-instruct | 0,27 / 0,85 | ej angivet i sidan |
| gemma-4-26b-a4b-it | 0,10 / 0,30 | ej angivet i sidan |

Ingen separat per-bild-avgift för vision-LLM hittades. Bilden räknas som inputtokens [Verifierat].
Kontroll: 4 410 neuron × 0,011/1 000 ≈ 0,049 USD, så tabellen är intern konsekvent [Verifierat].

Kostnadsberäkning. Antaganden: 1 700 input- och 500 output-tokens per sida (projektets antagande).
Engång: 500 sidor = 0,85 M in + 0,25 M ut. Månad: 50 sidor = 0,085 M in + 0,025 M ut.

| Modell | Engång (USD) | Per månad (USD) | Anmärkning |
|---|---|---|---|
| Llama 3.2 11B Vision (Workers AI) | ~0,21 | ~0,02 | ~19 100 neuron. Två dagar inom gratisnivån = 0 USD; allt en dag ≈ 0,10 USD |
| Gemma 4 26B (Workers AI) | ~0,16 | ~0,02 | Ej testad för bild |
| Mistral Small 3.1 (Workers AI) | ~0,44 | ~0,04 | |
| Llama 4 Scout (Workers AI) | ~0,44 | ~0,04 | |
| Claude Haiku 5.5 vision (nuvarande plan) | ~0,21 | ~0,02 | Pris $0,10/$0,50 från projektets plan. **Ej verifierat här** |

Kostnad är alltså inte avgörande. Skillnaden mellan alternativen är under 0,30 USD engångs.

## 3. Anropa från Python på Hetzner

**Endpoint** [Verifierat, REST API-sidan 2026-09-15]:
`https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/run/{MODELL_ID}`

**Autentisering** [Verifierat]:
- Header: `Authorization: Bearer {API_TOKEN}`.
- Token med behörigheterna **Workers AI – Read** och **Workers AI – Edit** (mallen i Cloudflare-dashboarden har dem).
- Lagra `CF_ACCOUNT_ID` och `CF_API_TOKEN` i miljövariabler på servern, inte i repot.

**Bildformat** [Delvis verifierat]:
- Cloudflares Workers-exempel [Verifierat, daterad 2026-08-25] skickar bilden i fältet `image` som base64
  med prefix `data:image/png;base64,...`, tillsammans med `messages`.
- Vi har **inte** verifierat exakt REST-body för bild. Tredjepartsguider nämner `messages` med
  `{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`. Det är inte bekräftat av Cloudflare [Antaget].
- Första steget vid implementation: ett testanrop mot REST-endpointen med en känd sida. Verifiera
  både `image`-fältet och `image_url`-formatet, och spara det fungerande formatet.

**Viktig fallgrop** [Verifierat]: standard `max_tokens` är **256**. En tabellsida med 500 output-tokens
skulle kapas. Sätt `max_tokens` till minst 2 000 för sidor.

**JSON / strukturerad output** [Verifierat, sidan daterad 2026-09-14]:
- `response_format` med `json_schema` finns, men listan över stödda modeller innehåller **inga vision-modeller**.
- Stöd för structured output för bildmodeller är därför ej dokumenterat. Använd prompt med JSON-krav,
  parsa, och validera (summor, kolumnantal). Hantera fel själv.
- JSON mode stödjer inte streaming.

**Rate limits** [Verifierat, daterad 2026-09-17]:
- Textgenererande modeller: 300 anrop/min. Modeller som kräver Paid-plan: 20 anrop/min (50 med prepaid AI Gateway).
- Bild-till-text: 720/min. Gäller inte explicit för vision-LLM, så använd den lägre gränsen som säker.
- 500 sidor vid 20 anrop/min ≈ 25 minuter. Inget problem.
- Maximal bildstorlek och request-storlek anges inte [Verifierat: saknas]. Testa med en sida i 200 dpi och en i 300 dpi.

**Minimalt Python-exempel** (skelett, ej testat; formatet för bilden måste verifieras):

```python
import base64, os, requests

ACCOUNT = os.environ["CF_ACCOUNT_ID"]
TOKEN = os.environ["CF_API_TOKEN"]
MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"
URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run/{MODEL}"

def ocr_page(png_bytes: bytes, prompt: str) -> str:
    b64 = base64.b64encode(png_bytes).decode()
    body = {
        "messages": [{"role": "user", "content": prompt}],
        "image": f"data:image/png;base64,{b64}",   # format ej slutligen verifierat via REST
        "max_tokens": 2000,
    }
    r = requests.post(URL, headers={"Authorization": f"Bearer {TOKEN}"}, json=body, timeout=60)
    r.raise_for_status()
    return r.json()["result"]["response"]          # svarsnyckel ej verifierad för REST
```

Timeout 60 s följer projektets regel för nätverksanrop.

## 4. Kvalitet för svenska finansiella tabeller

**Oberoende benchmark** (OmniDocBench v1.5, leaderboard hos Nanonets, ej daterad snapshot) [Verifierat som källa; snapshot okänd]:

| Modell | Overall | Tabell TEDS | På Workers AI? |
|---|---|---|---|
| Qwen2.5-VL-72B (rapporterad av modellgivare, ej i Nanonets-tabellen) | 87,0 | 82,2 | Nej |
| Gemini 3 Flash | 90,1 | 87,7 | Nej |
| Claude Sonnet 4.6 | 86,9 | 87,1 | Nej |
| Claude Haiku 4.5 (Haiku 5.5 ej listad) | 79,6 | 77,1 | Nej (vi kör Haiku via Anthropic) |
| Mistral Small 4 | 76,4 | 75,1 | Ej hittad på Workers AI |
| Gemma-3-12B | 44,6 | 31,6 | Ej aktuell variant |
| **Llama-3.2-Vision-11B** | **44,6** | **32,6** | **Ja (det enda dedikerade vision-alternativet)** |

Tolkning:
- Llama 3.2 11B är **klart sämre** än Haiku på tabeller. TEDS 32,6 mot ~77 för Haiku 4.5 är en stor skillnad.
  Det är den mest relevanta siffran för balansräkning och kassaflöde.
- Vi vet inte om Mistral Small 3.1 eller Gemma 4 på Workers AI presterar lika bra som Mistral Small 4 [Antaget].
  Ingen mätning hittades för dessa på Workers AI.
- Ingen källa anger prestanda för **svenska** dokument. Benchmarken täcker främst engelska och kinesiska
  akademiska och tryckta sidor. Svensk text med å/ä/ö och SEK-format bör testas separat [Okänt].
- Kontrollera att `Llama 3.2 Vision` kräver Metas licensgodkännande innan första anrop.

## 5. Rekommendation

| Alternativ | Kostnad engång | Kvalitet tabeller | Risk | Bedömning |
|---|---|---|---|---|
| A. Behåll Haiku för vision-OCR (nuvarande plan) | ~0,21 USD | Hög (Haiku-klass) | Låg. Redan planerat | **Rekommenderas** |
| B. Tesseract först, sedan Haiku endast för utvalda sidor | ~0,1–0,2 USD | Hög på valda sidor | Låg | **Rekommenderas tillsammans med A** |
| C. Byt till Workers AI Llama 3.2 11B | ~0,21 USD (≈ 0 med gratisnivå) | Låg (TEDS ~33) | Hög. Sämre siffror, okänd svensk prestanda | Avråds |
| D. Testa Workers AI Mistral Small 3.1 / Gemma 4 | ~0,16–0,44 USD | Okänd | Medel. Ingen mätning | Endast som experiment |

Konkret förslag:
1. Behåll B: Tesseract hittar sidor, Haiku läser de valda sidorna. Det är redan i `plan-tackning.md`.
2. Om vi vill prova Workers AI: ett litet experiment på 20 sidor med kända värden (kassa och
   kassaflöde från Bergs-rapporter och minst två andra bolag). Mät siffror rätt/fel, inte bara
   "ser rimligt ut". Jämför med Haiku på samma sidor. Byt endast om Workers AI klarar samma nivå.
3. Om Workers AI ändå ska användas: testa `mistral-small-3.1` och `gemma-4` före Llama 3.2.
4. Motivet för Workers AI blir främst vendor-samlad drift och att data stannar hos en leverantör. Det är
   inte kostnaden. Kostnaden är för låg för att påverka beslutet.

## 6. Osäkert och att verifiera

- Exakt REST-bildformat för vision-modeller (`image` vs `image_url`) och svarsnyckel.
- Svensk tabellprestanda för alla Workers AI-modeller. Ingen källa.
- Om Llama 3.2 11B och Mistral Small 3.1 kräver Paid-plan för vårt konto.
- Gemma 4 och Mistral Small 3.1: neuron-pris ej angivet på prissidan.
- Haiku 5.5 pris ($0,10/$0,50) kommer från projektets plan och är inte verifierat här.
- Vilket datum Nanonets-leaderboarden har. Modellversioner kan ha ändrats sedan dess.

## Källor

- Modellkatalog: https://developers.cloudflare.com/workers-ai/models/ (daterad 2026-08-12)
- Prissida: https://developers.cloudflare.com/workers-ai/platform/pricing/ (daterad 2026-10-20)
- Llama 3.2 11B Vision: https://developers.cloudflare.com/workers-ai/models/llama-3.2-11b-vision-instruct/
- Llama 4 Scout: https://developers.cloudflare.com/workers-ai/models/llama-4-scout-17b-16e-instruct/
- Mistral Small 3.1: https://developers.cloudflare.com/workers-ai/models/mistral-small-3.1-24b-instruct/
- Gemma 4 26B: https://developers.cloudflare.com/workers-ai/models/gemma-4-26b-a4b-it/
- Llama vision-tutorial (daterad 2026-08-25): https://developers.cloudflare.com/workers-ai/tutorials/llama-vision-tutorial/
- REST API (daterad 2026-09-15): https://developers.cloudflare.com/workers-ai/get-started/rest-api/
- Rate limits (daterad 2026-09-17): https://developers.cloudflare.com/workers-ai/platform/limits/
- JSON mode (daterad 2026-09-14): https://developers.cloudflare.com/workers-ai/features/json-mode/
- toMarkdown (daterad 2026-04-21): https://developers.cloudflare.com/workers-ai/features/markdown-conversion/
- OmniDocBench-leaderboard (Nanonets, snapshot okänd): https://benchmarking.nanonets.com/benchmarks/omnidocbench
- OmniDocBench (källkod och benchmark): https://github.com/OpenDataLab/OmniDocBench
