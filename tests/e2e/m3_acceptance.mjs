import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";

/**
 * Browser acceptance for the M3 boundary.
 *
 * The test intentionally drives the service through two isolated browser
 * contexts.  It does not call Engine or read expected values from a fixture:
 * all values below are read from the authoritative snapshot after ticks.
 * `restartBackend` is supplied by tools/run_e2e.mjs so the same persistent
 * data directory is used before and after a process restart.
 */
export async function runAcceptance({ chromium, browserExecutable, apiUrl, uiUrl, artifactDir, restartBackend }) {
  await fs.mkdir(artifactDir, { recursive: true });
  const browser = await chromium.launch({
    headless: true,
    // The bundled runtime includes Chromium but not Playwright's separate
    // headless-shell binary.  Launch the installed Chrome executable directly
    // so the test never attempts a network browser download.
    executablePath: browserExecutable || process.env.WF_BROWSER_EXECUTABLE || chromium.executablePath(),
  });
  let owner;
  let builder;
  let viewer;
  let ownerPage;
  let builderPage;
  let worldId;
  let savedHash;
  let savedTick;
  const evidence = { apiUrl, uiUrl, checks: [], failures: [] };

  const mark = (name, details = {}) => {
    evidence.checks.push({ name, ...details });
  };
  const fail = (name, error) => {
    evidence.failures.push({ name, error: String(error?.stack || error) });
    throw error;
  };

  async function saveScreenshot(page, name) {
    try { await page.screenshot({ path: path.join(artifactDir, `${name}.png`), fullPage: true }); } catch { /* evidence is best effort */ }
  }

  async function apiRequest(context, method, route, body, expected = null) {
    const options = { method, headers: { accept: "application/json" } };
    if (body !== undefined) {
      options.data = body;
      options.headers["content-type"] = "application/json";
    }
    const response = await context.request.fetch(`${apiUrl}${route}`, options);
    let value = null;
    try { value = await response.json(); } catch { /* preserve useful status below */ }
    if (expected && !expected.includes(response.status())) {
      throw new Error(`${method} ${route}: expected ${expected.join("/")}, got ${response.status()} ${JSON.stringify(value)}`);
    }
    return { response, value };
  }

  async function establishSession(context, actorId) {
    // M3 uses an HTTP session cookie.  A localhost fallback is retained for
    // development servers that have not enabled the session adapter yet; the
    // fallback still keeps actor identity outside command payloads.
    const response = await context.request.post(`${apiUrl}/api/sessions`, {
      data: { actor_id: actorId },
      headers: { accept: "application/json", "content-type": "application/json" },
    });
    if (response.status() === 404 || response.status() === 405) {
      await context.setExtraHTTPHeaders({ "X-Actor-ID": actorId });
      mark("session-fallback", { actorId, status: response.status() });
      return { fallback: true };
    }
    assert.ok([200, 201, 204].includes(response.status()), `session ${actorId} failed: ${response.status()} ${await response.text()}`);
    const body = response.status() === 204 ? {} : await response.json().catch(() => ({}));
    // Some local adapters return a bearer token as well as a cookie.  Accept
    // that explicit response while preferring HttpOnly cookie storage.
    const token = body.session_token || body.access_token;
    const headers = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body.csrf_token) headers["X-CSRF-Token"] = body.csrf_token;
    if (Object.keys(headers).length) await context.setExtraHTTPHeaders(headers);
    mark("session", { actorId, cookie: !token, csrf: Boolean(body.csrf_token) });
    return body;
  }

  async function createWorld() {
    const { response, value } = await apiRequest(owner, "POST", "/api/worlds", { scenario_id: "river_demo" }, [200, 201]);
    assert.ok(value?.world_id, `world create did not return world_id: ${JSON.stringify(value)}`);
    mark("world-created", { status: response.status(), worldId: value.world_id });
    return value;
  }

  async function command(context, commandId, kind, payload, expectedRulesEpoch = 1) {
    const { response, value } = await apiRequest(context, "POST", `/api/worlds/${encodeURIComponent(worldId)}/commands`, {
      command_id: commandId,
      kind,
      payload,
      expected_rules_epoch: expectedRulesEpoch,
    }, [200, 202]);
    assert.ok(value?.command_id === commandId, `${kind} response lost command id: ${JSON.stringify(value)}`);
    return { response, value };
  }

  async function snapshot(context) {
    const { response, value } = await apiRequest(context, "GET", `/api/worlds/${encodeURIComponent(worldId)}/snapshot`, undefined, [200]);
    assert.equal(response.status(), 200);
    assert.ok(value?.state_hash, `snapshot has no state_hash: ${JSON.stringify(value)}`);
    return value;
  }

  async function tick() {
    const { value } = await apiRequest(owner, "POST", `/api/worlds/${encodeURIComponent(worldId)}/tick`, undefined, [200, 202]);
    assert.ok(value?.tick !== undefined, `tick response has no tick: ${JSON.stringify(value)}`);
    return value;
  }

  async function updateMember(actorId, role, revoked = false) {
    const { value } = await apiRequest(owner, "POST", `/api/worlds/${encodeURIComponent(worldId)}/members`, { actor_id: actorId, role, revoked }, [200, 201]);
    return value;
  }

  function entitiesOf(state) {
    const entities = state?.entities || {};
    return Array.isArray(entities) ? entities : Object.values(entities);
  }

  function entityByNode(state, nodeId) {
    return entitiesOf(state).find((entry) => entry.node_id === nodeId || entry.node?.id === nodeId);
  }

  async function openWs(page, lastSeq) {
    const wsUrl = `${apiUrl.replace(/^http/, "ws")}/ws/worlds/${encodeURIComponent(worldId)}?actor_id=${encodeURIComponent(lastSeq.actorId || "owner")}`;
    return page.evaluate(({ url, seq }) => new Promise((resolve, reject) => {
      const socket = new WebSocket(url);
      const timeout = setTimeout(() => { socket.close(); reject(new Error("WebSocket timeout")); }, 5000);
      socket.onerror = () => { clearTimeout(timeout); reject(new Error("WebSocket connection failed")); };
      socket.onopen = () => socket.send(JSON.stringify({ type: "subscribe", last_seq: seq.value }));
      socket.onmessage = (event) => {
        clearTimeout(timeout);
        let value;
        try { value = JSON.parse(event.data); } catch (error) { reject(error); return; }
        socket.close();
        resolve(value);
      };
    }), { url: wsUrl, seq: lastSeq });
  }

  async function saveWorld(context) {
    const candidates = [
      ["/save", { tick: savedTick }],
      ["/checkpoint", { tick: savedTick }],
      ["/exports", { checkpoint: savedTick }],
    ];
    let last;
    for (const [suffix, body] of candidates) {
      const route = `/api/worlds/${encodeURIComponent(worldId)}${suffix}`;
      const result = await apiRequest(context, "POST", route, body);
      if ([200, 201, 202].includes(result.response.status())) {
        mark("save", { route, status: result.response.status() });
        return result.value || {};
      }
      last = `${route}: ${result.response.status()}`;
    }
    throw new Error(`no save/checkpoint endpoint succeeded (${last})`);
  }

  async function loadWorld(context, saveResult) {
    const candidates = [
      ["/load", saveResult],
      ["/restore", saveResult],
      ["/import", saveResult],
    ];
    let last;
    for (const [suffix, body] of candidates) {
      const route = suffix === "/import" ? "/api/worlds/import" : `/api/worlds/${encodeURIComponent(worldId)}${suffix}`;
      const result = await apiRequest(context, "POST", route, body || { tick: savedTick });
      if ([200, 201, 202].includes(result.response.status())) {
        mark("load", { route, status: result.response.status() });
        return result.value || {};
      }
      last = `${route}: ${result.response.status()}`;
    }
    throw new Error(`no load/restore endpoint succeeded (${last})`);
  }

  try {
    owner = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    builder = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    viewer = await browser.newContext({ viewport: { width: 1100, height: 800 } });
    await establishSession(owner, "owner");
    await establishSession(builder, "builder");
    await establishSession(viewer, "viewer");

    const world = await createWorld();
    worldId = world.world_id;
    const blueprintHash = world.available_blueprints?.river_mill || world.blueprint_hash;
    assert.ok(blueprintHash, `world metadata has no river_mill blueprint: ${JSON.stringify(world)}`);
    await updateMember("builder", "builder");

    const ownerMeta = await apiRequest(owner, "GET", `/api/worlds/${encodeURIComponent(worldId)}`, undefined, [200]);
    assert.equal(ownerMeta.value?.role, "owner");
    const viewerMeta = await apiRequest(viewer, "GET", `/api/worlds/${encodeURIComponent(worldId)}`, undefined, [403]);
    assert.equal(viewerMeta.response.status(), 403, "ungranted viewer must not read world");
    mark("role-denial", { status: viewerMeta.response.status() });

    await command(owner, "e2e-install", "INSTALL_VALIDATED_BLUEPRINT", { blueprint_hash: blueprintHash });
    await command(builder, "e2e-place", "PLACE", { blueprint_hash: blueprintHash, origin: [2, 1] });
    // The second browser context submits the only input batch.  This proves it
    // can use the installed mechanism before any production tick is advanced.
    await command(builder, "e2e-deposit", "DEPOSIT", { entity_id: "e2/mill", item: "grain", amount: 10 });
    mark("second-session-command", { actor: "builder", kind: "DEPOSIT" });
    await tick();

    ownerPage = await owner.newPage();
    builderPage = await builder.newPage();
    const browserDiagnostics = [];
    const externalRequests = [];
    for (const page of [ownerPage, builderPage]) {
      page.on("pageerror", (error) => browserDiagnostics.push({ type: "pageerror", message: String(error) }));
      page.on("console", (message) => { if (message.type() === "error") browserDiagnostics.push({ type: "console", message: message.text() }); });
      page.on("response", (response) => { if (response.status() >= 400) browserDiagnostics.push({ type: "http", status: response.status(), url: response.url() }); });
      page.on("request", (request) => {
        try {
          const url = new URL(request.url());
          if (url.protocol !== "data:" && !["127.0.0.1", "localhost"].includes(url.hostname)) externalRequests.push(request.url());
        } catch { /* ignore malformed browser-internal URLs */ }
      });
    }
    evidence.browserDiagnostics = browserDiagnostics;
    evidence.externalRequests = externalRequests;
    await ownerPage.goto(`${uiUrl}/?api=${encodeURIComponent(`${apiUrl}/api`)}&world=${encodeURIComponent(worldId)}&actor=owner&mode=fixture&ai=offline`, { waitUntil: "domcontentloaded" });
    await builderPage.goto(`${uiUrl}/?api=${encodeURIComponent(`${apiUrl}/api`)}&world=${encodeURIComponent(worldId)}&actor=builder&mode=fixture&ai=offline`, { waitUntil: "domcontentloaded" });
    await ownerPage.locator("#mode-badge").waitFor({ state: "visible", timeout: 15000 });
    await Promise.all([
      ownerPage.waitForFunction((id) => document.querySelector("#world-id")?.textContent?.includes(id), worldId, { timeout: 15000 }),
      builderPage.waitForFunction((id) => document.querySelector("#world-id")?.textContent?.includes(id), worldId, { timeout: 15000 }),
    ]);
    assert.equal(await ownerPage.locator("#mode-badge").textContent(), "mode=fixture");
    assert.equal(await ownerPage.locator("#ai-badge").textContent(), "AI_MODE=offline");
    await saveScreenshot(ownerPage, "owner-before-run");
    await saveScreenshot(builderPage, "builder-before-run");
    mark("dual-browser-ui", { owner: await ownerPage.locator("#world-id").textContent(), builder: await builderPage.locator("#world-id").textContent() });

    for (let i = 1; i < 12; i += 1) await tick();
    let final = await snapshot(owner);
    const state = final.state || final;
    const mill = entityByNode(state, "mill");
    assert.ok(mill, `mill entity absent after run: ${JSON.stringify(state.entities)}`);
    assert.equal(Number(mill.on_hand?.flour || 0), 4, "12 ticks must produce 4 flour");
    assert.equal(Number(mill.on_hand?.grain || 0), 6, "12 ticks must leave 6 grain");
    assert.equal(mill.active_job, null, "12 ticks must leave no active job");
    const ledger = state.energy_ledger || {};
    assert.equal(Number(ledger.external_withdrawn_eu), 96);
    assert.equal(Number(ledger.conversion_loss_eu), 24);
    assert.equal(Number(ledger.work_consumed_eu), 72);
    savedHash = final.state_hash;
    savedTick = Number(final.tick ?? state.tick);
    assert.equal(savedTick, 12);
    mark("standard-demo", { tick: savedTick, flour: mill.on_hand.flour, grain: mill.on_hand.grain, stateHash: savedHash, ledger });

    const builderFinal = await snapshot(builder);
    assert.equal(builderFinal.state_hash, savedHash, "second browser must see the same authoritative hash");
    await ownerPage.reload({ waitUntil: "domcontentloaded" });
    await ownerPage.waitForFunction((hash) => document.querySelector("#state-hash")?.textContent?.includes(hash), savedHash, { timeout: 15000 });
    assert.match(await ownerPage.locator("#state-hash").textContent(), new RegExp(savedHash));
    mark("same-hash-ui", { stateHash: savedHash });

    const wsInitial = await openWs(ownerPage, { actorId: "owner", value: 0 });
    assert.ok(["world_delta", "resync_required"].includes(wsInitial.type), `unexpected WS initial message ${JSON.stringify(wsInitial)}`);
    const priorSeq = Number(wsInitial.event_seq || 0);
    await tick();
    const wsReconnect = await openWs(ownerPage, { actorId: "owner", value: priorSeq });
    assert.ok(["world_delta", "resync_required"].includes(wsReconnect.type), `unexpected WS reconnect message ${JSON.stringify(wsReconnect)}`);
    mark("ws-reconnect", { initial: wsInitial.type, reconnect: wsReconnect.type, priorSeq });

    const queued = await command(builder, "e2e-revoke-write", "WITHDRAW", { entity_id: mill.entity_id, item: "flour", amount: 1 });
    assert.equal(queued.value.status, "accepted");
    await updateMember("builder", "builder", true);
    await tick();
    const late = await apiRequest(owner, "GET", `/api/worlds/${encodeURIComponent(worldId)}/commands/e2e-revoke-write`, undefined, [200]);
    assert.equal(late.value?.status, "rejected", `revoked queued command was not rejected: ${JSON.stringify(late.value)}`);
    mark("revoked-command", { status: late.value.status });

    final = await snapshot(owner);
    savedHash = final.state_hash;
    savedTick = Number(final.tick);
    const saveResult = await saveWorld(owner);
    await saveScreenshot(ownerPage, "before-restart");

    await owner.close();
    await builder.close();
    await viewer.close();
    await restartBackend();
    owner = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await establishSession(owner, "owner");
    // Exercise the explicit durable load route after the process restart. The
    // service may also restore automatically at startup, but load must remain
    // an idempotent, hash-checked operation for callers that request it.
    await loadWorld(owner, saveResult);
    const afterRestart = await apiRequest(owner, "GET", `/api/worlds/${encodeURIComponent(worldId)}/snapshot`, undefined, [200]);
    assert.equal(afterRestart.response.status(), 200, `world did not survive restart: ${JSON.stringify(afterRestart.value)}`);
    assert.equal(afterRestart.value.state_hash, savedHash, "save/restart/load must preserve state hash");
    assert.equal(Number(afterRestart.value.tick), savedTick);
    mark("restart-load", { tick: savedTick, stateHash: savedHash });
    ownerPage = await owner.newPage();
    await ownerPage.goto(`${uiUrl}/?api=${encodeURIComponent(`${apiUrl}/api`)}&world=${encodeURIComponent(worldId)}&actor=owner&mode=fixture&ai=offline`, { waitUntil: "domcontentloaded" });
    await ownerPage.waitForFunction((hash) => document.querySelector("#state-hash")?.textContent?.includes(hash), savedHash, { timeout: 15000 });
    assert.match(await ownerPage.locator("#state-hash").textContent(), new RegExp(savedHash));
    await saveScreenshot(ownerPage, "after-restart");
    mark("offline-mode", { mode: await ownerPage.locator("#mode-badge").textContent(), ai: await ownerPage.locator("#ai-badge").textContent() });
    assert.deepEqual(externalRequests, [], `offline UI made external requests: ${externalRequests.join(", ")}`);
    mark("offline-network", { externalRequests: externalRequests.length });
  } catch (error) {
    evidence.failures.push({ error: String(error?.stack || error) });
    if (ownerPage) await saveScreenshot(ownerPage, "failure-owner");
    if (builderPage) await saveScreenshot(builderPage, "failure-builder");
    throw error;
  } finally {
    await fs.writeFile(path.join(artifactDir, "evidence.json"), JSON.stringify(evidence, null, 2), "utf8");
    await ownerPage?.close().catch(() => {});
    await builderPage?.close().catch(() => {});
    await owner?.close().catch(() => {});
    await builder?.close().catch(() => {});
    await viewer?.close().catch(() => {});
    await browser.close().catch(() => {});
  }
  return evidence;
}
