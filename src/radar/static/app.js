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
  repayment: "Amortering", set_off: "Kvittningsemission", other: "Övrigt" };
const TYPE_ORDER = Object.keys(TYPE);
const EVENT_TYPE = { reverse_acquisition: "Omvänt förvärv", merger: "Fusion", acquisition: "Förvärv", name_change: "Namnbyte",
  public_offer: "Offentligt bud", delisting: "Avnotering", bankruptcy: "Konkurs", reorganisation: "Rekonstruktion",
  liquidation: "Likvidation", spin_off: "Avknoppning", other: "Övrig händelse" };
const STRUCT_WARNING = "Bolaget har förändrats efter rapporten (se händelserna nedan) och runway bygger på äldre siffror. Kontrollera manuellt.";
const STATUS = { announced: "Annonserad", completed: "Genomförd", cancelled: "Avbruten" };
const STATUS_CHIP = { announced: "pågår", completed: "genomförd", cancelled: "avbruten" };
const ACUTE_TEXT = "Kassan räcker mindre än en månad enligt senaste rapporten. Bolaget behöver kapital nu eller har kreditlöften (visas om de finns i rapporten).";

const expandedProspects = new Set();
const state = { items: [], band: "3_9", query: "", selected: null, overview: null, view: "list", prospects: null };
const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// Belopp i miljoner med valuta: rapportens valuta för kassa och burn, SEK för prospektens behov och marknadsvärde.
function msek(value, currency) {
  if (value == null) return "—";
  const m = value / 1e6;
  return `${m.toLocaleString("sv-SE", { maximumFractionDigits: Math.abs(m) < 10 ? 1 : 0 })} M${(currency || "SEK").toUpperCase()}`;
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

const REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function skeleton(cols, rows = 5) {
  const cells = Array.from({ length: cols }, (_, i) => `<td><span class="sk" style="width:${i === 0 ? 70 : 40 + ((i * 17) % 30)}%"></span></td>`).join("");
  return Array.from({ length: rows }, () => `<tr class="skel">${cells}</tr>`).join("");
}

// Rad med pro forma-runway om den säkrade finansieringen kommer in (visas bara när fältet finns).
function proForma(item) {
  if (item.runway_pro_forma == null) return "";
  const secured = item.secured_financing != null ? ` (${msek(item.secured_financing, item.currency)})` : "";
  return `<small class="proforma" title="Säkrad finansiering som ännu inte ingår i kassan">Pro forma ${months(item.runway_pro_forma)} om säkrad del${secured} kommer in</small>`;
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
  if (item.band === "acute") return `<span class="pill band-acute" title="${ACUTE_TEXT}">${months(item.runway_today)}</span><small>Akut: under 1 mån</small>${proForma(item)}${creditNote(item)}`;
  const recent = item.recent_runway_today != null
    ? `<small title="I senaste periodens takt">${months(item.recent_runway_today)} i senaste takt</small>` : "";
  const raise = item.band === "raising" ? '<small><span class="pill raise">Pågående kapitalanskaffning</span></small>' : "";
  const pill = item.band === "raising" ? "band-under_3" : `band-${item.band}`;
  return `<span class="pill ${pill}">${months(item.runway_today)}</span>${recent}${proForma(item)}${raise}${creditNote(item)}`;
}

// Runway idag inkluderar outnyttjad kreditlinje; här visas hur mycket som är kassan utan kredit.
function creditNote(item) {
  return item.available_credit > 0 && item.runway_cash_today != null
    ? `<small title="Outnyttjad kredit ${msek(item.available_credit, item.currency)}">${months(item.runway_cash_today)} utan kredit</small>` : "";
}

function renderBands() {
  const counts = {};
  pool().forEach((item) => { counts[item.band] = (counts[item.band] || 0) + 1; });
  $("#bands").innerHTML = BANDS.map(([key, label, hint, tone]) => {
    const count = key === "all" ? pool().length : (counts[key] || 0);
    return `<button class="band tone-${tone} ${state.band === key ? "active" : ""}" data-band="${key}" title="${escapeHtml(hint)}" aria-pressed="${state.band === key}">
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
      <td class="num">${item.cash_flow_positive ? "—" : msek(item.monthly_burn, item.currency)}</td>
      <td class="num">${msek(item.cash, item.currency)}</td>
      <td>${date(item.period_end)}<small>${months(item.months_since_report)} sedan</small></td>
      <td>${item.flags.length ? `<span class="warn" title="${escapeHtml(item.flags.map((f) => state.overview?.flag_text?.[f] || f).join(", "))}">${item.flags.length} ⚠</span>` : ""}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">${state.query.trim()
    ? `Inga bolag matchar "${escapeHtml(state.query.trim())}" i den här gruppen. Rensa sökningen eller välj <strong>Alla</strong> ovan.`
    : "Inga bolag i den här gruppen. Välj en annan runway-grupp ovan."}</td></tr>`;
  $("#rows").classList.add("anim");
  updateTabs();
}

// Markerar vald rad utan att bygga om tabellerna (bevarar öppna rader och animationer).
function markSelected() {
  document.querySelectorAll("tr[data-id]").forEach((tr) => tr.classList.toggle("selected", Number(tr.dataset.id) === state.selected));
}

function updateTabs() {
  const set = (view, n) => { const el = document.querySelector(`.view[data-view="${view}"] .cnt`); if (el) el.textContent = n == null ? "" : n; };
  set("list", state.items.length || null);
  set("prospects", state.prospects ? state.prospects.items.length : null);
}

function scoreClass(score) { return score >= 70 ? "high" : score >= 45 ? "mid" : "low"; }
function pct(value) { return value == null ? "okänt" : `${Math.round(value * 100)} %`; }

function lastIssue(item) {
  const e = item.last_issue;
  if (!e) return '<span class="source">Ingen känd</span>';
  return `${TYPE[e.type] || e.type}<small>${STATUS[e.status] || e.status} · ${date(e.date)}${e.amount != null ? " · " + msek(e.amount, e.currency) : ""}</small>`;
}

function nextReport(item) {
  return item.next_report ? `<small title="${escapeHtml(item.next_report.title || "")}">Nästa rapport ${escapeHtml(date(item.next_report.date))}</small>` : "";
}

function prospectRow(item, expandable) {
  const open = expandable && expandedProspects.has(item.id);
  const chev = expandable ? `<span class="chev" aria-hidden="true"></span>` : "";
  const aria = expandable ? ` aria-expanded="${open}"` : "";
  return `
    <tr data-id="${item.id}" class="${state.selected === item.id ? "selected" : ""}${expandable ? " expandable" : ""}${open ? " open" : ""}" tabindex="0"${aria}>
      <td>${chev}<span class="namecell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.market || "")}</small>${nextReport(item)}</span></td>
      <td class="num"><span class="score ${scoreClass(item.score)}" title="${escapeHtml(item.summary)}">${item.score}</span></td>
      <td class="num"><span class="pill band-${item.band}">${months(item.runway_today)}</span>${proForma(item)}</td>
      <td class="num">${msek(item.capital_need)}</td>
      <td class="num">${item.market_cap ? msek(item.market_cap) : "okänt"}</td>
      <td class="num">${pct(item.share)}</td>
      <td class="num">${item.emissions_24m ?? "–"}</td>
      <td>${lastIssue(item)}</td>
    </tr>`;
}

// Villkor och utfall för en emission: garanti, teckning, pris, rabatt, utspädning, TO-villkor.
function terms(e) {
  const cur = e.currency;
  const parts = [];
  if (e.subscription_commitments != null) parts.push(`åtaganden ${msek(e.subscription_commitments, cur)}`);
  if (e.guarantee_amount != null) parts.push(`garanti ${msek(e.guarantee_amount, cur)}${e.guarantee_fee_pct != null ? ` (ersättning ${e.guarantee_fee_pct} %)` : ""}`);
  if (e.subscription_rate_pct != null) parts.push(`teckningsgrad ${e.subscription_rate_pct} %`);
  if (e.guarantee_utilized != null) parts.push(`utnyttjad garanti ${msek(e.guarantee_utilized, cur)}`);
  if (e.issue_costs != null) parts.push(`kostnader ${msek(e.issue_costs, cur)}`);
  if (e.paid_in_shares) parts.push(`varav ${msek(e.paid_in_shares, cur)} till garanter i aktier (kvittning, ingen kassa)`);
  if (e.issue_price != null) parts.push(`kurs ${e.issue_price.toLocaleString("sv-SE")} ${cur || "SEK"}`);
  if (e.discount_pct != null) parts.push(`rabatt ${e.discount_pct} %`);
  if (e.dilution_pct != null) parts.push(`utspädning ${e.dilution_pct.toLocaleString("sv-SE")} %`);
  if (e.exercise_price != null) parts.push(`lösenpris ${e.exercise_price.toLocaleString("sv-SE")} ${cur || "SEK"}`);
  if (e.exercise_start || e.exercise_end) parts.push(`lösen ${date(e.exercise_start)}–${date(e.exercise_end)}`);
  const guarantors = e.guarantors ? ` · Garanter: ${escapeHtml(e.guarantors)}` : "";
  return parts.length || guarantors ? `<small>${parts.join(" · ")}${guarantors}</small>` : "";
}

// Utfällbar rad under ett prospekt: de senaste finansieringshändelserna i detalj.
function prospectDetail(item) {
  const list = (item.recent_financing || []).map((e) => `
    <li><div><span>${escapeHtml(e.date)} · ${TYPE[e.type] || e.type} · ${STATUS[e.status] || e.status}</span>
      <strong>${e.amount != null ? msek(e.amount, e.currency) : "belopp saknas"}</strong></div>
      ${terms(e)}
      <small>Rådgivare: ${e.advisers ? escapeHtml(e.advisers) : "ej angivet"}</small>
      ${e.quote ? `<q>${escapeHtml(e.quote)}</q>` : ""}</li>`).join("");
  const n = item.need_breakdown;
  const need = n ? `<p class="source">Kapitalbehov (SEK): burn 12 mån ${msek(n.operating_12m)} + skulder inom 12 mån ${msek(n.debt_due_12m)}
      − kassa ${msek(n.cash_today)} − kredit ${msek(n.available_credit)} = ${msek(n.net_need)} netto;
      med emissionskostnad ${msek(n.issue_costs)} blir det ${msek(n.gross_need)}.${n.currency !== "SEK"
        ? ` Omräknat från ${n.currency} (${n.sek_per_unit.toLocaleString("sv-SE")} kr).` : ""}</p>` : "";
  const basis = { ltm: "rullande tolv månader", annual: "helåret", longest_period: "längsta perioden i rapporten" }[item.burn_basis];
  const report = item.next_report ? ` · Nästa rapport: ${escapeHtml(item.next_report.date)}` : "";
  const a = item.authorization;
  const auth = a ? `<p class="source">Bemyndigande (stämma ${escapeHtml(date(a.meeting_date))}):
      ${a.max_dilution_pct != null ? `högst ${a.max_dilution_pct} % av aktierna` : a.max_shares != null ? `högst ${Number(a.max_shares).toLocaleString("sv-SE")} aktier` : "omfattning ej angiven"}${a.directed_allowed ? ", även riktad emission" : ""}.</p>` : "";
  return `
    <tr class="expand"><td colspan="9"><div class="xwrap"><div class="xinner">
      <p class="source">Kassa utan kredit: ${months(item.runway_cash_today)} · Outnyttjad kredit: ${msek(item.available_credit, item.currency)} ·
        Emissioner senaste 24 mån: ${item.emissions_24m ?? 0}${basis ? ` · Burn räknad på ${basis}` : ""}${report}</p>
      ${need}${auth}
      <h3>Varför poängen</h3>
      <ul class="checks">${(item.checks || []).map((c) => `<li class="st-${c.status}">
        <span class="st">${{ pass: "Ok", warn: "Varning", fail: "Underkänd", info: "Info" }[c.status] || c.status}</span> ${escapeHtml(c.label)}
        <span>${escapeHtml(c.reason)}</span></li>`).join("")}</ul>
      <h3>Finansiering senaste 24 mån</h3>
      ${list ? `<ul class="facts">${list}</ul>` : '<p class="source">Inga finansieringshändelser de senaste 24 månaderna.</p>'}
    </div></div></td></tr>`;
}

let closing = null;
// Bara ett prospekt är öppet åt gången: att öppna ett stänger det föregående.
function toggleProspect(id) {
  const wasOpen = expandedProspects.has(id);
  const wrap = document.querySelector("#prospectRows .xwrap");
  expandedProspects.clear();
  if (!wasOpen) expandedProspects.add(id);
  clearTimeout(closing);
  if (wasOpen && wrap && !REDUCED) {
    // Stäng mjukt: animera först, bygg sedan om raderna.
    wrap.classList.add("closing");
    const tr = document.querySelector(`#prospectRows tr[data-id="${id}"]`);
    if (tr) { tr.classList.remove("open"); tr.setAttribute("aria-expanded", "false"); }
    closing = setTimeout(() => renderProspects(false), 200);
  } else {
    renderProspects(false);
  }
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

function renderProspects(animate = true) {
  if (!state.prospects) return;
  const { items, raising, structural } = state.prospects;
  $("#structuralRows").innerHTML = structural.length ? structural.map(structuralRow).join("")
    : '<tr><td colspan="4" class="empty">Inga bolag med strukturell förändring just nu.</td></tr>';
  $("#prospectRows").innerHTML = items.length
    ? items.map((item) => prospectRow(item, true) + (expandedProspects.has(item.id) ? prospectDetail(item) : "")).join("")
    : '<tr><td colspan="8" class="empty">Inga prospekt just nu. Bolag hamnar här när runway understiger 9 månader utan annonserad emission. Se Lista för alla bolag.</td></tr>';
  $("#raisingRows").innerHTML = raising.length ? raising.map((item) => prospectRow(item, false)).join("")
    : '<tr><td colspan="8" class="empty">Inga bolag med pågående kapitalanskaffning.</td></tr>';
  ["#prospectRows", "#structuralRows", "#raisingRows"].forEach((sel) => $(sel).classList.toggle("anim", animate));
  updateTabs();
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
  if (state.prospects || state.loadingProspects) return;
  state.loadingProspects = true;
  $("#prospectRows").innerHTML = skeleton(8);
  $("#structuralRows").innerHTML = skeleton(4, 2);
  $("#raisingRows").innerHTML = skeleton(8, 2);
  try {
    const data = await api("api/prospects");
    state.prospects = { items: data.items, raising: data.raising || [], structural: data.structural || [] };
    renderProspects();
  } catch (error) {
    $("#prospectRows").innerHTML = `<tr><td colspan="8" class="empty">Prospekten kunde inte läsas (${escapeHtml(error.message)}). Ladda om sidan för att försöka igen.</td></tr>`;
    $("#structuralRows").innerHTML = "";
    $("#raisingRows").innerHTML = "";
  } finally { state.loadingProspects = false; }
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view").forEach((b) => { const on = b.dataset.view === view; b.classList.toggle("active", on); b.setAttribute("aria-selected", on); });
  $("#listView").hidden = view !== "list";
  $("#prospectView").hidden = view !== "prospects";
  if (view === "prospects") loadProspects();
}

function explanation(d) {
  if (d.status !== "ok") return "Runway kan inte beräknas: rapporten saknar kassa eller kassaflöde.";
  if (d.cash_flow_positive) return "Bolaget har positivt kassaflöde efter investeringar och bränner inte kassa.";
  if (d.band === "structural") return `${STRUCT_WARNING} Rapporten visade kassa ${msek(d.cash, d.currency)} den ${date(d.period_end)} och en burn på ${msek(d.monthly_burn, d.currency)} per månad (${months(d.runway_today)} kvar enligt de siffrorna).`;
  if (d.band === "acute") return `Kassan var ${msek(d.cash, d.currency)} den ${date(d.period_end)} med en burn på ${msek(d.monthly_burn, d.currency)} per månad. ${ACUTE_TEXT}`;
  if (d.band === "exhausted") return `Kassan ${msek(d.cash, d.currency)} den ${date(d.period_end)} räckte ${months(d.runway_at_report)} med en burn på`
    + ` ${msek(d.monthly_burn, d.currency)} per månad, men ${months(d.months_since_report)} har gått. Bolaget har med stor sannolikhet`
    + " tagit in ny finansiering efter rapporten. Kontrollera pressmeddelanden innan kontakt.";
  const chosen = d.periods.find((p) => Math.abs(p.monthly_burn - d.monthly_burn) < 1) || d.periods.at(-1);
  return `Kassan var ${msek(d.cash, d.currency)} den ${date(d.period_end)}. Bolaget bränner ${msek(d.monthly_burn, d.currency)} per månad`
    + ` (baserat på ${chosen?.months ?? "?"} månader). Sedan dess har ${months(d.months_since_report)} gått,`
    + ` så kvarvarande runway är ${months(d.runway_today)}.`
    + (d.recent_monthly_burn ? ` Senaste perioden bränner dock ${msek(d.recent_monthly_burn, d.currency)} per månad;`
      + ` i den takten räcker kassan ${months(d.recent_runway_today)}.` : "");
}

function financingNote(d) {
  const parts = [];
  if (d.confirmed_financing > 0) parts.push(` Bekräftad finansiering efter rapporten på ${msek(d.confirmed_financing, d.currency)} ingår i beräkningen.`);
  if (d.flags.includes("raise_announced")) parts.push(` En ytterligare finansiering på ${msek(d.announced_financing, d.currency)} är annonserad men inte genomförd och ingår inte.`);
  return parts.join("");
}

function financingItem(e, d) {
  const drawn = e.type === "credit_facility" && e.drawn_amount != null ? ` · utnyttjat ${msek(e.drawn_amount, d.currency)}` : "";
  return `<li><div><strong>${STATUS[e.status] || e.status}</strong>
    <span>${e.amount != null ? msek(e.amount, d.currency) : "belopp saknas"}${drawn} · ${date(e.date)}</span></div>
    ${e.quote ? `<q>${escapeHtml(e.quote)}</q>` : ""}
    ${e.url ? `<a href="${escapeHtml(e.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(e.title || "Pressmeddelande")}</a>` : escapeHtml(e.title || "")}</li>`;
}

function creditReserve(d) {
  if (!(d.available_credit > 0)) return "";
  return `<p class="source credit">Kreditreserv: ${msek(d.available_credit, d.currency)} — runway inkl. kredit ${months(d.runway_with_credit)}</p>`;
}

function financingSection(d) {
  const events = d.financing_events || [];
  const credit = creditReserve(d);
  if (!events.length) return `<h3>Finansiering efter rapporten</h3><p class="source">Inga finansieringshändelser funna.</p>${credit}`;
  const groups = TYPE_ORDER.filter((type) => events.some((e) => e.type === type)).map((type) =>
    `<h4>${TYPE[type]}</h4><ul class="facts">${events.filter((e) => e.type === type).map((e) => financingItem(e, d)).join("")}</ul>`);
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
    ? `<p class="source warn">Lån som förfaller inom 12 mån: ${msek(d.debt_due_12m, d.currency)} — runway efter amortering ${months(d.runway_after_debt)}</p>` : "";
  if (!rows.length) return debt;
  const items = rows.map((f) => `<li><div><strong>${f.kind === "post_period_outflow" ? "Ut" : "In"} · ${msek(f.value, d.currency)}</strong>
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
    <td class="num">${msek(p.operating_cash_flow, d.currency)}</td><td class="num">${msek(-p.investments, d.currency)}</td>
    <td class="num">${msek(p.monthly_burn, d.currency)}</td></tr>`).join("");
  const facts = d.facts.map((f) => `<li><div><strong>${KIND[f.kind] || f.kind}</strong>
    <span>${msek(f.value, d.currency)} · ${f.period_start ? `${date(f.period_start)}–` : ""}${date(f.period_end)} · ${pdfLink(d, f.page)}</span></div>
    ${f.quote ? `<q>${escapeHtml(f.quote)}</q>` : ""}</li>`).join("");
  const flags = Object.values(d.flag_text || {}).map((text) => `<li>${escapeHtml(text)}</li>`).join("");
  const pro = d.runway_pro_forma != null ? `
      <div class="wide proforma-kpi"><span>Pro forma runway</span><strong>${months(d.runway_pro_forma)}</strong>
        <small>om säkrad finansiering${d.secured_financing != null ? ` (${msek(d.secured_financing, d.currency)})` : ""} kommer in</small></div>` : "";
  const nextRep = d.next_report ? `<p class="source">Nästa rapport: ${escapeHtml(date(d.next_report.date))}${d.next_report.title ? ` (${escapeHtml(d.next_report.title)})` : ""}</p>` : "";
  $("#detail").innerHTML = `<div class="fade-in">
    <div class="detail-head">
      <h2>${escapeHtml(d.name)}</h2>
      <span>${escapeHtml(d.market || "")} ${d.segment ? "· " + escapeHtml(d.segment) : ""}</span>
    </div>
    <div class="kpis">
      <div><span>Runway idag</span><strong class="band-${d.band}">${d.cash_flow_positive ? "Positivt" : months(d.runway_today)}</strong></div>
      <div><span>Vid rapporten</span><strong>${months(d.runway_at_report)}</strong></div>
      <div><span>Burn / mån</span><strong>${d.cash_flow_positive ? "—" : msek(d.monthly_burn, d.currency)}</strong></div>
      <div><span>Kassa</span><strong>${msek(d.cash, d.currency)}</strong></div>${pro}
    </div>
    ${nextRep}
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
    <ul class="facts">${facts}</ul></div>`;
}

async function select(id) {
  state.selected = id;
  markSelected();
  $("#detail").innerHTML = '<div class="detail-skel"><span class="sk" style="width:60%;height:22px"></span><span class="sk" style="width:35%"></span><span class="sk block"></span><span class="sk block"></span><span class="sk" style="width:90%"></span><span class="sk" style="width:80%"></span></div>';
  try {
    const d = await api(`api/companies/${id}`);
    if (state.selected !== id) return;
    renderDetail(d);
  } catch (error) {
    if (state.selected === id) $("#detail").innerHTML = `<div class="placeholder">Kunde inte läsa bolaget (${escapeHtml(error.message)}).</div>`;
  }
}

async function load() {
  $("#rows").innerHTML = skeleton(6);
  try {
    const [overview, list] = await Promise.all([api("api/overview"), api("api/companies")]);
    state.overview = overview;
    state.items = list.items;
    const done = overview.reports?.extracted || 0;
    $("#meta").textContent = `${overview.with_result} av ${overview.companies} bolag beräknade · ${done} rapporter extraherade`;
    renderBands();
    renderRows();
    loadProspects();
  } catch (error) {
    $("#rows").innerHTML = `<tr><td colspan="6" class="empty">Underlaget kunde inte läsas (${escapeHtml(error.message)}). Ladda om sidan för att försöka igen.</td></tr>`;
  }
}

document.addEventListener("click", (event) => {
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) { setView(viewButton.dataset.view); return; }
  const bandButton = event.target.closest("[data-band]");
  if (bandButton) { state.band = bandButton.dataset.band; renderBands(); renderRows(); return; }
  const prospect = event.target.closest("#prospectRows tr[data-id]");
  // Prospekt: klick öppnar både detaljpanelen till höger (som i listan) och raden under med mer information.
  if (prospect) { const id = Number(prospect.dataset.id); toggleProspect(id); select(id); return; }
  const row = event.target.closest("tr[data-id]");
  if (row) select(Number(row.dataset.id));
});
document.addEventListener("keydown", (event) => {
  const row = event.target.closest?.("tr[data-id]");
  if (!row || event.key !== "Enter") return;
  const id = Number(row.dataset.id);
  if (row.closest("#prospectRows")) toggleProspect(id);
  select(id);
});
$("#search").addEventListener("input", (event) => { state.query = event.target.value; renderRows(); });
load();
