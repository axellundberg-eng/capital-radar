"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const flush = async () => { for (let index = 0; index < 8; index += 1) await new Promise(setImmediate); };
const deal = (id = "transaction:1:rights_issue:1") => ({
  id, issuer_id: 1, issuer_name: "Exempel AB", headline: "Exempel AB: emission",
  summary: "Verifierat källreferat", category: "rights_issue", status: "announced",
  published_at: "2026-10-06T09:00:00Z", institutions: [], history: [], amount: null,
});
const feed = (page = 1, id) => ({
  items: [deal(id)], page, page_size: 10, total: 30, total_pages: 3,
  has_next: page < 3, as_of: "2026-10-06T09:00:00Z",
});
const company = (name = "Exempel AB") => ({
  issuer: { id: 1, canonical_name: name }, securities: [], financing_events: [],
  financial_facts: [{ label: "Kassa", value: 1000000, document_id: 17 }],
  documents: [],
});
const analysis = () => ({ items: [{
  issuer_id: 1, reported_runway_months: 6, adjusted_runway_months: 9,
  delta_runway_months: 3, cash: 1000000, monthly_burn: 100000, currency: "SEK",
  events: [], pro_forma_comment: "Källbunden separat adjusted-analys.",
}] });
const reply = (body, httpStatus = 200) => ({ body, httpStatus });

function normal(pathname) {
  if (pathname.startsWith("/api/transactions?")) return feed();
  if (pathname === "/api/overview") return { metrics: { total_issuers: 520, urgent_under_6m: 12 } };
  if (pathname.startsWith("/api/issuers?")) return { items: [{ id: 1, canonical_name: "Exempel AB", headline: "Källkommentar" }], total_filtered: 1, next_cursor: null };
  if (pathname.startsWith("/api/radar/analysis?")) return analysis();
  if (pathname === "/api/issuer/1") return company();
  if (pathname === "/api/issuer-markets") return { items: ["First North"] };
  if (pathname === "/api/market-health") return { months: [] };
  if (pathname === "/api/operations/summary") return { actions: {} };
  if (pathname === "/api/research/status") return { enabled: false };
  if (pathname.startsWith("/api/research/history?")) return { items: [] };
  throw new Error("Unexpected request " + pathname);
}

function harness(resolver = normal, { autoAdvance = false } = {}) {
  const nodes = new Map(), calls = [], timers = new Map();
  let now = Date.parse("2026-10-06T09:00:00Z"), timerId = 0;
  function node(selector) {
    if (!nodes.has(selector)) {
      const classes = new Set();
      nodes.set(selector, {
        innerHTML: "", textContent: "", value: "", hidden: false, disabled: false,
        dataset: {}, isConnected: true, inert: false,
        classList: {
          add: (...names) => names.forEach(name => classes.add(name)),
          remove: (...names) => names.forEach(name => classes.delete(name)),
          contains: name => classes.has(name),
          toggle: (name, on) => { if (on ?? !classes.has(name)) classes.add(name); else classes.delete(name); },
        },
        setAttribute() {}, addEventListener() {}, focus() {}, closest: () => null,
        querySelectorAll: () => [], getClientRects: () => [],
        insertAdjacentHTML(where, html) { this.innerHTML = where === "afterbegin" ? html + this.innerHTML : this.innerHTML + html; },
      });
    }
    return nodes.get(selector);
  }
  function advance(id) {
    const timer = timers.get(id); if (!timer) return;
    timers.delete(id); now += timer.delay; timer.callback();
  }
  const source = fs.readFileSync(path.join(__dirname, "..", "..", "src", "capital_radar", "web", "workspace.js"), "utf8").replace(
    "window.CapitalWorkspace = {",
    "window.__testing = {state, load, loadDeals, loadHomeDeals, loadIssuerPage, openIssuerDetail, closeIssuerDetail, requests}; window.CapitalWorkspace = {",
  );
  const context = {
    window: { addEventListener() {} }, location: { hash: "#home" }, history: { replaceState() {} },
    document: { querySelector: node, getElementById: id => node("#" + id), querySelectorAll: () => [], addEventListener() {}, body: node("body"), activeElement: null },
    fetch: async (pathname, options) => {
      calls.push({ pathname, signal: options.signal });
      const result = await resolver(pathname, options, calls);
      const response = result && Object.hasOwn(result, "httpStatus") ? result : reply(result);
      return { status: response.httpStatus, ok: response.httpStatus >= 200 && response.httpStatus < 300, json: async () => structuredClone(response.body) };
    },
    setTimeout(callback, delay) {
      const id = ++timerId; timers.set(id, { callback, delay });
      if (autoAdvance) queueMicrotask(() => advance(id));
      return id;
    },
    clearTimeout: id => timers.delete(id),
    Date: class ClockDate extends Date { static now() { return now; } },
    AbortController, AbortSignal, URL, URLSearchParams, FormData, Intl, console,
  };
  vm.runInNewContext(source, context, { filename: "workspace.js", timeout: 1000 });
  return { ...context.window.__testing, node, calls, timers, advance, now: () => now };
}

