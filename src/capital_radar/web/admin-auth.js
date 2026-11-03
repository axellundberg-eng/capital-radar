"use strict";

function requestPassword() {
  return new Promise(resolve => {
    const dialog = document.createElement("dialog");
    dialog.setAttribute("aria-label", "Administratörsbehörighet");
    dialog.innerHTML = `<form><h2>Bekräfta administratörsbehörighet</h2><p>Ange din separata admin-token för den manuella åtgärden. Agenttoken kan inte användas. Uppgiften sparas bara tillfälligt i denna fliks minne.</p><label>Admin-token <input name="credential" type="password" autocomplete="off" spellcheck="false" required></label><div><button type="button" data-cancel>Avbryt</button><button type="submit">Verifiera och fortsätt</button></div></form>`;
    const input = dialog.querySelector("input");
    const finish = value => {
      input.value = "";
      dialog.close();
      dialog.remove();
      resolve(value);
    };
    dialog.querySelector("form").addEventListener("submit", event => {
      event.preventDefault();
      finish(input.value.trim() || null);
    });
    dialog.querySelector("[data-cancel]").addEventListener("click", () => finish(null));
    dialog.addEventListener("cancel", event => { event.preventDefault(); finish(null); });
    document.body.appendChild(dialog);
    dialog.showModal();
    input.focus();
  });
}

export function createAdminAuth({fetchFn = (...args) => fetch(...args), promptToken = requestPassword} = {}) {
  let token = "", prompt = null, epoch = 0;
  const clear = () => { token = ""; epoch++; };
  async function credential(provided) {
    const credentialEpoch = epoch;
    let candidate = typeof provided === "string" ? provided.trim() : token;
    if (!candidate) {
      if (!prompt) prompt = Promise.resolve().then(promptToken).finally(() => { prompt = null; });
      candidate = await prompt;
    }
    if (!candidate) throw new Error("Åtgärden avbröts. Formuläret har inte skickats.");
    let response, identity;
    try {
      response = await fetchFn("/api/auth/capabilities", {
        method: "GET", signal: AbortSignal.timeout(10000), cache: "no-store",
        redirect: "error", credentials: "same-origin", referrerPolicy: "no-referrer",
        headers: {Accept: "application/json", Authorization: `Bearer ${candidate}`},
      });
      identity = await response.json();
    } catch {
      clear();
      throw new Error("Behörigheten kunde inte verifieras. Ingen ändring har skickats.");
    }
    if (!response.ok || identity?.role !== "admin" || !Array.isArray(identity.permissions) || !identity.permissions.includes("admin")) {
      clear();
      throw new Error("Separat administratörsbehörighet krävs. Agenttoken kan bara läsa och skapa förslag.");
    }
    if (epoch !== credentialEpoch) throw new Error("Behörigheten har återkallats. Ingen ändring har skickats.");
    token = candidate;
    return candidate;
  }
  async function write(path, options = {}, providedToken) {
    const method = String(options.method || "POST").toUpperCase();
    if (!String(path).startsWith("/api/") || String(path).includes("\\") || !["POST", "PATCH", "PUT", "DELETE"].includes(method)) {
      throw new Error("Ogiltig manuell API-åtgärd.");
    }
    const headers = Object.fromEntries(Object.entries(options.headers || {}).filter(([name]) => name.toLowerCase() !== "authorization"));
    if (path !== "/api/radar/preview") headers.Authorization = `Bearer ${await credential(providedToken)}`;
    let response;
    try {
      const timeout = AbortSignal.timeout(Number(options.timeoutMs) || 30000);
      const signal = options.signal ? AbortSignal.any([options.signal, timeout]) : timeout;
      const request = {...options, method, headers, signal, cache: "no-store",
        redirect: "error", credentials: "same-origin", referrerPolicy: "no-referrer"};
      delete request.timeoutMs;
      response = await fetchFn(path, request);
    } catch {
      throw new Error("Skrivningen kunde inte bekräftas. Kontrollera status innan du försöker igen.");
    }
    if ([401, 403].includes(response.status)) {
      clear();
      throw new Error("Behörigheten har upphört. Ingen automatisk omsändning görs; kontrollera status innan du försöker igen.");
    }
    return response;
  }
  async function read(path) {
    if (!/^\/api\/guarantees(?:\/[1-9][0-9]*(?:\/candidates)?)?$/.test(String(path)) && !/^\/api\/agent\/review\/(?:runs(?:\/[0-9a-f]{32})?|findings)(?:\?(?:limit|after_id)=[0-9]+(?:&(?:limit|after_id)=[0-9]+)*)?$/.test(String(path))) throw new Error('Ogiltig privat läsning.');
    const response = await fetchFn(path,{method:'GET',headers:{Accept:'application/json',Authorization:`Bearer ${await credential()}`},
      signal:AbortSignal.timeout(10000),cache:'no-store',redirect:'error',credentials:'same-origin',referrerPolicy:'no-referrer'});
    if ([401,403].includes(response.status)) clear();
    return response;
  }
  return {write, read, clear};
}

export const adminAuth = createAdminAuth();
if (typeof window !== "undefined") window.addEventListener("pagehide", () => adminAuth.clear());
