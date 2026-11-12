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

const garanti = { id: null, results: [], active: -1, timer: null, seq: 0, searching: false, data: null, guarantor: null };
const expandedProspects = new Set();
// bands: valda runway-grupper (tom mängd = alla). facets: andra filterlagret, flerval per grupp.
const state = { items: [], bands: new Set(["3_9"]), facets: { sector: new Set(), marker: new Set() }, query: "",
  selected: null, overview: null, view: "list", prospects: null,
  sort: { key: "runway", dir: 1 }, quick: null, tab: "overview" };
// Webbläsarens lagring för bekvämligheter (bevakade bolag, tema). Sidan fungerar även utan den.
const store = {
  get(key, fallback) { try { const raw = localStorage.getItem(key); return raw == null ? fallback : JSON.parse(raw); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* privat läge */ } },
};
const watch = new Set(store.get("radar.watch", []));
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
  if (value == null) return "—";
  if (value > 60) return "över 60 mån";   // bolag nära noll i burn: exakta tal som 1 225 mån ser ut som fel
  return `${value.toLocaleString("sv-SE", { maximumFractionDigits: 1 })} mån`;
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

// ---------- Filter: runway-grupper (lager 1) och bransch/markörer (lager 2), båda med flerval ----------
const FACETS = ["sector", "marker"];
const FACET_LABEL = { sector: "Bransch", marker: "Markörer" };
const TIER_LABEL = { capital: "Kapitalbehov", half: "Indirekt kapitalbehov", context: "Sammanhang" };

function bandMatch(item) { return !state.bands.size || state.bands.has(item.band); }

function itemKeys(item, facet) {
  if (facet === "sector") return [item.sector?.subsector || "_none"];
  return (item.markers || []).map((m) => m.key);
}

// ELLER inom en grupp, OCH mellan grupper. `except` hoppar över en grupp (för antalen på dess knappar).
function facetMatch(item, except = null) {
  return FACETS.every((facet) => facet === except || !state.facets[facet].size
    || itemKeys(item, facet).some((key) => state.facets[facet].has(key)));
}

function facetOptions(facet) {
  if (facet === "sector") return [...(state.overview?.sectors || []).map(([key, label]) => [key, label, ""]), ["_none", "Ej klassad", ""]];
  return (state.overview?.markers || []).map(([key, label, tier]) => [key, label, tier]);
}

// Bara alternativ som har bolag i urvalet visas (valda alternativ visas alltid, så att de kan väljas bort).
function renderFacets(host, base) {
  if (!host) return;
  const groups = FACETS.map((facet) => {
    const counts = {};
    base.filter((item) => facetMatch(item, facet)).forEach((item) =>
      new Set(itemKeys(item, facet)).forEach((key) => { counts[key] = (counts[key] || 0) + 1; }));
    const options = facetOptions(facet).filter(([key]) => counts[key] || state.facets[facet].has(key));
    if (!options.length) return "";
    let tier = null;
    const buttons = options.map(([key, label, t]) => {
      const sub = facet === "marker" && t !== tier ? `<span class="facet-sub">${TIER_LABEL[t] || ""}</span>` : "";
      tier = t;
      const on = state.facets[facet].has(key);
      return `${sub}<button type="button" class="facet${on ? " on" : ""}" data-facet="${facet}" data-key="${escapeHtml(key)}" aria-pressed="${on}">${escapeHtml(label)} <small>${counts[key] || 0}</small></button>`;
    }).join("");
    return `<div class="facet-group" role="group" aria-label="${FACET_LABEL[facet]}"><span class="facet-label">${FACET_LABEL[facet]}</span>${buttons}</div>`;
  }).join("");
  const any = FACETS.some((facet) => state.facets[facet].size);
  host.innerHTML = groups + (any ? '<button type="button" class="facet-clear" data-facet-clear>Rensa filter</button>' : "");
}

// Filtret ur adressen kan innehålla okända nycklar (gamla länkar): ta bort dem när listorna är kända.
function pruneFacets() {
  FACETS.forEach((facet) => {
    const known = new Set(facetOptions(facet).map(([key]) => key));
    state.facets[facet] = new Set([...state.facets[facet]].filter((key) => known.has(key)));
  });
  state.bands = new Set([...state.bands].filter((key) => BANDS.some(([band]) => band === key && key !== "all")));
}

// Sortering: klick på en kolumnrubrik. Tomma värden hamnar alltid sist.
const SORTS = {
  name: (i) => i.name.toLowerCase(),
  runway: (i) => (i.status !== "ok" ? null : i.cash_flow_positive ? Infinity : i.runway_today),
  burn: (i) => (i.cash_flow_positive ? null : i.monthly_burn),
  cash: (i) => i.cash,
  report: (i) => i.period_end,
  flags: (i) => i.flags.length,
};
const SORT_DEFAULT_DIR = { name: 1, runway: 1, burn: -1, cash: -1, report: -1, flags: -1 };

function sorted(rows) {
  const get = SORTS[state.sort.key] || SORTS.runway;
  return [...rows].sort((a, b) => {
    const x = get(a), y = get(b);
    if (x == null || y == null) return (x == null) - (y == null);
    return (x < y ? -1 : x > y ? 1 : 0) * state.sort.dir || a.name.localeCompare(b.name, "sv");
  });
}

// Snabbfilter från nyckeltalskorten: bevakade bolag eller bolag som ändrats senaste veckan.
function changedRecently(item) {
  return Boolean(item.change && item.changed_at && (Date.now() - Date.parse(item.changed_at)) / 864e5 <= 7);
}
function quickMatch(item) {
  return !state.quick || (state.quick === "watch" ? watch.has(item.id) : changedRecently(item));
}

function visible() {
  const q = state.query.trim().toLowerCase();
  return sorted(pool().filter((item) => bandMatch(item) && facetMatch(item) && quickMatch(item)
    && (!q || item.name.toLowerCase().includes(q))));
}

function starButton(item) {
  const on = watch.has(item.id);
  return `<button type="button" class="star${on ? " on" : ""}" data-star="${item.id}" aria-pressed="${on}" aria-label="Bevaka ${escapeHtml(item.name)}" title="${on ? "Sluta bevaka" : "Bevaka bolaget"}">★</button>`;
}

// Nyckeltalskort överst: genvägar till de vyer som oftast behövs.
function renderInsights() {
  const host = $("#insights");
  if (!host) return;
  const items = pool();
  const need = state.prospects ? state.prospects.need.length
    : items.filter((i) => ["3_9", "under_3", "acute"].includes(i.band) && i.need_level
      && !(i.markers || []).some((m) => m.key === "recent_raise")).length;
  const going = items.filter((i) => (i.markers || []).some((m) => m.key === "going_concern")).length;
  const goingOn = state.facets.marker.size === 1 && state.facets.marker.has("going_concern");
  const card = (key, label, value, sub, on) => `<button type="button" class="insight${on ? " on" : ""}" data-insight="${key}" aria-pressed="${on}">
    <span>${label}</span><strong>${value}</strong><small>${sub}</small></button>`;
  host.innerHTML = card("need", "Uttalat kapitalbehov", need, "prospekt med stämmobeslut, plan eller lån · visa →", false)
    + card("going", "Osäkerhet om fortsatt drift", going, "enligt bolagens senaste rapport", goingOn)
    + card("changed", "Ändrade senaste 7 dagarna", items.filter(changedRecently).length, "bytt grupp eller ny finansiering", state.quick === "changed")
    + card("watch", "Bevakade", watch.size, watch.size ? "dina stjärnmärkta bolag" : "stjärnmärk bolag för att följa dem", state.quick === "watch");
}

function renderList() {
  renderInsights();
  renderBands();
  renderFacets($("#facets"), pool().filter((item) => bandMatch(item) && quickMatch(item)));
  renderRows();
  syncHash();
}

