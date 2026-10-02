/* WorldForge M3 UI adapter.
 *
 * This module deliberately contains no simulation rules. It fetches a complete
 * authoritative snapshot, draws it, and submits documented command envelopes.
 */
const query = new URLSearchParams(window.location.search);
const worldId = query.get("world") || "world-demo";
const apiBase = (query.get("api") || "/api").replace(/\/$/, "");
const actorId = query.get("actor") || "ui-local";
const displayMode = query.get("mode") || "fixture";
const aiMode = query.get("ai") || "offline";
const fixturePath = query.get("snapshot");

const $ = (id) => document.getElementById(id);
const elements = {
  mode: $("mode-badge"),
  ai: $("ai-badge"),
  connection: $("connection-badge"),
  world: $("world-id"),
  branch: $("branch-id"),
  epoch: $("rules-epoch"),
  tick: $("tick-value"),
  canvas: $("world-canvas"),
  canvasEmpty: $("canvas-empty"),
  external: $("energy-external"),
  loss: $("energy-loss"),
  work: $("energy-work"),
  unused: $("energy-unused"),
  balance: $("ledger-balance"),
  warehouse: $("warehouse-list"),
  entities: $("entities-body"),
  entityCount: $("entity-count"),
  selected: $("selected-label"),
  feedback: $("command-feedback"),
  stateHash: $("state-hash"),
  refresh: $("refresh-button"),
  advance: $("advance-button"),
  pause: $("pause-button"),
  resume: $("resume-button"),
  deposit: $("deposit-button"),
  withdraw: $("withdraw-button"),
  cancel: $("cancel-button"),
};

let state = null;
let metadata = {};
let selectedEntityId = null;
let commandCounter = 0;
let fixtureLoaded = false;

class WorldApi {
  constructor(base, id) {
    this.base = base;
    this.id = encodeURIComponent(id);
  }

  async snapshot() {
    const response = await fetch(`${this.base}/worlds/${this.id}/snapshot`, {
      headers: { Accept: "application/json", "X-Actor-ID": actorId },
      cache: "no-store",
    });
    return parseResponse(response);
  }

  async metadata() {
    const response = await fetch(`${this.base}/worlds/${this.id}`, {
      headers: { Accept: "application/json", "X-Actor-ID": actorId },
      cache: "no-store",
    });
    return parseResponse(response);
  }

  async command(kind, payload) {
    commandCounter += 1;
    const commandId = `ui-${Date.now()}-${commandCounter}`;
    const envelope = {
      command_id: commandId,
      actor_id: actorId,
      world_id: worldId,
      branch_id: metadata.branch_id || "branch-main",
      kind,
      payload,
      // The server assigns server_seq and authoritative effective_tick.
      // This hint lets the API reject stale writes without advancing physics.
      effective_tick: Number(state?.tick || 0) + 1,
      expected_rules_epoch: state?.rules_epoch,
    };
    const response = await fetch(`${this.base}/worlds/${this.id}/commands`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json", "X-Actor-ID": actorId },
      body: JSON.stringify(envelope),
    });
    return parseResponse(response);
  }

  async advance() {
    const response = await fetch(`${this.base}/worlds/${this.id}/tick`, {
      method: "POST",
      headers: { Accept: "application/json", "X-Actor-ID": actorId },
    });
    return parseResponse(response);
  }
}

const api = new WorldApi(apiBase, worldId);

async function parseResponse(response) {
  let value = null;
  try { value = await response.json(); } catch { /* API may return an empty error body. */ }
  if (!response.ok) {
    const error = value?.error || value || {};
    throw new Error(`${error.code || response.status}: ${error.message || response.statusText}`);
  }
  return value;
}

function unwrapSnapshot(value) {
  // API responses may wrap the state in {snapshot, metadata}; local fixture is
  // usually the state object itself. Keeping this adapter tolerant avoids UI
  // assumptions about persistence details.
  if (value && value.snapshot && typeof value.snapshot === "object") {
    return { state: value.snapshot, metadata: value };
  }
  if (value && value.state && typeof value.state === "object") {
    return { state: value.state, metadata: value };
  }
  return { state: value || {}, metadata: value || {} };
}

