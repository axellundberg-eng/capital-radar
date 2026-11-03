# Capital Radar

Källspårbar screener, runway och transaktionsflöde för Nasdaq Stockholm Small
Cap, First North och NGM. Efterträder `Screener-analys-hus`, som arkiveras.

## Status

| Fas | Innehåll | Status |
| --- | --- | --- |
| 1 | Grund: FastAPI, Docker, CI, backup och tunnel | Klar |
| 2 | Screener + runway (källor, dokument, extraction, runway) | Ej påbörjad |
| 3 | Transaktionsflöde (feed, rättningar, läsmodell, radar) | Ej påbörjad |
| 4 | Webbgränssnitt och API | Ej påbörjad |
| 5 | Drift på Hetzner med Cloudflare Access | Ej påbörjad |

## Lokal utveckling

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\python -m capital_radar
```

Öppna `http://127.0.0.1:8000/docs` för API:t och `/health` för driftstatus.

## Drift

- Varje push till `main` kör testerna och bygger
  `ghcr.io/axellundberg-eng/capital-radar`, följt av ett smoke-test.
- Servern kör `docker compose`. Appen lyssnar bara på `127.0.0.1:8000`; publik
  åtkomst sker enbart via Cloudflare Tunnel med Cloudflare Access framför.
- Data ligger i `/srv/capital-radar/data` på servern (ägd av uid 1000).
  Databasen `screener.db` har samma format som i den gamla tjänsten och kan
  kopieras över oförändrad.
- Litestream replikerar databasen löpande till Hetzner Object Storage.
- Hemligheter ligger i `/srv/capital-radar/.env`, aldrig i Git. Se
  `.env.example`.