// Exporterar det som visas (lista eller prospekt) som CSV för Excel (semikolon, decimalkomma).
function exportCsv() {
  const prospects = state.view === "prospects" && state.prospects;
  const rows = prospects
    ? [...state.prospects.need, ...state.prospects.items, ...state.prospects.recent].filter((i) => facetMatch(i))
    : visible();
  const num = (v) => (v == null ? "" : String(Math.round(v * 10) / 10).replace(".", ","));
  const bandLabel = Object.fromEntries(BANDS.map(([key, label]) => [key, label]));
  const head = ["Bolag", "Lista", "Bransch", "Grupp", "Runway idag (mån)", "Burn per månad", "Kassa", "Valuta",
    "Rapportperiod", "Markörer", "Poäng", "Kapitalbehov (SEK)", "Marknadsvärde (SEK)"];
  const lines = rows.map((i) => [i.name, i.market || "", i.sector?.label || "", bandLabel[i.band] || i.band,
    num(i.runway_today), num(i.monthly_burn), num(i.cash), i.currency || "SEK", date(i.period_end),
    (i.markers || []).map((m) => m.label).join(", "), i.score ?? "", num(i.capital_need), num(i.market_cap)]);
  const cell = (v) => `"${String(v).replace(/"/g, '""')}"`;
  const csv = "\ufeff" + [head, ...lines].map((r) => r.map(cell).join(";")).join("\r\n");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  link.download = `capital-radar-${prospects ? "prospekt" : "lista"}-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

// ---------- Kassadiagram i detaljpanelen ----------
const MONTH_MS = 30.4375 * 864e5;
const CHART = { W: 400, H: 150, L: 6, R: 6, T: 18, B: 24 };
function monthLabel(t) { return new Date(t).toLocaleDateString("sv-SE", { month: "short", year: "numeric" }); }

// Kassan från rapporten till idag och framåt tills den är slut, vid oförändrad burn.
function cashChart(d) {
  if (d.status !== "ok" || d.cash_flow_positive || !(d.monthly_burn > 0) || d.cash == null || !d.period_end) return "";
  const { W, H, L, R, T, B } = CHART;
  const start = Date.parse(d.period_end), now = Math.max(Date.now(), start);
  const today = d.cash_today ?? Math.max(0, d.cash - d.monthly_burn * (d.months_since_report || 0));
  const left = d.runway_cash_today ?? today / d.monthly_burn;
  const points = today > 0
    ? [[start, d.cash], [now, today], [now + left * MONTH_MS, 0]]
    : [[start, d.cash], [Math.min(now, start + (d.cash / d.monthly_burn) * MONTH_MS), 0], [now, 0]];
  const zero = today > 0 ? points[2][0] : points[1][0];
  const credit = d.available_credit > 0 && d.runway_today > left ? now + d.runway_today * MONTH_MS : null;
  const end = Math.max(credit || 0, zero, now) + MONTH_MS;
  const top = Math.max(d.cash, today, 1);
  const x = (t) => (L + ((W - L - R) * (t - start)) / (end - start)).toFixed(1);
  const y = (v) => (T + (H - T - B) * (1 - v / top)).toFixed(1);
  const line = points.map(([t, v]) => `${x(t)},${y(v)}`).join(" ");
  const base = y(0);
  const zeroText = today > 0 ? `Kassan slut ≈ ${monthLabel(zero)}` : `Kassan slut sedan ${monthLabel(zero)}`;
  const zx = Number(x(zero));
  const anchor = zx > W * 0.72 ? "end" : zx < W * 0.28 ? "start" : "middle";
  const data = escapeHtml(JSON.stringify({ start, end, points, cur: d.currency || "SEK" }));
  return `<figure class="cash-chart" data-chart="${data}">
    <header><span>Kassa över tid <small>vid oförändrad burn</small></span><strong class="chart-read">${zeroText}</strong></header>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Kassan var ${msek(d.cash, d.currency)} den ${date(d.period_end)} och är cirka ${msek(today, d.currency)} idag. ${zeroText}.">
      <polygon class="cc-area" points="${x(start)},${base} ${line} ${x(points[2][0])},${base}"/>
      <line class="cc-axis" x1="${L}" x2="${W - R}" y1="${base}" y2="${base}"/>
      ${credit ? `<line class="cc-credit" x1="${x(zero)}" x2="${x(credit)}" y1="${base}" y2="${base}"><title>Outnyttjad kredit räcker till ${monthLabel(credit)}</title></line>` : ""}
      <line class="cc-today" x1="${x(now)}" x2="${x(now)}" y1="${T - 5}" y2="${base}"/>
      <polyline class="cc-line" points="${line}"/>
      <circle class="cc-dot" cx="${x(start)}" cy="${y(d.cash)}" r="3.5"/>
      <circle class="cc-dot" cx="${x(now)}" cy="${y(today)}" r="3.5"/>
      <circle class="cc-zero" cx="${x(zero)}" cy="${base}" r="4"/>
      <line class="cc-cursor" x1="0" x2="0" y1="${T - 5}" y2="${base}" hidden/>
      ${zx > 90 ? `<text x="${L}" y="${H - 6}">${monthLabel(start)}</text>` : ""}
      <text x="${x(now)}" y="9" text-anchor="middle">Idag</text>
      <text x="${zx}" y="${H - 6}" text-anchor="${anchor}">${monthLabel(zero)}</text>
    </svg>
    ${credit ? `<figcaption>Streckad linje: outnyttjad kredit räcker till ${monthLabel(credit)}.</figcaption>` : ""}</figure>`;
}

// Pekaren över diagrammet visar beräknad kassa för den månaden.
function chartHover(event) {
  const svg = event.target.closest?.(".cash-chart svg");
  if (!svg) return;
  const figure = svg.parentElement, data = JSON.parse(figure.dataset.chart), box = svg.getBoundingClientRect();
  const inner = CHART.W - CHART.L - CHART.R;
  const fx = Math.min(1, Math.max(0, (((event.clientX - box.left) / box.width) * CHART.W - CHART.L) / inner));
  const t = data.start + fx * (data.end - data.start);
  let value = 0;
  for (let i = 0; i < data.points.length - 1; i += 1) {
    const [t0, v0] = data.points[i], [t1, v1] = data.points[i + 1];
    if (t >= t0 && t <= t1) { value = t1 === t0 ? v1 : v0 + ((v1 - v0) * (t - t0)) / (t1 - t0); break; }
  }
  const cursor = svg.querySelector(".cc-cursor"), cx = CHART.L + fx * inner;
  cursor.removeAttribute("hidden");
  cursor.setAttribute("x1", cx);
  cursor.setAttribute("x2", cx);
  const read = figure.querySelector(".chart-read");
  read.dataset.rest ??= read.textContent;
  read.textContent = `${monthLabel(t)}: ${msek(value, data.cur)}`;
}
function chartLeave(event) {
  const svg = event.target.closest?.(".cash-chart svg");
  if (!svg || event.relatedTarget?.closest?.(".cash-chart svg") === svg) return;
  svg.querySelector(".cc-cursor").setAttribute("hidden", "");
  const read = svg.parentElement.querySelector(".chart-read");
  if (read.dataset.rest) read.textContent = read.dataset.rest;
}

// Tunn stapel under runway-talet: andel av 18 månader, färgad efter grupp.
function runwayBar(item) {
  if (item.status !== "ok" || item.cash_flow_positive || item.runway_today == null) return "";
  const share = Math.max(3, Math.min(100, (item.runway_today / 18) * 100));
  return `<span class="rbar band-${item.band}" aria-hidden="true"><i style="width:${share.toFixed(0)}%"></i></span>`;
}

// Urvalet sparas i adressen (#lista?band=3_9,under_3&bransch=medtech&markor=egm_called) så att det kan delas.
function syncHash() {
  if (state.view === "garanti") return;
  const params = new URLSearchParams();
  if (state.view === "list") params.set("band", state.bands.size ? [...state.bands].join(",") : "alla");
  if (state.facets.sector.size) params.set("bransch", [...state.facets.sector].join(","));
  if (state.facets.marker.size) params.set("markor", [...state.facets.marker].join(","));
  const query = params.toString().replace(/%2C/g, ",");
  const hash = `#${state.view === "prospects" ? "prospekt" : "lista"}${query ? "?" + query : ""}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
}

