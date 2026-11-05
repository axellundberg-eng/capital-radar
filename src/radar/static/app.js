"use strict";

// [nyckel, etikett, förklaring (tooltip), färgklass]
const BANDS = [
  ["3_9", "3–9 mån", "Målgrupp", "target"],
  ["under_3", "Under 3 mån", "Kassan räcker mindre än 3 månader", "urgent"],
  ["acute", "Akut", "Kassan räcker mindre än 1 månad", "urgent"],
  ["structural", "Strukturell förändring", "Kontrollera manuellt: rapportens siffror kan vara inaktuella", "warn"],
  ["raising", "Pågående kapitalanskaffning", "Emission eller lån annonserat, utfall inte offentliggjort än", "warn"],
  ["exhausted", "Kassa slut", "Kassan borde vara slut enligt rapporten: kolla finansiering", "warn"],
  ["9_18", "9–18 mån", "Bevaka", "watch"],
  ["over_18", "Över 18 mån", "", "watch"],
  ["positive", "Kassaflödespositiva", "", "muted"],
  ["missing", "Saknar data", "", "muted"],
  ["all", "Alla", "", "muted"],
];
const KIND = { cash: "Likvida medel", undrawn_credit: "Outnyttjad kredit", short_term_investments: "Kortfristiga placeringar", operating_cash_flow: "Kassaflöde, löpande verksamhet",
  capex: "Investeringar (capex)", investing_cash_flow: "Kassaflöde, investeringar",
  post_period_outflow: "Utbetalning efter perioden", post_period_inflow: "Inbetalning efter perioden",
  debt_due_12m: "Lån som förfaller inom 12 mån" };

const TYPE = { rights_issue: "Företrädesemission", directed_issue: "Riktad emission", bridge_loan: "Brygglån",
  loan: "Lån", credit_facility: "Kreditfacilitet", convertible: "Konvertibel", warrants: "Teckningsoptioner",
  repayment: "Amortering", other: "Övrigt" };
const TYPE_ORDER = Object.keys(TYPE);
const EVENT_TYPE = { reverse_acquisition: "Omvänt förvärv", merger: "Fusion", acquisition: "Förvärv", name_change: "Namnbyte",
  public_offer: "Offentligt bud", delisting: "Avnotering", bankruptcy: "Konkurs", reorganisation: "Rekonstruktion",
  liquidation: "Likvidation", spin_off: "Avknoppning", other: "Övrig händelse" };
const STRUCT_WARNING = "Bolaget har förändrats efter rapporten (se händelserna nedan) och runway bygger på äldre siffror. Kontrollera manuellt.";
const STATUS = { announced: "Annonserad", completed: "Genomförd", cancelled: "Avbruten" };
const STATUS_CHIP = { announced: "pågår", completed: "genomförd", cancelled: "avbruten" };
const ACUTE_TEXT = "Kassan räcker mindre än en månad enligt senaste rapporten. Bolaget behöver kapital nu eller har kreditlöften (visas om de finns i rapporten).";

const state = { items: [], band: "3_9", query: "", selected: null, overview: null, view: "list", prospects: null };
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

function pool() {
  return state.items;
}

function structuralChips(item) {
  return (item.structural_events || []).map((e) =>
    `<span class="chip structural" title="${escapeHtml(date(e.date))}">${EVENT_TYPE[e.type] || e.type} · ${STATUS_CHIP[e.status] || e.status}</span>`).join("");
}

function chips(item) {
  const events = (item.financing_events || []).filter((e) => e.status !== "cancelled");
  const structural = structuralChips(item);
  if (!events.length && !structural) return "";
  return `<small class="chips">${structural}${events.map((e) => `<span class="chip ${e.status}" title="${escapeHtml(date(e.date))}">${TYPE[e.type] || e.type} · ${STATUS_CHIP[e.status] || e.status}</span>`).join("")}</small>`;
}

