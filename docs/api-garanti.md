# API-kontrakt: fliken Garanti

Delas av backend (web.py) och gränssnittet (static/app.js). Alla belopp i SEK om inget annat anges.
Fält som saknas skickas som null; gränssnittet visar då "—" eller döljer raden.

## GET /api/search?q=<text>&limit=20
Sök bolag på namn, ticker eller ISIN (skiftlägesokänsligt, delsträng). Tom q ger [].
[{"id": 313, "name": "Scibase Holding", "symbol": "SCIB", "isin": "SE...", "market": "...",
  "band": "3_9", "runway_today": 6.4}]

## GET /api/guarantee/{company_id}
{
  "company": {"id", "name", "symbol", "isin", "market", "segment", "sector"},
  "runway": {"band", "runway_today", "runway_cash_today", "runway_pro_forma", "cash_today", "monthly_burn",
             "currency", "period_end", "months_since_report", "burn_basis", "flags", "flag_text"},
  "need": {...need_breakdown från qualify, SEK...} eller null,
  "market": {"market_cap", "price", "price_change_1m_pct", "price_change_3m_pct", "price_change_12m_pct",
             "avg_daily_turnover_3m", "avg_daily_volume_3m", "high_52w", "low_52w", "as_of", "source", "error"},
  "capacity": {"need_share_of_market_cap_pct", "turnover_days_for_need", "assessment": "lätt|möjlig|svår|okänd",
               "reasons": ["..."]},
  "insiders": [{"date", "person", "role", "transaction", "volume", "price", "value", "currency"}],   nyast först, max 50
  "insider_summary": {"months": 6, "buys": SEK, "sells": SEK, "net": SEK, "buyers": n, "sellers": n},
  "emissions_24m": n,
  "financing": [...recent_financing-poster med villkor, dilution_pct, paid_in_shares...],
  "authorization": {...} eller null,
  "next_report": {"date", "title"} eller null,
  "updated_at": "ISO-tid"
}
404 om bolaget saknas.