function readHash() {
  const m = location.hash.match(/^#(lista|prospekt)(?:\?(.*))?$/);
  if (!m) return null;
  const params = new URLSearchParams(m[2] || "");
  const split = (value) => (value ? value.split(",").map((v) => v.trim()).filter(Boolean) : []);
  if (params.has("band")) state.bands = new Set(params.get("band") === "alla" ? [] : split(params.get("band")));
  state.facets.sector = new Set(split(params.get("bransch")));
  state.facets.marker = new Set(split(params.get("markor")));
  return m[1] === "prospekt" ? "prospects" : "list";
}

// Stämmobeslut om emission eller uttalad plan att ta in kapital, som inte är en annonserad emission än.
const SIGNAL = {
  stamma_issue: ["Stämman har beslutat om emission", "raise"],
  egm_called: ["Kallelse till extra stämma om emission", "raise"],
  going_concern: ["Osäkerhet om fortsatt drift", "raise"],
  plan: ["Planerad kapitalanskaffning", "raise"],
  evaluating: ["Utvärderar finansiering", "raise soft"],
};

function signalPill(item) {
  const s = item.capital_signal;
  if (!s || item.band === "raising" || !SIGNAL[s.kind]) return "";
  const [label, cls] = SIGNAL[s.kind];
  const when = s.timing ? ` · ${escapeHtml(s.timing)}` : "";
  return `<small><span class="pill ${cls}" title="${escapeHtml(s.quote || "")}">${label}${when}</span></small>`;
}

function signalSection(d) {
  const s = d.capital_signal;
  if (!s || d.band === "raising" || !SIGNAL[s.kind]) return "";
  const source = s.source === "report" ? "rapporten" : "pressmeddelandet";
  const link = s.url ? ` · <a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">Öppna ${source} →</a>` : "";
  const intro = s.kind === "stamma_issue"
    ? "En bolagsstämma har beslutat om en emission, men ingen genomförd emission har registrerats efter beslutet."
    : s.kind === "egm_called"
      ? "Bolaget har kallat till extra bolagsstämma med ett emissionsärende på dagordningen. Ingen genomförd emission har registrerats efter kallelsen."
    : s.kind === "going_concern"
      ? "Senaste rapporten anger väsentlig osäkerhet om fortsatt drift: bolaget behöver ny finansiering."
    : s.kind === "plan"
      ? `Bolaget har sagt att det ska ta in kapital${s.timing ? ` (${escapeHtml(s.timing)})` : ""}, men ingen emission är annonserad.`
      : "Bolaget säger att det utvärderar ny finansiering. Ingen emission är annonserad.";
  return `<div class="signal-box ${SIGNAL[s.kind][1]}"><strong>${SIGNAL[s.kind][0]}</strong>
    <p>${intro}</p>${s.quote ? `<q>${escapeHtml(s.quote)}</q>` : ""}
    <p class="source">Källa: ${source}, ${escapeHtml(date(s.published_at))}${link}</p></div>`;
}

function sectorText(item) {
  return item.sector?.label ? ` · ${escapeHtml(item.sector.label)}` : "";
}

// Alla markörer för bolaget, grupperade efter nivå, med källa och datum.
function markersSection(d) {
  const list = d.markers || [];
  if (!list.length) return "";
  const items = list.map((m) => `<li class="tier-${m.tier}"><div><strong>${escapeHtml(m.label)}</strong>
    <span class="tier">${TIER_LABEL[m.tier] || ""}</span></div>
    ${m.detail ? `<span>${escapeHtml(m.detail)}</span>` : ""}
    <small>${m.date ? escapeHtml(date(m.date)) : ""}${m.url ? `${m.date ? " · " : ""}<a href="${escapeHtml(m.url)}" target="_blank" rel="noopener noreferrer">Källa →</a>` : ""}</small></li>`).join("");
  return `<h3>Markörer</h3><ul class="facts markers">${items}</ul>`;
}

function runwayCell(item) {
  if (item.status !== "ok") return '<span class="pill muted">Saknar data</span>';
  if (item.cash_flow_positive) return `<span class="pill good">Positivt</span>${signalPill(item)}`;
  if (item.band === "structural") return `<span class="pill band-structural" title="${escapeHtml(STRUCT_WARNING)}">Strukturell förändring</span><small>Gammal runway ${months(item.runway_today)}</small>`;
  const recent = item.recent_runway_today != null
    ? `<small title="I senaste periodens takt">${months(item.recent_runway_today)} i senaste takt</small>` : "";
  if (item.band === "exhausted") return `<span class="pill band-exhausted" title="Kassan borde vara slut sedan rapporten">Slut?</span>${recent}${signalPill(item)}`;
  if (item.band === "acute") return `<span class="pill band-acute" title="${ACUTE_TEXT}">${months(item.runway_today)}</span>${runwayBar(item)}<small>Akut: under 1 mån</small>${recent}${proForma(item)}${signalPill(item)}${creditNote(item)}`;
  const raise = item.band === "raising" ? '<small><span class="pill raise">Pågående kapitalanskaffning</span></small>' : "";
  const pill = item.band === "raising" ? "band-under_3" : `band-${item.band}`;
  return `<span class="pill ${pill}">${months(item.runway_today)}</span>${runwayBar(item)}${recent}${proForma(item)}${raise}${signalPill(item)}${creditNote(item)}`;
}

// Runway idag inkluderar outnyttjad kreditlinje; här visas hur mycket som är kassan utan kredit.
function creditNote(item) {
  return item.available_credit > 0 && item.runway_cash_today != null
    ? `<small title="Outnyttjad kredit ${msek(item.available_credit, item.currency)}">${months(item.runway_cash_today)} utan kredit</small>` : "";
}

// Flerval: klick väljer till eller bort en grupp; "Alla" tömmer valet. Antalen följer bransch och markörer.
function renderBands() {
  const counts = {};
  const matching = pool().filter((item) => facetMatch(item) && quickMatch(item));
  matching.forEach((item) => { counts[item.band] = (counts[item.band] || 0) + 1; });
  $("#bands").innerHTML = BANDS.map(([key, label, hint, tone]) => {
    const count = key === "all" ? matching.length : (counts[key] || 0);
    const on = key === "all" ? !state.bands.size : state.bands.has(key);
    return `<button type="button" class="band tone-${tone}${on ? " on" : ""}" data-band="${key}" title="${escapeHtml(hint)}" aria-pressed="${on}">
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
      <td>${starButton(item)}<span class="namecell"><strong>${escapeHtml(item.name)}</strong>${changeBadge(item)}<small>${escapeHtml(item.market || "")}${sectorText(item)}</small>${chips(item)}</span></td>
      <td class="num">${runwayCell(item)}</td>
      <td class="num">${item.cash_flow_positive ? "—" : msek(item.monthly_burn, item.currency)}</td>
      <td class="num">${msek(item.cash, item.currency)}</td>
      <td>${date(item.period_end)}<small>${months(item.months_since_report)} sedan</small></td>
      <td>${item.flags.length ? `<span class="warn" title="${escapeHtml(item.flags.map((f) => state.overview?.flag_text?.[f] || f).join(", "))}">${item.flags.length} ⚠</span>` : ""}</td>
    </tr>`).join("") : `<tr><td colspan="6" class="empty">${state.quick === "watch" && !watch.size
    ? "Du bevakar inga bolag än. Klicka på stjärnan vid ett bolag för att följa det."
    : state.query.trim()
    ? `Inga bolag matchar "${escapeHtml(state.query.trim())}" i den här gruppen. Rensa sökningen eller välj <strong>Alla</strong> ovan.`
    : FACETS.some((facet) => state.facets[facet].size)
      ? "Inga bolag matchar filtren i de valda grupperna. Välj fler grupper eller rensa filtren."
      : "Inga bolag i de valda grupperna. Välj fler runway-grupper ovan."}</td></tr>`;
  $("#rows").classList.add("anim");
  document.querySelectorAll("#listView th[data-sort]").forEach((th) => th.setAttribute("aria-sort",
    th.dataset.sort === state.sort.key ? (state.sort.dir > 0 ? "ascending" : "descending") : "none"));
  const count = $("#resultCount");
  if (count) count.textContent = `${rows.length} bolag`;
  updateTabs();
}

// Markerar vald rad utan att bygga om tabellerna (bevarar öppna rader och animationer).
function markSelected() {
  document.querySelectorAll("tr[data-id]").forEach((tr) => tr.classList.toggle("selected", Number(tr.dataset.id) === state.selected));
}

function updateTabs() {
  const set = (view, n) => { const el = document.querySelector(`.view[data-view="${view}"] .cnt`); if (el) el.textContent = n == null ? "" : n; };
  set("list", state.items.length || null);
  set("prospects", state.prospects ? state.prospects.need.length + state.prospects.items.length + state.prospects.recent.length : null);
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
      <td>${chev}${starButton(item)}<span class="namecell"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.market || "")}${sectorText(item)}</small>${nextReport(item)}</span></td>
      <td class="num"><span class="score ${scoreClass(item.score)}" title="${escapeHtml(item.summary)}">${item.score}</span></td>
      <td class="num"><span class="pill band-${item.band}">${months(item.runway_today)}</span>${runwayBar(item)}${proForma(item)}${signalPill(item)}</td>
      <td class="num">${msek(item.capital_need)}</td>
      <td class="num">${item.market_cap ? msek(item.market_cap) : "okänt"}</td>
      <td class="num">${pct(item.share)}</td>
      <td class="num">${item.emissions_24m ?? "–"}</td>
      <td>${lastIssue(item)}</td>
    </tr>`;
}

// Ruta i detaljpanelen: genomförd finansiering efter rapporten och det som är annonserat men inte klart.
// Klick på rutan fäller ut affärerna bakom beloppet (capitalDetail nedan).
const DEBT_TYPES = ["loan", "bridge_loan", "convertible", "credit_facility", "repayment"];
function capitalBox(kind, label, done, announced, d, hint) {
  const value = done ? msek(done, d.currency) : "—";
  const months = done && d.monthly_burn ? ` · ${done > 0 ? "+" : ""}${(done / d.monthly_burn).toLocaleString("sv-SE", { maximumFractionDigits: 1 })} mån` : "";
  const note = announced ? `${msek(announced, d.currency)} annonserat` : "efter rapporten";
  return `<button type="button" class="kpi-btn" data-capital="${kind}" aria-expanded="false" title="${escapeHtml(hint)}">
    <span>${label}</span><strong>${value}</strong><small>${note}${months}</small><i class="kpi-chevron" aria-hidden="true"></i></button>`;
}

function capitalDetail(d, kind) {
  const events = (d.financing_events || []).filter((e) => e.type !== "set_off"
    && (kind === "debt") === DEBT_TYPES.includes(e.type));
  if (!events.length) return '<p class="source">Ingen sådan finansiering efter rapporten.</p>';
  const rows = events.map((e) => {
    const effect = e.unconverted ? "räknas inte (växelkurs saknas)"
      : e.status === "completed" ? `kassa ${msek(e.cash, d.currency)}${d.monthly_burn && e.cash ? ` · ${(e.cash / d.monthly_burn).toLocaleString("sv-SE", { maximumFractionDigits: 1 })} mån runway` : ""}`
      : e.status === "announced" ? "annonserad, ingår inte ännu" : "avbruten";
    const notes = [e.issue_costs != null ? `kostnader ${msek(e.issue_costs, d.currency)}` : (e.status === "completed" && !DEBT_TYPES.includes(e.type) && e.net_amount == null ? "kostnad enligt schablon 10 %" : ""),
      e.paid_in_shares ? `${msek(e.paid_in_shares, d.currency)} till garanter i aktier (ingen kassa)` : "",
      e.original_currency ? `omräknat från ${e.original_currency}` : ""].filter(Boolean).join(" · ");
    return `<li class="${e.status}"><div><span>${date(e.date)} · ${TYPE[e.type] || e.type}</span>
      <strong>${e.amount != null ? msek(e.amount, d.currency) : "—"}</strong></div>
      <small>${effect}${notes ? " · " + notes : ""}</small>
      ${e.url ? `<a href="${escapeHtml(e.url)}" target="_blank" rel="noopener">${escapeHtml(e.title || "Pressmeddelande")}</a>` : ""}</li>`;
  }).join("");
  return `<ul class="capital-list">${rows}</ul>`;
}

function toggleCapital(button) {
  const box = $("#capitalDetail");
  const d = state.detail;
  if (!box || !d) return;
  const open = button.getAttribute("aria-expanded") === "true";
  document.querySelectorAll("[data-capital]").forEach((b) => b.setAttribute("aria-expanded", "false"));
  if (open) { box.classList.remove("open"); return; }
  button.setAttribute("aria-expanded", "true");
  box.innerHTML = `<h4>${button.querySelector("span").textContent}</h4>${capitalDetail(d, button.dataset.capital)}`;
  box.classList.add("open");
}

// En affär kan bestå av flera pressmeddelanden (beslut, utfall, registrering): visa första och sista datum.
function roundDates(e) {
  const span = e.first_date && e.first_date !== e.date ? `${escapeHtml(date(e.first_date))} → ${escapeHtml(date(e.date))}` : escapeHtml(date(e.date));
  const count = e.press_releases > 1 ? ` <span class="muted-count" title="Antal pressmeddelanden som ingår i affären">(${e.press_releases} pm)</span>` : "";
  return span + count;
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
    <li><div><span>${roundDates(e)} · ${TYPE[e.type] || e.type} · ${STATUS[e.status] || e.status}</span>
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
  const all = state.prospects;
  renderFacets($("#pFacets"), [...all.need, ...all.items, ...all.recent, ...all.raising, ...all.structural]);
  const keep = (list) => list.filter((item) => facetMatch(item));
  const [need, items, recent, raising, structural] = [keep(all.need), keep(all.items), keep(all.recent), keep(all.raising), keep(all.structural)];
  const filtered = FACETS.some((facet) => state.facets[facet].size);
  const none = filtered ? "Inga bolag matchar filtren." : "";
  $("#structuralRows").innerHTML = structural.length ? structural.map(structuralRow).join("")
    : `<tr><td colspan="4" class="empty">${none || "Inga bolag med strukturell förändring just nu."}</td></tr>`;
  const rows = (list) => list.map((item) => prospectRow(item, true) + (expandedProspects.has(item.id) ? prospectDetail(item) : "")).join("");
  const group = (title, text) => `<tr class="group-row"><td colspan="8">${title}<small>${text}</small></td></tr>`;
  $("#prospectRows").innerHTML = need.length || items.length || recent.length
    ? (need.length ? group("Uttalat kapitalbehov", `${need.length} bolag med stämmobeslut, kallelse, plan, osäkerhet om fortsatt drift eller indirekt behov`) + rows(need) : "")
      + (need.length && items.length ? group("Övriga prospekt", `${items.length} bolag`) : "") + rows(items)
      + (recent.length ? group("Nyligen finansierade", `${recent.length} bolag som tagit in kapital de senaste 4 månaderna: troligen inget nära behov`) + rows(recent) : "")
    : `<tr><td colspan="8" class="empty">${none || "Inga prospekt just nu. Bolag hamnar här när runway understiger 9 månader utan annonserad emission. Se Lista för alla bolag."}</td></tr>`;
  $("#raisingRows").innerHTML = raising.length ? raising.map((item) => prospectRow(item, false)).join("")
    : `<tr><td colspan="8" class="empty">${none || "Inga bolag med pågående kapitalanskaffning."}</td></tr>`;
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
    state.prospects = { need: data.need || [], items: data.items, recent: data.recent || [], raising: data.raising || [], structural: data.structural || [] };
    renderProspects();
    renderInsights();
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
  $("#garantiView").hidden = view !== "garanti";
  document.querySelector(".layout").classList.toggle("garanti-mode", view === "garanti");
  if (view === "prospects") loadProspects();
  if (view === "garanti") {
    if (!location.hash.startsWith("#garanti")) history.replaceState(null, "", "#garanti" + (garanti.id ? "/" + garanti.id : ""));
    $("#gSearch").focus();
  } else syncHash();
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

// Kort, läsbar sammanfattning av affären på vanlig svenska.
function dealSummary(e, d) {
  const cur = e.currency || d.currency;
  const what = `${TYPE[e.type] || e.type}${e.amount != null ? ` på ${msek(e.amount, cur)}` : ""}`;
  const when = e.status === "completed" ? `genomfördes ${date(e.date)}` : e.status === "cancelled" ? `avbröts ${date(e.date)}` : `annonserades ${date(e.date)} och är inte genomförd`;
  const extras = [];
  if (e.guarantee_amount != null) extras.push(`garanterades till ${msek(e.guarantee_amount, cur)}`);
  if (e.subscription_rate_pct != null) extras.push(`tecknades till ${e.subscription_rate_pct} %`);
  if (e.dilution_pct != null) extras.push(`utspädning ${e.dilution_pct.toLocaleString("sv-SE")} %`);
  return `${what} ${when}${extras.length ? `. ${extras.join(", ").replace(/^./, (c) => c.toUpperCase())}` : ""}.`;
}

function financingItem(e, d) {
  const drawn = e.type === "credit_facility" && e.drawn_amount != null ? ` · utnyttjat ${msek(e.drawn_amount, d.currency)}` : "";
  const amount = e.amount != null ? msek(e.amount, d.currency) : "belopp saknas";
  const terms_ = terms(e);
  const link = e.url
    ? `<a class="src-link" href="${escapeHtml(e.url)}" target="_blank" rel="noopener noreferrer">${e.status === "completed" ? "Läs utfallet på MFN" : "Läs pressmeddelandet på MFN"} →</a>`
    : "";
  return `<li>
    <div class="fi-head"><strong>${STATUS[e.status] || e.status}</strong><span>${amount}${drawn} · ${date(e.date)}</span></div>
    <p class="fi-sum">${escapeHtml(dealSummary(e, d))}</p>
    ${terms_ ? `<div class="fi-terms">${terms_}</div>` : ""}
    ${e.quote ? `<q>${escapeHtml(e.quote)}</q>` : ""}
    ${link}</li>`;
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
  state.detail = d;
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
  const events = (d.financing_events || []).length + (d.structural_events || []).length;
  const tab = (key, label, extra = "") => `<button type="button" class="tab" role="tab" data-tab="${key}" aria-selected="${state.tab === key}">${label}${extra}</button>`;
  const pane = (key, html) => `<div data-pane="${key}" role="tabpanel"${state.tab === key ? "" : " hidden"}>${html}</div>`;
  $("#detail").innerHTML = `<div class="fade-in">
    <div class="detail-head">
      <div>
        <h2>${escapeHtml(d.name)}</h2>
        <span>${escapeHtml(d.market || "")} ${d.segment ? "· " + escapeHtml(d.segment) : ""}${sectorText(d)}</span>
      </div>
      <div class="detail-actions">
        ${starButton(d)}
        <button type="button" class="btn small" data-open-garanti="${d.id}" title="Marknadsdata, ägare, insynshandel och garantihistorik">Garantivy →</button>
      </div>
    </div>
    <div class="kpis">
      <div><span>Runway idag</span><strong class="band-${d.band}">${d.cash_flow_positive ? "Positivt" : months(d.runway_today)}</strong></div>
      <div><span>Vid rapporten</span><strong>${months(d.runway_at_report)}</strong></div>
      <div><span>Burn / mån</span><strong>${d.cash_flow_positive ? "—" : msek(d.monthly_burn, d.currency)}</strong></div>
      <div><span>Kassa</span><strong>${msek(d.cash, d.currency)}</strong></div>
      ${capitalBox("equity", "Kapital från emissioner", d.equity_after_report, d.equity_announced, d,
        "Nyemissioner, riktade emissioner och teckningsoptioner efter rapporten, netto efter kostnader. Klicka för affärerna.")}
      ${capitalBox("debt", "Lån, netto", d.debt_after_report, d.debt_announced, d,
        "Lån, brygglån, konvertibler och utnyttjad kredit efter rapporten, minus återbetalningar. Klicka för affärerna.")}${pro}
    </div>
    <div id="capitalDetail" class="capital-detail" aria-live="polite"></div>
    <div class="tabs" role="tablist" aria-label="Bolagets underlag">
      ${tab("overview", "Översikt")}${tab("financing", "Finansiering", events ? ` <small>${events}</small>` : "")}${tab("sources", "Underlag")}
    </div>
    ${pane("overview", `${cashChart(d)}
      ${nextRep}
      ${signalSection(d)}
      ${markersSection(d)}
      <p class="explain">${explanation(d)}${financingNote(d)}</p>
      ${flags ? `<ul class="flags">${flags}</ul>` : ""}
      ${qualificationSection(d)}`)}
    ${pane("financing", `${structuralSection(d)}
      ${afterPeriodSection(d)}
      ${financingSection(d)}`)}
    ${pane("sources", `<h3>Perioder</h3>
      <table class="mini"><thead><tr><th>Period</th><th class="num">Löpande</th><th class="num">Investeringar</th><th class="num">Burn / mån</th></tr></thead>
        <tbody>${periods || '<tr><td colspan="4">Inga perioder</td></tr>'}</tbody></table>
      <h3>Källor i rapporten</h3>
      <p class="source">${escapeHtml(d.report_title)} · publicerad ${date(d.published_at)} · ${pdfLink(d)}</p>
      <ul class="facts">${facts}</ul>`)}</div>`;
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
    pruneFacets();
    renderList();
    loadProspects();
  } catch (error) {
    $("#rows").innerHTML = `<tr><td colspan="6" class="empty">Underlaget kunde inte läsas (${escapeHtml(error.message)}). Ladda om sidan för att försöka igen.</td></tr>`;
  }
}

function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  try { localStorage.setItem("radar.theme", mode); } catch { /* privat läge */ }
}
function currentTheme() {
  return document.documentElement.dataset.theme
    || (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

document.addEventListener("pointermove", chartHover);
document.addEventListener("pointerout", chartLeave);
document.addEventListener("click", (event) => {
  const capital = event.target.closest("[data-capital]");
  if (capital) { toggleCapital(capital); return; }
  if (event.target.closest("#themeToggle")) { applyTheme(currentTheme() === "dark" ? "light" : "dark"); return; }
  if (event.target.closest("[data-export]")) { exportCsv(); return; }
  const star = event.target.closest("[data-star]");
  if (star) {
    const id = Number(star.dataset.star);
    if (watch.has(id)) watch.delete(id); else watch.add(id);
    store.set("radar.watch", [...watch]);
    document.querySelectorAll(`[data-star="${id}"]`).forEach((button) => {
      button.classList.toggle("on", watch.has(id));
      button.setAttribute("aria-pressed", watch.has(id));
    });
    if (state.quick === "watch") renderList(); else renderInsights();
    return;
  }
  const tabButton = event.target.closest("[data-tab]");
  if (tabButton) {
    state.tab = tabButton.dataset.tab;
    document.querySelectorAll("#detail [data-tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === state.tab));
    document.querySelectorAll("#detail [data-pane]").forEach((p) => { p.hidden = p.dataset.pane !== state.tab; });
    return;
  }
  const garantiButton = event.target.closest("[data-open-garanti]");
  if (garantiButton) { location.hash = `#garanti/${garantiButton.dataset.openGaranti}`; return; }
  const insight = event.target.closest("[data-insight]");
  if (insight) {
    const key = insight.dataset.insight;
    if (key === "need") { setView("prospects"); return; }
    if (key === "going") {
      const on = state.facets.marker.size === 1 && state.facets.marker.has("going_concern");
      state.facets.marker = new Set(on ? [] : ["going_concern"]);
      if (!on) { state.bands.clear(); state.quick = null; }
    } else {
      state.quick = state.quick === key ? null : key;
      if (state.quick) state.bands.clear();
    }
    renderList();
    renderProspects(false);
    return;
  }
  const sortHead = event.target.closest("#listView th[data-sort]");
  if (sortHead) { sortBy(sortHead.dataset.sort); return; }
  const viewButton = event.target.closest("[data-view]");
  if (viewButton) { setView(viewButton.dataset.view); return; }
  const bandButton = event.target.closest("[data-band]");
  if (bandButton) {
    const key = bandButton.dataset.band;
    if (key === "all") state.bands.clear();
    else if (state.bands.has(key)) state.bands.delete(key);
    else state.bands.add(key);
    renderList();
    return;
  }
  const facetButton = event.target.closest("[data-facet]");
  if (facetButton) {
    const set = state.facets[facetButton.dataset.facet];
    const key = facetButton.dataset.key;
    if (set.has(key)) set.delete(key); else set.add(key);
    renderList();
    renderProspects(false);
    syncHash();
    return;
  }
  if (event.target.closest("[data-facet-clear]")) {
    FACETS.forEach((facet) => state.facets[facet].clear());
    renderList();
    renderProspects(false);
    syncHash();
    return;
  }
  const prospect = event.target.closest("#prospectRows tr[data-id]");
  // Prospekt: klick öppnar både detaljpanelen till höger (som i listan) och raden under med mer information.
  if (prospect) { const id = Number(prospect.dataset.id); toggleProspect(id); select(id); return; }
  const row = event.target.closest("tr[data-id]");
  if (row) select(Number(row.dataset.id));
});
function sortBy(key) {
  state.sort = state.sort.key === key ? { key, dir: -state.sort.dir } : { key, dir: SORT_DEFAULT_DIR[key] || 1 };
  renderRows();
}