async function loadSnapshot() {
  setConnection("loading", "loading…");
  try {
    let value;
    if (fixturePath && !fixtureLoaded) {
      const response = await fetch(fixturePath, { headers: { Accept: "application/json", "X-Actor-ID": actorId }, cache: "no-store" });
      value = await parseResponse(response);
      fixtureLoaded = true;
      metadata = { ...unwrapSnapshot(value).metadata, mode: "fixture", ai_mode: "offline" };
      setConnection("ok", "fixture file");
    } else {
      const [snapshotValue, metadataValue] = await Promise.all([api.snapshot(), api.metadata()]);
      value = snapshotValue;
      metadata = { ...unwrapSnapshot(metadataValue).metadata, ...metadataValue };
      setConnection("ok", "API connected");
    }
    const unpacked = unwrapSnapshot(value);
    state = unpacked.state || {};
    metadata = { ...metadata, ...unpacked.metadata };
    render();
    setFeedback("已加载权威快照。", "ok");
  } catch (error) {
    setConnection("error", "API unavailable");
    setFeedback(`快照读取失败：${error.message}`, "error");
    elements.canvasEmpty.textContent = "无法读取权威快照";
    elements.canvasEmpty.classList.remove("hidden");
  }
}

function setConnection(kind, label) {
  elements.connection.textContent = label;
  elements.connection.className = `badge ${kind === "ok" ? "badge-ok" : kind === "error" ? "badge-error" : "badge-muted"}`;
}

function setFeedback(message, kind = "") {
  elements.feedback.textContent = message;
  elements.feedback.className = `feedback ${kind}`;
}

function integer(value, fallback = 0) {
  const number = Number(value);
  return Number.isSafeInteger(number) ? number : fallback;
}

function formatItems(items) {
  if (!items || typeof items !== "object") return "—";
  const rows = Object.entries(items).filter(([, amount]) => integer(amount) !== 0).sort(([a], [b]) => a.localeCompare(b));
  return rows.length ? rows.map(([item, amount]) => `${item} ${amount}`).join(" · ") : "—";
}

function render() {
  const entities = state?.entities && typeof state.entities === "object" ? state.entities : {};
  elements.mode.textContent = `mode=${metadata.mode || displayMode}`;
  elements.mode.className = `badge ${metadata.mode === "remote_model" ? "badge-live" : "badge-fixture"}`;
  elements.ai.textContent = `AI_MODE=${metadata.ai_mode || aiMode}`;
  elements.world.textContent = metadata.world_id || worldId;
  elements.branch.textContent = metadata.branch_id || state.branch_id || "branch-main";
  elements.epoch.textContent = state.rules_epoch ?? metadata.rules_epoch ?? "—";
  elements.tick.textContent = state.tick ?? metadata.tick ?? "—";
  elements.stateHash.textContent = `state_hash: ${metadata.state_hash || state.state_hash || "—"}`;

  renderLedger(state.energy_ledger || {});
  renderWarehouse(state.warehouse || {});
  renderEntities(entities);
  drawWorld(state, entities);
  updateCommandAvailability(entities);
}

function renderLedger(ledger) {
  const external = integer(ledger.external_withdrawn_eu);
  const loss = integer(ledger.conversion_loss_eu);
  const work = integer(ledger.work_consumed_eu);
  const unused = integer(ledger.unused_dissipated_eu);
  const balanced = external === loss + work + unused;
  elements.external.textContent = external;
  elements.loss.textContent = loss;
  elements.work.textContent = work;
  elements.unused.textContent = unused;
  elements.balance.textContent = balanced ? "balanced" : "ledger mismatch";
  elements.balance.className = `balance-pill${balanced ? "" : " bad"}`;
}

function renderWarehouse(warehouse) {
  const entries = Object.entries(warehouse).sort(([a], [b]) => a.localeCompare(b));
  elements.warehouse.innerHTML = entries.length ? entries.map(([item, amount]) => `<div class="inventory-item"><strong>${escapeHtml(amount)}</strong><span>${escapeHtml(item)}</span></div>`).join("") : `<span class="muted">空</span>`;
}

function statusClass(status) {
  const value = String(status || "idle");
  if (value === "running") return "status-running";
  if (value === "paused") return "status-paused";
  if (["no_input", "no_power", "output_full", "gated"].includes(value)) return "status-blocked";
  return "status-idle";
}

function jobProgress(entity) {
  const job = entity.active_job;
  if (!job) return "—";
  return `${integer(job.progress_eu)}/${integer(job.work_required_eu)} EU`;
}

