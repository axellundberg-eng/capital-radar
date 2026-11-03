"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "..", "..", "src", "capital_radar", "web", "admin-auth.js"), "utf8");
const modulePromise = import("data:text/javascript;base64," + Buffer.from(source).toString("base64"));
const reply = (status, body = {}) => ({status, ok: status >= 200 && status < 300, json: async () => body});
const administrator = {role: "admin", permissions: ["admin", "read", "propose"]};
const agent = {role: "agent", permissions: ["read", "propose"]};
const options = {method: "POST", headers: {"Content-Type": "application/json"}, body: '{"reason":"draft unchanged"}'};
test('private reads authenticate without persisting credentials and reject other URLs', async()=>{
  const {createAdminAuth}=await modulePromise;const calls=[];
  const auth=createAdminAuth({promptToken:async()=> 'fixture-private-admin',fetchFn:async(url,options)=>{calls.push({url,options});return reply(200,url==='/api/auth/capabilities'?administrator:{items:[]});}});
  await auth.read('/api/guarantees');
  assert.equal(calls.length,2);assert.equal(calls[1].options.headers.Authorization,'Bearer fixture-private-admin');
  await assert.rejects(auth.read('https://example.com/api/guarantees'));
  await assert.rejects(auth.read('/api/guarantees/../private'));
  assert.equal(calls.length,2);
});

test("credential cancellation sends nothing and preserves the original draft", async () => {
  const {createAdminAuth} = await modulePromise;
  let fetched = 0;
  const auth = createAdminAuth({promptToken: async () => null, fetchFn: async () => {fetched++;}});
  await assert.rejects(auth.write("/api/actions/interpret", options), /avbröts/);
  assert.equal(fetched, 0);
  assert.equal(options.body, '{"reason":"draft unchanged"}');
});

test("an agent token is rejected before any mutation", async () => {
  const {createAdminAuth} = await modulePromise;
  const calls = [];
  const auth = createAdminAuth({promptToken: async () => "agent-fixture-secret", fetchFn: async (url, request) => {
    calls.push({url, request}); return reply(200, agent);
  }});
  await assert.rejects(auth.write("/api/actions/interpret", options), error => /administratör/.test(error.message) && !error.message.includes("agent-fixture-secret"));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/auth/capabilities");
  assert.equal(calls[0].request.method, "GET");
});

test("each write verifies admin first and only keeps the credential in memory", async () => {
  const {createAdminAuth} = await modulePromise;
  const calls = [];
  let prompts = 0;
  const auth = createAdminAuth({promptToken: async () => {prompts++; return "admin-fixture-secret";}, fetchFn: async (url, request) => {
    calls.push({url, request}); return url === "/api/auth/capabilities" ? reply(200, administrator) : reply(202);
  }});
  await auth.write("/api/actions/interpret", options);
  await auth.write("/api/issuers/1/crm", {...options, method: "PATCH"});
  assert.equal(prompts, 1);
  assert.deepEqual(calls.map(call => call.url), ["/api/auth/capabilities", "/api/actions/interpret", "/api/auth/capabilities", "/api/issuers/1/crm"]);
  for (const {request} of calls) {
    assert.equal(request.headers.Authorization, "Bearer admin-fixture-secret");
    assert.equal(request.redirect, "error");
    assert.equal(request.cache, "no-store");
  }
  assert.equal(calls[1].request.body, options.body);
  assert.equal(Object.hasOwn(calls[1].request, "timeoutMs"), false);
  auth.clear();
  await auth.write("/api/actions/interpret", options);
  assert.equal(prompts, 2);
});

test("401 or 403 clears memory and never resends the write automatically", async () => {
  const {createAdminAuth} = await modulePromise;
  for (const status of [401, 403]) {
    let prompts = 0, writes = 0;
    const auth = createAdminAuth({promptToken: async () => {prompts++; return "private-fixture-token";}, fetchFn: async url => {
      if (url === "/api/auth/capabilities") return reply(200, administrator);
      writes++; return reply(status);
    }});
    await assert.rejects(auth.write("/api/radar/weight", options), /automatisk omsändning/);
    assert.equal(writes, 1);
    await assert.rejects(auth.write("/api/radar/weight", options), /automatisk omsändning/);
    assert.equal(writes, 2);
    assert.equal(prompts, 2);
  }
});

test("verification and transport failures cannot disclose a credential", async () => {
  const {createAdminAuth} = await modulePromise;
  const secret = "never-show-fixture-secret";
  const auth = createAdminAuth({promptToken: async () => secret, fetchFn: async () => {throw new Error(secret);}});
  await assert.rejects(auth.write("/api/actions/interpret", options), error => !error.message.includes(secret) && /Ingen ändring/.test(error.message));
  const transport = createAdminAuth({promptToken: async () => secret, fetchFn: async url => {
    if (url === "/api/auth/capabilities") return reply(200, administrator);
    throw new Error(secret);
  }});
  await assert.rejects(transport.write("/api/actions/interpret", options), error => !error.message.includes(secret) && /Kontrollera status/.test(error.message));
});

test("read-only preview needs no admin credential and cannot leak an old header", async () => {
  const {createAdminAuth} = await modulePromise;
  let prompts = 0;
  const calls = [];
  const auth = createAdminAuth({promptToken: async () => {prompts++;}, fetchFn: async (url, request) => {calls.push({url, request}); return reply(200);}});
  await auth.write("/api/radar/preview", {...options, headers: {Authorization: "Bearer must-not-send"}});
  assert.equal(prompts, 0);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].request.headers.Authorization, undefined);
});

test("explicit weight-form credential still needs verified admin permission", async () => {
  const {createAdminAuth} = await modulePromise;
  let prompts = 0;
  const auth = createAdminAuth({promptToken: async () => {prompts++;}, fetchFn: async url => url === "/api/auth/capabilities" ? reply(200, agent) : reply(200)});
  await assert.rejects(auth.write("/api/radar/weight", options, "agent-fixture-token"), /administratör/);
  assert.equal(prompts, 0);
});

test("no credential can be sent to external URLs or non-write methods", async () => {
  const {createAdminAuth} = await modulePromise;
  let requests = 0;
  const auth = createAdminAuth({fetchFn: async () => {requests++;}});
  for (const target of ["https://other.example/api/write", "//other.example/api/write", "/api/\\other"])
    await assert.rejects(auth.write(target, options), /Ogiltig/);
  await assert.rejects(auth.write("/api/issuers", {method: "GET"}), /Ogiltig/);
  assert.equal(requests, 0);
});

test("the credential dialog is masked and neither UI stores nor logs tokens", () => {
  assert.match(source, /type="password"/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|console\./);
  for (const file of ["workspace.js"]) {
    const ui = fs.readFileSync(path.join(__dirname, "..", "..", "src", "capital_radar", "web", file), "utf8");
    assert.match(ui, /import\("\/admin-auth\.js\?v=agent-auth-1"\)/);
  }
});