document.addEventListener("keydown", (event) => {
  // "/" hoppar till sökrutan; pil upp och ned flyttar mellan rader; Enter öppnar bolaget.
  if (event.key === "/" && !event.target.closest?.("input, textarea")) {
    const field = state.view === "garanti" ? $("#gSearch") : state.view === "list" ? $("#search") : null;
    if (field) { event.preventDefault(); field.focus(); }
    return;
  }
  const head = event.target.closest?.("#listView th[data-sort]");
  if (head && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); sortBy(head.dataset.sort); return; }
  if (event.target.closest?.("button, a, input")) return;
  const row = event.target.closest?.("tr[data-id]");
  if (row && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
    let next = row;
    do { next = event.key === "ArrowDown" ? next.nextElementSibling : next.previousElementSibling; } while (next && !next.dataset.id);
    if (next) { event.preventDefault(); next.focus(); }
    return;
  }
  if (!row || event.key !== "Enter") return;
  const id = Number(row.dataset.id);
  if (row.closest("#prospectRows")) toggleProspect(id);
  select(id);
});
$("#search").addEventListener("input", (event) => { state.query = event.target.value; renderRows(); });
// ---------- Fliken Garanti: sök ett bolag och se vad som avgör om en emission kan garanteras ----------
const ASSESS = { "lätt": "good", "möjlig": "mid", "svår": "bad", "okänd": "none" };

