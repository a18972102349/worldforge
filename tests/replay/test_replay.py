from __future__ import annotations

import json
from pathlib import Path

import pytest

from wf.app.storage import EventStore
from wf.compiler import compile_blueprint
from wf.core.canonical import blueprint_hash, digest
from wf.core.errors import WFError
from wf.verifier import check_report, verify_blueprint


ROOT = Path(__file__).resolve().parents[2]


def _fixtures():
    catalog = json.loads((ROOT / "examples/fixtures/catalog.json").read_text(encoding="utf8"))
    blueprint = json.loads((ROOT / "examples/blueprints/river_mill.json").read_text(encoding="utf8"))
    return catalog, blueprint


def test_report_is_bound_to_candidate_and_context():
    catalog, blueprint = _fixtures()
    report = verify_blueprint(blueprint, catalog)
    assert report["outcome"] == "passed"
    check_report(blueprint, report, catalog)
    changed = json.loads(json.dumps(blueprint, ensure_ascii=False))
    changed["name"] += " changed"
    with pytest.raises(WFError) as exc:
        check_report(changed, report, catalog)
    assert exc.value.code == "E_REPORT_BINDING"


def test_snapshot_tampering_is_detected(tmp_path):
    store = EventStore(tmp_path / "world.sqlite")
    store.create_world("w", "b", {"tick": 0})
    state = {"tick": 1, "entities": {}, "energy_ledger": {"external_withdrawn_eu": 0, "conversion_loss_eu": 0, "work_consumed_eu": 0, "unused_dissipated_eu": 0}}
    head = store.append_commit("w", "b", 1, state, digest("genesis", {"world_id": "w", "branch_id": "b"}), [], tmp_path / "snapshots")
    store.verify("w", "b")
    (tmp_path / "snapshots/snapshot-1.json").write_text('{"tick":999}', encoding="utf8")
    with pytest.raises(WFError) as exc:
        store.verify("w", "b")
    assert exc.value.code == "E_SNAPSHOT_TAMPERED"
    store.close()
