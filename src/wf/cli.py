"""Offline demonstration and replay command line interface."""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

from wf.app.storage import EventStore
from wf.compiler import compile_blueprint
from wf.core.canonical import blueprint_hash, canonical_bytes, digest
from wf.core.engine import Engine
from wf.verifier import verify_blueprint


ROOT = Path(__file__).resolve().parents[2]


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(value))


def _command(seq: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"server_seq": seq, "command_id": f"demo-{seq}", "actor_id": "demo", "kind": kind, "payload": payload, "effective_tick": 1}


def run_demo(output: Path, ticks: int, offline: bool = True) -> dict[str, Any]:
    if not offline:
        raise SystemExit("M2 only supports --offline")
    catalog = _read(ROOT / "examples" / "fixtures" / "catalog.json")
    world = _read(ROOT / "examples" / "fixtures" / "world.demo.json")
    source = _read(ROOT / "examples" / "blueprints" / "river_mill.json")
    ir = compile_blueprint(source, catalog)
    bp = ir.model_dump(mode="json")
    bp_hash = blueprint_hash(ir)
    report = verify_blueprint(bp, catalog)
    if report.get("outcome") != "passed":
        raise RuntimeError("blueprint verification failed: " + json.dumps(report.get("diagnostics"), ensure_ascii=False))
    output.mkdir(parents=True, exist_ok=True)
    world_id, branch_id = "world-demo", "branch-main"
    engine = Engine(world, catalog)
    engine.register_blueprint(bp_hash, ir)
    store = EventStore(output / "world.sqlite")
    world_config = dict(world)
    world_config["_state_hash_at_tick_0"] = engine.state_hash()
    store.create_world(world_id, branch_id, world_config)
    commands = [
        _command(1, "INSTALL_VALIDATED_BLUEPRINT", {"blueprint_hash": bp_hash}),
        _command(2, "PLACE", {"blueprint_hash": bp_hash, "origin": [2, 1]}),
        _command(3, "DEPOSIT", {"entity_id": "e2/mill", "item": "grain", "amount": 10}),
    ]
    _write(output / "blueprint.json", bp)
    _write(output / "validation_report.json", report)
    _write(output / "initial_snapshot.json", engine.snapshot())
    _write(output / "commands.json", commands)
    initial_head = store.genesis_head(world_id, branch_id)
    head = initial_head
    ticks_log: list[dict[str, Any]] = []
    events_log: list[dict[str, Any]] = []
    for tick in range(1, ticks + 1):
        batch = commands if tick == 1 else []
        for command in batch:
            store.record_command(world_id, "demo", command["command_id"], command, command["server_seq"], tick, status="executed")
        result = engine.step(batch)
        result_dict = result.as_dict()
        ticks_log.append({"tick": tick, "state_hash": result.state_hash, "summary": result.summary})
        events_log.extend({"tick": tick, **event} for event in result.events)
        head = store.append_commit(world_id, branch_id, tick, engine.snapshot(), head, result.events, output / "snapshots", commands=batch)
    final = engine.snapshot()
    _write(output / "ticks.json", ticks_log)
    _write(output / "events.json", events_log)
    _write(output / "final_snapshot.json", final)
    _write(output / "manifest.json", {"world_id": world_id, "branch_id": branch_id, "engine_version": "wf-engine-0.1", "blueprint_hash": bp_hash, "report_hash": report.get("report_hash"), "tick": ticks, "log_head": head, "offline": True})
    store.close()
    entity = final["entities"].get("e2/mill", {})
    summary = {"tick": ticks, "flour": entity.get("on_hand", {}).get("flour", 0), "grain": entity.get("on_hand", {}).get("grain", 0), "active_job": entity.get("active_job"), "energy_ledger": final.get("energy_ledger", {}), "state_hash": ticks_log[-1]["state_hash"] if ticks_log else digest("state", final)}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def replay_run(run: Path) -> dict[str, Any]:
    manifest = _read(run / "manifest.json")
    catalog = _read(ROOT / "examples" / "fixtures" / "catalog.json")
    world = _read(ROOT / "examples" / "fixtures" / "world.demo.json")
    bp = _read(run / "blueprint.json")
    ir = compile_blueprint(bp, catalog)
    if blueprint_hash(ir) != manifest.get("blueprint_hash"):
        raise RuntimeError("blueprint hash mismatch")
    expected_ticks = _read(run / "ticks.json")
    commands = _read(run / "commands.json")
    engine = Engine(world, catalog)
    engine.register_blueprint(manifest["blueprint_hash"], ir)
    for index, expected in enumerate(expected_ticks, 1):
        result = engine.step(commands if index == 1 else [])
        if result.state_hash != expected["state_hash"]:
            raise RuntimeError(f"replay mismatch at tick {index}: {result.state_hash} != {expected['state_hash']}")
    store = EventStore(run / "world.sqlite")
    store.verify(manifest["world_id"], manifest["branch_id"])
    store.close()
    result = {"status": "replayed", "ticks": len(expected_ticks), "state_hashes_match": True, "log_verified": True, "final_state_hash": expected_ticks[-1]["state_hash"] if expected_ticks else digest("state", engine.snapshot())}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def validate(path: Path) -> dict[str, Any]:
    catalog = _read(ROOT / "examples" / "fixtures" / "catalog.json")
    candidate = _read(path)
    report = verify_blueprint(candidate, catalog)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="worldforge")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo"); demo.add_argument("--offline", action="store_true"); demo.add_argument("--ticks", type=int, default=12); demo.add_argument("--output", type=Path, required=True)
    rep = sub.add_parser("replay"); rep.add_argument("--run", type=Path, required=True)
    val = sub.add_parser("validate"); val.add_argument("blueprint", type=Path)
    args = parser.parse_args(argv)
    if args.command == "demo": run_demo(args.output, args.ticks, args.offline)
    elif args.command == "replay": replay_run(args.run)
    else: validate(args.blueprint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