test("overview and company list render while feed, runway and health are delayed", async () => {
  const slowFeed = deferred(), slowAnalysis = deferred(), slowHealth = deferred();
  const app = harness(url => {
    if (url.startsWith("/api/transactions?")) return slowFeed.promise;
    if (url.startsWith("/api/radar/analysis?")) return slowAnalysis.promise;
    if (url === "/api/market-health") return slowHealth.promise;
    return normal(url);
  });
  const loading = app.load();
  await flush();
  assert.equal(app.node("#loading").classList.contains("hidden"), true);
  assert.match(app.node("#metrics").innerHTML, /520/);
  assert.match(app.node("#issuerList").innerHTML, /Exempel AB/);
  assert.match(app.node("#homeTransactions").innerHTML, /laddas/);
  assert.doesNotMatch(app.node("#homeTransactions").innerHTML, /Inga finansieringsaffärer/);
  assert.match(app.node("#radarCount").textContent, /Adjusted runway laddas/);
  slowFeed.resolve(feed()); slowAnalysis.resolve(analysis()); slowHealth.resolve({ months: [] });
  await loading; await flush();
  assert.match(app.node("#homeTransactions").innerHTML, /Verifierat källreferat/);
  assert.equal(app.state.proForma[1].reported_runway_months, 6);
  assert.equal(app.state.proForma[1].adjusted_runway_months, 9);
});

test("failed finance views do not discard successful overview and issuer records", async () => {
  const app = harness(url => {
    if (url.startsWith("/api/transactions?") || url.startsWith("/api/radar/analysis?") || url === "/api/market-health") throw new Error("timeout");
    return normal(url);
  });
  await app.load(); await flush();
  assert.match(app.node("#issuerList").innerHTML, /Exempel AB/);
  assert.match(app.node("#metrics").innerHTML, /520/);
  assert.match(app.node("#homeTransactions").innerHTML, /kunde inte laddas/);
  assert.doesNotMatch(app.node("#homeTransactions").innerHTML, /Inga finansieringsaffärer/);
  assert.match(app.node("#dealChart").innerHTML, /kunde inte laddas/);
  assert.doesNotMatch(app.node("#dealChart").innerHTML, /Historiska månadsslut saknas/);
  assert.match(app.node("#notice").textContent, /Adjusted runway/);
  assert.equal(app.state.proForma[1], undefined);
});

test("issuer detail shows base records before runway and keeps them after runway failure", async () => {
  const slow = deferred();
  const app = harness(url => url.startsWith("/api/radar/analysis?") ? slow.promise : normal(url));
  const loading = app.openIssuerDetail(1);
  await flush();
  assert.equal(app.node("#issuerDetailTitle").textContent, "Exempel AB");
  assert.match(app.node("#issuerDetailContent").innerHTML, /Bolagsuppgifter och källor visas redan/);
  assert.match(app.node("#issuerDetailContent").innerHTML, /Dokument 17/);
  slow.reject(new Error("timeout"));
  await loading;
  assert.match(app.node("#issuerDetailContent").innerHTML, /Adjusted runway kunde inte laddas/);
  assert.match(app.node("#issuerDetailContent").innerHTML, /Dokument 17/);
  assert.equal(app.state.issuerDetail.issuer.canonical_name, "Exempel AB");
});

test("late same-company detail responses cannot replace a newer open", async () => {
  const old = deferred(), latest = deferred(); let count = 0;
  const app = harness(url => url === "/api/issuer/1" ? (++count === 1 ? old.promise : latest.promise) : normal(url));
  const first = app.openIssuerDetail(1), second = app.openIssuerDetail(1);
  latest.resolve(company("Ny uppgift")); await second;
  old.resolve(company("Gammal uppgift")); await first;
  assert.equal(app.node("#issuerDetailTitle").textContent, "Ny uppgift");
  assert.equal(app.calls[0].signal.aborted, true);
});

test("late old reload cannot overwrite the newest overview", async () => {
  const old = deferred(), latest = deferred(); let count = 0;
  const app = harness(url => url === "/api/overview" ? (++count === 1 ? old.promise : latest.promise) : normal(url));
  const first = app.load(), second = app.load();
  latest.resolve({ metrics: { total_issuers: 222 } }); await second;
  old.resolve({ metrics: { total_issuers: 111 } }); await first;
  assert.equal(app.state.overview.metrics.total_issuers, 222);
  assert.match(app.node("#metrics").innerHTML, /222/);
});