function dash(value) { return value == null || value === "" ? "—" : value; }
function num(value, digits = 1) { return value == null ? "—" : Number(value).toLocaleString("sv-SE", { maximumFractionDigits: digits }); }
function signedPct(value) { return value == null ? "—" : `${value > 0 ? "+" : ""}${num(value)} %`; }
function pctClass(value) { return value == null ? "" : value > 0 ? "up" : value < 0 ? "down" : ""; }
function bandPill(band, runway) {
  if (runway == null && !band) return "";
  return `<span class="pill ${band ? `band-${band}` : "muted"}">${runway != null ? months(runway) : "—"}</span>`;
}

function renderGResults() {
  const box = $("#gResults");
  const input = $("#gSearch");
  const q = input.value.trim();
  input.setAttribute("aria-expanded", String(garanti.results.length > 0));
  if (!q) {
    box.innerHTML = garanti.id ? "" : '<li class="g-empty">Skriv ett bolagsnamn, en ticker eller ett ISIN-nummer för att se vad som avgör om en emission kan garanteras.</li>';
    input.removeAttribute("aria-activedescendant");
    return;
  }
  if (!garanti.results.length) {
    box.innerHTML = `<li class="g-empty">${garanti.searching ? "Söker…" : `Inga bolag matchar "${escapeHtml(q)}".`}</li>`;
    return;
  }
  box.innerHTML = garanti.results.map((r, i) => `
    <li role="option" id="gOpt${i}" data-gid="${r.id}" class="g-opt${i === garanti.active ? " active" : ""}" aria-selected="${i === garanti.active}">
      <span class="g-name"><strong>${escapeHtml(r.name)}</strong><small>${escapeHtml([r.symbol, r.market].filter(Boolean).join(" · "))}</small></span>
      <span class="g-run">${bandPill(r.band, r.runway_today)}</span></li>`).join("");
  if (garanti.active >= 0) {
    input.setAttribute("aria-activedescendant", `gOpt${garanti.active}`);
    document.getElementById(`gOpt${garanti.active}`)?.scrollIntoView({ block: "nearest" });
  } else input.removeAttribute("aria-activedescendant");
}

