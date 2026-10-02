"""M3 durability/authentication acceptance checks."""
from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from wf.web import create_app
from wf.web.app import PersistentWorldService


def _session(client: TestClient, actor: str):
    response = client.post("/api/sessions", json={"actor_id": actor})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def _world(client: TestClient, headers: dict[str, str]):
    response = client.post("/api/worlds", json={"scenario_id": "river_demo"}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_durable_restart_pending_tick_and_verify(tmp_path):
    db = tmp_path / "world.sqlite3"
    service = PersistentWorldService(db)
    client = TestClient(create_app(service), raise_server_exceptions=False)
    owner = _session(client, "owner")
    metadata = _world(client, owner)
    wid = metadata["world_id"]
    install = client.post(
        f"/api/worlds/{wid}/commands",
        json={"command_id": "install-1", "kind": "INSTALL_VALIDATED_BLUEPRINT", "payload": {"blueprint_hash": metadata["available_blueprints"]["river_mill"]}, "expected_rules_epoch": 1},
        headers=owner,
    )
    assert install.status_code == 200
    assert client.post(f"/api/worlds/{wid}/tick", headers=owner).json()["tick"] == 1
    accepted = client.post(
        f"/api/worlds/{wid}/commands",
        json={"command_id": "pause-1", "kind": "SET_PAUSED", "payload": {"paused": True}},
        headers=owner,
    )
    assert accepted.status_code == 200
    assert client.get(f"/api/worlds/{wid}/snapshot").json()["tick"] == 1
    service.close()

    restarted = PersistentWorldService(db)
    restarted_client = TestClient(create_app(restarted))
    owner2 = _session(restarted_client, "owner")
    assert restarted_client.get(f"/api/worlds/{wid}/snapshot", headers=owner2).json()["tick"] == 1
    # Control command and its idempotency record survive the restart.
    assert restarted_client.get(f"/api/worlds/{wid}/commands/pause-1", headers=owner2).json()["status"] == "executed"
    assert restarted_client.post(f"/api/worlds/{wid}/verify", headers=owner2).json()["ok"] is True
    restarted.close()


def test_tick_transaction_failure_can_retry_without_partial_state(tmp_path, monkeypatch):
    service = PersistentWorldService(tmp_path / "fault.sqlite3")
    client = TestClient(create_app(service), raise_server_exceptions=False)
    owner = _session(client, "owner")
    wid = _world(client, owner)["world_id"]
    actor = service.get(wid)
    original = actor.store.commit_tick

    def fail_once(**kwargs):
        actor.store.commit_tick = original
        raise sqlite3.OperationalError("injected commit fault")

    monkeypatch.setattr(actor.store, "commit_tick", fail_once)
    response = client.post(f"/api/worlds/{wid}/tick", headers=owner)
    assert response.status_code == 500
    assert client.get(f"/api/worlds/{wid}/snapshot", headers=owner).json()["tick"] == 0
    retry = client.post(f"/api/worlds/{wid}/tick", headers=owner)
    assert retry.status_code == 200 and retry.json()["tick"] == 1
    service.close()


def test_tampered_commit_is_rejected_by_verify(tmp_path):
    service = PersistentWorldService(tmp_path / "tamper.sqlite3")
    client = TestClient(create_app(service))
    owner = _session(client, "owner")
    wid = _world(client, owner)["world_id"]
    assert client.post(f"/api/worlds/{wid}/tick", headers=owner).status_code == 200
    service.store.db.execute("UPDATE wf_commits SET log_head='00' WHERE world_id=?", (wid,))
    service.store.db.commit()
    bad = client.post(f"/api/worlds/{wid}/verify", headers=owner)
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "E_LOG_TAMPERED"
    service.close()


def test_same_command_id_is_scoped_to_actor(tmp_path):
    service = PersistentWorldService(tmp_path / "actors.sqlite3")
    client = TestClient(create_app(service))
    owner = _session(client, "owner")
    wid = _world(client, owner)["world_id"]
    member = "builder"
    assert client.post(f"/api/worlds/{wid}/members", json={"actor_id": member, "role": "builder"}, headers=owner).status_code == 200
    builder_client = TestClient(create_app(service))
    builder = _session(builder_client, member)
    body = {"command_id": "same", "kind": "WITHDRAW", "payload": {"entity_id": "missing", "item": "grain", "amount": 1}}
    # Both identities may reserve the same textual id; the database key is
    # (world, actor, command_id), while permissions remain object-scoped.
    assert client.post(f"/api/worlds/{wid}/commands", json=body, headers=owner).status_code == 200
    assert builder_client.post(f"/api/worlds/{wid}/commands", json=body, headers=builder).status_code == 200
    service.close()