test("late old transaction page cannot replace a newer page", async () => {
  const old = deferred(), latest = deferred();
  const app = harness(url => url.includes("page=2&") ? old.promise : latest.promise);
  app.state.page = 2; const first = app.loadDeals();
  app.state.page = 3; const second = app.loadDeals();
  latest.resolve(feed(3, "new")); await second;
  old.resolve(feed(2, "old")); await first;
  assert.equal(app.state.transactions[0].id, "new");
  assert.equal(app.state.page, 3);
  assert.equal(app.node("#transactionPage").textContent, "Sida 3");
});

test("a delayed home response does not overwrite a newer market page", async () => {
  const old = deferred(), latest = deferred();
  const app = harness(url => url.includes("page=1&") ? old.promise : latest.promise);
  const home = app.loadHomeDeals();
  app.state.page = 2; const market = app.loadDeals();
  latest.resolve(feed(2, "market")); await market;
  old.resolve(feed(1, "home")); await home;
  assert.equal(app.state.homeTransactions[0].id, "home");
  assert.equal(app.state.transactions[0].id, "market");
  assert.equal(app.state.page, 2);
});

test("cold 202 remains pending beyond four responses and becomes ready without a false empty feed", async () => {
  let count = 0;
  const app = harness(() => {
    count += 1;
    return count <= 5 ? reply({ items: [], read_model: { status: "pending", retry_after: 2 } }, 202) : feed();
  }, { autoAdvance: true });
  await app.loadHomeDeals();
  assert.equal(count, 6);
  assert.equal(app.state.sections.homeFeed.status, "ready");
  assert.equal(app.state.homeTransactions.length, 1);
  assert.ok(app.now() - Date.parse("2026-10-06T09:00:00Z") >= 10000);
});

test("cold failed read model is an explicit failure, not a zero-deal market", async () => {
  const app = harness(() => reply({ items: [], read_model: { status: "failed", error_code: "read_model_refresh_failed" } }, 202));
  await app.loadHomeDeals();
  assert.equal(app.calls.length, 1);
  assert.equal(app.state.sections.homeFeed.status, "error");
  assert.equal(app.state.sections.deals.status, "error");
  assert.doesNotMatch(app.node("#homeTransactions").innerHTML, /Inga finansieringsaffärer/);
});

test("pending polling has a deadline and leaves an explicit manual retry instead of an empty list", async () => {
  const app = harness(() => reply({ items: [], read_model: { status: "pending", retry_after: 2 } }, 202), { autoAdvance: true });
  await app.loadHomeDeals();
  assert.equal(app.state.sections.homeFeed.status, "pending");
  assert.match(app.node("#homeTransactions").innerHTML, /Uppdatera för att kontrollera igen/);
  assert.doesNotMatch(app.node("#homeTransactions").innerHTML, /Inga finansieringsaffärer/);
  assert.ok(app.calls.length > 4 && app.calls.length < 32);
  assert.ok(app.now() - Date.parse("2026-10-06T09:00:00Z") <= 60000);
});

test("stale published data is displayed immediately and marked before the refreshed generation arrives", async () => {
  let count = 0;
  const app = harness(() => {
    count += 1;
    return count === 1 ? { ...feed(1, "old"), read_model: { status: "refreshing", stale: true, refreshing: true, generated_at: "2026-10-05T09:00:00Z", retry_after: 2 } } : { ...feed(1, "new"), read_model: { status: "ready", stale: false, refreshing: false } };
  });
  const loading = app.loadHomeDeals(); await flush();
  assert.equal(app.state.homeTransactions[0].id, "old");
  assert.match(app.node("#homeTransactions").innerHTML, /äldre underlag/);
  assert.equal(app.node("#loading").classList.contains("hidden"), true);
  app.advance(app.timers.keys().next().value); await loading;
  assert.equal(app.state.homeTransactions[0].id, "new");
  assert.doesNotMatch(app.node("#homeTransactions").innerHTML, /äldre underlag/);
});

test("failed refresh preserves the previous valid feed and does not label it as a new empty result", async () => {
  let fail = false;
  const app = harness(() => { if (fail) throw new Error("503"); return feed(); });
  await app.loadHomeDeals(); fail = true; await app.loadHomeDeals();
  assert.equal(app.state.homeTransactions.length, 1);
  assert.match(app.node("#homeTransactions").innerHTML, /Senast hämtade underlag visas/);
  assert.doesNotMatch(app.node("#homeTransactions").innerHTML, /Inga finansieringsaffärer/);
});
