(() => {
  "use strict";

  const state = {
    overview: {}, dashboard: {}, issuers: [], issuerTotal: 0, issuerNextCursor: null,
    issuerCursors: [null], issuerPageIndex: 0, issuerRenderedPageIndex: 0, issuerMarkets: [],
    issuerFilters: { q: "", market: "", capital_need: "", runway_max: "", analysis_status: "", sort: "priority", direction: "desc" },
    operations: { actions: {} }, operationTimer: null, operationLoading: false,
    transactions: [], research: {}, researchHistory: { items: [] }, page: 1,
    issuerDetailId: null, issuerDetail: null, issuerDetailTrigger: null,
    transactionDetailId: null, transactionCase: null, homeTransactions: [], proForma: {}, health: {}, dealFilters: {}, transactionMeta: {}, previewWeights: {},
    sections: {}, analysisStatus: {}, loadRevision: 0,
  };
  const requests = new Map();
  const sectionLabels = { overview: "Översikt", issuers: "Bolagsradar", analysis: "Runway idag", homeFeed: "Senaste affärer", deals: "Affärslista", health: "Market Health", issuerMarkets: "Handelsplatser", research: "Research", researchHistory: "Researchhistorik", operations: "Åtgärdskö", detailIssuer: "Bolagsuppgifter", detailAnalysis: "Runway idag", detailTransaction: "Transaktionsakt", detailCase: "Källakt, audit och runway" };
  const $ = (selector) => document.querySelector(selector);
  const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
  const list = (value, keys = ["items", "issuers", "transactions", "results", "data"]) => Array.isArray(value) ? value : keys.reduce((found, key) => found.length ? found : (Array.isArray(value?.[key]) ? value[key] : []), []);
  const safeUrl = (value) => {
    try { const parsed = new URL(String(value)); return ["https:", "http:"].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : ""; } catch { return ""; }
  };
  const amount = (value) => {
    if (value === null || value === undefined || value === "") return "Belopp ej angivet";
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 }).format(numeric)} MSEK` : escapeHtml(value);
  };
  const financialAmount = (value, currency = "", maximumFractionDigits = 0) => {
    if (value === null || value === undefined || value === "") return "Ej angivet";
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return escapeHtml(value);
    return `${new Intl.NumberFormat("sv-SE", { maximumFractionDigits }).format(numeric)}${currency ? ` ${currency}` : ""}`;
  };
  const marketPrice = (value) => financialAmount(value, "SEK", 4);
  const percent = (value) => value === null || value === undefined || value === "" ? "Ej angivet" : `${new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 }).format(Number(value) * 100)} %`;
  const transactionAmount = (item) => {
    if (item.amount == null && item.amount_msek == null) return "—";
    if (item.amount_msek !== null && item.amount_msek !== undefined) return amount(item.amount_msek);
    const value = Number(item.amount);
    if (!Number.isFinite(value)) return "Belopp ej angivet";
    const currency = item.currency || "";
    if (Math.abs(value) >= 1_000_000) return `${new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 }).format(value / 1_000_000)} M${currency}`;
    return financialAmount(value, currency);
  };
  const date = (value) => {
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? "Datum saknas" : new Intl.DateTimeFormat("sv-SE", { day: "2-digit", month: "short", year: "numeric" }).format(parsed);
  };
  const label = (value) => ({ rights_issue: "Företrädesemission", directed_issue: "Riktad emission", unit_issue: "Unitemission", warrants: "Teckningsoptioner", warrant_exercise: "Teckningsoptioner", convertible: "Konvertibelfinansiering", loan: "Bryggfinansiering", bridge_loan: "Brygglån", shareholder_loan: "Aktieägarlån", credit_facility: "Kreditfacilitet", other_equity: "Övrig aktiefinansiering", other_debt: "Övrig lånefinansiering", acquisition: "Förvärv", commercial_order: "Kommersiellt avtal", report: "Rapport" }[String(value || "").toLowerCase()] || "Kapitalmarknad");
  const status = (item) => String(item.status || item.transaction_status || item.outcome_status || "Bevakas").replace(/_/g, " ");
  const transactionSignal = (item) => {
    const tone = ["green", "yellow", "red"].includes(String(item.signal_tone)) ? String(item.signal_tone) : "neutral";
    const label = item.signal_label || ({ green: "Positivt emissionsutfall", yellow: "Planerad emission", red: "Undertecknad emission", neutral: "Transaktionsstatus" }[tone]);
    return `<span class="transaction-dot is-${tone}" role="img" aria-label="${escapeHtml(label)}" title="${escapeHtml(label)}"></span>`;
  };
  const issuerName = (item) => item.name || item.issuer_name || item.canonical_name || item.issuer || item.company_name || "Bolag ej angivet";
  const transactionTitle = (item) => item.title || item.headline || item.name || `${issuerName(item)}: ${label(item.category || item.transaction_type)}`;
  const description = (item) => item.summary || item.description || item.comment || item.ai_comment || "Primärkälla finns i marknadsflödet. Uppgift och villkor följs löpande när nytt underlag publiceras.";
  const number = (value) => new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 0 }).format(Number(value || 0));
  const decimal = (value) => new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 }).format(Number(value));
  const analysisLabel = (value) => ({ interpreted: "Tolkad", prepared: "Underlag klart", queued: "I analyskö", insufficient_facts: "Underlag otillräckligt", not_analyzed: "Ej analyserad", unseen: "Ej analyserad" }[String(value || "").toLowerCase()] || "Status saknas");
  const capitalLabel = (value) => ({ yes: "Kapitalbehov", no: "Ej kapitalbehov", unknown: "Ej bedömt" }[String(value || "").toLowerCase()] || "Ej bedömt");
  const relativeDate = (value) => value ? date(value) : "–";
  const dateTime = (value) => {
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? "–" : new Intl.DateTimeFormat("sv-SE", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(parsed);
  };
  const actionCount = (actions) => Number(actions?.queued || 0) + Number(actions?.running || 0);

  async function api(path, signal) {
    const timeout = AbortSignal.timeout(30000);
    const response = await fetch(path, { signal: signal ? AbortSignal.any([signal, timeout]) : timeout, headers: { Accept: "application/json" }, cache: "no-store" });
    if (!response.ok) throw new Error(`API ${response.status}`);
    const payload = await response.json();
    if (payload && typeof payload === "object") Object.defineProperty(payload, "_httpStatus", { value: response.status });
    return payload;
  }
  async function adminWrite(path, options, token) {
    const {adminAuth} = await import("/admin-auth.js?v=agent-auth-1");
    return adminAuth.write(path, options, token);
  }
  function notice(message) { const el = $("#notice"); el.textContent = message; el.hidden = !message; }
  function beginRequest(name) {
    requests.get(name)?.controller.abort();
    const request = { controller: new AbortController(), current: () => requests.get(name) === request };
    requests.set(name, request);
    return request;
  }
  function section(name) { return state.sections[name] || { status: "loading", loaded: false }; }
  function sectionMessage(name) {
    const value = section(name), label = sectionLabels[name] || name;
    if (value.status === "loading") return `${label} laddas${value.loaded ? ". Senast hämtade underlag visas" : ""}…`;
    if (value.status === "pending") return `${label} förbereds fortfarande. Uppdatera för att kontrollera igen. Inga tomma resultat har fastställts.`;
    if (value.status === "error") return `${label} kunde inte laddas. ${value.loaded ? "Senast hämtade underlag visas. " : ""}Uppdatera för att försöka igen.`;
    if (value.status === "stale") return `${label}: äldre underlag${value.readModel?.generated_at ? ` från ${dateTime(value.readModel.generated_at)}` : ""} visas. ${value.readModel?.refreshing ? "Uppdatering pågår." : "Uppdatera för att försöka igen."}`;
    return "";
  }
  function sectionMarkup(name) {
    const message = sectionMessage(name);
    return message ? `<p class="empty" role="status">${escapeHtml(message)}</p>` : "";
  }
  function revealWorkspace() { $("#loading").classList.add("hidden"); }
  function renderLoadNotice() {
    const errors = Object.keys(state.sections).filter(name => section(name).status === "error");
    notice(errors.length ? `Vissa underlag kunde inte laddas: ${errors.map(name => sectionLabels[name] || name).join(", ")}. Övriga underlag visas. Uppdatera för att försöka igen.` : "");
  }
  function retryPause(milliseconds, signal) {
    return new Promise((resolve, reject) => {
      if (signal.aborted) { reject(new Error("Avbruten uppdatering")); return; }
      const cancel = () => { clearTimeout(timer); reject(new Error("Avbruten uppdatering")); };
      const timer = setTimeout(() => { signal.removeEventListener("abort", cancel); resolve(); }, milliseconds);
      signal.addEventListener("abort", cancel, { once: true });
    });
  }
  async function loadResource(name, path, apply, renderSection, request = beginRequest(name)) {
    const previous = section(name);
    state.sections[name] = { ...previous, status: "loading" };
    renderSection();
    const signal = AbortSignal.any([request.controller.signal, AbortSignal.timeout(60000)]);
    const deadline = Date.now() + 60000;
    try {
      for (let attempt = 0; attempt < 32 && Date.now() < deadline; attempt += 1) {
        const payload = await api(path, signal);
        if (!request.current()) return;
        const readModel = payload?.read_model;
        if (payload?._httpStatus === 202) {
          state.sections[name] = { ...section(name), status: readModel?.status === "failed" ? "error" : "loading", readModel };
          renderSection(); revealWorkspace(); renderLoadNotice();
          if (readModel?.status === "failed") return;
        } else {
          apply(payload);
          state.sections[name] = { status: readModel?.stale ? "stale" : "ready", loaded: true, readModel };
          renderSection(); revealWorkspace(); renderLoadNotice();
          if (!readModel?.refreshing) return payload;
        }
        const baseDelay = Math.max(1, Number(readModel?.retry_after) || 2);
        const delay = Math.min(deadline - Date.now(), Math.min(8, baseDelay * 2 ** Math.floor(attempt / 3)) * 1000);
        await retryPause(delay, signal);
      }
      if (request.current() && !section(name).loaded) {
        state.sections[name] = { ...section(name), status: "pending" };
        renderSection(); revealWorkspace();
      }
    } catch (error) {
      if (!request.current()) return;
      const pending = !section(name).loaded && ["pending", "refreshing"].includes(section(name).readModel?.status);
      state.sections[name] = { ...section(name), status: pending ? "pending" : "error" };
      renderSection(); revealWorkspace(); renderLoadNotice();
    }
  }
  function currentRoute() { return location.hash.replace("#", "") || "home"; }
  function setRoute(route) {
    const valid = ["home", "market", "radar", "health", "control"].includes(route) ? route : "home";
    document.querySelectorAll("[data-view]").forEach((node) => { node.hidden = node.dataset.view !== valid; });
    document.querySelectorAll(".nav-item").forEach((node) => node.classList.toggle("is-active", node.dataset.route === valid));
    const titles = { health: ["", "Market Health"], home: ["CAPITAL INTELLIGENCE", "Capital markets feed"], market: ["MARKNADSFLÖDE", "Finansiella nyheter"], radar: ["BOLAGSRADAR", "Bolagsradar"], control: ["KONTROLLRUM", "Spårbarhet före tempo."] };
    $("#viewDescription").textContent = { home: "Senaste publicerade finansieringsaffärerna till vänster. Marknadspuls och historik till höger.", radar: "Tät screeningvy för kapitalbehov. Runway vid rapport och runway idag (framrullad) hålls åtskilda.", market: "Dedikerat deal feed. Ett objekt per affär, med lifecycle och AI:s svar synligt direkt.", health: "Historiska snapshots ger jämförbarhet över tid och undviker look-ahead.", control: "Research, arbetskö och spårbarhet." }[valid];
    $("#viewKicker").textContent = titles[valid][0]; $("#viewTitle").textContent = titles[valid][1];
    if (location.hash !== `#${valid}`) history.replaceState(null, "", `#${valid}`);
    if (valid === "control") refreshOperations().catch(() => notice("Kunde inte uppdatera åtgärdskön."));
    else clearTimeout(state.operationTimer);
  }
  function renderMetrics() {
    const values = state.overview?.metrics || {};
    const priority = values.urgent_under_6m;
    const thisMonth = state.health.months?.at(-1);
    const metrics = [["Affärer denna månad", thisMonth?.deal_count], ["Bolag i radarn", values.total_issuers], ["Runway <6M", priority]];
    $("#metrics").innerHTML = metrics.map(([name,value]) => `<article class="metric"><span>${escapeHtml(name)}</span><strong>${value == null ? "—" : number(value)}</strong></article>`).join("");
    $("#pulseNeed").innerHTML = sectionMarkup("overview") + `<div class="pulse-need"><div><strong>${priority == null ? "—" : `${number(priority)} bolag`}</strong><span>har &lt;6 mån runway i screening</span></div></div>`;
  }
  function stage(item) {
    const value = String(item.status || "");
    if (["registered"].includes(value)) return "completed";
    if (["completed", "preliminary_outcome", "outcome", "result", "subscribed", "allocated"].includes(value)) return "outcome";
    if (["subscription_period", "terms", "approved"].includes(value)) return "terms";
    if (["announced", "proposed", "conditional"].includes(value)) return "announcement";
    return "unknown";
  }
  function stageMarkup(item) {
    const key = stage(item);
    return `<span class="badge ${key}" title="${escapeHtml(item.status_label || item.status)}">${escapeHtml(key === "unknown" ? (item.status_label || item.status || "—") : key.toUpperCase())}</span>`;
  }
  function lifecycleProgress(item) {
    const status = String(item.status || "");
    const stages = [
      ["announced", "Offentliggjord"],
      ["approved", "Godkänd"],
      ["subscription_period", "Teckningsperiod"],
      ["outcome", "Utfall"],
    ];
    const positionByStatus = {
      proposed: 1, announced: 1, conditional: 1,
      approved: 2, terms: 2,
      subscription_period: 3,
      preliminary_outcome: 4, outcome: 4, result: 4, subscribed: 4, allocated: 4,
      completed: 4, registered: 4,
      cancelled: 0,
    };
    const position = positionByStatus[status] || 0;
    const label = status === "cancelled" ? (item.status_label || "Avbruten") : (item.status_label || stages[position - 1]?.[1] || "Status ej fastställd");
    return { position, total: stages.length, label, stages };
  }
  function lifecycleProgressMarkup(item) {
    const progress = lifecycleProgress(item);
    const stepMarkup = progress.stages.map(([key, name], index) => {
      const state = index + 1 < progress.position ? "is-complete" : index + 1 === progress.position ? "is-current" : "";
      return `<span class="lifecycle-progress-step ${state}" title="${escapeHtml(name)}"><i></i><span>${escapeHtml(name)}</span></span>`;
    }).join("");
return `<div class="lifecycle-progress" role="progressbar" aria-label="Transaktionens lifecycle" aria-valuemin="0" aria-valuemax="${progress.total}" aria-valuenow="${progress.position}" aria-valuetext="${escapeHtml(progress.label)}"><div class="lifecycle-progress-meta"><span>Lifecycle</span><strong>${escapeHtml(progress.label)}</strong></div><div class="lifecycle-progress-track" aria-hidden="true"><span class="lifecycle-fill-${progress.position}"></span></div><div class="lifecycle-progress-steps" aria-hidden="true">${stepMarkup}</div></div>`;
  }
  function institution(item, role) {
    return (item.institutions || []).filter(p => p.role === role).map(p => p.name).join(", ");
  }

  function guaranteeTrancheLabel(tier) {
    const key = String(tier || "").trim().toLowerCase().replace(/[\s_-]+/g, "_");
    const labels = {
      top: "Toppgaranti", top_guarantee: "Toppgaranti",
      middle: "Mellangaranti", mid: "Mellangaranti", middle_guarantee: "Mellangaranti",
      bottom: "Bottengaranti", bottom_guarantee: "Bottengaranti",
    };
    return labels[key] || String(tier || "Garantiåtagande");
  }

  function guaranteeTrancheField(tranche, name) {
    const raw = tranche?.[name];
    return raw && typeof raw === "object" && !Array.isArray(raw)
      ? raw
      : { value: raw, source_document_ids: tranche?.source_document_ids || [], basis: tranche?.basis || "" };
  }

  function guaranteeTrancheSources(tranche) {
    return ["commitment_amount", "fee_rate", "fee_amount", "utilized_amount"]
      .flatMap((name) => guaranteeTrancheField(tranche, name).source_document_ids || [])
      .filter(Boolean)
      .filter((id, index, all) => all.indexOf(id) === index);
  }

  function guaranteeTrancheBasis(tranche) {
    const basis = ["commitment_amount", "fee_rate", "fee_amount", "utilized_amount"]
      .map((name) => guaranteeTrancheField(tranche, name).basis)
      .find(Boolean) || tranche?.basis || "";
    if (/(pro[_ ]?forma|derived|estimated|calculated)/.test(basis)) return "PRO FORMA";
    if (guaranteeTrancheSources(tranche).length) return "KÄLLRAPPORTERAT";
    return "UNDERLAG SAKNAS";
  }

  function guaranteeTrancheBasisClass(tranche) {
    const label = guaranteeTrancheBasis(tranche);
    return label === "PRO FORMA" ? "is-pro-forma" : label === "KÄLLRAPPORTERAT" ? "is-source" : "is-missing";
  }

  function guaranteeTranchesMarkup(tranches, currency) {
    if (!Array.isArray(tranches) || !tranches.length) return "";
    const rows = tranches.map((tranche) => {
      const commitment = guaranteeTrancheField(tranche, "commitment_amount");
      const utilized = guaranteeTrancheField(tranche, "utilized_amount");
      const feeAmount = guaranteeTrancheField(tranche, "fee_amount");
      const feeRate = guaranteeTrancheField(tranche, "fee_rate");
      const sourceCount = guaranteeTrancheSources(tranche).length;
      const fee = [financialAmount(feeAmount.value, currency), percent(feeRate.value)].filter((value) => value !== "Ej angivet").join(" · ") || "Ej angivet";
      const sourceNote = sourceCount ? `${sourceCount} källdokument` : "Ingen källdokumentkoppling";
      return `<article class="guarantee-tranche"><header><div><strong>${escapeHtml(guaranteeTrancheLabel(tranche.tier))}</strong><small>${escapeHtml(sourceNote)}</small></div><span class="guarantee-tranche-basis ${guaranteeTrancheBasisClass(tranche)}">${escapeHtml(guaranteeTrancheBasis(tranche))}</span></header><dl>${detailRows([["Åtagande", financialAmount(commitment.value, currency)],["Utnyttjat", financialAmount(utilized.value, currency)],["Garantiersättning", fee]])}</dl></article>`;
    }).join("");
    return `<section class="issuer-detail-section guarantee-tranches"><div><p class="label">GARANTIUPPLÄGG</p><h3>Garanti-trancher</h3><p class="issuer-detail-note">Ersättning och utnyttjande visas per garantiåtagande. Källrapporterade värden och pro forma hålls åtskilda.</p></div><div class="guarantee-tranche-grid">${rows}</div></section>`;
  }

  function transactionMarkup(item, compact = false) {
    const title = escapeHtml(issuerName(item));
    const open = `<button class="transaction-title" type="button" data-open-transaction="${escapeHtml(item.id)}">${title}</button>`;
    const kind = `<span class="transaction-kind ${escapeHtml(item.category)}">${escapeHtml(item.category_label || label(item.category))}</span>`;
    const related = item.related_transactions?.length ? `<details class="related-notifications"><summary>${item.related_transactions.length} relaterade händelser</summary>${item.related_transactions.map(child=>`<div><button type="button" data-open-transaction="${escapeHtml(child.id)}">${escapeHtml(child.headline || label(child.category))}</button><small>${escapeHtml(label(child.category))} · ${transactionAmount(child)} · ${escapeHtml(date(child.published_at))}</small></div>`).join('')}<small>Separata transaktioner. Belopp och kassaeffekter slås inte ihop.</small></details>` : '';
    const commentary = `<div class="transaction-comment"><p>${escapeHtml(item.summary || "")}</p><small class="comment-origin">${item.editorial_source === "ai" ? "AI svarar och skriver" : "Källreferat"}</small>${related}${compact ? "" : '<span class="deal-open">Öppna deal detail →</span>'}</div>`;
    const advisor = escapeHtml(institution(item, "Finansiell rådgivare") || "—");
    const agent = escapeHtml(institution(item, "Emissionsinstitut") || "—");
    const bridgeAlert = item.details?.bridge_loan_alert;
    const bridgeAlertClass = bridgeAlert?.severity === "high" ? " bridge-alert" : "";
    const bridgeAlertBadge = bridgeAlert?.severity === "high" ? `<span class="badge bridge-alert-badge" title="${escapeHtml(bridgeAlert.message)}">BRYGGVARNING</span>` : "";
    if (compact) return `<article class="compact-row${bridgeAlertClass}" data-deal-row="${escapeHtml(item.id)}"><time>${escapeHtml(date(item.published_at))}</time><div>${open}${kind}${bridgeAlertBadge}</div><div class="stage-cell">${stageMarkup(item)}</div><span class="amount" title="${escapeHtml(item.amount_basis || "")}">${transactionAmount(item)}</span><div class="institution">${advisor}<small>${agent}</small></div>${commentary}</article>`;
    return `<article class="transaction-row${bridgeAlertClass}" data-deal-row="${escapeHtml(item.id)}"><time>${escapeHtml(dateTime(item.published_at))}</time><div>${open}<div class="deal-tags">${kind}${stageMarkup(item)}${bridgeAlertBadge}</div>${lifecycleProgressMarkup(item)}</div><span class="amount" title="${escapeHtml(item.amount_basis || "")}">${transactionAmount(item)}</span><div class="institution"><span>Rådgivare</span>${advisor}</div><div class="institution"><span>Issuing agent</span>${agent}</div>${commentary}</article>`;
  }

  function renderTransactions() {
    $("#homeTransactions").innerHTML = sectionMarkup("homeFeed") + (section("homeFeed").loaded ? state.homeTransactions.map(item => transactionMarkup(item, true)).join("") || '<p class="empty">Inga finansieringsaffärer i aktuellt underlag.</p>' : "");
    $("#transactions").innerHTML = sectionMarkup("deals") + (section("deals").loaded ? state.transactions.map(item => transactionMarkup(item)).join("") || '<p class="empty">Inga affärer matchar filtreringen.</p>' : "");
    $("#transactionPage").textContent = `Sida ${state.transactionMeta.page ?? state.page}`;
    $("#transactionStatus").textContent = sectionMessage("deals") || `${state.transactionMeta.total ?? state.transactions.length} grupperade affärer`;
    $("#newer").disabled = state.page <= 1;
    $("#older").disabled = !state.transactionMeta.has_next || section("deals").status === "loading";
  }
  function runwayMarkup(value, positive = false) {
    if (positive) return '<span class="runway band-positive">Cash-flow positive</span>';
    if (value == null) return '<span class="runway unknown">—</span>';
    const band = [3,6,12,18].findIndex(limit => value < limit);
    return `<span class="runway band-${band < 0 ? 4 : band}">${new Intl.NumberFormat("sv-SE",{maximumFractionDigits:1}).format(value)} mån</span>`;
  }
  function money(value, currency) {
    if (value == null) return "—";
    return `${new Intl.NumberFormat("sv-SE",{maximumFractionDigits:1}).format(value / 1000000)} M${escapeHtml(currency || "")}`;
  }

  function issuerComment(item, proForma) {
    const calculated = proForma.pro_forma_comment;
    if (calculated) {
      return `<span class="comment-origin">PRO FORMA · SAMMA UNDERLAG SOM ADJUSTED</span><p>${escapeHtml(calculated)}</p>`;
    }
    const screening = item.headline;
    if (screening) return `<span class="comment-origin">AI-SCREENING</span><p>${escapeHtml(screening)}</p>`;
    return '<span class="comment-origin">UNDERLAG SAKNAS</span><p>Ingen beräknad kommentar innan rapportfakta och runway finns.</p>';
  }

  function renderIssuers() {
    const issuers = state.issuers;
    const row = item => {
      const p = state.proForma[item.id] || {};
      return `<tr class="issuer-table-row" data-issuer-id="${item.id}" tabindex="0" aria-label="Öppna bolagsanalys för ${escapeHtml(issuerName(item))}"><td><strong>${escapeHtml(issuerName(item))}</strong><small>${escapeHtml(item.symbols || item.market || "")}</small></td><td>${runwayMarkup(p.reported_runway_months,p.cash_flow_positive)}</td><td>${runwayMarkup(p.adjusted_runway_months,p.cash_flow_positive)}</td><td class="delta">${p.delta_runway_months == null ? "—" : `${p.delta_runway_months >= 0 ? "+" : ""}${decimal(p.delta_runway_months)} mån`}</td><td>${money(p.cash,p.currency)}</td><td>${money(p.monthly_burn,p.currency)}</td><td class="ai-cell">${issuerComment(item,p)}</td><td>${escapeHtml(relativeDate(item.analysis_updated_at))}</td><td><button class="pill" data-override-issuer="${item.id}" ${p.events?.length ? "" : "disabled"}>${p.events?.some(e => e.weight !== 1) ? "Justerad" : "100 %"}</button></td></tr>`;
    };
    $("#issuerList").innerHTML = issuers.map(row).join("") || (section("issuers").loaded ? '<tr><td colspan="9" class="empty">Inga bolag matchar filtreringen.</td></tr>' : `<tr><td colspan="9">${sectionMarkup("issuers")}</td></tr>`);
    const from = state.issuerTotal ? state.issuerRenderedPageIndex * 25 + 1 : 0;
    const to = Math.min(from + issuers.length - 1, state.issuerTotal);
    $("#radarCount").textContent = [section("issuers").loaded ? `${number(from)}–${number(Math.max(from,to))} av ${number(state.issuerTotal)} bolag` : "", sectionMessage("issuers"), sectionMessage("analysis")].filter(Boolean).join(" · ");
    $("#issuerPage").textContent = `Sida ${state.issuerRenderedPageIndex + 1}`;
    $("#issuerPrevious").disabled = state.issuerPageIndex === 0;
    $("#issuerNext").disabled = !state.issuerNextCursor || section("issuers").status === "loading";
    document.querySelectorAll(".issuer-sort").forEach((button) => {
      const active = button.dataset.issuerSort === state.issuerFilters.sort;
      button.classList.toggle("is-active", active);
      button.dataset.direction = active ? state.issuerFilters.direction : "";
      button.closest("th")?.setAttribute("aria-sort", active ? (state.issuerFilters.direction === "asc" ? "ascending" : "descending") : "none");
    });
    const summary = [["Kort runway", "runway_max", "6"], ["Kapitalbehov", "capital_need", "yes"], ["Ej analyserade", "analysis_status", "not_analyzed"]];
    $("#radarSummary").innerHTML = summary.map(([title, key, value]) => `<button class="radar-chip ${state.issuerFilters[key] === value ? "is-active" : ""}" data-filter-key="${key}" data-filter-value="${value}" type="button">${title}</button>`).join("");
    $("#priorityIssuers").innerHTML = state.issuers.slice().sort((a, b) => (Number(a.runway_months) || 999) - (Number(b.runway_months) || 999)).slice(0, 5).map((item) => `<article class="issuer-row"><div><strong>${escapeHtml(issuerName(item))}</strong><small>Runway ${escapeHtml(item.runway_months ?? "–")} mån · ${escapeHtml(item.alert_level || "ej klassad")}</small></div><span class="tone">${escapeHtml(item.need_timing || "bevaka")}</span></article>`).join("") || '<p class="empty">Inga bolag i radarn.</p>';
  }
  function sourceMarkup(document) {
    const url = safeUrl(document.canonical_url || document.source_url || document.url);
    const title = escapeHtml(document.title || "Källa utan rubrik");
    const metadata = [date(document.published_at), document.status_label || document.profile_category || document.relevance_reason].filter(Boolean).join(" · ");
    return `<li><div><strong>${title}</strong><small>${escapeHtml(metadata)}</small></div>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Öppna källa</a>` : "<span>Ingen verifierbar länk</span>"}</li>`;
  }
  function financingMarkup(event) {
    const value = event.net_amount ?? event.gross_amount ?? event.cash_effect_amount ?? event.committed_amount ?? event.conditional_amount;
    const title = event.description || event.event_type || "Finansieringshändelse";
    const metadata = [event.status, (event.effective_date || event.announcement_date) ? date(event.effective_date || event.announcement_date) : ""].filter(Boolean).join(" · ");
    const basis = event.net_amount != null ? "Nettolikvid" : event.gross_amount != null ? "Bruttolikvid · netto ej fastställt" : event.cash_effect_amount != null ? "Kassaeffekt" : event.committed_amount != null ? "Säkerställd likviditet" : event.conditional_amount != null ? "Villkorad likviditet" : "Belopp ej kvantifierat";
    return `<li><div><strong>${escapeHtml(title)}</strong><small>${escapeHtml(metadata || "Källhänvisning saknas")}</small></div><span>${escapeHtml(financialAmount(value, event.currency || ""))}<small>${escapeHtml(basis)}</small></span></li>`;
  }
  function renderIssuerDetail(data) {
    const drafts = Array.from(document.querySelectorAll("#issuerDetailContent form input, #issuerDetailContent form textarea, #issuerDetailContent form select"))
      .filter(input => input.name && input.type !== "password" && input.form?.id)
      .map(input => ({ form: input.form.id, name: input.name, value: input.value, focused: input === document.activeElement }));
    const issuer = data.issuer || {}, a = data.analysis_summary || data.interpretation || {}, r = data.runway || {}, p = data.pro_forma || {};
    $("#issuerDetailTitle").textContent = issuer.canonical_name || "Bolagsanalys";
    $("#issuerDetailMeta").textContent = list(data.securities).map(s => [s.symbol,s.market].filter(Boolean).join(" · ")).join(" / ");
    const metrics = [["Runway vid rapport",runwayMarkup(p.reported_runway_months,p.cash_flow_positive)],["Runway idag utan ny finansiering",runwayMarkup(p.runway_from_today_months,p.cash_flow_positive)],["Runway idag pro forma",runwayMarkup(p.adjusted_runway_months,p.cash_flow_positive)],["Rapportens ålder",p.months_since_report == null ? "—" : `${decimal(p.months_since_report)} mån`],["Finansieringseffekt",p.delta_runway_months == null ? "—" : `${p.delta_runway_months >= 0 ? "+" : ""}${decimal(p.delta_runway_months)} mån`],["Cash",money(p.cash,p.currency)],["Monthly burn",money(p.monthly_burn,p.currency)],["Need score",escapeHtml(a.need_score ?? r.adjusted_need_score ?? "—")]];
    const weights = (p.events || []).map(e => `<li><div><strong>${escapeHtml(e.category_label || label(e.category))}</strong><small>${escapeHtml(date(e.announcement_date))} · ${escapeHtml(e.status || "")}</small><small>${escapeHtml(e.amount_basis || "")} ${escapeHtml(e.exclusion_reason || "")}</small></div><div>${money(e.amount,e.currency)}<small>Kassaeffekt ${e.cash_effect == null ? "Okänd" : money(e.cash_effect,e.currency)}</small></div><select class="event-weight" data-weight="${escapeHtml(e.transaction_id)}" aria-label="Vikt för ${escapeHtml(e.category_label || label(e.category))} ${escapeHtml(e.announcement_date)}" ${e.exclusion_reason ? "disabled" : ""}>${[1,.75,.5,.25,0].map(w=>`<option value="${w}" ${e.weight === w ? "selected" : ""}>${w*100} %</option>`).join("")}</select></li>`).join("") || '<li>Inga post-report-finansieringar i tillgängligt underlag.</li>';
    const evidence = Array.isArray(data.financial_facts) ? data.financial_facts : (data.interpretation?.evidence || []);
    const evidenceHtml = evidence.map(e => `<article class="evidence-row"><strong>${escapeHtml(e.fact_type || e.label || "Evidens")}</strong><p>${escapeHtml(e.normalized_value ?? e.value ?? "")} ${escapeHtml(e.currency || "")} · Sida ${escapeHtml(e.source_page ?? e.page ?? "—")} · Confidence ${e.confidence == null ? "—" : `${Math.round(e.confidence*100)} %`}</p>${e.source_excerpt || e.excerpt ? `<blockquote>${escapeHtml(e.source_excerpt || e.excerpt)}</blockquote>` : ""}<small>Dokument ${escapeHtml(e.document_id ?? "—")}</small></article>`).join("") || '<p class="empty">Ingen detaljerad evidens i tillgängligt underlag.</p>';
    const reports = (data.report_history || data.analysis_versions || []).map(v=>`<li><div><strong>${escapeHtml(v.headline || v.report_period_end || "Analysversion")}</strong><small>${escapeHtml(date(v.created_at || v.publication_date))}</small></div><span>${escapeHtml(v.runway_months == null ? "" : `${v.runway_months} mån`)}</span></li>`).join("");
    const notes = (data.notes || data.issuer_notes || []).map(n => `<p class="workflow-notes">${escapeHtml(n.body || n.note || n.content || "")}</p>`).join("");
    const legacyScreening = p.pro_forma_comment ? "" : `<section class="panel"><h3>AI — screening av kapitalbehov</h3><p>${escapeHtml(a.interpretation_headline || a.headline || "Ingen färdig tolkning")}</p><p>${escapeHtml(a.summary || data.interpretation?.summary || "")}</p><p class="issuer-detail-note">${escapeHtml(a.need_timing || r.need_timing || "")} · ${escapeHtml(a.evidence_posture || r.evidence_posture || "")}</p></section>`;
    $("#issuerDetailContent").innerHTML = `<div class="issuer-detail-kpis">${metrics.map(([name,value])=>`<article><span>${name}</span><strong>${value}</strong></article>`).join("")}</div><div class="issuer-layout"><div>${legacyScreening}<section class="panel"><h3>Analyskommentar · Runway idag</h3><p class="pro-forma-comment">${escapeHtml(p.pro_forma_comment || "Ingen beräknad pro forma-kommentar innan rapportfakta och runway finns.")}</p><p class="issuer-detail-note">${escapeHtml(p.basis || "")} Runway vid rapport ändras inte. Screeningvärden bevaras.</p><ul class="issuer-detail-list">${weights}</ul><p id="previewStatus" role="status" tabindex="-1">${Object.keys(state.previewWeights).length ? "Förhandsvisning – osparade vikter." : "Sparade vikter. Standard: genomförd 100 %, annonserad 0 %."}</p>${(p.events || []).some(e=>!e.exclusion_reason) ? `<details><summary>Spara ändrade vikter</summary><form id="saveWeights" class="override-form"><label>Ändringsorsak<input name="reason" required maxlength="1000"></label><label>Behörig API-session<input name="token" type="password" autocomplete="off" required></label><button type="submit">Bekräfta och spara vikter</button><small>Sessionsuppgiften används endast för denna skrivning och sparas inte i webbläsaren.</small></form></details>` : ""}</section><section class="panel"><h3>Finansieringshistorik</h3><ul class="issuer-detail-list">${list(data.financing_events).map(financingMarkup).join("") || "<li>Ingen finansieringshistorik i underlaget.</li>"}</ul></section><section class="panel"><h3>Report history / finansiella fakta</h3><ul class="issuer-detail-list">${reports || "<li>Ingen rapporthistorik i underlaget.</li>"}</ul></section><section class="panel"><h3>Analyst notes & workflow</h3><p>${escapeHtml(data.workflow?.status || "new")} · ${escapeHtml(data.workflow?.owner || "")} · ${escapeHtml(data.workflow?.next_action || "")}</p>${notes}</section></div><div><section class="panel"><h3>Evidens & datakällor</h3>${evidenceHtml}</section><section class="panel"><h3>Debt / liquidity</h3><dl>${detailRows([["Bekräftad likviditet",financialAmount(r.committed_liquidity,r.currency)],["Villkorad likviditet",financialAmount(r.conditional_liquidity,r.currency)],["Bekräftad kassajustering",financialAmount(r.confirmed_post_report_adjustment,r.currency)],["Rapportperiod",relativeDate(r.report_period_end)]])}</dl></section><section class="panel"><h3>Källdokument</h3><ul class="issuer-detail-list">${list(data.documents).map(sourceMarkup).join("") || "<li>Källdokument saknas.</li>"}</ul></section></div></div>`;
    for (const draft of drafts) {
      const input = document.getElementById(draft.form)?.elements.namedItem(draft.name);
      if (input) { input.value = draft.value; if (draft.focused) input.focus(); }
    }
    const message = data.pro_forma_status === "loading" ? "Runway idag laddas separat. Bolagsuppgifter och källor visas redan." : data.pro_forma_status === "error" ? "Runway idag kunde inte laddas. Bolagsuppgifter och källor finns kvar. Uppdatera för att försöka igen." : data.pro_forma_status === "stale" ? "Äldre adjusted-runway-underlag visas medan uppdateringen pågår." : "";
    if (message) $("#issuerDetailContent").insertAdjacentHTML("afterbegin", `<p class="empty" role="status">${escapeHtml(message)}</p>`);
  }
  async function openIssuerDetail(issuerId, trigger) {
    const id = Number(issuerId); if (!Number.isInteger(id) || id < 1) return;
    const request = beginRequest("detail");
    state.issuerDetailId = id; state.issuerDetail = null; state.transactionDetailId = null; state.previewWeights = {}; $("#issuerDetail").classList.add("issuer-page"); state.issuerDetailTrigger = trigger || document.activeElement;
    $("#issuerDetailBackdrop").hidden = false; $("#issuerDetail").classList.add("is-open"); $("#issuerDetail").inert = false; $("#workspace").inert = true; $("#issuerDetail").setAttribute("aria-hidden", "false"); document.body.classList.add("issuer-detail-open");
    $("#issuerDetailKicker").textContent = "BOLAGSANALYS"; $("#issuerDetailTitle").textContent = "Laddar bolag"; $("#issuerDetailMeta").textContent = ""; $("#issuerDetailContent").innerHTML = '<p class="empty">Hämtar analys, finansiering och källor…</p>';
    $("#issuerDetailClose").focus();
    let company = null, analysis = null;
    const renderDetail = () => {
      if (!request.current() || state.issuerDetailId !== id) return;
      if (!company) {
        if (section("detailIssuer").status === "error") $("#issuerDetailContent").innerHTML = sectionMarkup("detailIssuer");
        return;
      }
      company.pro_forma = analysis || {};
      company.pro_forma_status = section("detailAnalysis").status;
      state.issuerDetail = company;
      renderIssuerDetail(company);
    };
    state.sections.detailIssuer = { status: "loading", loaded: false };
    state.sections.detailAnalysis = { status: "loading", loaded: false };
    await Promise.allSettled([
      loadResource("detailIssuer", `/api/issuer/${id}`, payload => { company = payload; }, renderDetail, request),
      loadResource("detailAnalysis", `/api/radar/analysis?issuer_ids=${id}`, payload => {
        if (!Array.isArray(payload.items)) throw new Error("Ogiltigt analysunderlag");
        analysis = payload.items[0] || {};
      }, renderDetail, request),
    ]);
  }
  function renderTransactionWorkspace(item, caseData, view) {
    const { details, lifecycle, dossierHtml, guaranteeTranchesHtml, reactionHtml,
      bridgeAlertHtml, points, termRows, sources, ratio, outcomeBasis,
      editorialButton, editorialStatus } = view;
    const currency = details.currency || item.currency || "";
    const docs = (Array.isArray(caseData.documents) ? caseData.documents : Array.isArray(caseData.source_documents) ? caseData.source_documents : [])
      .filter(document => document.in_selected_transaction !== false);
    const selectedDocumentIds = new Set((item.source_document_ids || []).map(Number));
    const selectedEventIds = new Set((Array.isArray(caseData.financing_event_evidence) ? caseData.financing_event_evidence : [])
      .filter(row => selectedDocumentIds.has(Number(row.document_id))).map(row => Number(row.financing_event_id)));
    const correctionGroups = caseData.audit || caseData.corrections || {};
    const auditKinds = [["grouping_corrections", "Gruppering"], ["value_corrections", "Transaktionsvärde"],
      ["financing_corrections", "Finansieringsfakta"], ["field_proposals", "Fältförslag"]];
    const correctionItems = auditKinds.flatMap(([key, kind]) =>
      (Array.isArray(correctionGroups[key]) ? correctionGroups[key] : []).filter(entry =>
        entry.transaction_id === item.id ||
        (entry.source_document_id != null && selectedDocumentIds.has(Number(entry.source_document_id))) ||
        (entry.document_id != null && selectedDocumentIds.has(Number(entry.document_id))) ||
        (entry.financing_event_id != null && selectedEventIds.has(Number(entry.financing_event_id)))).map(entry => ({ ...entry, kind })));
    const appliedIds = Array.isArray(item.applied_transaction_correction_ids) ? item.applied_transaction_correction_ids : [];
    const appliedCorrections = correctionItems.filter(entry => entry.status === "applied");
    const appliedCount = Math.max(appliedIds.length, appliedCorrections.length);
    const appliedLabels = appliedCorrections.length
      ? appliedCorrections.map(entry => `${entry.kind} #${entry.id}`).join(", ")
      : appliedIds.join(", ");
    const fields = Array.isArray(item.effective_corrected_fields) ? item.effective_corrected_fields : [];
    const original = item.original_transaction_values || {};
    const effective = item.effective_transaction_values || {};
    const correctionRows = fields.map(field => `<div><dt>${escapeHtml(field.replace(/_/g, " "))}</dt><dd><span>${escapeHtml(original[field] ?? "Ej angivet")}</span><strong>${escapeHtml(effective[field] ?? "Ej angivet")}</strong></dd></div>`).join("");
    const eventChanges = (Array.isArray(caseData.financing_events) ? caseData.financing_events : [])
      .filter(event => selectedEventIds.has(Number(event.financing_event_id)))
      .flatMap(event => Object.keys(event.effective_values || {}).filter(field =>
        JSON.stringify(event.original_values?.[field]) !== JSON.stringify(event.effective_values[field]))
        .map(field => ({ eventId: event.financing_event_id, field,
          before: event.original_values?.[field], after: event.effective_values[field] }))).slice(0, 8);
    const eventRows = eventChanges.map(change => `<div><dt>Händelse #${escapeHtml(change.eventId)} · ${escapeHtml(change.field.replace(/_/g, " "))}</dt><dd><span>${escapeHtml(String(change.before ?? "Ej angivet").slice(0, 90))}</span><strong>${escapeHtml(String(change.after ?? "Ej angivet").slice(0, 90))}</strong></dd></div>`).join("");
    const correctionList = correctionItems.map(entry => `<li><strong>${escapeHtml(entry.kind)}${entry.id != null ? ` #${escapeHtml(entry.id)}` : ""}</strong><small>${escapeHtml([entry.status, entry.field_name || entry.action, entry.applied_at || entry.created_at ? date(entry.applied_at || entry.created_at) : ""].filter(Boolean).join(" · "))}</small></li>`).join("") || "<li>Ingen rättelsehistorik i akten.</li>";
    const sourceCount = Number(details.source_count ?? (docs.length || lifecycle.length));
    const sourceList = docs.length ? docs.map(sourceMarkup).join("") : sources;
    const timeline = lifecycle.map(entry => `<li><span class="deal-timeline-dot"></span><div><small>${escapeHtml(date(entry.published_at))}</small><strong>${escapeHtml(entry.status_label || entry.status || "Nytt besked")}</strong><span>${escapeHtml(entry.title || "Källdokument")}</span>${safeUrl(entry.url) ? `<a href="${escapeHtml(safeUrl(entry.url))}" target="_blank" rel="noopener noreferrer">Läs originalet ↗</a>` : ""}</div></li>`).join("") || "<li>Ingen källhistorik i underlaget.</li>";
    const runway = caseData.runway || caseData.analysis || {};
    const months = value => value == null ? "Ej beräknad" : `${decimal(value)} mån`;
    const runwayHtml = Object.keys(runway).length ? `<section class="deal-workspace-card"><p class="label">KAPITALEFFEKT</p><h3>Bolagets runway</h3><dl class="deal-sidebar-dl">${detailRows([["Reported", months(runway.reported?.runway_months ?? runway.reported_runway_months)], ["Adjusted", months(runway.adjusted?.runway_months ?? runway.adjusted_runway_months)]])}</dl></section>` : "";
    $("#issuerDetailContent").innerHTML = `<section class="deal-workspace">
      <header class="deal-workspace-hero"><div><p class="label">AKTUELL AFFÄRSBILD</p><div class="deal-tags"><span class="transaction-kind ${escapeHtml(item.category)}">${escapeHtml(item.category_label || label(item.category))}</span>${stageMarkup(item)}</div><h3>${escapeHtml(item.status_label || status(item))}</h3><p>${escapeHtml(item.summary || details.overview || "Sammanfattning saknas.")}</p><small>Senaste besked ${escapeHtml(date(lifecycle.at(-1)?.published_at || item.published_at))} · ${sourceCount} källdokument</small></div><div class="deal-hero-amount"><span>Affärsbelopp</span><strong>${transactionAmount(item)}</strong><small>${escapeHtml(item.amount_basis || "Beloppsgrund saknas")}</small></div></header>
      ${bridgeAlertHtml}
      <div class="deal-snapshot"><article><span>Annonserat</span><strong>${escapeHtml(financialAmount(details.announced_amount, currency))}</strong><small>${escapeHtml(details.announced_amount_basis || "Underlag saknas")}</small></article><article><span>Utfall</span><strong>${escapeHtml(financialAmount(details.outcome_amount, currency))}</strong><small>${escapeHtml(outcomeBasis)}</small></article><article><span>Bekräftad kassaeffekt</span><strong>${escapeHtml((details.confirmed_cash_effect_amount == null ? "Okänd" : financialAmount(details.confirmed_cash_effect_amount, currency)))}</strong><small>Effektiv transaktionsbild</small></article><article><span>Utfall / annonserat</span><strong>${escapeHtml(ratio)}</strong><small>${escapeHtml(item.signal_label || "Källbaserad status")}</small></article></div>
      <div class="deal-progress">${lifecycleProgressMarkup(item)}</div>
      <div class="deal-workspace-grid"><div class="deal-workspace-main">
        <article class="deal-workspace-card deal-analysis"><div class="deal-section-head"><div><p class="label">DEAL ANALYSIS</p><h3>Analys & nästa milstolpe</h3></div><span class="deal-source-tag">${item.editorial_source === "ai" ? "Skrivet av AI" : "Källreferat"}</span></div><p class="deal-analysis-lead">${escapeHtml(details.detail_summary || item.summary || details.overview || "Fördjupning saknas.")}</p>${points}<dl class="deal-analysis-facts">${detailRows([["Kapitalanvändning", details.use_of_proceeds || "Ej uttryckligen angiven"], ["Nästa milstolpe", details.next_milestone || "Ej fastställd"]])}</dl><div class="deal-analysis-action">${editorialButton}<p id="editorialStatus" class="issuer-detail-note" role="status">${escapeHtml(editorialStatus)}</p></div></article>
        ${dossierHtml}${guaranteeTranchesHtml}${reactionHtml}
        <section class="deal-workspace-card"><p class="label">VILLKOR</p><h3>Transaktionsstruktur</h3><ul class="issuer-detail-list">${termRows}</ul></section>
        <section class="deal-workspace-card"><div class="deal-section-head"><div><p class="label">KÄLLSPÅR</p><h3>Affärens tidslinje</h3></div><span class="deal-source-tag">${sourceCount} dokument</span></div><ol class="deal-timeline">${timeline}</ol><details class="deal-source-index"><summary>Visa samtliga källdokument</summary><ul class="issuer-detail-list issuer-source-list">${sourceList}</ul></details></section>
      </div><aside class="deal-workspace-side" aria-label="Granskning och parter">
        <section class="deal-workspace-card deal-action-card"><p class="label">GRANSKNING & ÅTGÄRDER</p><h3>Nuvarande läge</h3><span class="deal-review-status ${item.review_required ? "is-open" : "is-clear"}">${item.review_required ? "Granskning krävs" : "Ingen granskningsflagga"}</span><p class="issuer-detail-note">${item.review_required ? "Kontrollera belopp, villkor och källor före användning som beslutsunderlag." : "Ingen granskningsflagga finns i den aktuella transaktionsbilden."}</p><div class="deal-action-count"><strong>${appliedCount}</strong><span>tillämpade auditerade rättelser</span></div>${appliedCount ? `<p class="issuer-detail-note">${escapeHtml(appliedLabels)}</p>` : ""}${correctionRows || eventRows ? `<div class="deal-correction-legend"><span>Ursprungligt</span><strong>Effektivt</strong></div><dl class="deal-correction-rows">${correctionRows}${eventRows}</dl>` : ""}<details><summary>Visa rättelsehistorik</summary><ul class="issuer-detail-list">${correctionList}</ul></details></section>
        ${runwayHtml}
        <section class="deal-workspace-card"><p class="label">PARTER</p><h3>Rådgivare & institut</h3><dl class="deal-sidebar-dl">${detailRows([["Finansiell rådgivare", institution(item, "Finansiell rådgivare") || "Ej angivet"], ["Emissionsinstitut", institution(item, "Emissionsinstitut") || "Ej angivet"]])}</dl></section>
      </aside></div></section>`;
  }

  function renderTransactionDetail(item, caseData = {}) {
    const details = item.details || {}; const lifecycle = Array.isArray(details.lifecycle) ? details.lifecycle : [];
    const dossier = details.financing_dossier || {}; const dossierCurrency = details.currency || item.currency || "";
    const keyPoints = Array.isArray(details.key_points) ? details.key_points : []; const terms = Array.isArray(details.terms) ? details.terms : [];
    const sources = lifecycle.map((entry) => sourceMarkup({ ...entry, canonical_url: entry.url })).join("") || "<li><span>Källhistorik saknas.</span></li>";
    const ratio = details.completion_ratio_pct == null ? "Ej beräknad" : `${new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 1 }).format(details.completion_ratio_pct)} %`;
    const outcomeBasis = details.outcome_amount_basis === "directed_issue_cash_assumption" ? "Riktad emission · behandlas som inbetald" : (details.outcome_amount_basis || "Ännu inte publicerat");
    const points = keyPoints.length ? `<ul class="transaction-detail-points">${keyPoints.map((point) => `<li>${escapeHtml(point)}</li>`).join("")}</ul>` : '<p class="issuer-detail-note">AI bearbetar den fördjupade texten.</p>';
    const termRows = terms.length ? terms.map((term) => `<li><span>${escapeHtml(term)}</span></li>`).join("") : "<li><span>Ytterligare villkor har inte kunnat fastställas.</span></li>";
    const reaction = details.market_reaction;
    const bridgeAlert = details.bridge_loan_alert;
    const bridgeAlertHtml = bridgeAlert ? `<section class="bridge-loan-alert ${escapeHtml(bridgeAlert.severity || "info")}"><strong>${bridgeAlert.severity === "high" ? "Brygglånevarning" : "Brygglån med emissionsbesked"}</strong><p>${escapeHtml(bridgeAlert.message || "")}</p></section>` : "";
    const reactionRows = reaction?.daily_bars?.length ? reaction.daily_bars.map((bar) => `<tr><td>${escapeHtml(date(bar.date))}</td><td>${escapeHtml(marketPrice(bar.close))}</td><td>${escapeHtml(number(bar.volume))}</td></tr>`).join("") : "";
    const reactionHtml = reaction ? `<section class="issuer-detail-section"><div><p class="label">MARKNADSREAKTION</p><h3>Fem handelsdagar efter utfall</h3></div><div class="issuer-detail-kpis"><article><span>Snitt stängningskurs</span><strong>${escapeHtml(marketPrice(reaction.average_close_5d))}</strong></article><article><span>Volymvägd kurs</span><strong>${escapeHtml(marketPrice(reaction.volume_weighted_average_close_5d))}</strong></article><article><span>Omsatta aktier</span><strong>${escapeHtml(number(reaction.total_volume_5d))}</strong></article></div><table class="issuer-detail-table"><thead><tr><th>Datum</th><th>Stängning</th><th>Omsatt antal aktier</th></tr></thead><tbody>${reactionRows}</tbody></table><p class="issuer-detail-note">${escapeHtml(reaction.source || "Marknadskälla")} · ${reaction.complete ? "Fem kompletta handelsdagar" : "Fönstret är ännu inte komplett"}</p></section>` : "";
    const dossierHasValues = Object.entries(dossier).some(([key, value]) => !["canonical_round_keys", "net_amount_estimated"].includes(key) && value !== null && value !== undefined && value !== "");
    const amountAndPercent = (amountValue, percentValue) => [financialAmount(amountValue, dossierCurrency), percent(percentValue)].filter(value => value !== "Ej angivet").join(" · ") || "Ej angivet";
    const netBasis = dossier.net_amount_basis === "source_reported_actual" ? "Källrapporterad i slutligt utfall" : dossier.net_amount_basis === "source_reported" ? "Källrapporterad vid annonsering" : dossier.net_amount_basis === "derived_gross_less_source_costs" ? "Beräknad: brutto minus källrapporterade kostnader" : "Underlag saknas";
    const costBasis = dossier.transaction_cost_basis === "source_reported_excluding_guarantee_fee" ? "Exklusive garantiersättning" : "Källrapporterad kostnad";
    const guaranteeTranchesHtml = guaranteeTranchesMarkup(dossier.guarantee_tranches, dossierCurrency);
    const feeOptions = dossier.guarantee_fee_options || {};
    const dossierHtml = dossierHasValues ? `<section class="issuer-detail-section financing-dossier"><div><p class="label">EMISSIONSAKT</p><h3>Likvid, säkerställande och garanti</h3><p class="issuer-detail-note">Källrapporterade värden hålls isär från beräknad nettolikvid och outnyttjade kreditramar.</p></div><div class="issuer-detail-kpis"><article><span>Annonserad brutto</span><strong>${escapeHtml(financialAmount(dossier.announced_gross_amount, dossierCurrency))}</strong><small>Villkor</small></article><article><span>Faktiskt brutto</span><strong>${escapeHtml(financialAmount(dossier.actual_gross_amount, dossierCurrency))}</strong><small>Slutligt utfall</small></article><article><span>Beräknad netto</span><strong>${escapeHtml(financialAmount(dossier.estimated_net_amount, dossierCurrency))}</strong><small>Brutto minus källrapporterade kostnader</small></article><article><span>Annonserad netto</span><strong>${escapeHtml(financialAmount(dossier.announced_net_amount, dossierCurrency))}</strong><small>Endast uttryckligt källvärde</small></article><article><span>Faktisk netto</span><strong>${escapeHtml(financialAmount(dossier.actual_net_amount, dossierCurrency))}</strong><small>Endast uttryckligt slutligt källvärde</small></article></div><dl>${detailRows([["Kreditram", financialAmount(dossier.credit_limit, dossierCurrency)],["Utbetalt/utnyttjat", financialAmount(dossier.drawn_amount, dossierCurrency)],["Bekräftad kassaeffekt", (dossier.confirmed_cash_effect_amount == null ? "Okänd" : financialAmount(dossier.confirmed_cash_effect_amount, dossierCurrency))],["Outnyttjad kredit", financialAmount(dossier.undrawn_commitment, dossierCurrency)],["Teckningsförbindelser", amountAndPercent(dossier.subscription_commitment_amount, dossier.subscription_commitment_pct)],["Garantiåtaganden", amountAndPercent(dossier.guarantee_commitment_amount, dossier.guarantee_commitment_pct)],["Totalt säkerställt", amountAndPercent(dossier.secured_total_amount, dossier.secured_total_pct)],["Tilldelade/utgivna aktier", dossier.shares_issued == null ? "Ej angivet" : Number(dossier.shares_issued).toLocaleString("sv-SE")],["Slutlig teckning", percent(dossier.final_subscription_pct)],["Tecknat med företrädesrätt", percent(dossier.subscription_with_rights_pct)],["Tecknat utan företrädesrätt", percent(dossier.subscription_without_rights_pct)],["Teckning utan garantier", percent(dossier.subscription_without_guarantees_pct)],["Tecknat av garanter (% av emission)", percent(dossier.guarantee_take_up_pct_of_issue)],["Garantiutnyttjande (% av åtagande)", amountAndPercent(dossier.guarantee_utilized_amount, dossier.guarantee_utilized_pct)],["Garantiersättning", amountAndPercent(dossier.guarantee_fee_amount, dossier.guarantee_fee_pct)],["Kontant garantiersättning", percent(feeOptions.cash_fee_pct)],["Aktiebaserad garantiersättning", percent(feeOptions.share_fee_pct)],["Avgiftsbas", feeOptions.fee_basis || "Ej angivet"],["Garantivillkor", dossier.guarantee_fee_details || "Ej angivet"],["Totala kostnader", financialAmount(dossier.transaction_cost_total, dossierCurrency)],["Övriga emissionskostnader", amountAndPercent(dossier.transaction_cost, dossier.transaction_cost_pct)],["Teckningskurs", dossier.issue_price == null ? "Ej angivet" : marketPrice(dossier.issue_price)],["Finansiell rådgivare", dossier.adviser || "Ej angivet"],["Emissionsinstitut", dossier.issuer_agent || "Ej angivet"],["Utfall", dossier.outcome || "Ej angivet"]])}</dl></section>` : "";
    $("#issuerDetailTitle").textContent = issuerName(item);
    $("#issuerDetailMeta").textContent = `${item.category_label || label(item.category)} · ${item.status_label || status(item)} · ${date(item.published_at)}`;
    const requestStatus = item.editorial_request?.status;
    const requestActive = ["queued", "running", "enriching"].includes(requestStatus);
    const buttonLabel = requestActive ? "Följ pågående analys" : requestStatus === "failed" ? "Försök igen" : item.editorial_source === "ai" ? "Komplettera affärsfält" : "Låt AI svara och skriva";
    const editorialButton = `<button type="button" class="editorial-run-button" data-run-editorial>${buttonLabel}</button>`;
    const editorialStatus = requestStatus === "enriching" ? "Kommentaren är klar. Kontrollerar och fyller i källbelagda affärsfält…" : requestActive ? "Läser källhistoriken och skriver kommentar…" : requestStatus === "failed" ? (item.editorial_source === "ai" ? "Kommentaren är klar, men fältifyllnaden misslyckades. Försök igen." : "AI kunde inte skriva svaret. Försök igen.") : item.editorial_source === "ai" ? "AI har svarat och skrivit" : item.editorial_status === "failed" ? "AI-svaret kunde inte faktavalideras. Försök igen." : "Tryck på knappen så läser AI hela källhistoriken och skriver svaret.";
    renderTransactionWorkspace(item, caseData, { details, lifecycle, dossierHtml, guaranteeTranchesHtml,
      reactionHtml, bridgeAlertHtml, points, termRows, sources, ratio, outcomeBasis,
      editorialButton, editorialStatus });
  }
  async function openTransactionDetail(transactionId, trigger) {
    if (!transactionId) return;
    const request = beginRequest("detail");
    $("#issuerDetail").classList.remove("issuer-page"); state.transactionDetailId = String(transactionId); state.issuerDetailId = null; state.issuerDetailTrigger = trigger || document.activeElement;
    $("#issuerDetailBackdrop").hidden = false; $("#issuerDetail").classList.add("is-open"); $("#issuerDetail").inert = false; $("#workspace").inert = true; $("#issuerDetail").setAttribute("aria-hidden", "false"); document.body.classList.add("issuer-detail-open");
    $("#issuerDetailKicker").textContent = "TRANSAKTIONSAKT"; $("#issuerDetailTitle").textContent = "Laddar affär"; $("#issuerDetailMeta").textContent = ""; $("#issuerDetailContent").innerHTML = '<p class="empty">Hämtar villkor, utfall och källhistorik…</p>'; $("#issuerDetailClose").focus();
    let detail = null, completeCase = null, caseTask = null;
    state.sections.detailTransaction = { status: "loading", loaded: false };
    state.sections.detailCase = { status: "loading", loaded: false };
    const renderDetail = () => {
      if (!request.current() || state.transactionDetailId !== String(transactionId)) return;
      if (!detail) { $("#issuerDetailContent").innerHTML = sectionMarkup("detailTransaction"); return; }
      state.transactionCase = completeCase || { transaction: detail };
      renderTransactionDetail(completeCase?.transaction || detail, state.transactionCase);
      $("#issuerDetailContent").insertAdjacentHTML("afterbegin", sectionMarkup("detailTransaction") + sectionMarkup("detailCase"));
    };
    await loadResource("detailTransaction", `/api/transactions/${encodeURIComponent(transactionId)}`, payload => {
      if (!payload?.id) throw new Error("Ogiltig transaktionsakt");
      detail = payload;
      if (!caseTask) caseTask = loadResource("detailCase", `/api/transactions/${encodeURIComponent(transactionId)}/case`, value => { completeCase = value; }, renderDetail, request);
    }, renderDetail, request);
    if (caseTask) await caseTask;
  }
  async function runEditorial(button) {
    const id = state.transactionDetailId;
    if (!id || !button) return;
    button.disabled = true;
    button.textContent = "AI skriver…";
    const status = $("#editorialStatus");
    if (status) status.textContent = "Läser hela källhistoriken och skriver kommentar…";
    try {
      const request = await post(`/api/transactions/${encodeURIComponent(id)}/editorial`);
      const deadline = Date.now() + 180000;
      while (Date.now() < deadline && state.transactionDetailId === id) {
        await new Promise(resolve => setTimeout(resolve, 3000));
        const updated = await api(`/api/transactions/${encodeURIComponent(id)}`);
        if (state.transactionDetailId !== id) return;
        const progress = updated.editorial_request;
        if (!progress || progress.request_id !== request.request_id) continue;
        state.transactionCase = { ...(state.transactionCase || {}), transaction: updated };
        renderTransactionDetail(updated, state.transactionCase);
        const currentStatus = $("#editorialStatus");
        const currentButton = $("[data-run-editorial]");
        if (progress.status === "completed") return;
        if (progress.status === "failed") {
          if (currentStatus) currentStatus.textContent = updated.editorial_source === "ai" ? "Kommentaren är klar, men fältifyllnaden misslyckades. Försök igen." : "AI kunde inte skriva svaret. Försök igen.";
          if (currentButton) { currentButton.hidden = false; currentButton.disabled = false; currentButton.textContent = "Försök igen"; }
          return;
        }
        if (currentButton) { currentButton.disabled = true; currentButton.textContent = "AI skriver…"; }
        if (currentStatus) currentStatus.textContent = progress.status === "enriching" ? "Kommentaren är klar. Kontrollerar och fyller i källbelagda affärsfält…" : "Läser källhistoriken och skriver kommentar…";
      }
      if (state.transactionDetailId !== id) return;
      const currentStatus = $("#editorialStatus");
      const currentButton = $("[data-run-editorial]");
      if (currentButton) { currentButton.hidden = false; currentButton.disabled = false; currentButton.textContent = "Kontrollera analys"; }
      if (currentStatus) currentStatus.textContent = "Analysen arbetar fortfarande. Tryck för att kontrollera status igen.";
    } catch (error) {
      if (state.transactionDetailId !== id) return;
      const currentButton = $("[data-run-editorial]");
      const currentStatus = $("#editorialStatus");
      if (currentButton) { currentButton.hidden = false; currentButton.disabled = false; currentButton.textContent = "Försök igen"; }
      if (currentStatus) currentStatus.textContent = `AI-svarets status kunde inte hämtas: ${error.message}`;
    }
  }  function closeIssuerDetail() {
    requests.get("detail")?.controller.abort(); requests.delete("detail");
    for (const name of ["detailIssuer", "detailAnalysis", "detailTransaction", "detailCase"]) delete state.sections[name];
    renderLoadNotice();
    $("#issuerDetailBackdrop").hidden = true; $("#issuerDetail").classList.remove("is-open", "issuer-page"); $("#issuerDetail").inert = true; $("#workspace").inert = false; $("#issuerDetail").setAttribute("aria-hidden", "true"); document.body.classList.remove("issuer-detail-open"); state.issuerDetailId = null; state.transactionDetailId = null; state.transactionCase = null; state.issuerDetail = null;
    if (state.issuerDetailTrigger?.isConnected) state.issuerDetailTrigger.focus();
  }
  function detailRows(entries) { return entries.map(([key, value]) => `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd></div>`).join(""); }
  function renderControl() {
    const r = state.research || {}; const live = r.enabled === true;
    $("#researchBadge").textContent = live ? "KONFIGURERAD" : "INTE AKTIV"; $("#researchBadge").classList.toggle("is-live", live);
    $("#researchCopy").textContent = live ? "Källor hämtas genom en server-side provider. Resultat är navigationsunderlag, inte finansiell evidens." : "Kanalen är avstängd tills en behörig server-side provider har konfigurerats. Inga nycklar lämnar webbläsaren.";
    const fallbacks = Array.isArray(r.fallback_backends) && r.fallback_backends.length ? r.fallback_backends.join(" → ") : "–";
    const persistence = r.persistence === "validated_history" ? "automatiskt validerad historik" : (r.persistence === "reviewable_history" ? "granskningsbar historik" : (r.persistence === "none" ? "ingen" : (r.persistence || "–")));
    $("#researchDetails").innerHTML = sectionMarkup("research") + (section("research").loaded ? detailRows([["Status", live ? "i produktion" : "avstängd"], ["Primärmotor", r.primary_backend || r.provider || "–"], ["Reservmotorer", fallbacks], ["Region", r.region || "–"], ["Max träffar per sökning", r.max_results ?? "–"], ["Lagring", persistence], ["Källstatus", r.validation_posture === "trusted_primary_sources_auto_approved" ? "primärkällor auto-valideras" : (r.validation_posture === "source_checked_requires_review" ? "valideras vid analys" : "–")], ["Åtkomst", r.search_requires_bearer ? "Bearer krävs" : "ej exponerad"]]) : "");
    const researchItems = list(state.researchHistory, ["items"]);
    $("#researchHistory").innerHTML = researchItems.length
      ? `<p class="label">SENASTE KÄLLKANDIDATER</p>${researchItems.slice(0, 3).map((item) => `<p><strong>${escapeHtml(item.validation_status === "approved" ? "Godkänd" : item.validation_status === "rejected" ? "Avvisad" : "Källkandidat")}</strong><br><span>${escapeHtml(item.title || "Källa saknar rubrik")}</span></p>`).join("")}`
      : '<p class="muted">Inga researchkandidater har sparats ännu.</p>';
    const actions = state.operations?.actions || {}; const current = actions.current || {}; const monitor = state.operations?.mfn_monitor || {};
    const monitorStatus = monitor.status === "idle" && Number(monitor.error_count || 0) > 0 ? "Aktiv med avvikelser" : (({ idle: "Aktiv", starting: "Startar", running: "Hämtar MFN", error: "Kräver tillsyn", disabled: "Avstängd" })[monitor.status] || "Okänd");
    const monitorProgress = monitor.issuer_total == null ? "–" : `${number(monitor.issuer_finished || 0)} av ${number(monitor.issuer_total)}`;
    $("#operationDetails").innerHTML = detailRows([["Automatisk analys", state.operations?.automation?.enabled ? "Aktiv" : "Avstängd"], ["Runwaytolerans", "±1 månad"], ["Saknade fakta", "Automatiska återförsök"], ["Försök per dygn", state.operations?.automation?.max_attempts_per_day ?? "–"], ["MFN-bevakning", monitorStatus], ["Nästa kontroll", dateTime(monitor.next_poll_at)], ["Senaste kontroll", dateTime(monitor.last_completed_at || monitor.last_started_at)], ["Kontrollerade bolag", monitorProgress], ["Nya artiklar", monitor.articles_new ?? "–"], ["Senaste MFN-fel", monitor.last_error || "–"], ["Öppna åtgärder", actionCount(actions)], ["Pågår", actions.running || 0], ["Väntar", actions.queued || 0], ["Aktuellt bolag", current.issuer || "–"], ["Fas", String(current.phase || "–").replace(/_/g, " ")], ["Senast uppdaterad", dateTime(current.updated_at)]]);
    $("#navHealth").textContent = monitor.status === "error" ? "MFN-bevakning kräver tillsyn" : monitor.enabled ? "MFN-bevakning aktiv" : (live ? "Systemstatus läst" : "MFN-bevakning avstängd");
  }
  function render() { renderMetrics(); renderTransactions(); renderIssuers(); renderControl(); $("#asOf").textContent = `Underlag läst ${new Intl.DateTimeFormat("sv-SE", { hour: "2-digit", minute: "2-digit" }).format(new Date())}`; }
  function issuerParams() {
    const params = new URLSearchParams({ limit: "25" });
    Object.entries(state.issuerFilters).forEach(([key, value]) => { if (value) params.set(key, value); });
    const cursor = state.issuerCursors[state.issuerPageIndex]; if (cursor) params.set("cursor", cursor);
    return params;
  }
  async function loadIssuerPage({ reset = false } = {}) {
    if (reset) { state.issuerCursors = [null]; state.issuerPageIndex = 0; state.issuerNextCursor = null; }
    const request = beginRequest("issuers");
    const pageIndex = state.issuerPageIndex;
    requests.get("analysis")?.controller.abort(); requests.delete("analysis");
    state.sections.analysis = { ...section("analysis"), status: "loading" };
    return loadResource("issuers", `/api/issuers?${issuerParams().toString()}`, payload => {
      state.issuers = list(payload);
      state.issuerRenderedPageIndex = pageIndex;
      state.issuerTotal = Number(payload.total_filtered ?? state.issuers.length);
      state.issuerNextCursor = payload.next_cursor || null;
      state.issuers.forEach(item => { if (item.pro_forma) state.proForma[item.id] = item.pro_forma; });
      const ids = state.issuers.filter(item => !item.pro_forma).map(item => item.id);
      if (!ids.length) { state.sections.analysis = { status: "ready", loaded: true }; return; }
      const analysisRequest = beginRequest("analysis"), isCurrent = analysisRequest.current;
      analysisRequest.current = () => isCurrent() && request.current();
      // Rendering the source-backed issuer list never waits for adjusted runway.
      void loadResource("analysis", `/api/radar/analysis?issuer_ids=${ids.join(",")}`, analysis => {
        if (!Array.isArray(analysis.items)) throw new Error("Ogiltigt analysunderlag");
        analysis.items.forEach(item => { state.proForma[item.issuer_id] = item; });
      }, renderIssuers, analysisRequest);
    }, renderIssuers, request);
  }
  async function loadIssuerMarkets() {
    return loadResource("issuerMarkets", "/api/issuer-markets", payload => { state.issuerMarkets = list(payload).filter(Boolean); }, renderIssuerMarkets);
  }
  function renderIssuerMarkets() {
    const current = state.issuerFilters.market;
    $("#issuerMarket").innerHTML = `<option value="">Alla marknader</option>${state.issuerMarkets.map((market) => `<option value="${escapeHtml(market)}">${escapeHtml(market)}</option>`).join("")}`;
    $("#issuerMarket").value = current;
  }
  async function refreshRadar({ reset = true } = {}) { await loadIssuerPage({ reset }); renderIssuerMarkets(); renderIssuers(); renderMetrics(); }
  function scheduleOperationsPoll() {
    clearTimeout(state.operationTimer);
    if (currentRoute() !== "control") return;
    state.operationTimer = setTimeout(() => {
      refreshOperations().catch(() => notice("Kunde inte uppdatera åtgärdskön."));
    }, 10_000);
  }
  async function refreshOperations() {
    if (state.operationLoading) return;
    state.operationLoading = true;
    try {
      await loadResource("operations", "/api/operations/summary", payload => { state.operations = payload; }, () => { renderMetrics(); renderControl(); });
    } finally {
      state.operationLoading = false;
      scheduleOperationsPoll();
    }
  }
  async function post(path, payload, token) {
    const options = {method:"POST",timeoutMs:30000,headers:{"Content-Type":"application/json",Accept:"application/json"},body:JSON.stringify(payload)};
    const response = path === "/api/radar/preview" ? await fetch(path,{...options,signal:AbortSignal.timeout(30000),cache:"no-store"}) : await adminWrite(path,options,token);
    const body = await response.json(); if (!response.ok) throw new Error(body.error || `API ${response.status}`); return body;
  }
  async function loadDeals() {
    const page = state.page;
    const params = new URLSearchParams({page:String(state.page),page_size:"10",...state.dealFilters});
    return loadResource("deals", `/api/transactions?${params}`, payload => {
      if (!Array.isArray(payload.items) || payload.items.some(item => !item || typeof item !== "object")) throw new Error("Ogiltigt affärsunderlag");
      state.transactions = payload.items; state.transactionMeta = payload; state.page = page;
    }, renderTransactions);
  }
  async function loadHomeDeals() {
    const marketRequest = requests.get("deals");
    const updateMarket = () => requests.get("deals") === marketRequest && state.page === 1 && !Object.keys(state.dealFilters).length;
    return loadResource("homeFeed", "/api/transactions?page=1&page_size=10", payload => {
      if (!Array.isArray(payload.items) || payload.items.some(item => !item || typeof item !== "object")) throw new Error("Ogiltigt affärsunderlag");
      state.homeTransactions = payload.items;
      // A delayed overview response must not replace a newer filtered/page request.
      if (updateMarket()) {
        state.transactions = payload.items; state.transactionMeta = payload;
        state.sections.deals = { status: payload.read_model?.stale ? "stale" : "ready", loaded: true, readModel: payload.read_model };
      }
    }, () => { if (updateMarket()) state.sections.deals = { ...section("homeFeed") }; renderTransactions(); });
  }
  async function load() {
    const revision = ++state.loadRevision;
    notice("");
    if (!Object.values(state.sections).some(value => value.loaded)) $("#loading").classList.remove("hidden");
    for (const name of Object.keys(sectionLabels)) state.sections[name] = { ...section(name), status: "loading" };
    renderIssuerMarkets(); render(); renderHealth();
    // Every section publishes its own result immediately. Slow finance views
    // cannot hold overview or source-backed company data behind the overlay.
    const work = [
      loadResource("overview", "/api/overview", payload => { state.overview = payload; }, renderMetrics),
      loadIssuerPage({ reset: true }), loadHomeDeals(), loadIssuerMarkets(),
      loadResource("research", "/api/research/status", payload => { state.research = payload; }, renderControl),
      loadResource("researchHistory", "/api/research/history?limit=5", payload => { state.researchHistory = payload; }, renderControl),
      loadResource("operations", "/api/operations/summary", payload => { state.operations = payload; }, renderControl),
      loadResource("health", "/api/market-health", payload => { state.health = payload; }, () => { renderHealth(); renderMetrics(); }),
    ];
    if (state.page !== 1 || Object.keys(state.dealFilters).length) work.push(loadDeals());
    await Promise.allSettled(work);
    if (revision !== state.loadRevision) return;
    revealWorkspace(); renderLoadNotice();
  }
  const bands = ["0–3","3–6","6–12","12–18","18+"];
  const colors = ["#B42318","#D97706","#A67C00","#4D8C57","#19703D"];
  function renderHealth() {
    const months = state.health.months || [];
    const legend = bands.map((b,i)=>`<span class="runway band-${i}">${b} mån</span>`).join("") + '<span class="runway band-positive">Cash-flow positive</span>';
    $("#runwayLegend").innerHTML=legend; $("#healthLegend").innerHTML=legend;
    $("#healthBasis").textContent=sectionMessage("health") || state.health.history_basis || "Historiskt underlag saknas.";
    $("#dealChart").innerHTML=sectionMarkup("health") + (section("health").loaded ? chart(months,false,false) : "");
    $("#runwayChart").innerHTML=sectionMarkup("health") + (section("health").loaded ? chart(months,true,false) : "");
    $("#miniHealth").innerHTML=sectionMarkup("health") + (section("health").loaded ? chart(months,true,true) : "");
  }
  function chart(months,runway,mini) {
    if (!months.length || (runway && !months.some(m=>m.snapshot_available))) return '<p class="empty">Historiska månadsslut saknas. Historik fylls inte bakåt med dagens data.</p>';
    const width=1000,height=260,pad=40,plot=height-65,step=(width-2*pad)/Math.max(months.length-1,1);
    const max = Math.max(1,...months.flatMap(m=>runway ? Object.values(m.runway_bands || {}) : [m.deal_count]));
    const y=v=>plot-(v/max)*(plot-20), x=i=>pad+i*step;
    const ticks=max<4?Array.from({length:Math.floor(max)+1},(_,i)=>i):Array.from({length:5},(_,i)=>max*i/4);
    let content=ticks.map(value=>`<line class="grid" x1="${pad}" x2="${width-pad}" y1="${y(value)}" y2="${y(value)}"/><text x="${pad-8}" y="${y(value)+4}" text-anchor="end">${Math.round(value)}</text>`).join("");
    content+=months.map((m,i)=>`<text x="${x(i)}" y="${plot+22}" text-anchor="middle">${escapeHtml(m.month.slice(2))}</text>`).join("");
    if(runway) bands.forEach((band,index)=>{let active=false;let d="";months.forEach((m,i)=>{if(!m.snapshot_available){active=false;return;}const value=m.runway_bands?.[band] || 0;d+=`${active?"L":"M"}${x(i)},${y(value)} `;active=true;});content+=`<path d="${d}" stroke="${colors[index]}"/>`;});
    else content+=months.map((m,i)=>`<rect x="${x(i)-16}" y="${y(m.deal_count)}" width="32" height="${plot-y(m.deal_count)}" rx="3" fill="#2563EB"><title>${escapeHtml(m.month)}: ${m.deal_count} affärer</title></rect>`).join("");
    const table=mini?"":`<details><summary>Visa exakta månadsvärden</summary><div class="table-scroll"><table class="chart-table"><thead><tr><th>Månad</th>${(runway?bands:["Affärer"]).map(b=>`<th>${b}</th>`).join("")}</tr></thead><tbody>${months.map(m=>`<tr><td>${m.month}</td>${(runway?bands.map(b=>m.snapshot_available?(m.runway_bands?.[b]||0):"—"):[m.deal_count]).map(v=>`<td>${v}</td>`).join("")}</tr>`).join("")}</tbody></table></div></details>`;
    return `<svg class="chart ${mini?"mini-chart":""}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${runway?"Historiska runway-band":"Antal finansieringsaffärer"} per månad">${content}</svg>${table}`;
  }
  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-route]").forEach((button) => button.addEventListener("click", () => { location.hash = button.dataset.route; }));
    $("#refresh").addEventListener("click", load);
    $("#dealFilters").addEventListener("submit",event=>{event.preventDefault();state.dealFilters=Object.fromEntries([...new FormData(event.target)].filter(([,v])=>v));state.page=1;loadDeals().catch(e=>notice(e.message));});
    $("#issuerDetailContent").addEventListener("change",async event=>{
      const select=event.target.closest("[data-weight]");if(!select)return;
      const id=state.issuerDetailId;state.previewWeights[select.dataset.weight]=Number(select.value);select.disabled=true;
      try{const result=await post("/api/radar/preview",{issuer_id:id,weights:state.previewWeights});if(state.issuerDetailId!==id)return;state.issuerDetail.pro_forma=result.items[0]||{};renderIssuerDetail(state.issuerDetail);$("#previewStatus").focus();}
      catch(error){select.disabled=false;$("#previewStatus").textContent=error.message;}
    });
    $("#issuerDetailContent").addEventListener("submit",async event=>{
      if(event.target.id!=="saveWeights")return;event.preventDefault();const form=event.target,values=new FormData(form),id=state.issuerDetailId;const button=form.querySelector("button");button.disabled=true;
      const token=String(values.get("token"));form.elements.token.value="";
      try{let result;for(const [transaction_id,weight] of Object.entries(state.previewWeights)){result=await post("/api/radar/weight",{issuer_id:id,transaction_id,weight,reason:values.get("reason"),confirm:true},token);}
        if(state.issuerDetailId!==id)return;if(result){state.previewWeights={};state.issuerDetail.pro_forma=result.items[0]||{};state.proForma[id]=state.issuerDetail.pro_forma;renderIssuerDetail(state.issuerDetail);renderIssuers();$("#previewStatus").textContent="Vikterna är sparade.";}else{$("#previewStatus").textContent="Inga ändrade vikter att spara.";button.disabled=false;}
      }catch(error){button.disabled=false;$("#previewStatus").textContent=error.message;}
    });
    document.addEventListener("keydown",event=>{if(event.key!=="Tab"||!$("#issuerDetail").classList.contains("is-open"))return;const focusable=[...$("#issuerDetail").querySelectorAll('a[href],button:not(:disabled),input,select:not(:disabled),summary')].filter(n=>n.getClientRects().length);const first=focusable[0],last=focusable.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus();}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus();}});

    let issuerSearchTimer;
    $("#issuerQuery").addEventListener("input", () => { clearTimeout(issuerSearchTimer); issuerSearchTimer = setTimeout(() => { state.issuerFilters.q = $("#issuerQuery").value.trim(); refreshRadar().catch((error) => notice(`Kunde inte läsa bolagsradarn: ${error.message}`)); }, 250); });
    [["#issuerMarket", "market"], ["#issuerCapitalNeed", "capital_need"], ["#issuerRunway", "runway_max"], ["#issuerAnalysis", "analysis_status"]].forEach(([selector, key]) => $(selector).addEventListener("change", () => { state.issuerFilters[key] = $(selector).value; refreshRadar().catch((error) => notice(`Kunde inte läsa bolagsradarn: ${error.message}`)); }));
    $("#issuerPrevious").addEventListener("click", () => { if (!state.issuerPageIndex) return; state.issuerPageIndex -= 1; refreshRadar({ reset: false }).catch((error) => notice(`Kunde inte läsa bolagsradarn: ${error.message}`)); });
    $("#issuerNext").addEventListener("click", () => { if (!state.issuerNextCursor) return; state.issuerCursors[state.issuerPageIndex + 1] = state.issuerNextCursor; state.issuerPageIndex += 1; refreshRadar({ reset: false }).catch((error) => notice(`Kunde inte läsa bolagsradarn: ${error.message}`)); });
    $("#issuerDetailClose").addEventListener("click", closeIssuerDetail); $("#issuerDetailBackdrop").addEventListener("click", closeIssuerDetail);
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && (state.issuerDetailId !== null || state.transactionDetailId !== null)) closeIssuerDetail(); if ((event.key === "Enter" || event.key === " ") && event.target.closest(".issuer-table-row")) { event.preventDefault(); const row = event.target.closest(".issuer-table-row"); openIssuerDetail(row.dataset.issuerId, row); } });
    document.addEventListener("click", (event) => {
      const editorial = event.target.closest("[data-run-editorial]"); if (editorial) { runEditorial(editorial); return; }
      const override = event.target.closest("[data-override-issuer]"); if (override) { openIssuerDetail(override.dataset.overrideIssuer,override); return; }
      const deal = event.target.closest("[data-deal-row]"); if(deal && !event.target.closest("button,a")) { openTransactionDetail(deal.dataset.dealRow,deal.querySelector("button")); return; }
      const transaction = event.target.closest("[data-open-transaction]"); const sort = event.target.closest(".issuer-sort"); const filter = event.target.closest(".radar-chip"); const issuerRow = event.target.closest(".issuer-table-row"); if (transaction) { openTransactionDetail(transaction.dataset.openTransaction, transaction); return; } if (issuerRow) { openIssuerDetail(issuerRow.dataset.issuerId, issuerRow); return; } if (sort) { const next = sort.dataset.issuerSort; state.issuerFilters.direction = state.issuerFilters.sort === next && state.issuerFilters.direction === "asc" ? "desc" : "asc"; state.issuerFilters.sort = next; refreshRadar().catch((error) => notice(`Kunde inte sortera bolagsradarn: ${error.message}`)); } if (filter) { const { filterKey, filterValue } = filter.dataset; state.issuerFilters[filterKey] = state.issuerFilters[filterKey] === filterValue ? "" : filterValue; const input = document.querySelector({ capital_need: "#issuerCapitalNeed", runway_max: "#issuerRunway", analysis_status: "#issuerAnalysis" }[filterKey]); if (input) input.value = state.issuerFilters[filterKey]; refreshRadar().catch((error) => notice(`Kunde inte filtrera bolagsradarn: ${error.message}`)); } });
    $("#older").addEventListener("click", () => { state.page += 1; loadDeals().catch(e=>notice(e.message)); });
    $("#newer").addEventListener("click", () => { state.page = Math.max(1, state.page - 1); loadDeals().catch(e=>notice(e.message)); });
    window.addEventListener("hashchange", () => setRoute(currentRoute())); setRoute(currentRoute()); load().catch((error) => { $("#loading").classList.add("hidden"); notice(`Kunde inte läsa marknadsunderlaget: ${error.message}`); });
  });
  window.CapitalWorkspace = { safeUrl, label, list, stage, runwayMarkup, transactionAmount };
})();