function visible() {
  const q = state.query.trim().toLowerCase();
  return pool().filter((item) =>
    (state.band === "all" || item.band === state.band)
    && (!q || item.name.toLowerCase().includes(q)));
}

function runwayCell(item) {
  if (item.status !== "ok") return '<span class="pill muted">Saknar data</span>';
  if (item.cash_flow_positive) return '<span class="pill good">Positivt</span>';
  if (item.band === "structural") return `<span class="pill band-structural" title="${escapeHtml(STRUCT_WARNING)}">Strukturell förändring</span><small>Gammal runway ${months(item.runway_today)}</small>`;
  if (item.band === "exhausted") return '<span class="pill band-exhausted" title="Kassan borde vara slut sedan rapporten">Slut?</span>';
  if (item.band === "acute") return `<span class="pill band-acute" title="${ACUTE_TEXT}">${months(item.runway_today)}</span><small>Akut: under 1 mån</small>${creditNote(item)}`;
  const recent = item.recent_runway_today != null
    ? `<small title="I senaste periodens takt">${months(item.recent_runway_today)} i senaste takt</small>` : "";
  const raise = item.band === "raising" ? '<small><span class="pill raise">Pågående kapitalanskaffning</span></small>' : "";
  const pill = item.band === "raising" ? "band-under_3" : `band-${item.band}`;
  return `<span class="pill ${pill}">${months(item.runway_today)}</span>${recent}${raise}${creditNote(item)}`;
}

function creditNote(item) {
  return item.runway_with_credit != null
    ? `<small title="Outnyttjad kredit ${msek(item.available_credit)}">${months(item.runway_with_credit)} inkl. kredit</small>` : "";
}

function renderBands() {
  const counts = {};
  pool().forEach((item) => { counts[item.band] = (counts[item.band] || 0) + 1; });
  $("#bands").innerHTML = BANDS.map(([key, label, hint]) => {
    const count = key === "all" ? pool().length : (counts[key] || 0);
    return `<button class="band ${state.band === key ? "active" : ""}" data-band="${key}" title="${escapeHtml(hint)}">
      <strong>${count}</strong><span>${label}</span></button>`;
  }).join("");
}

function changeBadge(item) {
  if (!item.change || !item.changed_at) return "";
  const days = (Date.now() - Date.parse(item.changed_at)) / 86400000;
  if (days > 7) return "";
  return ` <span class="pill new" title="Ändrat ${date(item.changed_at)}">${escapeHtml(item.change)}</span>`;
}

function renderRows() {
  const rows = visible();
  $("#rows").innerHTML = rows.length ? rows.map((item) => `
    <tr data-id="${item.id}" class="${state.selected === item.id ? "selected" : ""}" tabindex="0">
      <td><strong>${escapeHtml(item.name)}</strong>${changeBadge(item)}<small>${escapeHtml(item.market || "")}</small>${chips(item)}</td>
      <td class="num">${runwayCell(item)}</td>
      <td class="num">${item.cash_flow_positive ? "—" : msek(item.monthly_burn)}</td>
      <td class="num">${msek(item.cash)}</td>
      <td>${date(item.period_end)}<small>${months(item.months_since_report)} sedan</small></td>
      <td>${item.flags.length ? `<span class="warn" title="${escapeHtml(item.flags.map((f) => state.overview?.flag_text?.[f] || f).join(", "))}">${item.flags.length} ⚠</span>` : ""}</td>
    </tr>`).join("") : '<tr><td colspan="6" class="empty">Inga bolag i den här gruppen.</td></tr>';
}

function scoreClass(score) { return score >= 70 ? "high" : score >= 45 ? "mid" : "low"; }
function pct(value) { return value == null ? "okänt" : `${Math.round(value * 100)} %`; }

function lastIssue(item) {
  const e = item.last_issue;
  if (!e) return '<span class="source">Ingen känd</span>';
  return `${TYPE[e.type] || e.type}<small>${STATUS[e.status] || e.status} · ${date(e.date)}${e.amount != null ? " · " + msek(e.amount) : ""}</small>`;
}