async function gSearch() {
  const q = $("#gSearch").value.trim();
  const seq = ++garanti.seq;
  if (!q) { garanti.results = []; garanti.active = -1; garanti.searching = false; renderGResults(); return; }
  garanti.searching = true;
  try {
    const data = await api(`api/search?q=${encodeURIComponent(q)}&limit=20`);
    if (seq !== garanti.seq) return;
    garanti.results = Array.isArray(data) ? data : (data.items || []);
    garanti.active = garanti.results.length ? 0 : -1;
    garanti.searching = false;
    renderGResults();
  } catch (error) {
    if (seq !== garanti.seq) return;
    garanti.results = [];
    garanti.searching = false;
    $("#gResults").innerHTML = `<li class="g-empty">Sökningen misslyckades (${escapeHtml(error.message)}). Försök igen.</li>`;
  }
}

function gCard(title, body) { return `<section class="g-card"><h3>${title}</h3>${body}</section>`; }
function gKv(label, value, hint) {
  return `<div class="g-kv"${hint ? ` title="${escapeHtml(hint)}"` : ""}><span>${label}</span><strong>${value}</strong></div>`;
}

function gHeader(d) {
  const c = d.company || {};
  const r = d.runway || {};
  const verdict = (d.capacity && d.capacity.assessment) || "okänd";
  const reasons = ((d.capacity && d.capacity.reasons) || []).map((t) => `<li>${escapeHtml(t)}</li>`).join("");
  return `<section class="g-card g-head">
    <div class="g-head-top">
      <div><h2>${escapeHtml(c.name || "Okänt bolag")}</h2>
        <p class="source">${escapeHtml([c.symbol, c.market, c.segment, c.sector].filter(Boolean).join(" · "))}</p></div>
      <div class="g-badges">${bandPill(r.band, r.runway_today)}
        <span class="g-verdict v-${ASSESS[verdict] || "none"}" title="Bedömd möjlighet att garantera behovet">Garanti: ${escapeHtml(verdict)}</span></div>
    </div>
    ${reasons ? `<ul class="g-reasons">${reasons}</ul>` : ""}</section>`;
}

function gNeed(d) {
  const n = d.need, r = d.runway || {};
  let body;
  if (n) {
    // Belopp i behovsdelen är redan omräknade till SEK (docs/api-garanti.md); n.currency är bara rapportens valuta.
    const orig = n.currency || "SEK";
    const cells = [["Burn 12 mån", n.operating_12m], ["+ Lån inom 12 mån", n.debt_due_12m], ["− Kassa", n.cash_today],
      ["− Outnyttjad kredit", n.available_credit], ["= Nettobehov", n.net_need], ["+ Emissionskostnad", n.issue_costs], ["= Bruttobehov", n.gross_need]];
    body = `<div class="g-eq">${cells.map(([l, v], i) => `<div class="g-kv${i === 6 ? " total" : ""}"><span>${l}</span><strong>${msek(v, "SEK")}</strong></div>`).join("")}</div>
      ${orig !== "SEK" && n.sek_per_unit ? `<p class="source">Omräknat från ${escapeHtml(orig)} (${num(n.sek_per_unit, 3)} kr per enhet).</p>` : ""}`;
  } else body = '<p class="source">Kapitalbehov kan inte beräknas: underlag saknas.</p>';
  const rw = `<div class="g-grid">${gKv("Runway idag", months(r.runway_today))}${gKv("Endast kassa", months(r.runway_cash_today), "Utan outnyttjad kredit")}${gKv("Pro forma", months(r.runway_pro_forma), "Om säkrad finansiering kommer in")}${gKv("Kassa idag", msek(r.cash_today, r.currency))}${gKv("Burn / mån", msek(r.monthly_burn, r.currency))}</div>`;
  const flagTexts = r.flag_text ? Object.values(r.flag_text) : [];
  const flags = flagTexts.length ? `<ul class="g-reasons warn">${flagTexts.map((t) => `<li>${escapeHtml(t)}</li>`).join("")}</ul>` : "";
  const src = r.period_end ? `<p class="source">Rapport ${date(r.period_end)}${r.months_since_report != null ? ` (${months(r.months_since_report)} sedan)` : ""}.</p>` : "";
  return gCard("Kapitalbehov", body + rw + src + flags);
}

