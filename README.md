# Capital Radar

Källspårbar screener för svenska small caps: rapporter blir runway från idag, pressmeddelanden
efter rapporten justerar den (emissioner, lån, kreditramar, återbetalningar, strukturella
händelser), och bolag med 3–9 månaders runway rankas som prospekt.

- Webb och API: `https://radar.perplexinvest.se/v2/` (bakom Cloudflare Access).
- Kommandon: `python -m radar --help` (`collect`, `run --latest`, `news --latest`, `qualify`,
  `schedule`, `serve`).
- Drift: se `deploy/README.md`. Planer och beslut: `docs/`.
- Regler för kod och agenter: `AGENTS.md`.