function prospectRow(item) {
  return `
    <tr data-id="${item.id}" class="${state.selected === item.id ? "selected" : ""}" tabindex="0">
      <td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.market || "")}</small></td>
      <td class="num"><span class="score ${scoreClass(item.score)}" title="${escapeHtml(item.summary)}">${item.score}</span></td>
      <td class="num"><span class="pill band-${item.band}">${months(item.runway_today)}</span></td>
      <td class="num">${msek(item.capital_need)}</td>
      <td class="num">${item.market_cap ? msek(item.market_cap) : "okänt"}</td>
      <td class="num">${pct(item.share)}</td>
      <td>${lastIssue(item)}</td>
    </tr>`;
}

function structuralRow(item) {
  return `
    <tr data-id="${item.id}" class="${state.selected === item.id ? "selected" : ""}" tabindex="0">
      <td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.market || "")}</small></td>
      <td><small class="chips">${structuralChips(item)}</small></td>
      <td>${date(item.period_end)}<small>${months(item.months_since_report)} sedan</small></td>
      <td class="num">${months(item.runway_today)}</td>
    </tr>`;
}

function renderProspects() {
  if (!state.prospects) return;
  const { items, raising, structural } = state.prospects;
  $("#structuralRows").innerHTML = structural.length ? structural.map(structuralRow).join("")
    : '<tr><td colspan="4" class="empty">Inga bolag med strukturell förändring.</td></tr>';
  $("#prospectRows").innerHTML = items.length ? items.map(prospectRow).join("")
    : '<tr><td colspan="7" class="empty">Inga prospekt just nu.</td></tr>';
  $("#raisingRows").innerHTML = raising.length ? raising.map(prospectRow).join("")
    : '<tr><td colspan="7" class="empty">Inga bolag med pågående kapitalanskaffning.</td></tr>';
}

function qualificationSection(d) {
  const q = d.qualification;
  if (!q || state.view !== "prospects") return "";
  const items = q.checks.map((c) => `<li class="st-${c.status}"><div><strong>${escapeHtml(c.label)}</strong>
    <span class="st">${{ pass: "ok", warn: "varning", fail: "underkänd", info: "info" }[c.status]}</span></div>
    <span>${escapeHtml(c.reason)}</span></li>`).join("");
  return `<h3>Kvalificering · poäng ${q.score}/100</h3><p class="explain">${escapeHtml(q.summary)}</p>
    <ul class="facts checks">${items}</ul>`;
}

async function loadProspects() {
  try {
    const data = await api("api/prospects");
    state.prospects = { items: data.items, raising: data.raising || [], structural: data.structural || [] };
    renderProspects();
  } catch (error) {
    $("#prospectRows").innerHTML = `<tr><td colspan="7" class="empty">Prospekten kunde inte läsas (${escapeHtml(error.message)}).</td></tr>`;
  }
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $("#listView").hidden = view !== "list";
  $("#prospectView").hidden = view !== "prospects";
  if (view === "prospects" && !state.prospects) loadProspects();
}

