"use strict";

const BANDS = [
  ["3_9", "3–9 mån", "Målgrupp"],
  ["under_3", "Under 3 mån", "Akut"],
  ["exhausted", "Kassa slut enl. rapport", "Kolla finansiering"],
  ["9_18", "9–18 mån", "Bevaka"],
  ["over_18", "Över 18 mån", ""],
  ["positive", "Kassaflödespositiva", ""],
  ["missing", "Saknar data", ""],
  ["all", "Alla", ""],
];
const KIND = { cash: "Likvida medel", short_term_investments: "Kortfristiga placeringar", operating_cash_flow: "Kassaflöde, löpande verksamhet",
  capex: "Investeringar (capex)", investing_cash_flow: "Kassaflöde, investeringar" };

const state = { items: [], band: "3_9", query: "", hideStale: false, selected: null, overview: null };
const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function msek(value) {
  if (value == null) return "—";
  const m = value / 1e6;
  return `${m.toLocaleString("sv-SE", { maximumFractionDigits: Math.abs(m) < 10 ? 1 : 0 })} MSEK`;
}
function months(value) {
  return value == null ? "—" : `${value.toLocaleString("sv-SE", { maximumFractionDigits: 1 })} mån`;
}
function date(value) { return value ? String(value).slice(0, 10) : "—"; }

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" }, cache: "no-store" });
  if (!response.ok) throw new Error(`API ${response.status}`);
  return response.json();
}

function visible() {
  const q = state.query.trim().toLowerCase();
  return state.items.filter((item) =>
    (state.band === "all" || item.band === state.band)
    && (!q || item.name.toLowerCase().includes(q))
    && (!state.hideStale || !item.flags.includes("stale_report")));
}

function runwayCell(item) {
  if (item.status !== "ok") return '<span class="pill muted">Saknar data</span>';
  if (item.cash_flow_positive) return '<span class="pill good">Positivt</span>';
  if (item.band === "exhausted") return '<span class="pill band-exhausted" title="Kassan borde vara slut sedan rapporten">Slut?</span>';
  const recent = item.recent_runway_today != null
    ? `<small title="I senaste periodens takt">${months(item.recent_runway_today)} i senaste takt</small>` : "";
  return `<span class="pill band-${item.band}">${months(item.runway_today)}</span>${recent}`;
}

function renderBands() {
  const counts = state.overview?.bands || {};
  $("#bands").innerHTML = BANDS.map(([key, label, hint]) => {
    const count = key === "all" ? state.items.length : (counts[key] || 0);
    return `<button class="band ${state.band === key ? "active" : ""}" data-band="${key}">
      <strong>${count}</strong><span>${label}</span>${hint ? `<small>${hint}</small>` : ""}</button>`;
  }).join("");
}

function renderRows() {
  const rows = visible();
  $("#rows").innerHTML = rows.length ? rows.map((item) => `
    <tr data-id="${item.id}" class="${state.selected === item.id ? "selected" : ""}" tabindex="0">
      <td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.market || "")}</small></td>
      <td class="num">${runwayCell(item)}</td>
      <td class="num">${item.cash_flow_positive ? "—" : msek(item.monthly_burn)}</td>
      <td class="num">${msek(item.cash)}</td>
      <td>${date(item.period_end)}<small>${months(item.months_since_report)} sedan</small></td>
      <td>${item.flags.length ? `<span class="warn" title="${escapeHtml(item.flags.map((f) => state.overview?.flag_text?.[f] || f).join(", "))}">${item.flags.length} ⚠</span>` : ""}</td>
    </tr>`).join("") : '<tr><td colspan="6" class="empty">Inga bolag i den här gruppen.</td></tr>';
}

function explanation(d) {
  if (d.status !== "ok") return "Runway kan inte beräknas: rapporten saknar kassa eller kassaflöde.";
  if (d.cash_flow_positive) return "Bolaget har positivt kassaflöde efter investeringar och bränner inte kassa.";
  if (d.band === "exhausted") return `Kassan ${msek(d.cash)} den ${date(d.period_end)} räckte ${months(d.runway_at_report)} med en burn på`
    + ` ${msek(d.monthly_burn)} per månad, men ${months(d.months_since_report)} har gått. Bolaget har med stor sannolikhet`
    + " tagit in ny finansiering efter rapporten. Kontrollera pressmeddelanden innan kontakt.";
  const chosen = d.periods.find((p) => Math.abs(p.monthly_burn - d.monthly_burn) < 1) || d.periods.at(-1);
  return `Kassan var ${msek(d.cash)} den ${date(d.period_end)}. Bolaget bränner ${msek(d.monthly_burn)} per månad`
    + ` (baserat på ${chosen?.months ?? "?"} månader). Sedan dess har ${months(d.months_since_report)} gått,`
    + ` så kvarvarande runway är ${months(d.runway_today)}.`
    + (d.recent_monthly_burn ? ` Senaste perioden bränner dock ${msek(d.recent_monthly_burn)} per månad;`
      + ` i den takten räcker kassan ${months(d.recent_runway_today)}.` : "");
}