function gMarket(d) {
  const m = d.market || {}, c = d.capacity || {};
  if (m.market_cap == null && m.price == null) {
    return gCard("Marknad", `<p class="source">Marknadsdata saknas${m.error ? ` (${escapeHtml(m.error)})` : ""}.</p>`);
  }
  const ch = (label, v) => `<div class="g-kv"><span>${label}</span><strong class="${pctClass(v)}">${signedPct(v)}</strong></div>`;
  const range = m.low_52w != null || m.high_52w != null ? `${num(m.low_52w, 2)} – ${num(m.high_52w, 2)}` : "—";
  const days = c.turnover_days_for_need != null ? `${num(c.turnover_days_for_need)} dagars omsättning` : "—";
  const share = c.need_share_of_market_cap_pct != null ? `${num(c.need_share_of_market_cap_pct)} % av börsvärdet` : "—";
  const foot = [m.as_of ? `Per ${date(m.as_of)}` : "", m.source ? escapeHtml(m.source) : "", m.error ? escapeHtml(m.error) : ""].filter(Boolean).join(" · ");
  return gCard("Marknad", `<div class="g-grid">${gKv("Börsvärde", msek(m.market_cap))}${gKv("Kurs", m.price != null ? `${num(m.price, 2)} kr` : "—")}
    ${ch("1 mån", m.price_change_1m_pct)}${ch("3 mån", m.price_change_3m_pct)}${ch("12 mån", m.price_change_12m_pct)}
    ${gKv("Omsättning / dag (3 mån)", msek(m.avg_daily_turnover_3m))}${gKv("52 veckor", range)}</div>
    <div class="g-callouts"><div><span>Behovet motsvarar</span><strong>${days}</strong></div><div><span>Behovet motsvarar</span><strong>${share}</strong></div></div>
    ${foot ? `<p class="source">${foot}</p>` : ""}`);
}

function gInsiders(d) {
  const s = d.insider_summary, rows = d.insiders || [];
  const sum = s ? `<div class="g-grid">${gKv(`Köp (${s.months || 6} mån)`, `${msek(s.buys)}${s.buyers != null ? ` · ${s.buyers} pers.` : ""}`)}
    ${gKv("Sälj", `${msek(s.sells)}${s.sellers != null ? ` · ${s.sellers} pers.` : ""}`)}${gKv("Netto", `<span class="${pctClass(s.net)}">${msek(s.net)}</span>`)}</div>` : "";
  const table = rows.length ? `<div class="table-wrap g-table"><table><thead><tr><th>Datum</th><th>Person</th><th>Roll</th><th>Typ</th><th class="num">Antal</th><th class="num">Kurs</th><th class="num">Värde</th></tr></thead><tbody>
    ${rows.map((t) => `<tr><td>${date(t.date)}</td><td>${escapeHtml(dash(t.person))}</td><td>${escapeHtml(dash(t.role))}</td><td>${escapeHtml(dash(t.transaction))}</td>
      <td class="num">${num(t.volume, 0)}</td><td class="num">${num(t.price, 2)}</td><td class="num">${msek(t.value, t.currency)}</td></tr>`).join("")}</tbody></table></div>`
    : '<p class="source">Inga insynshandlingar registrerade.</p>';
  return gCard("Insynshandel", sum + table);
}

