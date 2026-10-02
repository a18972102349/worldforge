import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from wf.web import create_app


def test_world_lifecycle_and_roles():
    client = TestClient(create_app())
    assert client.get("/health").json()["status"] == "ok"
    created = client.post("/api/worlds", json={"scenario_id": "river_demo"}, headers={"X-Actor-ID": "owner"})
    assert created.status_code == 201
    world_id = created.json()["world_id"]
    assert client.get(f"/api/worlds/{world_id}", headers={"X-Actor-ID": "viewer"}).status_code == 403
    service = client.app.state.world_service
    service.get(world_id).grant("viewer", "viewer")
    assert client.get(f"/api/worlds/{world_id}", headers={"X-Actor-ID": "viewer"}).status_code == 200
    denied = client.post(f"/api/worlds/{world_id}/commands", json={"command_id": "c", "kind": "PLACE", "payload": {}}, headers={"X-Actor-ID": "viewer"})
    assert denied.status_code == 403


def test_command_idempotency_and_payload_conflict():
    client = TestClient(create_app())
    created = client.post("/api/worlds", json={"scenario_id": "river_demo"}, headers={"X-Actor-ID": "owner"}).json()
    wid = created["world_id"]
    body = {"command_id": "pause", "kind": "SET_PAUSED", "payload": {"paused": True}}
    one = client.post(f"/api/worlds/{wid}/commands", json=body, headers={"X-Actor-ID": "owner"})
    two = client.post(f"/api/worlds/{wid}/commands", json=body, headers={"X-Actor-ID": "owner"})
    assert one.status_code == two.status_code == 200
    assert one.json() == two.json()
    conflict = client.post(f"/api/worlds/{wid}/commands", json={**body, "payload": {"paused": False}}, headers={"X-Actor-ID": "owner"})
    assert conflict.status_code == 409


def test_websocket_subscription_gets_full_or_delta():
    client = TestClient(create_app())
    created = client.post("/api/worlds", json={"scenario_id": "river_demo"}, headers={"X-Actor-ID": "owner"}).json()
    wid = created["world_id"]
    with client.websocket_connect(f"/ws/worlds/{wid}", headers={"X-Actor-ID": "owner"}) as ws:
        ws.send_json({"type": "subscribe", "last_seq": 0})
        message = ws.receive_json()
        assert message["type"] == "world_delta"
        assert message["world_id"] == wid


def test_local_ui_origin_is_allowed_but_unknown_origin_is_not():
    client = TestClient(create_app())
    allowed = client.options(
        "/api/worlds",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-actor-id,content-type",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    denied = client.options(
        "/api/worlds",
        headers={"Origin": "https://example.invalid", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in denied.headers


def test_two_sessions_queue_tick_and_revoke_before_execution():
    client = TestClient(create_app())
    created = client.post("/api/worlds", json={"scenario_id": "river_demo"}, headers={"X-Actor-ID": "owner"}).json()
    wid = created["world_id"]
    assert client.post(f"/api/worlds/{wid}/members", json={"actor_id": "builder", "role": "builder"}, headers={"X-Actor-ID": "owner"}).status_code == 200
    h = created["available_blueprints"]["river_mill"]
    install = client.post(f"/api/worlds/{wid}/commands", json={"command_id": "install", "kind": "INSTALL_VALIDATED_BLUEPRINT", "payload": {"blueprint_hash": h}, "expected_rules_epoch": 1}, headers={"X-Actor-ID": "owner"})
    place = client.post(f"/api/worlds/{wid}/commands", json={"command_id": "place", "kind": "PLACE", "payload": {"blueprint_hash": h, "origin": [2, 1]}}, headers={"X-Actor-ID": "builder"})
    deposit = client.post(f"/api/worlds/{wid}/commands", json={"command_id": "deposit", "kind": "DEPOSIT", "payload": {"entity_id": "e2/mill", "item": "grain", "amount": 10}}, headers={"X-Actor-ID": "builder"})
    assert install.json()["status"] == place.json()["status"] == deposit.json()["status"] == "accepted"
    assert client.get(f"/api/worlds/{wid}/snapshot", headers={"X-Actor-ID": "builder"}).json()["tick"] == 0
    advanced = client.post(f"/api/worlds/{wid}/tick", headers={"X-Actor-ID": "owner"})
    assert advanced.status_code == 200
    snapshot = client.get(f"/api/worlds/{wid}/snapshot", headers={"X-Actor-ID": "builder"}).json()
    assert snapshot["tick"] == 1
    assert "e2/mill" in snapshot["state"]["entities"]
    assert client.get(f"/api/worlds/{wid}/commands/place", headers={"X-Actor-ID": "builder"}).json()["status"] == "executed"

    # A queued write is checked again after membership is revoked.
    queued = client.post(f"/api/worlds/{wid}/commands", json={"command_id": "late", "kind": "WITHDRAW", "payload": {"entity_id": "e2/mill", "item": "flour", "amount": 1}}, headers={"X-Actor-ID": "builder"})
    assert queued.json()["status"] == "accepted"
    assert client.post(f"/api/worlds/{wid}/members", json={"actor_id": "builder", "revoked": True}, headers={"X-Actor-ID": "owner"}).status_code == 200
    client.post(f"/api/worlds/{wid}/tick", headers={"X-Actor-ID": "owner"})
    assert client.get(f"/api/worlds/{wid}/commands/late", headers={"X-Actor-ID": "owner"}).json()["status"] == "rejected"
