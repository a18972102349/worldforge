"""Offline M3 HTTP/WebSocket service for WorldForge.

The web layer is deliberately a thin adapter around the deterministic Engine.  A
WorldActor owns one engine and serializes command submission with a lock; HTTP
and websocket handlers never mutate engine state directly.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

try:  # Keep the core importable without HTTP dependencies.
    from fastapi import FastAPI, Header, Request, Response, WebSocket, WebSocketDisconnect
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover - exercised when web extra absent
    FastAPI = None  # type: ignore[assignment]
    _FASTAPI_IMPORT_ERROR = exc

from wf.compiler.compiler import compile_blueprint
from wf.core.canonical import blueprint_hash, canonical_bytes, check_canonical_value, digest, strict_loads
from wf.core.engine import Engine
from wf.core.errors import WFError
from wf.pack import MAX_PACK_BYTES, import_rule_pack, make_rule_pack
from wf.adapters import FixtureProvider, InventionCompiler, InventionRequest, ManualProvider, RemoteProvider
from wf.chain import LocalDevelopmentChain
from wf.verifier import check_report
from wf.web.persistence import SQLiteWebStore

_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_DIR = _ROOT / "examples" / "fixtures"
_BLUEPRINT_DIR = _ROOT / "examples" / "blueprints"
_CATALOG_PATH = _FIXTURE_DIR / "catalog.json"
_SCENARIOS = {
    "river_demo": _FIXTURE_DIR / "world.demo.json",
    "world.demo": _FIXTURE_DIR / "world.demo.json",
    "demo": _FIXTURE_DIR / "world.demo.json",
}

# Commands are checked before persistence and checked again while executing.
_COMMAND_ROLES = {
    "INSTALL_BLUEPRINT": "owner",
    "INSTALL_VALIDATED_BLUEPRINT": "owner",
    "INSTALL_PACK": "owner",
    "DISABLE_FUTURE_PLACEMENT": "owner",
    "SET_PAUSED": "owner",
    "PLACE": "builder",
    "CONNECT": "builder",
    "DISCONNECT": "builder",
    "DEPOSIT": "builder",
    "WITHDRAW": "builder",
    "CANCEL_JOB": "builder",
}
_ROLE_RANK = {"viewer": 0, "inventor": 1, "builder": 2, "owner": 3}


def _load_json(path: Path) -> dict[str, Any]:
    return strict_loads(path.read_bytes())


def _request_id() -> str:
    return "req-" + uuid.uuid4().hex


def _actor_id(header: str | None, body: Mapping[str, Any] | None = None, authorization: str | None = None) -> str:
    # X-Actor-ID is the local development session identity.  Actor identity is
    # deliberately taken from transport authentication, never from a command
    # payload that a client can rewrite.
    bearer = None
    if authorization is not None:
        if not authorization.lower().startswith("bearer "):
            raise WFError("E_AUTH", "unsupported authorization scheme")
        bearer = authorization[7:].strip()
    value = header if header is not None else bearer if bearer is not None else "local-owner"
    if isinstance(value, str):
        value = value.strip()
    if not isinstance(value, str) or not value or len(value) > 128:
        raise WFError("E_AUTH", "invalid actor identity")
    return value


class WorldActor:
    """Single writer for one world and its subscribed committed deltas."""

    def __init__(self, world_id: str, owner_id: str, config: dict[str, Any], catalog: dict[str, Any], *, branch_id: str = "branch-main", store: SQLiteWebStore | None = None):
        self.world_id = world_id
        self.branch_id = branch_id
        self.owner_id = owner_id
        self.config = deepcopy(config)
        self.catalog = deepcopy(catalog)
        self.engine = Engine(self.config, self.catalog)
        self.memberships: dict[str, str] = {owner_id: "owner"}
        self.commands: dict[tuple[str, str], dict[str, Any]] = {}
        self._next_seq = 0
        self._event_seq = 0
        self.events: list[dict[str, Any]] = []
        self._event_history_limit = 256
        self.queue: list[dict[str, Any]] = []
        self.paused = False
        self.lock = threading.RLock()
        self.store = store
        self.restore_error: WFError | None = None
        self.fixture_blueprints: dict[str, str] = {}
        self.packs: dict[str, dict[str, Any]] = {}
        self._load_fixture_blueprints()
        if self.store is not None:
            self._restore_persisted()

    def _restore_persisted(self) -> None:
        row = self.store.world(self.world_id)
        if row is None:
            return
        try:
            restored_state = strict_loads(str(row["state_json"]).encode("utf-8"))
        except Exception as exc:
            self.restore_error = WFError("E_SNAPSHOT_TAMPERED", "persisted world state is unreadable")
            return
        if digest("state", restored_state) != str(row["state_hash"]):
            self.restore_error = WFError("E_SNAPSHOT_TAMPERED", "persisted world state hash mismatch")
        self.engine.state = restored_state
        self.paused = bool(row["paused"])
        self._next_seq = int(row["next_seq"])
        self._event_seq = int(row["event_seq"])
        self.memberships = self.store.memberships(self.world_id)
        self.memberships.setdefault(self.owner_id, "owner")
        for command in self.store.commands(self.world_id):
            payload = strict_loads(str(command["payload_json"]).encode("utf-8"))
            result = strict_loads(str(command["result_json"]).encode("utf-8")) if command["result_json"] else None
            response = {"command_id": command["command_id"], "world_id": self.world_id, "actor_id": command["actor_id"], "server_seq": int(command["server_seq"]), "effective_tick": int(command["effective_tick"]), "status": command["status"], "result": deepcopy(result)}
            self.commands[(str(command["actor_id"]), str(command["command_id"]))] = {
                "command_id": str(command["command_id"]), "actor_id": str(command["actor_id"]), "kind": payload.get("kind"), "payload": deepcopy(payload.get("payload", {})), "expected_rules_epoch": payload.get("expected_rules_epoch"), "server_seq": int(command["server_seq"]), "effective_tick": int(command["effective_tick"]), "payload_hash": str(command["payload_hash"]), "status": str(command["status"]), "result": deepcopy(result), "response": response,
            }
            if str(command["status"]) == "accepted":
                self.queue.append({"command_id": str(command["command_id"]), "actor_id": str(command["actor_id"]), "kind": payload.get("kind"), "payload": deepcopy(payload.get("payload", {})), "expected_rules_epoch": payload.get("expected_rules_epoch"), "server_seq": int(command["server_seq"]), "effective_tick": int(command["effective_tick"])})
        self.events = self.store.events_since(self.world_id, self.branch_id, max(0, self._event_seq - self._event_history_limit))
        pack_rows = self.store.linked_packs(self.world_id) if hasattr(self.store, "linked_packs") else []
        linked_rows_present = bool(pack_rows)
        # Older databases predate world-specific links; retaining the global
        # fallback lets those rows be read while all new imports are scoped.
        if not pack_rows:
            pack_rows = self.store.packs() if hasattr(self.store, "packs") else []
        for pack_row in pack_rows:
            try:
                imported = import_rule_pack(bytes(pack_row["archive_blob"]), catalog=self.catalog, expected_engine_artifact_hash="engine-v0", expected_policy_hash="policy-v0")
                self.packs[imported.pack_hash] = {"blueprint_hash": imported.blueprint_hash, "manifest": imported.manifest, "archive": bytes(pack_row["archive_blob"]), "created_by": str(pack_row["created_by"])}
                self.engine.register_blueprint(imported.blueprint_hash, imported.blueprint)
            except WFError:
                # A corrupt/unusable pack remains visible to an operator via
                # its durable row but is never made executable after restart.
                if linked_rows_present and self.restore_error is None:
                    self.restore_error = WFError("E_PACK_TAMPERED", "linked rule pack failed validation during restore")
                continue

    def reload_persisted(self) -> None:
        with self.lock:
            if self.store is None:
                return
            self.commands.clear()
            self.queue.clear()
            self.events.clear()
            self._restore_persisted()

    def _record_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        if len(self.events) > self._event_history_limit:
            del self.events[: len(self.events) - self._event_history_limit]

    def _load_fixture_blueprints(self) -> None:
        # Fixture aliases are resolved server-side from a fixed allow-list.  No
        # request can name a filesystem path.
        for path in sorted(_BLUEPRINT_DIR.glob("*.json")):
            try:
                raw = _load_json(path)
                ir = compile_blueprint(raw, self.catalog)
                h = blueprint_hash(ir)
                self.engine.register_blueprint(h, ir)
                self.fixture_blueprints[path.stem] = h
            except (OSError, WFError, ValueError):
                continue

    def add_pack(self, pack_hash: str, imported: Any, archive: bytes, actor_id: str) -> None:
        """Register an immutable validated pack for this world's catalogue."""
        self.packs[pack_hash] = {
            "blueprint_hash": imported.blueprint_hash,
            "manifest": deepcopy(imported.manifest),
            "archive": bytes(archive),
            "created_by": actor_id,
        }
        self.engine.register_blueprint(imported.blueprint_hash, imported.blueprint)
        if self.store is not None:
            self.store.add_pack(
                pack_hash=pack_hash,
                blueprint_hash=imported.blueprint_hash,
                manifest=imported.manifest,
                archive=archive,
                created_by=actor_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self.store.link_pack(self.world_id, pack_hash)

    def uninstall_pack(self, pack_hash: str) -> None:
        found = self.packs.get(pack_hash)
        if found is None:
            raise WFError("E_PACK_NOT_FOUND", "rule pack does not exist in this world")
        blueprint_hash_value = str(found["blueprint_hash"])
        queued_or_executed = any(record.get("kind") == "INSTALL_PACK" and record.get("payload", {}).get("pack_hash") == pack_hash and record.get("status") in {"accepted", "executed"} for record in self.commands.values())
        if queued_or_executed or blueprint_hash_value in set(self.engine.state.get("installed_blueprint_hashes", [])) or any(str(entity.get("blueprint_hash")) == blueprint_hash_value for entity in self.engine.state.get("entities", {}).values()):
            raise WFError("E_PACK_IN_USE", "cannot uninstall a pack used by an installed blueprint or entity")
        self.packs.pop(pack_hash, None)
        self.engine.blueprints.pop(blueprint_hash_value, None)
        if self.store is not None:
            self.store.disable_pack(self.world_id, pack_hash)

    def role(self, actor_id: str) -> str | None:
        role = self.memberships.get(actor_id)
        return role if role else None

    def require_member(self, actor_id: str) -> str:
        role = self.role(actor_id)
        if role is None:
            raise WFError("E_FORBIDDEN", "actor is not a member of this world")
        return role

    def revoke(self, actor_id: str) -> None:
        if not isinstance(actor_id, str) or not actor_id or actor_id == self.owner_id:
            raise WFError("E_ROLE", "owner membership cannot be revoked")
        with self.lock:
            self.memberships.pop(actor_id, None)
            if self.store is not None:
                self.store.update_membership(self.world_id, actor_id, None)

    def grant(self, actor_id: str, role: str) -> None:
        if not isinstance(actor_id, str) or not actor_id or len(actor_id) > 128:
            raise WFError("E_SCHEMA", "actor_id must be a non-empty string")
        if role not in _ROLE_RANK or role == "owner":
            raise WFError("E_ROLE", "role must be inventor, builder, or viewer")
        with self.lock:
            self.memberships[actor_id] = role
            if self.store is not None:
                self.store.update_membership(self.world_id, actor_id, role)

    def metadata(self, actor_id: str) -> dict[str, Any]:
        role = self.require_member(actor_id)
        return {
            "world_id": self.world_id,
            "branch_id": self.branch_id,
            "mode": "fixture",
            "owner_id": self.owner_id,
            "role": role,
            "rules_epoch": int(self.engine.state.get("rules_epoch", 1)),
            "tick": int(self.engine.state.get("tick", 0)),
            "paused": self.paused,
            "limits": deepcopy(self.config.get("limits", {})),
            "members": {k: v for k, v in sorted(self.memberships.items())},
            "available_blueprints": dict(sorted(self.fixture_blueprints.items())),
            "available_packs": {h: {"blueprint_hash": value["blueprint_hash"], "manifest": deepcopy(value["manifest"])} for h, value in sorted(self.packs.items())},
        }

    def snapshot(self, actor_id: str) -> dict[str, Any]:
        self.require_member(actor_id)
        with self.lock:
            if self.restore_error is not None:
                raise self.restore_error
            state = self.engine.snapshot()
            return {
                "world_id": self.world_id,
                "branch_id": self.branch_id,
                "last_seq": self._event_seq,
                "tick": int(state.get("tick", 0)),
                "state_hash": self.engine.state_hash(),
                "state": state,
            }

    def _check_command_role(self, actor_id: str, kind: str) -> str:
        role = self.require_member(actor_id)
        required = _COMMAND_ROLES.get(kind)
        if required is None:
            raise WFError("E_UNKNOWN_COMMAND", f"unsupported command {kind}")
        if _ROLE_RANK[role] < _ROLE_RANK[required]:
            raise WFError("E_FORBIDDEN", f"role {role} cannot execute {kind}")
        return role

    def submit(self, actor_id: str, command_id: str, kind: str, payload: dict[str, Any], expected_rules_epoch: int | None = None) -> dict[str, Any]:
        if not isinstance(command_id, str) or not command_id or len(command_id) > 128:
            raise WFError("E_SCHEMA", "command_id must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise WFError("E_SCHEMA", "kind must be a non-empty string")
        if not isinstance(payload, dict):
            raise WFError("E_SCHEMA", "payload must be an object")
        try:
            check_canonical_value(payload, path="/payload")
            if len(canonical_bytes(payload)) > 64 * 1024:
                raise WFError("E_LIMIT", "command payload exceeds 64KiB")
        except WFError:
            raise
        except Exception as exc:
            raise WFError("E_SCHEMA", "payload is not canonical JSON") from exc
        if expected_rules_epoch is not None and type(expected_rules_epoch) is not int:
            raise WFError("E_NUMERIC_TYPE", "expected_rules_epoch must be an integer")
        # Fixture aliases are CLI-only syntax.  The HTTP API accepts resolved
        # content hashes and entity IDs, never a path or alias to server files.
        if "blueprint_alias" in payload or "entity_alias" in payload or "instance_alias" in payload or "path" in payload or "blueprint_path" in payload:
            raise WFError("E_SCHEMA", "fixture aliases are not accepted by the production API")
        if "actor_id" in payload:
            raise WFError("E_SCHEMA", "actor identity must come from the authenticated transport")
        if kind == "INSTALL_PACK":
            pack_hash = payload.get("pack_hash")
            if not isinstance(pack_hash, str) or pack_hash not in self.packs:
                raise WFError("E_PACK_NOT_FOUND", "pack must be imported and validated before installation")
            expected_blueprint = str(self.packs[pack_hash]["blueprint_hash"])
            supplied_blueprint = payload.get("blueprint_hash")
            if supplied_blueprint is not None and supplied_blueprint != expected_blueprint:
                raise WFError("E_PACK_HASH", "pack blueprint hash does not match payload")
            payload = deepcopy(payload)
            payload["blueprint_hash"] = expected_blueprint
        with self.lock:
            self._check_command_role(actor_id, kind)
            old_paused = self.paused
            command_payload = {"kind": kind, "payload": payload, "expected_rules_epoch": expected_rules_epoch}
            payload_hash = digest("command", command_payload)
            key = (actor_id, command_id)
            old = self.commands.get(key)
            if old is not None:
                if old["payload_hash"] != payload_hash:
                    raise WFError("E_COMMAND_ID_CONFLICT", "command_id was already used with different payload")
                return deepcopy(old["response"])
            if kind in ("INSTALL_BLUEPRINT", "INSTALL_VALIDATED_BLUEPRINT", "INSTALL_PACK") and expected_rules_epoch is None:
                raise WFError("E_SCHEMA", "expected_rules_epoch is required for install")
            self._next_seq += 1
            current_tick = int(self.engine.state.get("tick", 0))
            # Control commands are applied at the current boundary so a
            # resume command cannot be stranded behind a paused future tick.
            effective_tick = current_tick if kind == "SET_PAUSED" else current_tick + 1
            record: dict[str, Any] = {
                "command_id": command_id,
                "actor_id": actor_id,
                "kind": kind,
                "payload": deepcopy(payload),
                "server_seq": self._next_seq,
                "effective_tick": effective_tick,
                "payload_hash": payload_hash,
                "status": "accepted",
                "result": None,
                "expected_rules_epoch": expected_rules_epoch,
            }
            if kind != "SET_PAUSED" and len(self.queue) >= 1000:
                # Persist a deterministic rejection under the idempotency key;
                # callers may safely retry/query after the queue drains.
                record["status"] = "rejected"
                record["result"] = {"code": "E_LIMIT", "message": "world command queue is full"}
                response = {
                    "command_id": command_id,
                    "world_id": self.world_id,
                    "actor_id": actor_id,
                    "server_seq": self._next_seq,
                    "effective_tick": effective_tick,
                    "status": record["status"],
                    "result": deepcopy(record["result"]),
                }
                record["response"] = response
                self.commands[key] = record
                return deepcopy(response)
            # SET_PAUSED is a control-plane operation: it must remain usable
            # while paused and must not advance simulation time.
            if kind == "SET_PAUSED":
                self.paused = bool(payload.get("paused"))
                record["status"] = "executed"
                record["result"] = {"ok": True, "paused": self.paused}
                self._event_seq += 1
                self._record_event({
                    "type": "world_delta",
                    "world_id": self.world_id,
                    "branch_id": self.branch_id,
                    "event_seq": self._event_seq,
                    "tick": current_tick,
                    "state_hash": self.engine.state_hash(),
                    "changes": {"paused": self.paused},
                    "events": [{"type": "world_paused" if self.paused else "world_resumed"}],
                })
            elif self.paused:
                record["status"] = "rejected"
                record["result"] = {"error": {"code": "E_PAUSED", "message": "world is paused"}}
            else:
                envelope = {"command_id": command_id, "actor_id": actor_id, "kind": kind, "payload": deepcopy(payload), "server_seq": self._next_seq, "effective_tick": effective_tick}
                if expected_rules_epoch is not None:
                    envelope["expected_rules_epoch"] = expected_rules_epoch
                self.queue.append(envelope)
            response = {
                "command_id": command_id,
                "world_id": self.world_id,
                "actor_id": actor_id,
                "server_seq": self._next_seq,
                "effective_tick": effective_tick,
                "status": record["status"],
                "result": deepcopy(record["result"]),
            }
            record["response"] = response
            self.commands[key] = record
            if self.store is not None:
                try:
                    if kind == "SET_PAUSED":
                        self.store.control_commit(world_id=self.world_id, actor_id=actor_id, command=record, paused=self.paused, event=self.events[-1], event_seq=self._event_seq)
                    else:
                        self.store.insert_command(self.world_id, record)
                except Exception:
                    self.commands.pop(key, None)
                    self.queue = [item for item in self.queue if not (item.get("actor_id") == actor_id and item.get("command_id") == command_id)]
                    self._next_seq -= 1
                    if kind == "SET_PAUSED":
                        self.paused = old_paused
                        self._event_seq -= 1
                        if self.events:
                            self.events.pop()
                    raise
            return deepcopy(response)

    def step(self) -> dict[str, Any]:
        """Close the current command batch and advance one authoritative tick."""
        with self.lock:
            if self.paused:
                return {"status": "paused", "tick": self.engine.state.get("tick", 0), "state_hash": self.engine.state_hash(), "events": []}
            old_state = deepcopy(self.engine.state)
            old_frozen = self.engine.frozen
            old_queue = deepcopy(self.queue)
            old_commands = deepcopy(self.commands)
            old_events = deepcopy(self.events)
            old_event_seq = self._event_seq
            batch, self.queue = self.queue, []
            executable = []
            for envelope in batch:
                record = self.commands.get((envelope["actor_id"], envelope["command_id"]))
                try:
                    allowed = self.role(envelope["actor_id"]) is not None and self._check_command_role(envelope["actor_id"], envelope["kind"]) is not None
                except WFError:
                    allowed = False
                if not allowed:
                    if record:
                        record["status"] = "rejected"; record["result"] = {"code": "E_FORBIDDEN", "message": "membership revoked before execution"}
                        record["response"]["status"] = "rejected"; record["response"]["result"] = deepcopy(record["result"])
                else:
                    executable.append(envelope)
            try:
                result = self.engine.step(executable)
            except Exception:
                self.engine.state = old_state
                self.engine.frozen = self.engine.frozen or old_frozen
                self.queue = old_queue
                self.commands = old_commands
                self.events = old_events
                self._event_seq = old_event_seq
                raise
            by_id = {x.get("command_id"): x for x in result.command_results}
            for envelope in executable:
                record = self.commands.get((envelope["actor_id"], envelope["command_id"])); cr = by_id.get(envelope["command_id"], {})
                if record is not None:
                    record["status"] = str(cr.get("status", "executed")); record["result"] = cr.get("result") if record["status"] == "executed" else cr.get("error")
                    record["response"]["status"] = record["status"]
                    record["response"]["result"] = deepcopy(record["result"])
            self._event_seq += 1
            delta = {"type": "world_delta", "world_id": self.world_id, "branch_id": self.branch_id, "event_seq": self._event_seq, "tick": result.tick, "state_hash": result.state_hash, "changes": result.summary, "events": result.events}
            self._record_event(delta)
            if self.store is not None:
                try:
                    previous_row = self.store.latest_commit(self.world_id, self.branch_id)
                    previous = str(previous_row["log_head"]) if previous_row is not None else str(self.store.world(self.world_id)["genesis_hash"])
                    command_records = []
                    for rec in sorted(self.commands.values(), key=lambda x: int(x["server_seq"])):
                        if int(rec["effective_tick"]) == int(result.tick) and rec.get("kind") != "SET_PAUSED":
                            command_records.append({"actor_id": rec["actor_id"], "command_id": rec["command_id"], "kind": rec["kind"], "payload": rec["payload"], "server_seq": rec["server_seq"], "effective_tick": rec["effective_tick"]})
                    payloads = [{"tick": result.tick, "ordinal": 0, "event": delta}]
                    log_head = digest("commit", {"previous_head": previous, "tick": result.tick, "commands": command_records, "events": payloads, "state_hash": result.state_hash})
                    updates = [self.commands[(env["actor_id"], env["command_id"])] for env in batch if (env["actor_id"], env["command_id"]) in self.commands]
                    self.store.commit_tick(world_id=self.world_id, branch_id=self.branch_id, tick=result.tick, state=self.engine.snapshot(), state_hash=result.state_hash, paused=self.paused, next_seq=self._next_seq, event_seq=self._event_seq, result=result.as_dict(), updates=updates, events=[delta], previous_head=previous, log_head=log_head)
                except Exception:
                    self.engine.state = old_state
                    self.queue = old_queue
                    self.commands = old_commands
                    self.events = old_events
                    self._event_seq = old_event_seq
                    raise
            return result.as_dict()

    def command(self, actor_id: str, command_id: str) -> dict[str, Any]:
        with self.lock:
            role = self.require_member(actor_id)
            found = None
            for (submitter, cid), record in self.commands.items():
                if cid == command_id:
                    if submitter != actor_id and role != "owner":
                        raise WFError("E_FORBIDDEN", "only the submitter or owner may inspect a command")
                    found = record
                    break
            if found is None:
                raise WFError("E_NOT_FOUND", "command does not exist")
            return deepcopy(found["response"])

    def deltas_since(self, actor_id: str, last_seq: int) -> list[dict[str, Any]]:
        self.require_member(actor_id)
        with self.lock:
            if last_seq < 0:
                raise WFError("E_SCHEMA", "last_seq must be non-negative")
            if self.events and last_seq < self.events[0]["event_seq"] - 1:
                return [{"type": "resync_required", "world_id": self.world_id, "last_seq": self._event_seq}]
            return deepcopy([event for event in self.events if event["event_seq"] > last_seq])


class WorldService:
    """Registry and lifecycle for world actors (one actor is one writer)."""

    def __init__(self, *, catalog: Mapping[str, Any] | None = None, store: SQLiteWebStore | None = None, db_path: str | Path | None = None):
        self.catalog = deepcopy(dict(catalog)) if catalog is not None else _load_json(_CATALOG_PATH)
        self.worlds: dict[str, WorldActor] = {}
        self.lock = threading.RLock()
        self.store = store or (SQLiteWebStore(db_path) if db_path is not None else None)
        if self.store is not None:
            for row in self.store.world_rows():
                try:
                    config = strict_loads(str(row["config_json"]).encode("utf-8"))
                    actor = WorldActor(str(row["world_id"]), str(row["owner_id"]), config, self.catalog, branch_id=str(row["branch_id"]), store=self.store)
                    self.worlds[actor.world_id] = actor
                except Exception:
                    continue

    def create_world(self, actor_id: str, scenario_id: str, limits: Mapping[str, Any] | None = None) -> WorldActor:
        if not isinstance(scenario_id, str) or scenario_id not in _SCENARIOS:
            raise WFError("E_UNKNOWN_SCENARIO", "scenario_id must name a built-in fixture")
        config = _load_json(_SCENARIOS[scenario_id])
        if limits is not None:
            if not isinstance(limits, Mapping):
                raise WFError("E_SCHEMA", "limits must be an object")
            check_canonical_value(dict(limits), path="/limits")
            config["limits"] = deepcopy(dict(limits))
        world_id = "world-" + uuid.uuid4().hex
        actor = WorldActor(world_id, actor_id, config, self.catalog, store=self.store)
        if self.store is not None:
            state = actor.engine.snapshot()
            config_for_hash = deepcopy(config)
            config_for_hash["_state_hash_at_tick_0"] = actor.engine.state_hash()
            genesis = digest("genesis", {"config_hash": digest("config", config), "state_hash_at_tick_0": actor.engine.state_hash()})
            self.store.create_world(world_id=world_id, branch_id=actor.branch_id, owner_id=actor_id, config=config_for_hash, state=state, state_hash=actor.engine.state_hash(), genesis_hash=genesis, created_at=datetime.now(timezone.utc).isoformat())
        with self.lock:
            self.worlds[world_id] = actor
        return actor

    def get(self, world_id: str) -> WorldActor:
        with self.lock:
            actor = self.worlds.get(world_id)
        if actor is None:
            raise WFError("E_NOT_FOUND", "world does not exist")
        return actor

    def fork_world(self, source_world_id: str, actor_id: str, checkpoint_tick: int | None = None) -> WorldActor:
        """Create an independent world branch from a committed snapshot."""
        source = self.get(source_world_id)
        if source.role(actor_id) != "owner":
            raise WFError("E_FORBIDDEN", "owner required to fork a world")
        current = int(source.engine.state.get("tick", 0))
        if checkpoint_tick is not None and type(checkpoint_tick) is not int:
            raise WFError("E_NUMERIC_TYPE", "checkpoint tick must be an integer")
        if checkpoint_tick is not None and checkpoint_tick != current:
            raise WFError("E_CHECKPOINT_TICK", "fork must bind to the latest committed tick")
        if self.store is None:
            # Memory mode remains useful for unit tests; durable forks are the
            # production path and preserve all committed journal rows.
            new_id = "world-" + uuid.uuid4().hex
            branch = "branch-fork-" + digest("fork", {"source_world_id": source_world_id, "tick": current})[:16]
            actor = WorldActor(new_id, actor_id, source.config, self.catalog, branch_id=branch, store=None)
            actor.engine.state = source.engine.snapshot()
            actor.engine.blueprints.update(deepcopy(source.engine.blueprints))
            actor.packs = deepcopy(source.packs)
            actor.memberships = deepcopy(source.memberships)
            actor.memberships[actor_id] = "owner"
            with self.lock:
                self.worlds[new_id] = actor
            return actor
        new_id = "world-" + uuid.uuid4().hex
        branch = "branch-fork-" + digest("fork", {"source_world_id": source_world_id, "tick": current, "new_world_id": new_id})[:16]
        self.store.fork_world(source_world_id, new_id, branch, actor_id, datetime.now(timezone.utc).isoformat())
        actor = WorldActor(new_id, actor_id, source.config, self.catalog, branch_id=branch, store=self.store)
        with self.lock:
            self.worlds[new_id] = actor
        return actor

    def close(self) -> None:
        if self.store is not None:
            self.store.close()


class PersistentWorldService(WorldService):
    """Durable service used by the real ASGI entry point.

    Existing rows are loaded at startup and resume exactly at their committed
    tick.  No wall-clock catch-up is performed.
    """

    def __init__(self, path: str | Path | None = None, *, catalog: Mapping[str, Any] | None = None):
        db_path = path or os.environ.get("WORLDFORGE_DB", str(_ROOT / "work" / "worldforge.sqlite3"))
        self.db_path = str(db_path)
        super().__init__(catalog=catalog, store=SQLiteWebStore(self.db_path))

SQLiteWorldService = PersistentWorldService


def _error_response(exc: WFError, status: int | None = None) -> JSONResponse:
    code_status = status or ({"E_AUTH": 401, "E_FORBIDDEN": 403, "E_NOT_FOUND": 404, "E_COMMAND_ID_CONFLICT": 409, "E_COMMAND_CONFLICT": 409, "E_LIMIT": 429}.get(exc.code, 400))
    return JSONResponse(status_code=code_status, content={"error": {**exc.as_dict(), "request_id": _request_id(), "retryable": False}})


def create_app(service: WorldService | None = None):
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError("FastAPI is required; install worldforge[web]") from _FASTAPI_IMPORT_ERROR
    app = FastAPI(title="WorldForge", version="0.1.0")
    configured_origins = [origin.strip() for origin in os.environ.get("WORLDFORGE_UI_ORIGINS", "").split(",") if origin.strip()]
    allowed_origins = configured_origins or [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]
    allowed_origin_hosts = {urlparse(origin).netloc for origin in allowed_origins}
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Accept", "Content-Type", "Authorization", "X-Actor-ID", "X-CSRF-Token"],
    )
    app.state.world_service = service or WorldService()
    # A service explicitly injected by tests may use the legacy actor header.
    # The module-level ASGI app injects PersistentWorldService and therefore
    # requires the opaque local session cookie by default.
    app.state.dev_headers = (service is None or service.store is None or os.environ.get("WORLDFORGE_DEV_MODE") == "1")
    app.state.memory_sessions: dict[str, dict[str, str | bool]] = {}
    app.state.invention_tasks: dict[str, dict[str, Any]] = {}
    if app.state.world_service.store is not None and hasattr(app.state.world_service.store, "inventions"):
        for row in app.state.world_service.store.inventions():
            try:
                app.state.invention_tasks[str(row["task_id"])] = {
                    "task_id": str(row["task_id"]), "actor_id": str(row["actor_id"]), "world_id": str(row["world_id"]),
                    "status": str(row["status"]), "mode": str(row["mode"]),
                    "request": strict_loads(str(row["request_json"]).encode("utf-8")),
                    "result": strict_loads(str(row["result_json"]).encode("utf-8")),
                }
            except Exception:
                # A malformed task is never exposed as executable content.
                continue

    def session_row(sid: str | None):
        if not sid:
            return None
        if app.state.world_service.store is not None:
            return app.state.world_service.store.session(sid)
        return app.state.memory_sessions.get(sid)

    def resolve_identity(request: Request, x_actor_id: str | None, authorization: str | None, *, mutate: bool = False, body: Mapping[str, Any] | None = None) -> str:
        sid = request.cookies.get("wf_session")
        row = session_row(sid)
        if row is not None:
            actor_id = str(row["actor_id"] if isinstance(row, sqlite3.Row) else row["actor_id"])
            if mutate:
                token = request.headers.get("x-csrf-token")
                expected = str(row["csrf_token"] if isinstance(row, sqlite3.Row) else row["csrf_token"])
                if not token or not secrets.compare_digest(token, expected):
                    raise WFError("E_CSRF", "missing or invalid CSRF token")
            return actor_id
        if sid:
            raise WFError("E_AUTH", "session is expired or revoked")
        if app.state.dev_headers:
            return _actor_id(x_actor_id, body, authorization)
        raise WFError("E_AUTH", "an authenticated local session is required")

    @app.exception_handler(WFError)
    async def handle_wf_error(_request: Request, exc: WFError):
        return _error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(_request: Request, exc: RequestValidationError):
        path = ""
        if exc.errors():
            location = exc.errors()[0].get("loc", ())
            path = "".join(f"/{part}" for part in location if part != "body")
        return _error_response(WFError("E_SCHEMA", "request body does not match the API schema", path), 400)

    @app.post("/api/sessions")
    async def create_session(body: dict[str, Any], response: Response):
        actor_id = body.get("actor_id")
        if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 128:
            return _error_response(WFError("E_SCHEMA", "actor_id must be a non-empty string"))
        actor_id = actor_id.strip()
        # The local bootstrap identity may create the first session. Once a
        # world exists, additional sessions must be invited members.
        worlds = list(app.state.world_service.worlds.values())
        if worlds and not any(w.role(actor_id) is not None for w in worlds):
            return _error_response(WFError("E_FORBIDDEN", "actor has not been invited to a world"))
        sid = "s_" + secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc).isoformat()
        row = {"actor_id": actor_id, "csrf_token": csrf}
        if app.state.world_service.store is not None:
            app.state.world_service.store.add_session(sid, actor_id, csrf, now)
        else:
            app.state.memory_sessions[sid] = row
        response.set_cookie("wf_session", sid, httponly=True, samesite="lax", secure=False, path="/")
        return {"actor_id": actor_id, "csrf_token": csrf, "session_id": sid, "mode": "local"}

    @app.get("/api/sessions/me")
    async def session_me(request: Request):
        row = session_row(request.cookies.get("wf_session"))
        if row is None:
            return _error_response(WFError("E_AUTH", "session is expired or revoked"))
        actor_id = str(row["actor_id"] if isinstance(row, sqlite3.Row) else row["actor_id"])
        return {"actor_id": actor_id, "mode": "local", "authenticated": True}

    @app.post("/api/sessions/logout")
    @app.post("/api/sessions/revoke")
    async def revoke_session(request: Request, response: Response, body: dict[str, Any] | None = None):
        sid = request.cookies.get("wf_session")
        row = session_row(sid)
        if row is None:
            return _error_response(WFError("E_AUTH", "session is expired or revoked"))
        token = request.headers.get("x-csrf-token")
        expected = str(row["csrf_token"] if isinstance(row, sqlite3.Row) else row["csrf_token"])
        if not token or not secrets.compare_digest(token, expected):
            return _error_response(WFError("E_CSRF", "missing or invalid CSRF token"))
        current_actor = str(row["actor_id"] if isinstance(row, sqlite3.Row) else row["actor_id"])
        target_actor = str((body or {}).get("actor_id") or current_actor)
        if target_actor != current_actor and not any(world.role(current_actor) == "owner" for world in app.state.world_service.worlds.values()):
            return _error_response(WFError("E_FORBIDDEN", "owner required to revoke another actor session"))
        if app.state.world_service.store is not None:
            if target_actor == current_actor:
                app.state.world_service.store.revoke_session(str(sid))
            else:
                app.state.world_service.store.revoke_actor_sessions(target_actor)
        else:
            for session_key, session_value in list(app.state.memory_sessions.items()):
                if str(session_value.get("actor_id")) == target_actor:
                    app.state.memory_sessions.pop(session_key, None)
        if target_actor == current_actor:
            response.delete_cookie("wf_session", path="/")
        return {"ok": True, "actor_id": target_actor}

    @app.get("/health")
    async def health():
        return {"status": "ok", "mode": "offline", "fixture_mode": True, "version": "0.1.0"}

    @app.post("/api/worlds", status_code=201)
    async def create_world(request: Request, body: dict[str, Any], x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True, body=body)
            scenario_id = body.get("scenario_id")
            actor = app.state.world_service.create_world(actor_id, scenario_id, body.get("limits"))
            return actor.metadata(actor_id)
        except WFError as exc:
            return _error_response(exc)

    @app.get("/api/packs/{pack_hash}")
    async def get_pack(request: Request, pack_hash: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization)
            found = None
            for world in app.state.world_service.worlds.values():
                if world.role(actor_id) is not None and pack_hash in world.packs:
                    found = world.packs[pack_hash]
                    break
            if found is None:
                raise WFError("E_NOT_FOUND", "rule pack does not exist or is not visible")
            return {"pack_hash": pack_hash, "blueprint_hash": found["blueprint_hash"], "manifest": deepcopy(found["manifest"])}
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/import", status_code=201)
    async def import_world(request: Request, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True)
            content_type = (request.headers.get("content-type") or "").lower()
            body: dict[str, Any] = {}
            raw = await request.body()
            if "application/zip" in content_type or "application/octet-stream" in content_type:
                archive = raw
            else:
                if len(raw) > MAX_PACK_BYTES * 2:
                    raise WFError("E_PACK_LIMIT", "JSON import envelope exceeds the archive limit")
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                    check_canonical_value(body)
                except (UnicodeDecodeError, json.JSONDecodeError, WFError) as exc:
                    if isinstance(exc, WFError):
                        raise
                    raise WFError("E_PACK_SCHEMA", "import body is not valid JSON") from exc
                if not isinstance(body, dict):
                    raise WFError("E_PACK_SCHEMA", "import body must be an object")
                encoded = body.get("archive_base64", body.get("pack_base64"))
                if isinstance(encoded, str):
                    try:
                        archive = base64.b64decode(encoded.encode("ascii"), validate=True)
                    except Exception as exc:
                        raise WFError("E_PACK_ARCHIVE", "archive_base64 is invalid") from exc
                elif isinstance(body.get("archive"), list) and all(type(x) is int and 0 <= x <= 255 for x in body["archive"]):
                    archive = bytes(body["archive"])
                else:
                    raise WFError("E_PACK_SCHEMA", "import requires archive_base64 or application/zip")
            catalog_hash = digest("catalog", app.state.world_service.catalog)
            imported = import_rule_pack(archive, catalog=app.state.world_service.catalog, expected_engine_artifact_hash="engine-v0", expected_catalog_hash=catalog_hash, expected_policy_hash="policy-v0")
            actor = app.state.world_service.create_world(actor_id, str(body.get("scenario_id", "river_demo")), body.get("limits"))
            actor.add_pack(imported.pack_hash, imported, archive, actor_id)
            return {"world_id": actor.world_id, "branch_id": actor.branch_id, "pack_hash": imported.pack_hash, "blueprint_hash": imported.blueprint_hash, "metadata": actor.metadata(actor_id)}
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/fork", status_code=201)
    async def fork_world(request: Request, world_id: str, body: dict[str, Any] | None = None, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True, body=body or {})
            source = app.state.world_service.get(world_id)
            requested = (body or {}).get("checkpoint_id", (body or {}).get("tick"))
            tick = None
            if requested is not None:
                if isinstance(requested, str) and requested.count(":") >= 2:
                    try:
                        tick = int(requested.rsplit(":", 1)[-1])
                    except ValueError as exc:
                        raise WFError("E_CHECKPOINT_TICK", "checkpoint_id is invalid") from exc
                elif type(requested) is int:
                    tick = requested
                else:
                    raise WFError("E_CHECKPOINT_TICK", "fork checkpoint must be an integer or checkpoint_id")
            forked = app.state.world_service.fork_world(world_id, actor_id, tick)
            return {"world_id": forked.world_id, "branch_id": forked.branch_id, "parent_world_id": source.world_id, "parent_branch_id": source.branch_id, "fork_tick": int(forked.engine.state.get("tick", 0)), "state_hash": forked.engine.state_hash(), "metadata": forked.metadata(actor_id)}
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/exports")
    async def export_world(request: Request, world_id: str, body: dict[str, Any] | None = None, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=False, body=body or {})
            actor = app.state.world_service.get(world_id)
            if actor.role(actor_id) != "owner":
                raise WFError("E_FORBIDDEN", "owner required to export a world")
            mode = str((body or {}).get("mode", (body or {}).get("kind", "blueprints_only")))
            if mode not in {"blueprints_only", "rule_pack"}:
                raise WFError("E_EXPORT_MODE", "only blueprints_only/rule_pack exports are supported")
            pack_hash = (body or {}).get("pack_hash")
            if pack_hash is not None:
                if not isinstance(pack_hash, str) or pack_hash not in actor.packs:
                    raise WFError("E_PACK_NOT_FOUND", "requested pack is not installed/imported")
                archive = bytes(actor.packs[pack_hash]["archive"])
            else:
                installed = list(actor.engine.state.get("installed_blueprint_hashes", []))
                if not installed:
                    raise WFError("E_PACK_NOT_FOUND", "no installed blueprint is available for export")
                blueprint_hash_value = str(installed[0])
                blueprint = actor.engine.blueprints.get(blueprint_hash_value)
                if blueprint is None:
                    raise WFError("E_PACK_NOT_FOUND", "installed blueprint content is unavailable")
                archive = make_rule_pack(blueprint, catalog=actor.catalog, engine_artifact_hash="engine-v0", policy_hash="policy-v0").archive
            return Response(content=archive, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="worldforge-{world_id}-{mode}.wfpack"'})
        except WFError as exc:
            return _error_response(exc)

    @app.delete("/api/worlds/{world_id}/packs/{pack_hash}")
    async def uninstall_world_pack(request: Request, world_id: str, pack_hash: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True)
            actor = app.state.world_service.get(world_id)
            if actor.role(actor_id) != "owner":
                raise WFError("E_FORBIDDEN", "owner required to uninstall a pack")
            actor.uninstall_pack(pack_hash)
            return {"ok": True, "world_id": world_id, "pack_hash": pack_hash}
        except WFError as exc:
            return _error_response(exc)

    @app.get("/api/worlds/{world_id}")
    async def world_metadata(request: Request, world_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization)
            return app.state.world_service.get(world_id).metadata(actor_id)
        except WFError as exc:
            return _error_response(exc)

    @app.get("/api/worlds/{world_id}/snapshot")
    async def world_snapshot(request: Request, world_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization)
            return app.state.world_service.get(world_id).snapshot(actor_id)
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/inventions")
    async def create_invention(request: Request, world_id: str, body: dict[str, Any], x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        """Run the bounded local invention pipeline without blocking the actor."""
        try:
            if set(body) - {"prompt", "mode", "parent_hash", "scenario_id", "candidate"}:
                raise WFError("E_SCHEMA", "unknown invention request field")
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True, body=body)
            actor = app.state.world_service.get(world_id)
            role = actor.role(actor_id)
            if role not in {"owner", "inventor"}:
                raise WFError("E_FORBIDDEN", "inventor role required")
            prompt = body.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise WFError("E_MODEL_INPUT", "prompt must be non-empty")
            mode = body.get("mode", "fixture")
            if mode == "fixture":
                scenario_id = body.get("scenario_id")
                fixtures: dict[str, Any] = {}
                if isinstance(scenario_id, str):
                    candidate_path = _BLUEPRINT_DIR / f"{scenario_id}.json"
                    if candidate_path.exists():
                        fixtures[scenario_id] = _load_json(candidate_path)
                provider = FixtureProvider(fixtures)
            elif mode == "manual":
                candidate = body.get("candidate")
                if not isinstance(candidate, Mapping):
                    raise WFError("E_SCHEMA", "manual mode requires candidate object")
                provider = ManualProvider(candidate)
            elif mode == "remote":
                provider = RemoteProvider(model_id="configured-remote", endpoint="", enabled=False)
            else:
                raise WFError("E_SCHEMA", "mode must be fixture, manual, or remote")
            task_id = "inv-" + uuid.uuid4().hex
            result = InventionCompiler(provider, actor.catalog).compile(InventionRequest(prompt=prompt, context={"scenario_id": body.get("scenario_id")} if body.get("scenario_id") is not None else {}, parent_hash=body.get("parent_hash"), request_id=task_id))
            task = {"task_id": task_id, "actor_id": actor_id, "world_id": world_id, "status": result.outcome, "mode": mode, "request": {"prompt": prompt, "parent_hash": body.get("parent_hash")}, "result": result.as_dict()}
            app.state.invention_tasks[task_id] = task
            if app.state.world_service.store is not None and hasattr(app.state.world_service.store, "add_invention"):
                app.state.world_service.store.add_invention(task, datetime.now(timezone.utc).isoformat())
            return {"task_id": task_id, "status": result.outcome, "candidate_hash": result.candidate_hash, "report_hash": result.report.get("report_hash") if result.report else None, "diagnostics": list(result.diagnostics), "attempts": len(result.attempts)}
        except WFError as exc:
            return _error_response(exc)

    @app.get("/api/inventions/{task_id}")
    async def get_invention(request: Request, task_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization)
            task = app.state.invention_tasks.get(task_id)
            if task is None:
                raise WFError("E_NOT_FOUND", "invention task does not exist")
            world = app.state.world_service.get(str(task["world_id"]))
            if actor_id != task["actor_id"] and world.role(actor_id) != "owner":
                raise WFError("E_FORBIDDEN", "task is private")
            return deepcopy(task)
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/inventions/{task_id}/approve")
    async def approve_invention(request: Request, task_id: str, body: dict[str, Any], x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            if set(body) - {"candidate_hash", "report_hash", "expected_rules_epoch"}:
                raise WFError("E_SCHEMA", "unknown approval field")
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True, body=body)
            task = app.state.invention_tasks.get(task_id)
            if task is None:
                raise WFError("E_NOT_FOUND", "invention task does not exist")
            world = app.state.world_service.get(str(task["world_id"]))
            if world.role(actor_id) != "owner":
                raise WFError("E_FORBIDDEN", "owner required")
            result = task["result"]
            if result.get("outcome") != "validated" or body.get("candidate_hash") != result.get("candidate_hash") or body.get("report_hash") != (result.get("report") or {}).get("report_hash"):
                raise WFError("E_REPORT_BINDING", "candidate and report hashes do not match")
            check_report(result["candidate"], result["report"], world.catalog)
            task["status"] = "approved"
            if app.state.world_service.store is not None and hasattr(app.state.world_service.store, "update_invention_status"):
                app.state.world_service.store.update_invention_status(task_id, "approved")
            return {"task_id": task_id, "status": "approved", "candidate_hash": result["candidate_hash"], "report_hash": result["report"]["report_hash"]}
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/publications/prepare")
    async def prepare_publication(request: Request, body: dict[str, Any], x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        """Create unsigned local-chain registration parameters only."""
        try:
            if set(body) - {"pack_hash", "chain_id", "contract", "parent_ids", "license_hash", "content_uri"}:
                raise WFError("E_SCHEMA", "unknown publication field")
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True, body=body)
            pack_hash = body.get("pack_hash")
            if not isinstance(pack_hash, str):
                raise WFError("E_SCHEMA", "pack_hash is required")
            pack_lookup = pack_hash.lower().removeprefix("0x")
            service = app.state.world_service
            if service.store is not None:
                if service.store.pack(pack_lookup) is None:
                    raise WFError("E_PACK_NOT_FOUND", "pack must be imported before publication")
            elif not any(pack_lookup in world.packs for world in service.worlds.values()):
                raise WFError("E_PACK_NOT_FOUND", "pack must be imported before publication")
            chain = LocalDevelopmentChain()
            chain_id = body.get("chain_id", chain.chain_id)
            if type(chain_id) is not int or chain_id != chain.chain_id:
                raise WFError("E_CHAIN_ID", "only the explicit local development chain is available")
            contract = body.get("contract", chain.registry.contract_address)
            if contract != chain.registry.contract_address:
                raise WFError("E_CHAIN_CONTRACT", "contract is not in the local allow-list")
            params = chain.prepare_register(registrant="0x" + digest("registrant", {"actor_id": actor_id})[-40:], pack_hash="0x" + pack_lookup, parent_ids=body.get("parent_ids", []), license_hash=body.get("license_hash", ""), content_uri=body.get("content_uri", ""))
            intent_id = "pub-" + uuid.uuid4().hex
            if service.store is not None and hasattr(service.store, "add_publication_intent"):
                service.store.add_publication_intent(intent_id=intent_id, actor_id=actor_id, pack_hash=pack_lookup, chain_id=chain_id, contract=contract, status="unsigned", params=params, created_at=datetime.now(timezone.utc).isoformat())
            return {"intent_id": intent_id, "status": "unsigned", "unsigned_tx": params, "private_key_required": False, "broadcast": False}
        except WFError as exc:
            return _error_response(exc)

    @app.get("/api/publications/{intent_id}")
    async def get_publication(request: Request, intent_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization)
            service = app.state.world_service
            row = service.store.publication_intent(intent_id) if service.store is not None and hasattr(service.store, "publication_intent") else None
            if row is None:
                raise WFError("E_NOT_FOUND", "publication intent does not exist")
            if str(row["actor_id"]) != actor_id:
                raise WFError("E_FORBIDDEN", "publication intent is private")
            return {"intent_id": intent_id, "actor_id": str(row["actor_id"]), "pack_hash": str(row["pack_hash"]), "chain_id": int(row["chain_id"]), "contract": str(row["contract"]), "status": str(row["status"]), "tx_hash": row["tx_hash"], "last_error": row["last_error"], "unsigned_tx": strict_loads(str(row["params_json"]).encode("utf-8"))}
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/commands")
    async def submit_command(request: Request, world_id: str, body: dict[str, Any], x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            if set(body) - {"command_id", "kind", "payload", "expected_rules_epoch"}:
                raise WFError("E_SCHEMA", "unknown command envelope field")
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True, body=body)
            actor = app.state.world_service.get(world_id)
            response = actor.submit(actor_id, body.get("command_id"), body.get("kind", ""), body.get("payload", {}), body.get("expected_rules_epoch"))
            return response
        except WFError as exc:
            return _error_response(exc)

    @app.get("/api/worlds/{world_id}/commands/{command_id}")
    async def get_command(request: Request, world_id: str, command_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization)
            return app.state.world_service.get(world_id).command(actor_id, command_id)
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/tick")
    async def advance_tick(request: Request, world_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True)
            actor = app.state.world_service.get(world_id)
            if actor.role(actor_id) != "owner":
                raise WFError("E_FORBIDDEN", "owner required to advance the fixture actor")
            return actor.step()
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/members")
    async def update_member(request: Request, world_id: str, body: dict[str, Any], x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True, body=body)
            actor = app.state.world_service.get(world_id)
            if actor.role(actor_id) != "owner":
                raise WFError("E_FORBIDDEN", "owner required")
            member_id = str(body.get("actor_id", ""))
            if not member_id or len(member_id) > 128:
                raise WFError("E_SCHEMA", "actor_id must be a non-empty string")
            if member_id == actor.owner_id:
                raise WFError("E_ROLE", "owner membership cannot be changed")
            if body.get("revoked"):
                actor.revoke(member_id)
            else:
                actor.grant(member_id, str(body.get("role", "")))
            return actor.metadata(actor_id)
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/checkpoint")
    @app.post("/api/worlds/{world_id}/checkpoints")
    @app.post("/api/worlds/{world_id}/save")
    async def save_checkpoint(request: Request, world_id: str, body: dict[str, Any] | None = None, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True, body=body or {})
            actor = app.state.world_service.get(world_id)
            if actor.role(actor_id) != "owner":
                raise WFError("E_FORBIDDEN", "owner required")
            row = actor.store.latest_commit(world_id, actor.branch_id) if actor.store is not None else None
            snap = actor.snapshot(actor_id)
            requested = (body or {}).get("tick")
            if requested is not None:
                if type(requested) is not int:
                    raise WFError("E_NUMERIC_TYPE", "checkpoint tick must be an integer")
                if requested != int(snap["tick"]):
                    raise WFError("E_CHECKPOINT_TICK", "checkpoint must bind to the latest committed tick")
            if row is None and snap["tick"] != 0:
                raise WFError("E_CHECKPOINT_MISSING", "no durable commit exists")
            return {"world_id": world_id, "branch_id": actor.branch_id, "tick": snap["tick"], "state_hash": snap["state_hash"], "checkpoint_id": f"{world_id}:{actor.branch_id}:{snap['tick']}"}
        except WFError as exc:
            return _error_response(exc)

    @app.get("/api/worlds/{world_id}/checkpoint")
    @app.get("/api/worlds/{world_id}/checkpoints")
    async def get_checkpoint(request: Request, world_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization)
            actor = app.state.world_service.get(world_id)
            actor.require_member(actor_id)
            snap = actor.snapshot(actor_id)
            return {"world_id": world_id, "branch_id": actor.branch_id, "tick": snap["tick"], "state_hash": snap["state_hash"], "checkpoint_id": f"{world_id}:{actor.branch_id}:{snap['tick']}"}
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/load")
    @app.post("/api/worlds/{world_id}/restore")
    async def load_checkpoint(request: Request, world_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=True)
            actor = app.state.world_service.get(world_id)
            actor.require_member(actor_id)
            actor.reload_persisted()
            return actor.snapshot(actor_id)
        except WFError as exc:
            return _error_response(exc)

    @app.post("/api/worlds/{world_id}/verify")
    @app.get("/api/worlds/{world_id}/verify")
    async def verify_world(request: Request, world_id: str, x_actor_id: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        try:
            actor_id = resolve_identity(request, x_actor_id, authorization, mutate=False)
            actor = app.state.world_service.get(world_id)
            if actor.role(actor_id) != "owner":
                raise WFError("E_FORBIDDEN", "owner required")
            if actor.store is None:
                return {"world_id": world_id, "branch_id": actor.branch_id, "verified_ticks": 0, "state_hash": actor.engine.state_hash(), "ok": True, "durable": False}
            return actor.store.verify(actor.world_id, actor.branch_id, actor.config, actor.catalog, actor.engine.blueprints)
        except WFError as exc:
            return _error_response(exc)

    @app.websocket("/ws/worlds/{world_id}")
    async def world_ws(websocket: WebSocket, world_id: str):
        authorization = websocket.headers.get("authorization")
        if authorization is not None and not authorization.lower().startswith("bearer "):
            await websocket.close(code=1008)
            return
        bearer = authorization[7:].strip() if authorization is not None else None
        # Do not put long-lived identities in query strings: URLs are commonly
        # logged.  A local default and transport headers are sufficient here.
        actor_header = websocket.headers.get("x-actor-id")
        session_id = websocket.cookies.get("wf_session")
        session = session_row(session_id)
        if session is not None:
            actor_id = str(session["actor_id"] if isinstance(session, sqlite3.Row) else session["actor_id"])
        elif app.state.dev_headers:
            actor_id = actor_header if actor_header is not None else bearer if bearer is not None else "local-owner"
        else:
            await websocket.close(code=1008)
            return
        actor_id = actor_id.strip() if isinstance(actor_id, str) else actor_id
        origin = websocket.headers.get("origin")
        host = websocket.headers.get("host", "127.0.0.1")
        if origin:
            origin_host = urlparse(origin).netloc
            configured_hosts = {urlparse(value).netloc for value in allowed_origins if urlparse(value).netloc}
            allowed_hosts = {host, "testserver", "localhost", "127.0.0.1", "localhost:5173", "127.0.0.1:5173", "127.0.0.1:8010", *configured_hosts}
            if origin_host not in allowed_hosts and origin_host not in allowed_origin_hosts:
                await websocket.close(code=1008)
                return
        try:
            actor = app.state.world_service.get(world_id)
            actor.require_member(actor_id)
        except WFError:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        try:
            message = await websocket.receive_json()
            if not isinstance(message, dict) or message.get("type") != "subscribe":
                await websocket.send_json({"type": "error", "code": "E_SCHEMA", "message": "first message must be subscribe"})
                await websocket.close(code=1008)
                return
            last_seq = message.get("last_seq", 0)
            try:
                last_seq = int(last_seq)
            except (TypeError, ValueError):
                await websocket.send_json({"type": "error", "code": "E_SCHEMA", "message": "last_seq must be an integer"})
                await websocket.close(code=1008)
                return
            cursor = last_seq
            deltas = actor.deltas_since(actor_id, last_seq)
            if not deltas:
                snap = actor.snapshot(actor_id)
                full = {"type": "world_delta", "world_id": world_id, "branch_id": actor.branch_id, "event_seq": snap["last_seq"], "tick": snap["tick"], "state_hash": snap["state_hash"], "changes": snap["state"], "events": []}
                if len(json.dumps(full, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
                    await websocket.send_json({"type": "resync_required", "world_id": world_id, "last_seq": snap["last_seq"]})
                else:
                    await websocket.send_json(full)
                cursor = snap["last_seq"]
            else:
                for delta in deltas:
                    # Never send an unbounded payload.  Clients can fetch a full
                    # snapshot over HTTP when a delta exceeds the wire budget.
                    if len(json.dumps(delta, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
                        await websocket.send_json({"type": "resync_required", "world_id": world_id, "last_seq": actor._event_seq})
                        cursor = actor._event_seq
                        break
                    await websocket.send_json(delta)
                    cursor = max(cursor, int(delta.get("event_seq", 0)))
            # Keep the subscription alive and read-only.  Receiving any further
            # subscribe allows reconnect clients to catch up without writes. A
            # short receive timeout also lets HTTP command commits push deltas
            # to already connected sessions.
            while True:
                try:
                    message = await asyncio.wait_for(websocket.receive_json(), timeout=0.25)
                except asyncio.TimeoutError:
                    message = None
                if message is None:
                    if session_id is not None and session_row(session_id) is None:
                        await websocket.close(code=1008)
                        return
                    try:
                        updates = actor.deltas_since(actor_id, cursor)
                    except WFError:
                        await websocket.close(code=1008)
                        return
                    for delta in updates:
                        await websocket.send_json(delta)
                        cursor = max(cursor, int(delta.get("event_seq", cursor)))
                    continue
                if isinstance(message, dict) and message.get("type") == "subscribe":
                    try:
                        seq = int(message.get("last_seq", 0))
                    except (TypeError, ValueError):
                        await websocket.send_json({"type": "error", "code": "E_SCHEMA", "message": "last_seq must be an integer"})
                        continue
                    try:
                        updates = actor.deltas_since(actor_id, seq)
                    except WFError:
                        await websocket.close(code=1008)
                        return
                    for delta in updates:
                        await websocket.send_json(delta)
                        cursor = max(cursor, int(delta.get("event_seq", cursor)))
        except WebSocketDisconnect:
            return

    return app


# A conventional ASGI target for ``uvicorn wf.web.app:app``.  Importing the
# module remains possible in core-only environments where the optional web
# dependency is absent; in that case callers should install ``worldforge[web]``
# before serving.
app = create_app(PersistentWorldService()) if FastAPI is not None else None

__all__ = ["WorldActor", "WorldService", "PersistentWorldService", "SQLiteWorldService", "create_app", "app"]
