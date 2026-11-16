"""PIN-inloggning för hela appen.

En enda delad PIN (standard 4030, ändras med miljövariabeln RADAR_PIN). Rätt PIN ger en signerad
kaka (HttpOnly, SameSite=Lax) som gäller i 30 dagar. Signeringsnyckeln kommer från RADAR_SECRET, annars
skapas och sparas den en gång i datakatalogen så att inloggningen överlever omstarter.

Skydd mot gissning: fem felaktiga försök från samma adress låser den i fem minuter. Jämförelser görs med
konstant tid. En fyrsiffrig PIN är ändå ett enkelt skydd; Cloudflare Access ligger kvar framför appen.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

COOKIE = "radar_session"
SESSION_SECONDS = 30 * 24 * 3600
MAX_ATTEMPTS = 5
LOCK_SECONDS = 300
DEFAULT_PIN = "4030"


def pin() -> str:
    return os.getenv("RADAR_PIN", DEFAULT_PIN).strip() or DEFAULT_PIN


def secret(data_dir: Path) -> bytes:
    """Signeringsnyckel: RADAR_SECRET eller en slumpad nyckel som sparas bredvid databasen."""
    env = os.getenv("RADAR_SECRET")
    if env:
        return env.encode()
    path = Path(data_dir) / ".session_secret"
    try:
        if path.exists():
            return path.read_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        path.write_bytes(key)
        return key
    except OSError:
        return secrets.token_bytes(32)   # skrivskyddad katalog: nyckeln gäller till nästa omstart


def make_token(key: bytes, now: float | None = None) -> str:
    """Token = utgångstid + signatur. PIN:en ingår i signaturen, så ett byte av PIN loggar ut alla."""
    expires = int((now or time.time()) + SESSION_SECONDS)
    sig = hmac.new(key, f"{expires}:{pin()}".encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def valid_token(key: bytes, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires, _, sig = token.partition(".")
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return False
    good = hmac.new(key, f"{expires}:{pin()}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def check_pin(candidate: str) -> bool:
    return hmac.compare_digest(candidate.strip().encode(), pin().encode())


class Throttle:
    """Räknar felaktiga försök per adress och låser adressen en stund."""

    def __init__(self) -> None:
        self.failures: dict[str, list[float]] = {}
        self.locked: dict[str, float] = {}

    def wait_seconds(self, who: str, now: float | None = None) -> int:
        now = now or time.time()
        until = self.locked.get(who, 0)
        return max(0, int(until - now)) if until > now else 0

    def fail(self, who: str, now: float | None = None) -> int:
        """Registrera ett fel; returnerar låsningstid i sekunder om adressen nu är låst, annars 0."""
        now = now or time.time()
        recent = [t for t in self.failures.get(who, []) if now - t < LOCK_SECONDS] + [now]
        self.failures[who] = recent
        if len(recent) >= MAX_ATTEMPTS:
            self.locked[who] = now + LOCK_SECONDS
            self.failures[who] = []
            return LOCK_SECONDS
        return 0

    def ok(self, who: str) -> None:
        self.failures.pop(who, None)
        self.locked.pop(who, None)


LOGIN_HTML = """<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <meta name="robots" content="noindex">
  <title>Logga in · Perplex Invest</title>
  <style>
    :root { --mint: #4fe3c6; --ink: #e6eff4; --muted: #8fa5b5; --err: #ff8f85; }
    * { box-sizing: border-box; }
    html, body { height: 100%; margin: 0; }
    body { display: grid; place-items: center; padding: 16px; color: var(--ink);
      font-family: Inter, "Segoe UI", system-ui, sans-serif;
      background:
        radial-gradient(90% 120% at 100% 0%, rgba(79,227,198,.22), transparent 50%),
        linear-gradient(120deg, #04121f, #072338 55%, #0c3d4c); }
    main { width: min(380px, 100%); padding: 32px 28px; border-radius: 18px; text-align: center;
      background: rgba(11,32,50,.72); border: 1px solid rgba(79,227,198,.28); backdrop-filter: blur(8px);
      box-shadow: 0 24px 60px rgba(0,0,0,.4); }
    .brand { display: inline-flex; align-items: center; gap: 12px; margin-bottom: 4px; }
    .brand svg { height: 26px; }
    .brand span { letter-spacing: .26em; font-size: 15px; }
    .product { color: var(--mint); text-transform: uppercase; letter-spacing: .14em; font-size: 11px; margin: 0 0 24px; }
    h1 { font-size: 18px; margin: 0 0 6px; }
    p.hint { color: var(--muted); font-size: 13px; margin: 0 0 18px; }
    .pin { display: flex; justify-content: center; gap: 10px; margin-bottom: 14px; }
    .pin input { width: 52px; height: 60px; text-align: center; font: inherit; font-size: 26px; color: var(--ink);
      background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.2); border-radius: 12px;
      transition: border-color .15s, box-shadow .15s; }
    .pin input:focus { outline: none; border-color: var(--mint); box-shadow: 0 0 0 3px rgba(79,227,198,.25); }
    .pin.bad input { border-color: var(--err); animation: shake .35s; }
    @keyframes shake { 20%, 60% { transform: translateX(-5px); } 40%, 80% { transform: translateX(5px); } }
    button { width: 100%; padding: 12px; font: inherit; font-weight: 600; color: #04121f; background: var(--mint);
      border: 0; border-radius: 12px; cursor: pointer; transition: filter .15s, transform .15s; }
    button:hover { filter: brightness(1.08); }
    button:active { transform: scale(.98); }
    button:disabled { opacity: .5; cursor: default; }
    .msg { min-height: 20px; margin: 10px 0 0; font-size: 13px; color: var(--err); }
    @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
  </style>
</head>
<body>
  <main>
    <div class="brand">
      <svg viewBox="0 0 40 30" aria-hidden="true"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#6cf2d6"/><stop offset="1" stop-color="#1c9c9a"/></linearGradient></defs><path d="M1 1h9l10 28h-9z" fill="url(#g)"/><path d="M16 1h9l10 28h-9z" fill="url(#g)"/></svg>
      <span>PERPLEX INVEST</span>
    </div>
    <p class="product">Capital Radar</p>
    <h1>Ange PIN</h1>
    <p class="hint">Fyra siffror för att komma in.</p>
    <form id="form" autocomplete="off">
      <div class="pin" id="pin" role="group" aria-label="PIN-kod">
        <input inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Siffra 1" autofocus>
        <input inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Siffra 2">
        <input inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Siffra 3">
        <input inputmode="numeric" pattern="[0-9]*" maxlength="1" autocomplete="off" aria-label="Siffra 4">
      </div>
      <button type="submit" id="go">Logga in</button>
      <p class="msg" id="msg" role="alert" aria-live="assertive"></p>
    </form>
  </main>
  <script>
    (function () {
      var boxes = Array.prototype.slice.call(document.querySelectorAll("#pin input"));
      var form = document.getElementById("form"), msg = document.getElementById("msg"), go = document.getElementById("go");
      var wrap = document.getElementById("pin");
      function value() { return boxes.map(function (b) { return b.value; }).join(""); }
      function clear() { boxes.forEach(function (b) { b.value = ""; }); boxes[0].focus(); }
      function submit() {
        var pin = value();
        if (pin.length !== 4) { msg.textContent = "Ange fyra siffror."; return; }
        go.disabled = true; msg.textContent = "";
        fetch("login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pin: pin }), credentials: "same-origin" })
          .then(function (r) { return r.json().then(function (d) { return [r.status, d]; }); })
          .then(function (res) {
            if (res[0] === 200) { location.replace(res[1].next || "./"); return; }
            msg.textContent = res[1].error || "Fel PIN.";
            wrap.classList.add("bad"); setTimeout(function () { wrap.classList.remove("bad"); }, 400);
            clear(); go.disabled = false;
          })
          .catch(function () { msg.textContent = "Kunde inte nå servern. Försök igen."; go.disabled = false; });
      }
      boxes.forEach(function (box, i) {
        box.addEventListener("input", function () {
          box.value = box.value.replace(/\\D/g, "").slice(-1);
          if (box.value && i < 3) boxes[i + 1].focus();
          if (value().length === 4) submit();
        });
        box.addEventListener("keydown", function (e) {
          if (e.key === "Backspace" && !box.value && i > 0) { boxes[i - 1].value = ""; boxes[i - 1].focus(); }
          if (e.key === "ArrowLeft" && i > 0) boxes[i - 1].focus();
          if (e.key === "ArrowRight" && i < 3) boxes[i + 1].focus();
        });
        box.addEventListener("paste", function (e) {
          var text = (e.clipboardData || window.clipboardData).getData("text").replace(/\\D/g, "").slice(0, 4);
          if (!text) return;
          e.preventDefault();
          text.split("").forEach(function (ch, k) { if (boxes[k]) boxes[k].value = ch; });
          boxes[Math.min(text.length, 3)].focus();
          if (text.length === 4) submit();
        });
      });
      form.addEventListener("submit", function (e) { e.preventDefault(); submit(); });
    })();
  </script>
</body>
</html>
"""
