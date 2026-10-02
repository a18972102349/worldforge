"""Offline end-to-end checks driven by the actual deterministic engine."""
from __future__ import annotations

import json
from pathlib import Path

from wf.compiler import compile_blueprint
from wf.core import Engine, blueprint_hash, strict_loads


ROOT = Path(__file__).resolve().parents[2]


def _fixture_world_and_blueprint() -> tuple[dict, dict, str, dict]:
    world = strict_loads((ROOT / "examples" / "fixtures" / "world.demo.json").read_bytes())
    catalog = strict_loads((ROOT / "examples" / "fixtures" / "catalog.json").read_bytes())
    raw = strict_loads((ROOT / "examples" / "blueprints" / "river_mill.json").read_bytes())
    ir = compile_blueprint(raw, catalog)
    data = ir.model_dump(mode="json")
    return world, catalog, blueprint_hash(data), data


def test_standard_demo_runs_from_fixture_inputs() -> None:
    world, catalog, bp_hash, bp = _fixture_world_and_blueprint()
    engine = Engine(world, catalog)
    engine.register_blueprint(bp_hash, bp)

    # Alias resolution is a fixture-runner concern; the engine receives only
    # the resolved content hash and entity ID payloads.
    first = engine.step(
        [
            {"command_id": "install-demo", "server_seq": 1, "effective_tick": 1, "kind": "INSTALL_BLUEPRINT", "payload": {"blueprint_hash": bp_hash}},
            {"command_id": "place-demo", "server_seq": 2, "effective_tick": 1, "kind": "PLACE", "payload": {"blueprint_hash": bp_hash, "origin": [2, 1]}},
            {"command_id": "load-demo", "server_seq": 3, "effective_tick": 1, "kind": "DEPOSIT", "payload": {"entity_id": "e2/mill", "item": "grain", "amount": 10}},
        ]
    )
    assert all(item["status"] == "executed" for item in first.command_results)
    mill = "e2/mill"
    assert first.command_results[-1]["status"] == "executed"
    for _ in range(11):
        engine.step([])

    state = engine.snapshot()
    assert state["tick"] == 12
    assert state["entities"][mill]["on_hand"] == {"grain": 6, "flour": 4}
    assert state["entities"][mill]["active_job"] is None
    assert state["energy_ledger"] == {
        "external_withdrawn_eu": 96,
        "conversion_loss_eu": 24,
        "work_consumed_eu": 72,
        "unused_dissipated_eu": 0,
    }


def test_same_commands_produce_same_tick_hashes() -> None:
    world, catalog, bp_hash, bp = _fixture_world_and_blueprint()

    def run() -> list[str]:
        engine = Engine(world, catalog)
        engine.register_blueprint(bp_hash, bp)
        hashes: list[str] = []
        hashes.append(engine.step([{"command_id": "install", "server_seq": 1, "effective_tick": 1, "kind": "INSTALL_BLUEPRINT", "payload": {"blueprint_hash": bp_hash}}, {"command_id": "place", "server_seq": 2, "effective_tick": 1, "kind": "PLACE", "payload": {"blueprint_hash": bp_hash, "origin": [2, 1]}}, {"command_id": "deposit", "server_seq": 3, "effective_tick": 1, "kind": "DEPOSIT", "payload": {"entity_id": "e2/mill", "item": "grain", "amount": 10}}]).state_hash)
        for _ in range(11):
            hashes.append(engine.step([]).state_hash)
        return hashes

    assert run() == run()

