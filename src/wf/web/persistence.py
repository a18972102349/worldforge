"""Small SQLite journal used by the M3 web actor.

The core engine remains unaware of SQLite.  This module stores complete
authoritative states at committed tick boundaries and the command journal used
to rebuild them.  Every state/commit is written in one transaction by the
single world actor.
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from wf.core.canonical import canonical_bytes, digest, strict_loads
from wf.core.errors import WFError


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS wf_worlds (
  world_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, owner_id TEXT NOT NULL,
  config_json TEXT NOT NULL, state_json TEXT NOT NULL, state_hash TEXT NOT NULL,
  tick INTEGER NOT NULL, rules_epoch INTEGER NOT NULL, paused INTEGER NOT NULL DEFAULT 0,
  next_seq INTEGER NOT NULL DEFAULT 0, event_seq INTEGER NOT NULL DEFAULT 0,
  genesis_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wf_memberships (
  world_id TEXT NOT NULL, actor_id TEXT NOT NULL, role TEXT NOT NULL,
  revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(world_id, actor_id)
);
CREATE TABLE IF NOT EXISTS wf_commands (
  world_id TEXT NOT NULL, actor_id TEXT NOT NULL, command_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, server_seq INTEGER NOT NULL,
  effective_tick INTEGER NOT NULL, status TEXT NOT NULL, result_json TEXT,
  PRIMARY KEY(world_id, actor_id, command_id), UNIQUE(world_id, server_seq)
);
CREATE TABLE IF NOT EXISTS wf_events (
  world_id TEXT NOT NULL, branch_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
  tick INTEGER NOT NULL, payload_json TEXT NOT NULL,
  PRIMARY KEY(world_id, branch_id, event_seq)
);
CREATE TABLE IF NOT EXISTS wf_commits (
  world_id TEXT NOT NULL, branch_id TEXT NOT NULL, tick INTEGER NOT NULL,
  state_hash TEXT NOT NULL, previous_head TEXT NOT NULL, log_head TEXT NOT NULL,
  state_json TEXT NOT NULL, result_json TEXT NOT NULL,
  PRIMARY KEY(world_id, branch_id, tick)
);
CREATE TABLE IF NOT EXISTS wf_sessions (
  session_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, csrf_token TEXT NOT NULL,
  created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS wf_packs (
  pack_hash TEXT PRIMARY KEY, blueprint_hash TEXT NOT NULL,
  manifest_json TEXT NOT NULL, archive_blob BLOB NOT NULL,
  created_by TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wf_world_packs (
  world_id TEXT NOT NULL, pack_hash TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(world_id, pack_hash)
);
CREATE TABLE IF NOT EXISTS wf_inventions (
  task_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, world_id TEXT NOT NULL,
  status TEXT NOT NULL, mode TEXT NOT NULL, request_json TEXT NOT NULL,
  result_json TEXT NOT NULL, attempts INTEGER NOT NULL, candidate_hash TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wf_publication_intents (
  intent_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, pack_hash TEXT NOT NULL,
  chain_id INTEGER NOT NULL, contract TEXT NOT NULL, status TEXT NOT NULL,
  tx_hash TEXT, last_error TEXT, params_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def _json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def _decode(value: str) -> Any:
    return strict_loads(value.encode("utf-8"))


class SQLiteWebStore:
    """Thread-safe-enough SQLite store; actor locking serializes writers."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.execute("PRAGMA synchronous=FULL")

    def close(self) -> None:
        self.db.close()

    def create_world(self, *, world_id: str, branch_id: str, owner_id: str,
                     config: dict[str, Any], state: dict[str, Any], state_hash: str,
                     genesis_hash: str, created_at: str) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO wf_worlds(world_id,branch_id,owner_id,config_json,state_json,state_hash,tick,rules_epoch,paused,next_seq,event_seq,genesis_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (world_id, branch_id, owner_id, _json(config), _json(state), state_hash,
                 int(state.get("tick", 0)), int(state.get("rules_epoch", 1)), 0, 0, 0,
                 genesis_hash, created_at),
            )
            self.db.execute("INSERT INTO wf_memberships VALUES(?,?,?,0)", (world_id, owner_id, "owner"))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def world_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wf_worlds ORDER BY created_at,world_id").fetchall()

    def world(self, world_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM wf_worlds WHERE world_id=?", (world_id,)).fetchone()

    def fork_world(self, source_world_id: str, new_world_id: str, new_branch_id: str, owner_id: str, created_at: str) -> None:
        """Clone a committed world journal into an independent branch."""
        source = self.world(source_world_id)
        if source is None:
            raise WFError("E_NOT_FOUND", "source world does not exist")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "INSERT INTO wf_worlds(world_id,branch_id,owner_id,config_json,state_json,state_hash,tick,rules_epoch,paused,next_seq,event_seq,genesis_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_world_id, new_branch_id, owner_id, source["config_json"], source["state_json"], source["state_hash"], source["tick"], source["rules_epoch"], source["paused"], source["next_seq"], source["event_seq"], source["genesis_hash"], created_at),
            )
            self.db.execute("INSERT INTO wf_memberships(world_id,actor_id,role,revoked) SELECT ?,actor_id,CASE WHEN actor_id=? THEN 'owner' ELSE role END,0 FROM wf_memberships WHERE world_id=? AND revoked=0", (new_world_id, owner_id, source_world_id))
            self.db.execute("INSERT INTO wf_memberships(world_id,actor_id,role,revoked) VALUES(?,?,?,0) ON CONFLICT(world_id,actor_id) DO UPDATE SET role='owner',revoked=0", (new_world_id, owner_id, "owner"))
            self.db.execute("INSERT INTO wf_world_packs(world_id,pack_hash,disabled) SELECT ?,pack_hash,disabled FROM wf_world_packs WHERE world_id=?", (new_world_id, source_world_id))
            self.db.execute(
                "INSERT INTO wf_commands(world_id,actor_id,command_id,payload_hash,payload_json,server_seq,effective_tick,status,result_json) SELECT ?,actor_id,command_id,payload_hash,payload_json,server_seq,effective_tick,status,result_json FROM wf_commands WHERE world_id=?",
                (new_world_id, source_world_id),
            )
            self.db.execute(
                "INSERT INTO wf_events(world_id,branch_id,event_seq,tick,payload_json) SELECT ?,?,event_seq,tick,payload_json FROM wf_events WHERE world_id=? AND branch_id=?",
                (new_world_id, new_branch_id, source_world_id, source["branch_id"]),
            )
            self.db.execute(
                "INSERT INTO wf_commits(world_id,branch_id,tick,state_hash,previous_head,log_head,state_json,result_json) SELECT ?,?,tick,state_hash,previous_head,log_head,state_json,result_json FROM wf_commits WHERE world_id=? AND branch_id=?",
                (new_world_id, new_branch_id, source_world_id, source["branch_id"]),
            )
            # Rebind event envelopes and rebuild the branch's commit hash
            # chain.  A fork is a new durable history even when its initial
            # state bytes equal the parent checkpoint.
            previous = str(source["genesis_hash"])
            command_rows = self.db.execute("SELECT actor_id,command_id,payload_json,server_seq,effective_tick FROM wf_commands WHERE world_id=? ORDER BY server_seq", (new_world_id,)).fetchall()
            commands_by_tick: dict[int, list[dict[str, Any]]] = {}
            for row in command_rows:
                payload = _decode(str(row[2]))
                if payload.get("kind") == "SET_PAUSED":
                    continue
                commands_by_tick.setdefault(int(row[4]), []).append({"actor_id": row[0], "command_id": row[1], "kind": payload.get("kind"), "payload": payload.get("payload", {}), "server_seq": int(row[3]), "effective_tick": int(row[4])})
            commits = self.db.execute("SELECT tick,state_hash FROM wf_commits WHERE world_id=? AND branch_id=? ORDER BY tick", (new_world_id, new_branch_id)).fetchall()
            for commit in commits:
                tick = int(commit[0])
                event_rows = self.db.execute("SELECT event_seq,payload_json FROM wf_events WHERE world_id=? AND branch_id=? AND tick=? ORDER BY event_seq", (new_world_id, new_branch_id, tick)).fetchall()
                payloads: list[dict[str, Any]] = []
                for ordinal, event_row in enumerate(event_rows):
                    event_payload = _decode(str(event_row[1]))
                    if isinstance(event_payload, dict):
                        event_payload["world_id"] = new_world_id
                        event_payload["branch_id"] = new_branch_id
                    self.db.execute("UPDATE wf_events SET payload_json=? WHERE world_id=? AND branch_id=? AND event_seq=?", (_json(event_payload), new_world_id, new_branch_id, int(event_row[0])))
                    payloads.append({"tick": tick, "ordinal": ordinal, "event": event_payload})
                log_head = digest("commit", {"previous_head": previous, "tick": tick, "commands": sorted(commands_by_tick.get(tick, []), key=lambda c: c["server_seq"]), "events": payloads, "state_hash": str(commit[1])})
                self.db.execute("UPDATE wf_commits SET previous_head=?,log_head=? WHERE world_id=? AND branch_id=? AND tick=?", (previous, log_head, new_world_id, new_branch_id, tick))
                previous = log_head
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def memberships(self, world_id: str) -> dict[str, str]:
        rows = self.db.execute("SELECT actor_id,role FROM wf_memberships WHERE world_id=? AND revoked=0", (world_id,)).fetchall()
        return {str(r[0]): str(r[1]) for r in rows}

    def commands(self, world_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wf_commands WHERE world_id=? ORDER BY server_seq", (world_id,)).fetchall()

    def insert_command(self, world_id: str, record: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO wf_commands(world_id,actor_id,command_id,payload_hash,payload_json,server_seq,effective_tick,status,result_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (world_id, record["actor_id"], record["command_id"], record["payload_hash"],
             _json({"kind": record["kind"], "payload": record["payload"], "expected_rules_epoch": record.get("expected_rules_epoch")} ),
             int(record["server_seq"]), int(record["effective_tick"]), record["status"],
             None if record.get("result") is None else _json(record["result"])),
        )

    def update_command(self, world_id: str, record: dict[str, Any]) -> None:
        self.db.execute(
            "UPDATE wf_commands SET status=?,result_json=? WHERE world_id=? AND actor_id=? AND command_id=?",
            (record["status"], None if record.get("result") is None else _json(record["result"]),
             world_id, record["actor_id"], record["command_id"]),
        )

    def update_membership(self, world_id: str, actor_id: str, role: str | None) -> None:
        if role is None:
            self.db.execute("UPDATE wf_memberships SET revoked=1 WHERE world_id=? AND actor_id=?", (world_id, actor_id))
        else:
            self.db.execute("INSERT INTO wf_memberships(world_id,actor_id,role,revoked) VALUES(?,?,?,0) ON CONFLICT(world_id,actor_id) DO UPDATE SET role=excluded.role,revoked=0", (world_id, actor_id, role))

    def commit_tick(self, *, world_id: str, branch_id: str, tick: int, state: dict[str, Any], state_hash: str,
                    paused: bool, next_seq: int, event_seq: int, result: dict[str, Any],
                    updates: Iterable[dict[str, Any]], events: Iterable[dict[str, Any]],
                    previous_head: str, log_head: str) -> None:
        """Atomically persist state, command outcomes, events, and commit head."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute(
                "UPDATE wf_worlds SET state_json=?,state_hash=?,tick=?,rules_epoch=?,paused=?,next_seq=?,event_seq=? WHERE world_id=?",
                (_json(state), state_hash, int(tick), int(state.get("rules_epoch", 1)), int(paused), int(next_seq), int(event_seq), world_id),
            )
            for record in updates:
                self.update_command(world_id, record)
            for event in events:
                self.db.execute("INSERT INTO wf_events(world_id,branch_id,event_seq,tick,payload_json) VALUES(?,?,?,?,?)", (world_id, branch_id, int(event["event_seq"]), int(event["tick"]), _json(event)))
            self.db.execute("INSERT INTO wf_commits(world_id,branch_id,tick,state_hash,previous_head,log_head,state_json,result_json) VALUES(?,?,?,?,?,?,?,?)", (world_id, branch_id, int(tick), state_hash, previous_head, log_head, _json(state), _json(result)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def control_commit(self, *, world_id: str, actor_id: str, command: dict[str, Any], paused: bool, event: dict[str, Any], event_seq: int) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.insert_command(world_id, command)
            self.db.execute("UPDATE wf_worlds SET paused=?,event_seq=? WHERE world_id=?", (int(paused), int(event_seq), world_id))
            # Control-plane pause/resume events are associated with the
            # current tick for clients but kept out of the simulation tick's
            # deterministic event segment (the tick may already be committed).
            self.db.execute("INSERT INTO wf_events(world_id,branch_id,event_seq,tick,payload_json) SELECT world_id,branch_id,?,?,? FROM wf_worlds WHERE world_id=?", (int(event_seq), -1, _json(event), world_id))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def events_since(self, world_id: str, branch_id: str, last_seq: int) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT payload_json FROM wf_events WHERE world_id=? AND branch_id=? AND event_seq>? ORDER BY event_seq", (world_id, branch_id, int(last_seq))).fetchall()
        return [_decode(str(r[0])) for r in rows]

    def latest_commit(self, world_id: str, branch_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM wf_commits WHERE world_id=? AND branch_id=? ORDER BY tick DESC LIMIT 1", (world_id, branch_id)).fetchone()

    def commit_rows(self, world_id: str, branch_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wf_commits WHERE world_id=? AND branch_id=? ORDER BY tick", (world_id, branch_id)).fetchall()

    def checkpoint(self, world_id: str, branch_id: str, tick: int, state: dict[str, Any], state_hash: str) -> None:
        # Checkpoints are represented by a commit-shaped row only after the
        # tick has committed; writing a duplicate tick is intentionally rejected.
        row = self.db.execute("SELECT tick FROM wf_commits WHERE world_id=? AND branch_id=? AND tick=?", (world_id, branch_id, tick)).fetchone()
        if row is None:
            raise WFError("E_CHECKPOINT_TICK", "checkpoint must bind to a committed tick")
        committed = self.db.execute("SELECT state_hash FROM wf_commits WHERE world_id=? AND branch_id=? AND tick=?", (world_id, branch_id, tick)).fetchone()
        if committed is None or committed[0] != state_hash:
            raise WFError("E_CHECKPOINT_HASH", "checkpoint state hash mismatch")

    def add_session(self, sid: str, actor_id: str, csrf: str, created_at: str) -> None:
        self.db.execute("INSERT INTO wf_sessions(session_id,actor_id,csrf_token,created_at,revoked) VALUES(?,?,?,?,0)", (sid, actor_id, csrf, created_at))

    def session(self, sid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM wf_sessions WHERE session_id=? AND revoked=0", (sid,)).fetchone()

    def revoke_session(self, sid: str) -> None:
        self.db.execute("UPDATE wf_sessions SET revoked=1 WHERE session_id=?", (sid,))

    def revoke_actor_sessions(self, actor_id: str) -> None:
        self.db.execute("UPDATE wf_sessions SET revoked=1 WHERE actor_id=?", (actor_id,))

    def add_pack(self, *, pack_hash: str, blueprint_hash: str, manifest: dict[str, Any], archive: bytes, created_by: str, created_at: str) -> None:
        self.db.execute(
            "INSERT INTO wf_packs(pack_hash,blueprint_hash,manifest_json,archive_blob,created_by,created_at) VALUES(?,?,?,?,?,?) ON CONFLICT(pack_hash) DO NOTHING",
            (pack_hash, blueprint_hash, _json(manifest), sqlite3.Binary(bytes(archive)), created_by, created_at),
        )

    def link_pack(self, world_id: str, pack_hash: str) -> None:
        self.db.execute("INSERT INTO wf_world_packs(world_id,pack_hash,disabled) VALUES(?,?,0) ON CONFLICT(world_id,pack_hash) DO UPDATE SET disabled=0", (world_id, pack_hash))

    def disable_pack(self, world_id: str, pack_hash: str) -> None:
        self.db.execute("UPDATE wf_world_packs SET disabled=1 WHERE world_id=? AND pack_hash=?", (world_id, pack_hash))

    def linked_packs(self, world_id: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT p.*,wp.disabled AS world_disabled FROM wf_packs p JOIN wf_world_packs wp ON wp.pack_hash=p.pack_hash WHERE wp.world_id=? AND wp.disabled=0 ORDER BY p.created_at,p.pack_hash", (world_id,)).fetchall()

    def pack(self, pack_hash: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM wf_packs WHERE pack_hash=?", (pack_hash,)).fetchone()

    def packs(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wf_packs ORDER BY created_at,pack_hash").fetchall()

    def add_invention(self, task: dict[str, Any], created_at: str) -> None:
        result = task.get("result", {})
        self.db.execute(
            "INSERT INTO wf_inventions(task_id,actor_id,world_id,status,mode,request_json,result_json,attempts,candidate_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task["task_id"], task["actor_id"], task["world_id"], task["status"], task["mode"], _json(task.get("request", {})), _json(result), len(result.get("attempts", [])), result.get("candidate_hash"), created_at),
        )

    def update_invention_status(self, task_id: str, status: str) -> None:
        self.db.execute("UPDATE wf_inventions SET status=? WHERE task_id=?", (status, task_id))

    def inventions(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM wf_inventions ORDER BY created_at,task_id").fetchall()

    def add_publication_intent(self, *, intent_id: str, actor_id: str, pack_hash: str, chain_id: int, contract: str, status: str, params: dict[str, Any], created_at: str) -> None:
        self.db.execute("INSERT INTO wf_publication_intents(intent_id,actor_id,pack_hash,chain_id,contract,status,tx_hash,last_error,params_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (intent_id, actor_id, pack_hash, int(chain_id), contract, status, None, None, _json(params), created_at))

    def publication_intent(self, intent_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM wf_publication_intents WHERE intent_id=?", (intent_id,)).fetchone()

    def verify(self, world_id: str, branch_id: str, config: dict[str, Any], catalog: dict[str, Any], blueprints: dict[str, Any]) -> dict[str, Any]:
        """Verify persisted chain and re-execute every committed tick."""
        from wf.core.engine import Engine

        world = self.world(world_id)
        if world is None:
            raise WFError("E_NOT_FOUND", "world does not exist")
        engine = Engine(config, catalog, blueprints=blueprints)
        previous = str(world["genesis_hash"])
        commits = self.commit_rows(world_id, branch_id)
        command_rows = self.commands(world_id)
        by_tick: dict[int, list[dict[str, Any]]] = {}
        log_by_tick: dict[int, list[dict[str, Any]]] = {}
        for row in command_rows:
            payload = _decode(str(row["payload_json"]))
            command = {"actor_id": row["actor_id"], "command_id": row["command_id"], "kind": payload.get("kind"), "payload": payload.get("payload", {}), "server_seq": int(row["server_seq"]), "effective_tick": int(row["effective_tick"]), "expected_rules_epoch": payload.get("expected_rules_epoch")}
            if command["kind"] == "SET_PAUSED":
                continue
            log_by_tick.setdefault(int(row["effective_tick"]), []).append(command)
            # Rejected commands are still part of the durable log, but the
            # actor did not apply them to the engine state.  Replaying them
            # would make an E_PAUSED or revoked command appear executable.
            if str(row["status"]) == "executed":
                by_tick.setdefault(int(row["effective_tick"]), []).append(command)
        for row in commits:
            tick = int(row["tick"])
            result = engine.step(sorted(by_tick.get(tick, []), key=lambda c: (c["server_seq"], c["command_id"])))
            if result.state_hash != row["state_hash"]:
                raise WFError("E_LOG_TAMPERED", f"state hash mismatch at tick {tick}")
            # The web commit includes its world_delta event; full event payload
            # verification is performed below, after reading that row.
            events = self.db.execute("SELECT payload_json FROM wf_events WHERE world_id=? AND branch_id=? AND tick=? ORDER BY event_seq", (world_id, branch_id, tick)).fetchall()
            payloads = [{"tick": tick, "ordinal": i, "event": _decode(str(e[0]))} for i, e in enumerate(events)]
            expected = digest("commit", {"previous_head": previous, "tick": tick, "commands": [{"actor_id": c["actor_id"], "command_id": c["command_id"], "kind": c["kind"], "payload": c["payload"], "server_seq": c["server_seq"], "effective_tick": c["effective_tick"]} for c in sorted(log_by_tick.get(tick, []), key=lambda c: c["server_seq"])], "events": payloads, "state_hash": row["state_hash"]})
            if expected != row["log_head"] or row["previous_head"] != previous:
                raise WFError("E_LOG_TAMPERED", f"log mismatch at tick {tick}")
            persisted_state = _decode(str(row["state_json"]))
            if digest("state", persisted_state) != row["state_hash"]:
                raise WFError("E_SNAPSHOT_TAMPERED", f"snapshot hash mismatch at tick {tick}")
            previous = str(row["log_head"])
        if engine.state_hash() != str(world["state_hash"]):
            raise WFError("E_SNAPSHOT_TAMPERED", "current world state hash mismatch")
        return {"world_id": world_id, "branch_id": branch_id, "verified_ticks": len(commits), "state_hash": engine.state_hash(), "ok": True}


__all__ = ["SQLiteWebStore"]
