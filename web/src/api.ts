export type JsonObject = Record<string, unknown>;
export type Role = "owner" | "inventor" | "builder" | "viewer";

export interface Metadata {
  world_id: string;
  branch_id: string;
  mode?: string;
  ai_mode?: string;
  owner_id?: string;
  role?: Role;
  rules_epoch?: number;
  tick?: number;
  paused?: boolean;
  members?: Record<string, Role>;
  available_blueprints?: Record<string, string>;
  state_hash?: string;
  [key: string]: unknown;
}
export interface Snapshot {
  world_id: string;
  branch_id: string;
  last_seq: number;
  tick: number;
  state_hash: string;
  state: WorldState;
}
export interface WorldState {
  tick?: number;
  rules_epoch?: number;
  map?: { width?: number; height?: number; default_tile?: string; overrides?: Array<{ cell?: number[]; tile?: string }> };
  sources?: Array<{ id?: string; cell?: number[]; [key: string]: unknown }>;
  warehouse?: Record<string, number>;
  entities?: Record<string, EntityState>;
  energy_ledger?: Record<string, number>;
  [key: string]: unknown;
}
export interface EntityState {
  entity_id?: string;
  node_id?: string;
  cell?: number[];
  last_status?: string;
  on_hand?: Record<string, number>;
  input_escrow?: Record<string, number>;
  active_job?: { progress_eu?: number; work_required_eu?: number; [key: string]: unknown } | null;
  [key: string]: unknown;
}
export interface CommandResult extends JsonObject { command_id?: string; status?: string; result?: unknown; }

function errorText(value: unknown, fallback: string): string {
  if (value && typeof value === "object" && "error" in value) {
    const err = (value as JsonObject).error;
    if (err && typeof err === "object") return `${(err as JsonObject).code || "E_REQUEST"}: ${(err as JsonObject).message || fallback}`;
  }
  return fallback;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;
  constructor(status: number, body: unknown) { super(errorText(body, `HTTP ${status}`)); this.status = status; this.body = body; }
}