function explanation(d) {
  if (d.status !== "ok") return "Runway kan inte beräknas: rapporten saknar kassa eller kassaflöde.";
  if (d.cash_flow_positive) return "Bolaget har positivt kassaflöde efter investeringar och bränner inte kassa.";
  if (d.band === "structural") return `${STRUCT_WARNING} Rapporten visade kassa ${msek(d.cash)} den ${date(d.period_end)} och en burn på ${msek(d.monthly_burn)} per månad (${months(d.runway_today)} kvar enligt de siffrorna).`;
  if (d.band === "acute") return `Kassan var ${msek(d.cash)} den ${date(d.period_end)} med en burn på ${msek(d.monthly_burn)} per månad. ${ACUTE_TEXT}`;
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

function financingNote(d) {
  const parts = [];
  if (d.confirmed_financing > 0) parts.push(` Bekräftad finansiering efter rapporten på ${msek(d.confirmed_financing)} ingår i beräkningen.`);
  if (d.flags.includes("raise_announced")) parts.push(` En ytterligare finansiering på ${msek(d.announced_financing)} är annonserad men inte genomförd och ingår inte.`);
  return parts.join("");
}

function financingItem(e) {
  const drawn = e.type === "credit_facility" && e.drawn_amount != null ? ` · utnyttjat ${msek(e.drawn_amount)}` : "";
  return `<li><div><strong>${STATUS[e.status] || e.status}</strong>
    <span>${e.amount != null ? msek(e.amount) : "belopp saknas"}${drawn} · ${date(e.date)}</span></div>
    ${e.quote ? `<q>${escapeHtml(e.quote)}</q>` : ""}
    ${e.url ? `<a href="${escapeHtml(e.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(e.title || "Pressmeddelande")}</a>` : escapeHtml(e.title || "")}</li>`;
}

function creditReserve(d) {
  if (!(d.available_credit > 0)) return "";
  return `<p class="source credit">Kreditreserv: ${msek(d.available_credit)} — runway inkl. kredit ${months(d.runway_with_credit)}</p>`;
}

function financingSection(d) {
  const events = d.financing_events || [];
  const credit = creditReserve(d);
  if (!events.length) return `<h3>Finansiering efter rapporten</h3><p class="source">Inga finansieringshändelser funna.</p>${credit}`;
  const groups = TYPE_ORDER.filter((type) => events.some((e) => e.type === type)).map((type) =>
    `<h4>${TYPE[type]}</h4><ul class="facts">${events.filter((e) => e.type === type).map(financingItem).join("")}</ul>`);
  return `<h3>Finansiering efter rapporten</h3>${credit}${groups.join("")}`;
}

function structuralItem(e) {
  const counterparty = e.counterparty ? ` · ${escapeHtml(e.counterparty)}` : "";
  return `<li><div><strong>${EVENT_TYPE[e.type] || e.type} · ${STATUS[e.status] || e.status}</strong>
    <span>${date(e.date)}${counterparty}</span></div>
    ${e.quote ? `<q>${escapeHtml(e.quote)}</q>` : ""}
    ${e.url ? `<a href="${escapeHtml(e.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(e.title || "Pressmeddelande")}</a>` : escapeHtml(e.title || "")}</li>`;
}

function structuralSection(d) {
  const events = d.structural_events || [];
  if (!events.length) return "";
  const warning = d.flags.includes("structural_change") ? `<p class="explain struct-warning">${escapeHtml(STRUCT_WARNING)}</p>` : "";
  return `<h3>Företagshändelser efter rapporten</h3>${warning}<ul class="facts">${events.map(structuralItem).join("")}</ul>`;
}

function afterPeriodSection(d) {
  const rows = d.facts.filter((f) => f.kind === "post_period_outflow" || f.kind === "post_period_inflow");
  const debt = d.debt_due_12m > 0
    ? `<p class="source warn">Lån som förfaller inom 12 mån: ${msek(d.debt_due_12m)} — runway efter amortering ${months(d.runway_after_debt)}</p>` : "";
  if (!rows.length) return debt;
  const items = rows.map((f) => `<li><div><strong>${f.kind === "post_period_outflow" ? "Ut" : "In"} · ${msek(f.value)}</strong>
    <span>${f.period_start ? date(f.period_start) + " · " : ""}${pdfLink(d, f.page)}</span></div>
    ${f.quote ? `<q>${escapeHtml(f.quote)}</q>` : ""}</li>`).join("");
  return `<h3>Händelser efter perioden</h3><ul class="facts">${items}</ul>${debt}`;
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
    <p class="explain">${explanation(d)}${financingNote(d)}</p>
    ${flags ? `<ul class="flags">${flags}</ul>` : ""}
    ${qualificationSection(d)}
    ${structuralSection(d)}
    ${afterPeriodSection(d)}
    ${financingSection(d)}
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
  renderProspects();
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
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) { setView(viewButton.dataset.view); return; }
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
load();