function renderEntities(entities) {
  const rows = Object.values(entities).sort((a, b) => String(a.entity_id).localeCompare(String(b.entity_id)));
  elements.entityCount.textContent = `${rows.length} entities`;
  if (!rows.length) {
    elements.entities.innerHTML = `<tr><td colspan="6" class="muted">当前快照没有实体</td></tr>`;
    elements.selected.textContent = "未选择实体";
    return;
  }
  elements.entities.innerHTML = rows.map((entity) => {
    const id = String(entity.entity_id || "");
    const status = String(entity.last_status || "idle");
    const selected = id === selectedEntityId ? " selected" : "";
    const cell = Array.isArray(entity.cell) ? `[${entity.cell.join(", ")}]` : "—";
    return `<tr class="${selected}" data-entity-id="${escapeHtml(id)}"><td><span class="entity-id">${escapeHtml(id)}</span></td><td class="mono">${escapeHtml(entity.node_id || "—")}</td><td class="mono">${escapeHtml(cell)}</td><td><span class="status ${statusClass(status)}">${escapeHtml(status)}</span></td><td>${escapeHtml(formatItems(entity.on_hand))}</td><td class="mono">${escapeHtml(jobProgress(entity))}</td></tr>`;
  }).join("");
  elements.entities.querySelectorAll("tr[data-entity-id]").forEach((row) => row.addEventListener("click", () => selectEntity(row.dataset.entityId)));
  const selected = entities[selectedEntityId];
  elements.selected.textContent = selected ? `已选择 ${selected.entity_id} · ${selected.node_id || "node"}` : "未选择实体";
}

function selectEntity(entityId) {
  selectedEntityId = entityId;
  render();
}

function drawWorld(world, entities) {
  const canvas = elements.canvas;
  const ratio = window.devicePixelRatio || 1;
  const cssWidth = canvas.clientWidth || 900;
  const cssHeight = canvas.clientHeight || 548;
  if (canvas.width !== Math.round(cssWidth * ratio) || canvas.height !== Math.round(cssHeight * ratio)) {
    canvas.width = Math.round(cssWidth * ratio);
    canvas.height = Math.round(cssHeight * ratio);
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, cssWidth, cssHeight);
  context.fillStyle = "#0b1824";
  context.fillRect(0, 0, cssWidth, cssHeight);
  const map = world?.map || {};
  const mapWidth = integer(map.width, 32) || 32;
  const mapHeight = integer(map.height, 32) || 32;
  const margin = 26;
  const cellSize = Math.min((cssWidth - margin * 2) / mapWidth, (cssHeight - margin * 2) / mapHeight);
  const originX = (cssWidth - cellSize * mapWidth) / 2;
  const originY = (cssHeight - cellSize * mapHeight) / 2;
  const overrides = new Map((map.overrides || []).map((entry) => [Array.isArray(entry.cell) ? entry.cell.join(",") : "", entry.tile]));
  for (let y = 0; y < mapHeight; y += 1) {
    for (let x = 0; x < mapWidth; x += 1) {
      const tile = overrides.get(`${x},${y}`) || map.default_tile || "land";
      context.fillStyle = tile === "water" || tile === "river" ? "#123e51" : "#112331";
      context.fillRect(originX + x * cellSize, originY + y * cellSize, cellSize - .8, cellSize - .8);
    }
  }
  for (const source of world?.sources || []) {
    const cell = source.cell;
    if (!Array.isArray(cell) || cell.length !== 2) continue;
    const [x, y] = cell;
    context.fillStyle = "#6cb6ff";
    context.beginPath();
    context.arc(originX + (x + .5) * cellSize, originY + (y + .5) * cellSize, Math.max(3, cellSize * .23), 0, Math.PI * 2);
    context.fill();
  }
  for (const entity of Object.values(entities || {})) {
    const cell = entity.cell;
    if (!Array.isArray(cell) || cell.length !== 2) continue;
    const [x, y] = cell;
    const status = String(entity.last_status || "idle");
    context.fillStyle = status === "running" ? "#5de1c5" : ["no_input", "no_power", "output_full", "gated"].includes(status) ? "#f27f82" : "#b0c1cf";
    context.fillRect(originX + x * cellSize + 2, originY + y * cellSize + 2, Math.max(5, cellSize - 4), Math.max(5, cellSize - 4));
    if (entity.entity_id === selectedEntityId) {
      context.strokeStyle = "#ffffff";
      context.lineWidth = 2;
      context.strokeRect(originX + x * cellSize + 1, originY + y * cellSize + 1, Math.max(7, cellSize - 2), Math.max(7, cellSize - 2));
    }
  }
  elements.canvasEmpty.classList.toggle("hidden", Object.keys(entities || {}).length > 0);
  canvas.__wfGrid = { originX, originY, cellSize, mapWidth, mapHeight, entities };
}

