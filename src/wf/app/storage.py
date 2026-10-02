"""Deterministic SQLite event log and snapshot persistence for M2.

The store is deliberately small: one writer owns a world, while replay only
reads committed rows.  Every row is canonicalized before hashing, so changing
an event, command, or snapshot is detected before a replay is accepted.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from wf.core.canonical import canonical_bytes, digest
from wf.core.errors import WFError


SCHEMA = """
CREATE TABLE IF NOT EXISTS worlds (
  id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, engine_hash TEXT NOT NULL,
  capability_hash TEXT NOT NULL, policy_hash TEXT NOT NULL, config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
  world_id TEXT NOT NULL, actor_id TEXT NOT NULL, command_id TEXT NOT NULL,
  payload_hash TEXT NOT NULL, payload_json TEXT NOT NULL, server_seq INTEGER NOT NULL,
  effective_tick INTEGER NOT NULL, status TEXT NOT NULL, result_json TEXT,
  PRIMARY KEY(world_id, actor_id, command_id), UNIQUE(world_id, server_seq)
);
CREATE TABLE IF NOT EXISTS tick_commits (
  world_id TEXT NOT NULL, branch_id TEXT NOT NULL, tick INTEGER NOT NULL,
  state_hash TEXT NOT NULL, previous_head TEXT NOT NULL, log_head TEXT NOT NULL,
  PRIMARY KEY(world_id, branch_id, tick)
);
CREATE TABLE IF NOT EXISTS events (
  world_id TEXT NOT NULL, branch_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
  tick INTEGER NOT NULL, ordinal INTEGER NOT NULL, type TEXT NOT NULL,
  payload_json TEXT NOT NULL, PRIMARY KEY(world_id, branch_id, event_seq)
);
CREATE TABLE IF NOT EXISTS snapshots (
  world_id TEXT NOT NULL, branch_id TEXT NOT NULL, tick INTEGER NOT NULL,
  state_hash TEXT NOT NULL, bytes_path TEXT NOT NULL, bytes_hash TEXT NOT NULL,
  PRIMARY KEY(world_id, branch_id, tick)
);
"""


def _json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


class EventStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def create_world(self, world_id: str, branch_id: str, config: dict[str, Any], *, engine_hash: str = "engine-v0", capability_hash: str = "catalog-v0", policy_hash: str = "policy-v0") -> None:
        self.db.execute("INSERT OR REPLACE INTO worlds VALUES (?,?,?,?,?,?)", (world_id, branch_id, engine_hash, capability_hash, policy_hash, _json(config)))
        self.db.commit()

    def genesis_head(self, world_id: str, branch_id: str) -> str:
        row = self.db.execute("SELECT config_json FROM worlds WHERE id=?", (world_id,)).fetchone()
        if row is not None:
            config = json.loads(row[0])
            state0 = config.pop("_state_hash_at_tick_0", None)
            if state0 is not None:
                return digest("genesis", {"config_hash": digest("config", config), "state_hash_at_tick_0": state0})
        # Compatibility for minimal stores created by callers without a tick-0
        # snapshot marker.
        return digest("genesis", {"world_id": world_id, "branch_id": branch_id})

    def record_command(self, world_id: str, actor_id: str, command_id: str, payload: dict[str, Any], server_seq: int, effective_tick: int, *, status: str = "accepted", result: Any = None) -> None:
        command_payload = {"kind": payload.get("kind"), "payload": payload.get("payload", {})} if isinstance(payload, dict) and "kind" in payload else payload
        payload_hash = digest("command", command_payload)
        try:
            old = self.db.execute("SELECT payload_hash FROM commands WHERE world_id=? AND actor_id=? AND command_id=?", (world_id, actor_id, command_id)).fetchone()
            if old is not None:
                if old[0] != payload_hash:
                    raise WFError("E_COMMAND_ID_CONFLICT", "command_id was already used with different payload")
                return
            self.db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?,?,?)", (world_id, actor_id, command_id, payload_hash, _json(command_payload), server_seq, effective_tick, status, None if result is None else _json(result)))
            self.db.commit()
        except sqlite3.IntegrityError as exc:
            self.db.rollback()
            raise WFError("E_COMMAND_CONFLICT", str(exc)) from exc

    def append_commit(self, world_id: str, branch_id: str, tick: int, state: dict[str, Any], previous_head: str, events: Iterable[dict[str, Any]], snapshot_dir: str | Path | None = None, commands: Iterable[dict[str, Any]] | None = None) -> str:
        state_hash = digest("state", state)
        evs = list(events)
        payloads = [{"tick": tick, "ordinal": i, "event": e} for i, e in enumerate(evs)]
        if commands is None:
            rows = self.db.execute("SELECT actor_id,command_id,payload_json,server_seq,effective_tick FROM commands WHERE world_id=? AND effective_tick=? ORDER BY server_seq", (world_id, tick)).fetchall()
            command_records = [{"actor_id": r[0], "command_id": r[1], "kind": json.loads(r[2]).get("kind"), "payload": json.loads(r[2]).get("payload", {}), "server_seq": r[3], "effective_tick": r[4]} for r in rows]
        else:
            command_records = list(commands)
        log_head = digest("commit", {"previous_head": previous_head, "tick": tick, "commands": command_records, "events": payloads, "state_hash": state_hash})
        try:
            self.db.execute("BEGIN")
            event_seq = self.db.execute("SELECT COALESCE(MAX(event_seq),0) FROM events WHERE world_id=? AND branch_id=?", (world_id, branch_id)).fetchone()[0]
            for i, event in enumerate(evs):
                event_seq += 1
                self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (world_id, branch_id, event_seq, tick, i, str(event.get("type", "event")), _json(event)))
            self.db.execute("INSERT INTO tick_commits VALUES (?,?,?,?,?,?)", (world_id, branch_id, tick, state_hash, previous_head, log_head))
            if snapshot_dir is not None:
                directory = Path(snapshot_dir); directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"snapshot-{tick}.json"
                temp = path.with_suffix(".tmp")
                encoded = canonical_bytes(state)
                temp.write_bytes(encoded)
                temp.replace(path)
                self.db.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)", (world_id, branch_id, tick, state_hash, str(path), digest("snapshot", state)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return log_head

    def latest(self, world_id: str, branch_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM tick_commits WHERE world_id=? AND branch_id=? ORDER BY tick DESC LIMIT 1", (world_id, branch_id)).fetchone()

    def load_snapshot(self, world_id: str, branch_id: str, tick: int | None = None) -> dict[str, Any]:
        if tick is None:
            row = self.db.execute("SELECT * FROM snapshots WHERE world_id=? AND branch_id=? ORDER BY tick DESC LIMIT 1", (world_id, branch_id)).fetchone()
        else:
            row = self.db.execute("SELECT * FROM snapshots WHERE world_id=? AND branch_id=? AND tick=?", (world_id, branch_id, tick)).fetchone()
        if row is None:
            raise WFError("E_SNAPSHOT_MISSING", "requested snapshot does not exist")
        try:
            value = json.loads(Path(row["bytes_path"]).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WFError("E_SNAPSHOT_TAMPERED", "snapshot is unreadable") from exc
        if digest("state", value) != row["state_hash"] or digest("snapshot", value) != row["bytes_hash"]:
            raise WFError("E_SNAPSHOT_TAMPERED", "snapshot hash mismatch")
        return value

    def verify(self, world_id: str, branch_id: str) -> None:
        rows = self.db.execute("SELECT * FROM tick_commits WHERE world_id=? AND branch_id=? ORDER BY tick", (world_id, branch_id)).fetchall()
        previous = self.genesis_head(world_id, branch_id)
        for row in rows:
            events = self.db.execute("SELECT ordinal,type,payload_json FROM events WHERE world_id=? AND branch_id=? AND tick=? ORDER BY ordinal", (world_id, branch_id, row["tick"])).fetchall()
            payloads = [{"tick": row["tick"], "ordinal": e["ordinal"], "event": json.loads(e["payload_json"])} for e in events]
            command_rows = self.db.execute("SELECT actor_id,command_id,payload_json,server_seq,effective_tick FROM commands WHERE world_id=? AND effective_tick=? ORDER BY server_seq", (world_id, row["tick"])).fetchall()
            command_records = [{"actor_id": r[0], "command_id": r[1], "kind": json.loads(r[2]).get("kind"), "payload": json.loads(r[2]).get("payload", {}), "server_seq": r[3], "effective_tick": r[4]} for r in command_rows]
            expected = digest("commit", {"previous_head": previous, "tick": row["tick"], "commands": command_records, "events": payloads, "state_hash": row["state_hash"]})
            if expected != row["log_head"] or previous != row["previous_head"]:
                raise WFError("E_LOG_TAMPERED", f"log mismatch at tick {row['tick']}")
            snap = self.db.execute("SELECT * FROM snapshots WHERE world_id=? AND branch_id=? AND tick=?", (world_id, branch_id, row["tick"])).fetchone()
            if snap is not None:
                path = Path(snap["bytes_path"])
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise WFError("E_SNAPSHOT_TAMPERED", f"snapshot at tick {row['tick']} is unreadable") from exc
                if digest("snapshot", value) != snap["bytes_hash"] or digest("state", value) != snap["state_hash"]:
                    raise WFError("E_SNAPSHOT_TAMPERED", f"snapshot hash mismatch at tick {row['tick']}")
            previous = row["log_head"]


__all__ = ["EventStore"]
