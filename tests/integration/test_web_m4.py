from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from wf.web.app import WorldService, create_app
from wf.web.app import PersistentWorldService
from wf.pack import import_rule_pack, make_rule_pack


ROOT = Path(__file__).resolve().parents[2]


def test_invention_pipeline_task_and_owner_approval() -> None:
    app = create_app(WorldService())
    client = TestClient(app)
    candidate = json.loads((ROOT / "examples" / "blueprints" / "river_mill.json").read_text(encoding="utf-8"))
    created = client.post("/api/worlds", headers={"X-Actor-ID": "owner"}, json={"scenario_id": "river_demo"})
    assert created.status_code == 201
    world_id = created.json()["world_id"]
    response = client.post(f"/api/worlds/{world_id}/inventions", headers={"X-Actor-ID": "owner"}, json={"prompt": "build the fixture mechanism", "mode": "manual", "candidate": candidate})
    assert response.status_code == 200, response.text
    task = response.json()
    assert task["status"] == "validated"
    detail = client.get(f"/api/inventions/{task['task_id']}", headers={"X-Actor-ID": "owner"})
    assert detail.status_code == 200
    stored = detail.json()["result"]
    approved = client.post(f"/api/inventions/{task['task_id']}/approve", headers={"X-Actor-ID": "owner"}, json={"candidate_hash": stored["candidate_hash"], "report_hash": stored["report"]["report_hash"]})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"


def test_invention_and_unsigned_publication_survive_restart(tmp_path: Path) -> None:
    catalog = json.loads((ROOT / "examples" / "fixtures" / "catalog.json").read_text(encoding="utf-8"))
    blueprint = json.loads((ROOT / "examples" / "blueprints" / "river_mill.json").read_text(encoding="utf-8"))
    pack = make_rule_pack(blueprint, catalog=catalog, license_text="CC0")
    imported = import_rule_pack(pack.archive, catalog=catalog, expected_engine_artifact_hash="engine-v0", expected_policy_hash="policy-v0")
    service = PersistentWorldService(tmp_path / "world.sqlite")
    client = TestClient(create_app(service))
    csrf = client.post("/api/sessions", json={"actor_id": "owner"}).json()["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    world_id = client.post("/api/worlds", headers=headers, json={"scenario_id": "river_demo"}).json()["world_id"]
    service.get(world_id).add_pack(pack.pack_hash, imported, pack.archive, "owner")
    invention = client.post(f"/api/worlds/{world_id}/inventions", headers=headers, json={"prompt": "build the fixture mechanism", "mode": "manual", "candidate": blueprint})
    assert invention.status_code == 200, invention.text
    task_id = invention.json()["task_id"]
    publication = client.post("/api/publications/prepare", headers=headers, json={"pack_hash": pack.pack_hash, "license_hash": "0x" + "44" * 32, "content_uri": "ipfs://worldforge/pack"})
    assert publication.status_code == 200, publication.text
    intent_id = publication.json()["intent_id"]
    assert publication.json()["status"] == "unsigned"
    service.close()
    restarted = PersistentWorldService(tmp_path / "world.sqlite")
    restarted_client = TestClient(create_app(restarted))
    restarted_csrf = restarted_client.post("/api/sessions", json={"actor_id": "owner"}).json()["csrf_token"]
    detail = restarted_client.get(f"/api/publications/{intent_id}", headers={"X-CSRF-Token": restarted_csrf})
    assert detail.status_code == 200
    assert detail.json()["status"] == "unsigned"
    task_detail = restarted_client.get(f"/api/inventions/{task_id}", headers={"X-CSRF-Token": restarted_csrf})
    assert task_detail.status_code == 200
    assert task_detail.json()["status"] == "validated"
    restarted.close()