function updateCommandAvailability(entities) {
  const hasSelection = Boolean(selectedEntityId && entities[selectedEntityId]);
  [elements.deposit, elements.withdraw, elements.cancel].forEach((button) => { button.disabled = !hasSelection; });
  elements.advance.disabled = Boolean(metadata.role && metadata.role !== "owner");
  elements.pause.disabled = false;
  elements.resume.disabled = false;
}

async function submitCommand(kind, payload) {
  setFeedback(`正在提交 ${kind}…`);
  [elements.advance, elements.pause, elements.resume, elements.deposit, elements.withdraw, elements.cancel].forEach((button) => { button.disabled = true; });
  try {
    const response = await api.command(kind, payload);
    setFeedback(`${kind} 已接受：${response?.command_id || response?.status || "ok"}。等待下一份快照。`, "ok");
    await loadSnapshotFromApi();
  } catch (error) {
    setFeedback(`命令失败：${error.message}`, "error");
    updateCommandAvailability(state?.entities || {});
  }
}

async function loadSnapshotFromApi() {
  const [value, metadataValue] = await Promise.all([api.snapshot(), api.metadata()]);
  const unpacked = unwrapSnapshot(value);
  state = unpacked.state || {};
  metadata = { ...metadata, ...metadataValue, ...unpacked.metadata };
  setConnection("ok", "API connected");
  render();
}

function requireSelection() {
  if (!selectedEntityId || !state?.entities?.[selectedEntityId]) {
    setFeedback("请先在画布或实体表中选择实体。", "error");
    return null;
  }
  return state.entities[selectedEntityId];
}

function promptItemAmount(defaultItem) {
  const item = window.prompt("物料名称", defaultItem);
  if (!item) return null;
  const amountRaw = window.prompt("数量（正整数）", "1");
  const amount = Number(amountRaw);
  if (!Number.isSafeInteger(amount) || amount <= 0) {
    setFeedback("数量必须是正整数。", "error");
    return null;
  }
  return { item, amount };
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
}

elements.refresh.addEventListener("click", () => loadSnapshotFromApi().catch((error) => setFeedback(`刷新失败：${error.message}`, "error")));
elements.advance.addEventListener("click", async () => {
  setFeedback("正在请求权威 tick…");
  elements.advance.disabled = true;
  try {
    await api.advance();
    await loadSnapshotFromApi();
    setFeedback("已提交一个权威 tick。", "ok");
  } catch (error) {
    setFeedback(`推进失败：${error.message}`, "error");
    updateCommandAvailability(state?.entities || {});
  }
});
elements.pause.addEventListener("click", () => submitCommand("SET_PAUSED", { paused: true }));
elements.resume.addEventListener("click", () => submitCommand("SET_PAUSED", { paused: false }));
elements.deposit.addEventListener("click", () => {
  const entity = requireSelection();
  if (!entity) return;
  const values = promptItemAmount("grain");
  if (values) submitCommand("DEPOSIT", { entity_id: entity.entity_id, ...values });
});
elements.withdraw.addEventListener("click", () => {
  const entity = requireSelection();
  if (!entity) return;
  const values = promptItemAmount("flour");
  if (values) submitCommand("WITHDRAW", { entity_id: entity.entity_id, ...values });
});
elements.cancel.addEventListener("click", () => {
  const entity = requireSelection();
  if (entity) submitCommand("CANCEL_JOB", { entity_id: entity.entity_id });
});
elements.canvas.addEventListener("click", (event) => {
  const grid = elements.canvas.__wfGrid;
  if (!grid) return;
  const rect = elements.canvas.getBoundingClientRect();
  const x = Math.floor((event.clientX - rect.left - grid.originX) / grid.cellSize);
  const y = Math.floor((event.clientY - rect.top - grid.originY) / grid.cellSize);
  const hit = Object.values(grid.entities || {}).find((entity) => Array.isArray(entity.cell) && entity.cell[0] === x && entity.cell[1] === y);
  if (hit) selectEntity(hit.entity_id);
});
window.addEventListener("resize", () => { if (state) drawWorld(state, state.entities || {}); });

// Labels are visible before the first response, which makes the trust boundary
// explicit even while the API is offline.
elements.mode.textContent = `mode=${displayMode}`;
elements.ai.textContent = `AI_MODE=${aiMode}`;
elements.world.textContent = worldId;
loadSnapshot();