function pdfLink(d, page) {
  if (!d.pdf_url) return "";
  const label = page ? `s. ${page}` : "PDF";
  return `<a href="${escapeHtml(d.pdf_url)}${page ? `#page=${page}` : ""}" target="_blank" rel="noopener noreferrer">${label}</a>`;
}

function renderDetail(d) {
  const periods = d.periods.map((p) => `<tr><td>${p.months} mån från ${date(p.start)}</td>
    <td class="num">${msek(p.operating_cash_flow)}</td><td class="num">${msek(-p.investments)}</td>
    <td class="num">${msek(p.monthly_burn)}</td></tr>`).join("");
  const facts = d.facts.map((f) => `<li><div><strong>${KIND[f.kind] || f.kind}</strong>
    <span>${msek(f.value)} · ${f.period_start ? `${date(f.period_start)}–` : ""}${date(f.period_end)} · ${pdfLink(d, f.page)}</span></div>
    ${f.quote ? `<q>${escapeHtml(f.quote)}</q>` : ""}</li>`).join("");
  const flags = Object.values(d.flag_text || {}).map((text) => `<li>${escapeHtml(text)}</li>`).join("");
  $("#detail").innerHTML = `
    <div class="detail-head">
      <h2>${escapeHtml(d.name)}</h2>
      <span>${escapeHtml(d.market || "")} ${d.segment ? "· " + escapeHtml(d.segment) : ""}</span>
    </div>
    <div class="kpis">
      <div><span>Runway idag</span><strong class="band-${d.band}">${d.cash_flow_positive ? "Positivt" : months(d.runway_today)}</strong></div>
      <div><span>Vid rapporten</span><strong>${months(d.runway_at_report)}</strong></div>
      <div><span>Burn / mån</span><strong>${d.cash_flow_positive ? "—" : msek(d.monthly_burn)}</strong></div>
      <div><span>Kassa</span><strong>${msek(d.cash)}</strong></div>
    </div>
    <p class="explain">${explanation(d)}</p>
    ${flags ? `<ul class="flags">${flags}</ul>` : ""}
    <h3>Perioder</h3>
    <table class="mini"><thead><tr><th>Period</th><th class="num">Löpande</th><th class="num">Investeringar</th><th class="num">Burn / mån</th></tr></thead>
      <tbody>${periods || '<tr><td colspan="4">Inga perioder</td></tr>'}</tbody></table>
    <h3>Källor i rapporten</h3>
    <p class="source">${escapeHtml(d.report_title)} · publicerad ${date(d.published_at)} · ${pdfLink(d)}</p>
    <ul class="facts">${facts}</ul>`;
}

async function select(id) {
  state.selected = id;
  renderRows();
  $("#detail").innerHTML = '<div class="placeholder">Laddar…</div>';
  try {
    renderDetail(await api(`api/companies/${id}`));
  } catch (error) {
    $("#detail").innerHTML = `<div class="placeholder">Kunde inte läsa bolaget (${escapeHtml(error.message)}).</div>`;
  }
}

async function load() {
  try {
    const [overview, list] = await Promise.all([api("api/overview"), api("api/companies")]);
    state.overview = overview;
    state.items = list.items;
    const done = overview.reports?.extracted || 0;
    $("#meta").textContent = `${overview.with_result} av ${overview.companies} bolag beräknade · ${done} rapporter extraherade`;
    renderBands();
    renderRows();
  } catch (error) {
    $("#rows").innerHTML = `<tr><td colspan="6" class="empty">Underlaget kunde inte läsas (${escapeHtml(error.message)}).</td></tr>`;
  }
}

document.addEventListener("click", (event) => {
  const bandButton = event.target.closest("[data-band]");
  if (bandButton) { state.band = bandButton.dataset.band; renderBands(); renderRows(); return; }
  const row = event.target.closest("tr[data-id]");
  if (row) select(Number(row.dataset.id));
});
document.addEventListener("keydown", (event) => {
  const row = event.target.closest?.("tr[data-id]");
  if (row && event.key === "Enter") select(Number(row.dataset.id));
});
$("#search").addEventListener("input", (event) => { state.query = event.target.value; renderRows(); });
$("#hideStale").addEventListener("change", (event) => { state.hideStale = event.target.checked; renderRows(); });
load();