export class WorldForgeApi {
  readonly base: string;
  readonly actor: string;
  private csrfToken = "";
  private sessionActor = "";
  constructor(base = "/api", actor = "local-owner") {
    this.base = base.replace(/\/$/, ""); this.actor = actor; try { this.csrfToken = sessionStorage.getItem("wf_csrf") || ""; this.sessionActor = sessionStorage.getItem("wf_actor") || ""; } catch { /* storage can be disabled in hardened browsers */ }
  }
  private headers(extra: HeadersInit = {}): Headers {
    const headers = new Headers(extra); headers.set("Accept", "application/json");
    // Once the opaque session cookie is established, actor identity comes
    // from the server-side session. The header is only a local dev fallback
    // before sign-in and is never sent alongside an authenticated session.
    if (!this.sessionActor) headers.set("X-Actor-ID", this.actor);
    if (this.csrfToken) headers.set("X-CSRF-Token", this.csrfToken);
    return headers;
  }
  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(`${this.base}${path}`, { ...init, credentials: "include", headers: this.headers(init.headers) });
    let body: unknown = null; try { body = await response.json(); } catch { /* empty response */ }
    if (!response.ok) throw new ApiError(response.status, body);
    return body as T;
  }
  async signIn(actorId = this.actor): Promise<{ actor_id: string; csrf_token?: string; session_id?: string }> {
    try {
      const value = await this.request<{ actor_id?: string; csrf_token?: string; session_id?: string }>("/sessions", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor_id: actorId }) });
      this.sessionActor = value.actor_id || actorId; this.csrfToken = value.csrf_token || ""; try { sessionStorage.setItem("wf_actor", this.sessionActor); if (this.csrfToken) sessionStorage.setItem("wf_csrf", this.csrfToken); } catch { /* storage can be disabled */ } return { ...value, actor_id: this.sessionActor };
    } catch (error) {
      // Current fixture API uses explicit local actor headers. Keep the UI
      // usable while the persisted session endpoint is unavailable.
      if (error instanceof ApiError && (error.status === 404 || error.status === 405)) { this.sessionActor = actorId; return { actor_id: actorId }; }
      throw error;
    }
  }
  async createWorld(scenarioId = "river_demo"): Promise<Metadata> { return this.request<Metadata>("/worlds", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ scenario_id: scenarioId }) }); }
  metadata(worldId: string): Promise<Metadata> { return this.request<Metadata>(`/worlds/${encodeURIComponent(worldId)}`); }
  snapshot(worldId: string): Promise<Snapshot> { return this.request<Snapshot>(`/worlds/${encodeURIComponent(worldId)}/snapshot`); }
  async command(worldId: string, kind: string, payload: JsonObject, expectedRulesEpoch?: number): Promise<CommandResult> {
    const commandId = `ui-${Date.now().toString(36)}-${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}`;
    return this.request<CommandResult>(`/worlds/${encodeURIComponent(worldId)}/commands`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ command_id: commandId, actor_id: this.sessionActor || this.actor, world_id: worldId, kind, payload, expected_rules_epoch: expectedRulesEpoch }) });
  }
  tick(worldId: string): Promise<JsonObject> { return this.request<JsonObject>(`/worlds/${encodeURIComponent(worldId)}/tick`, { method: "POST" }); }
  members(worldId: string, actorId: string, role?: Role, revoked = false): Promise<Metadata> { return this.request<Metadata>(`/worlds/${encodeURIComponent(worldId)}/members`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ actor_id: actorId, role, revoked }) }); }
  async save(worldId: string): Promise<JsonObject> { return this.tryPaths<JsonObject>([`/worlds/${encodeURIComponent(worldId)}/checkpoints`, `/worlds/${encodeURIComponent(worldId)}/save`], { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind: "checkpoint" }) }); }
  async load(worldId: string, checkpointId?: string): Promise<JsonObject> { const suffix = checkpointId ? `?checkpoint_id=${encodeURIComponent(checkpointId)}` : ""; return this.tryPaths<JsonObject>([`/worlds/${encodeURIComponent(worldId)}/load${suffix}`, `/worlds/${encodeURIComponent(worldId)}/restore${suffix}`], { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(checkpointId ? { checkpoint_id: checkpointId } : {}) }); }
  async verify(worldId: string): Promise<JsonObject> { return this.tryPaths<JsonObject>([`/worlds/${encodeURIComponent(worldId)}/verify`, `/worlds/${encodeURIComponent(worldId)}/replay`], { method: "POST" }); }
  private async tryPaths<T>(paths: string[], init: RequestInit = {}): Promise<T> { let last: unknown; for (const path of paths) { try { return await this.request<T>(path, init); } catch (error) { last = error; if (!(error instanceof ApiError) || ![404, 405].includes(error.status)) throw error; } } throw last instanceof Error ? last : new Error("endpoint unavailable"); }
  websocket(worldId: string, lastSeq: number, onMessage: (message: JsonObject) => void, onStatus: (status: string) => void): () => void {
    const base = new URL(this.base, window.location.href); const scheme = base.protocol === "https:" ? "wss:" : "ws:"; const url = `${scheme}//${base.host}/ws/worlds/${encodeURIComponent(worldId)}`;
    let closed = false; let socket: WebSocket | undefined; let timer: number | undefined;
    const connect = () => { if (closed) return; onStatus("connecting"); socket = new WebSocket(url); socket.addEventListener("open", () => { onStatus("connected"); socket?.send(JSON.stringify({ type: "subscribe", last_seq: lastSeq })); }); socket.addEventListener("message", event => { try { const message = JSON.parse(String(event.data)) as JsonObject; onMessage(message); if (typeof message.event_seq === "number") lastSeq = Math.max(lastSeq, message.event_seq); } catch { onStatus("protocol error"); } }); socket.addEventListener("close", () => { if (closed) return; onStatus("reconnecting"); timer = window.setTimeout(connect, 750); }); socket.addEventListener("error", () => onStatus("socket error")); };
    connect(); return () => { closed = true; if (timer) window.clearTimeout(timer); socket?.close(); };
  }
}