function gFinancing(d) {
  const list = d.financing || [];
  const items = list.map((e) => `<li><div><span>${roundDates(e)} · ${TYPE[e.type] || e.type} · ${STATUS[e.status] || e.status}</span>
      <strong>${e.amount != null ? msek(e.amount, e.currency) : "belopp saknas"}</strong></div>${terms(e)}
      <small>Rådgivare: ${e.advisers ? escapeHtml(e.advisers) : "ej angivet"}</small>
      ${e.url ? `<a href="${escapeHtml(e.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(e.title || "Pressmeddelande")}</a>` : ""}
      ${e.quote ? `<q>${escapeHtml(e.quote)}</q>` : ""}</li>`).join("");
  const n = d.emissions_24m != null ? `<p class="source">${d.emissions_24m} emissioner senaste 24 månaderna.</p>` : "";
  return gCard("Emissionshistorik 24 mån", n + (items ? `<ul class="facts">${items}</ul>` : '<p class="source">Inga finansieringshändelser de senaste 24 månaderna.</p>'));
}

function gAuth(d) {
  const a = d.authorization, r = d.next_report;
  const auth = a ? `<p>Stämma ${escapeHtml(date(a.meeting_date))}: ${a.max_dilution_pct != null ? `högst ${num(a.max_dilution_pct)} % av aktierna` : a.max_shares != null ? `högst ${Number(a.max_shares).toLocaleString("sv-SE")} aktier` : "omfattning ej angiven"}${a.directed_allowed ? ", även riktad emission" : ""}.</p>`
    : '<p class="source">Inget bemyndigande registrerat.</p>';
  const rep = r ? `<p>Nästa rapport: <strong>${escapeHtml(date(r.date))}</strong>${r.title ? ` (${escapeHtml(r.title)})` : ""}</p>` : '<p class="source">Nästa rapportdatum okänt.</p>';
  return gCard("Bemyndigande och nästa rapport", auth + rep);
}

function gPct(v, digits) { return v == null || Number.isNaN(Number(v)) ? "—" : `${num(Number(v), digits == null ? 1 : digits)} %`; }
function gName(g) { return g && g.name != null ? g.name : g; }
// Backend skickar emissionens garanter som texten "namn; namn" (financing.py), inte som lista.
function gGuarantors(v) {
  if (Array.isArray(v)) return v.map(gName).filter(Boolean).map(String);
  return String(v || "").split(/[;,]/).map((s) => s.replace(/\s+/g, " ").trim()).filter(Boolean);
}

function gOwners(d) {
  const o = d.owners;
  if (!o || !Array.isArray(o.top) || !o.top.length) return gCard("Ägare", '<p class="source">Ägardata saknas för bolaget.</p>');
  const chips = [["Topp 3", o.top3_pct, ""], ["Topp 10", o.top10_pct, ""], ["Fritt flöde ca", o.free_float_estimate_pct, "Uppskattning, inte en rapporterad siffra"]]
    .filter(([, v]) => v != null).map(([l, v, h]) => `<span class="chip g-chip"${h ? ` title="${escapeHtml(h)}"` : ""}>${l}: ${gPct(v)}${h ? " (uppskattning)" : ""}</span>`).join("");
  const top10 = o.top.slice(0, 10);
  const sum = top10.reduce((a, t) => a + (Number(t.pct_capital) || 0), 0);
  const shown = Math.max(0, Math.min(100, o.top10_pct != null ? Number(o.top10_pct) : sum));
  const segs = top10.filter((t) => Number(t.pct_capital) > 0).map((t, i) => `<span class="g-seg s${i % 5}" style="width:${Math.min(100, Number(t.pct_capital))}%" title="${escapeHtml(t.name)}: ${gPct(t.pct_capital)}"></span>`).join("");
  const rest = gPct(Math.max(0, 100 - shown));
  const bar = `<div class="g-stack" role="img" aria-label="Topp 10 äger ${gPct(shown)} av kapitalet, övriga ${rest}">${segs}<span class="g-seg rest" style="flex:1" title="Övriga: ${rest}"></span></div>
    <p class="source g-stack-legend">Topp 10 ${gPct(shown)} av kapitalet · övriga ${rest}</p>`;
  const rows = o.top.map((t, i) => `<tr${i < 3 ? ' class="g-top"' : ""}><td>${escapeHtml(dash(t.name))}</td><td class="num">${gPct(t.pct_capital, 2)}</td><td class="num">${gPct(t.pct_votes, 2)}</td><td class="num">${t.shares != null ? Number(t.shares).toLocaleString("sv-SE") : "—"}</td></tr>`).join("");
  const table = `<div class="table-wrap g-table"><table><thead><tr><th>Ägare</th><th class="num">Kapital</th><th class="num">Röster</th><th class="num">Antal aktier</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  const src = [o.as_of ? `Per ${escapeHtml(date(o.as_of))}` : "", o.source_title ? escapeHtml(o.source_title) : ""].filter(Boolean).join(" · ");
  return gCard("Ägare", `${src ? `<p class="source">${src}</p>` : ""}<div class="chips g-chips">${chips}</div>${bar}${table}`);
}

function gShort(d) {
  const list = Array.isArray(d.short_positions) ? d.short_positions : [];
  const total = d.short_total_pct;
  const none = '<p class="source">Inga blankningspositioner över 0,5 % rapporterade.</p>';
  if (!list.length && total == null) return gCard("Blankning", none);
  const chip = total != null ? `<div class="chips g-chips"><span class="chip g-chip${Number(total) > 2 ? " warn" : ""}">Total blankning: ${gPct(total, 2)}</span></div>` : "";
  const items = list.length ? `<ul class="facts g-short">${list.map((p) => `<li><div><span>${escapeHtml(dash(p.holder))}</span><strong>${gPct(p.pct, 2)}</strong></div><small>${escapeHtml(date(p.date))}</small></li>`).join("")}</ul>` : none;
  return gCard("Blankning", chip + items);
}

function gHistory(d) {
  const h = d.guarantee_history;
  const all = h && Array.isArray(h.issues) ? h.issues : [];
  if (!all.length) return gCard("Garantihistorik", '<p class="source">Ingen garantihistorik registrerad.</p>');
  const sel = garanti.guarantor;
  const issues = sel ? all.filter((i) => gGuarantors(i.guarantors).includes(sel)) : all;
  const avg = h.avg_subscription_rate_pct != null ? `<div class="chips g-chips"><span class="chip g-chip">Snitt teckningsgrad: ${gPct(h.avg_subscription_rate_pct)}</span></div>` : "";
  const gs = Array.isArray(h.guarantors) ? h.guarantors : [];
  const gl = gs.length ? `<div class="g-guarantors" role="group" aria-label="Återkommande garanter">${gs.map((g) => `<button type="button" class="g-gbtn${g.name === sel ? " on" : ""}" data-guarantor="${escapeHtml(g.name)}" aria-pressed="${g.name === sel}">${escapeHtml(g.name)} <small>${g.issues} ggr${g.avg_fee_pct != null ? ` · snitt ${gPct(g.avg_fee_pct, 1)}` : ""}</small></button>`).join("")}</div>` : "";
  const note = sel ? `<p class="source">Visar ${issues.length} av ${all.length} emissioner med ${escapeHtml(sel)}. Klicka igen på garanten för att visa alla.</p>` : "";
  const rows = issues.map((i) => `<tr><td>${escapeHtml(date(i.date))}</td><td>${escapeHtml(TYPE[i.type] || dash(i.type))}</td><td class="num">${msek(i.amount, i.currency)}</td><td class="num">${gPct(i.subscription_rate_pct)}</td>
    <td class="num">${msek(i.guarantee_amount, i.currency)}</td><td class="num">${msek(i.guarantee_utilized, i.currency)}</td><td class="num">${gPct(i.guarantee_fee_pct, 2)}</td><td>${escapeHtml(gGuarantors(i.guarantors).join(", ") || "—")}</td></tr>`).join("");
  const table = issues.length ? `<div class="table-wrap g-table"><table><thead><tr><th>Datum</th><th>Typ</th><th class="num">Belopp</th><th class="num">Teckningsgrad</th><th class="num">Garanti</th><th class="num">Utnyttjad</th><th class="num">Ersättning</th><th>Garanter</th></tr></thead><tbody>${rows}</tbody></table></div>`
    : '<p class="source">Inga emissioner matchar vald garant.</p>';
  return gCard("Garantihistorik", avg + gl + note + table);
}

function gWarrantState(w) {
  const day = 86400000, now = Date.now();
  const s = w.exercise_start ? Date.parse(w.exercise_start) : NaN, e = w.exercise_end ? Date.parse(w.exercise_end) : NaN;
  if (!Number.isNaN(e) && e + day < now) return "";
  if (!Number.isNaN(s) && s > now) return s - now <= 60 * day ? "soon" : "";
  return !Number.isNaN(s) || !Number.isNaN(e) ? "open" : "";
}

function gWarrants(d) {
  const list = Array.isArray(d.warrants_outstanding) ? d.warrants_outstanding : [];
  if (!list.length) return gCard("Utestående teckningsoptioner", '<p class="source">Inga utestående teckningsoptioner registrerade.</p>');
  const rows = list.map((w) => {
    const st = gWarrantState(w);
    const badge = st === "open" ? ' <span class="chip g-chip warn">öppen nu</span>' : st === "soon" ? ' <span class="chip g-chip warn">startar inom 60 dagar</span>' : "";
    return `<tr${st ? ' class="g-hot"' : ""}><td class="num">${w.exercise_price != null ? `${num(w.exercise_price, 2)} ${escapeHtml(w.currency || "kr")}` : "—"}</td><td>${escapeHtml(date(w.exercise_start))} – ${escapeHtml(date(w.exercise_end))}${badge}</td><td class="num">${msek(w.amount, w.currency)}</td></tr>`;
  }).join("");
  return gCard("Utestående teckningsoptioner", `<div class="table-wrap g-table"><table><thead><tr><th class="num">Lösenpris</th><th>Lösenperiod</th><th class="num">Belopp</th></tr></thead><tbody>${rows}</tbody></table></div>`);
}

function renderGarantiCompany(d) {
  garanti.data = d;
  const upd = d.updated_at ? `<p class="source">Uppdaterad ${escapeHtml(String(d.updated_at).replace("T", " ").slice(0, 16))}</p>` : "";
  $("#gCompany").innerHTML = `<div class="g-company fade-in">${gHeader(d)}${gNeed(d)}${gMarket(d)}${gOwners(d)}${gShort(d)}${gInsiders(d)}${gFinancing(d)}<div id="gHistory">${gHistory(d)}</div>${gAuth(d)}${gWarrants(d)}${upd}</div>`;
}

async function openGaranti(id, push = true) {
  garanti.id = id;
  garanti.guarantor = null;
  garanti.seq++;
  if (push) history.replaceState(null, "", `#garanti/${id}`);
  garanti.results = [];
  renderGResults();
  $("#gSearch").setAttribute("aria-expanded", "false");
  $("#gCompany").innerHTML = '<div class="g-company"><div class="g-card"><span class="sk" style="width:50%;height:24px"></span><span class="sk" style="width:30%;margin-top:10px"></span></div><div class="g-card"><span class="sk block"></span></div><div class="g-card"><span class="sk block"></span></div></div>';
  try {
    const d = await api(`api/guarantee/${id}`);
    if (garanti.id !== id) return;
    renderGarantiCompany(d);
  } catch (error) {
    if (garanti.id !== id) return;
    $("#gCompany").innerHTML = `<div class="g-card"><p class="source">${error.message === "API 404" ? "Bolaget hittades inte." : `Bolaget kunde inte läsas (${escapeHtml(error.message)}).`}</p></div>`;
  }
}

function routeHash() {
  const view = readHash();
  if (view) {
    if (state.overview) pruneFacets();
    setView(view);
    if (state.items.length) renderList();
    renderProspects(false);
    return;
  }
  const m = location.hash.match(/^#garanti(?:\/(\d+))?$/);
  if (!m) return;
  setView("garanti");
  if (m[1] && Number(m[1]) !== garanti.id) openGaranti(Number(m[1]), false);
}

$("#gSearch").addEventListener("input", () => { clearTimeout(garanti.timer); garanti.timer = setTimeout(gSearch, 200); });
$("#gSearch").addEventListener("keydown", (event) => {
  const n = garanti.results.length;
  if (event.key === "ArrowDown" && n) { event.preventDefault(); garanti.active = (garanti.active + 1) % n; renderGResults(); }
  else if (event.key === "ArrowUp" && n) { event.preventDefault(); garanti.active = (garanti.active - 1 + n) % n; renderGResults(); }
  else if (event.key === "Enter" && garanti.results[garanti.active]) { event.preventDefault(); openGaranti(garanti.results[garanti.active].id); }
  else if (event.key === "Escape") { $("#gSearch").value = ""; garanti.results = []; renderGResults(); }
});
$("#gResults").addEventListener("click", (event) => {
  const li = event.target.closest("[data-gid]");
  if (li) openGaranti(Number(li.dataset.gid));
});
$("#gCompany").addEventListener("click", (event) => {
  const btn = event.target.closest("[data-guarantor]");
  if (!btn || !garanti.data) return;
  garanti.guarantor = garanti.guarantor === btn.dataset.guarantor ? null : btn.dataset.guarantor;
  const host = $("#gHistory");
  if (host) host.innerHTML = gHistory(garanti.data);
  $("#gHistory [aria-pressed=true]")?.focus();
});
window.addEventListener("hashchange", routeHash);

renderGResults();
load();
routeHash();
