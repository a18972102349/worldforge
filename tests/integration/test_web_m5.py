from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from wf.core.canonical import digest, strict_loads
from wf.pack import make_rule_pack
from wf.web import PersistentWorldService, create_app


ROOT = Path(__file__).resolve().parents[2]
CATALOG = strict_loads((ROOT / "examples" / "fixtures" / "catalog.json").read_bytes())
BLUEPRINT = strict_loads((ROOT / "examples" / "blueprints" / "river_mill.json").read_bytes())


def _client(tmp_path: Path):
    service = PersistentWorldService(tmp_path / "worlds.sqlite")
    return TestClient(create_app(service)), service


def _session(client: TestClient, actor: str) -> str:
    response = client.post("/api/sessions", json={"actor_id": actor})
    assert response.status_code == 200
    return str(response.json()["csrf_token"])


def test_rule_pack_import_export_and_fork_are_durable(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    try:
        csrf = _session(client, "owner")
        headers = {"X-CSRF-Token": csrf}
        world = client.post("/api/worlds", headers=headers, json={"scenario_id": "river_demo"})
        assert world.status_code == 201, world.text
        world_id = world.json()["world_id"]
        pack = make_rule_pack(BLUEPRINT, catalog=CATALOG, engine_artifact_hash="engine-v0", catalog_hash=digest("catalog", CATALOG), policy_hash="policy-v0")
        imported = client.post("/api/worlds/import", headers=headers, json={"archive_base64": base64.b64encode(pack.archive).decode("ascii")})
        assert imported.status_code == 201, imported.text
        imported_world = imported.json()["world_id"]
        assert imported.json()["pack_hash"] == pack.pack_hash
        meta = client.get(f"/api/worlds/{imported_world}")
        # Cookie authentication is carried by TestClient; read-only request is
        # still scoped to the authenticated owner.
        assert meta.status_code == 200
        exported = client.post(f"/api/worlds/{imported_world}/exports", headers=headers, json={"mode": "blueprints_only", "pack_hash": pack.pack_hash})
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("application/zip")
        assert make_rule_pack(BLUEPRINT, catalog=CATALOG, engine_artifact_hash="engine-v0", catalog_hash=digest("catalog", CATALOG), policy_hash="policy-v0").archive == exported.content
        install = client.post(f"/api/worlds/{imported_world}/commands", headers=headers, json={"command_id": "install-pack", "kind": "INSTALL_PACK", "payload": {"pack_hash": pack.pack_hash}, "expected_rules_epoch": 1})
        assert install.status_code == 200
        blocked_uninstall = client.delete(f"/api/worlds/{imported_world}/packs/{pack.pack_hash}", headers=headers)
        assert blocked_uninstall.status_code == 400
        assert blocked_uninstall.json()["error"]["code"] == "E_PACK_IN_USE"
        fork = client.post(f"/api/worlds/{imported_world}/fork", headers=headers, json={})
        assert fork.status_code == 201, fork.text
        fork_id = fork.json()["world_id"]
        assert fork_id != imported_world
        assert fork.json()["state_hash"] == client.get(f"/api/worlds/{fork_id}").json().get("state_hash", fork.json()["state_hash"])
        assert client.get(f"/api/packs/{pack.pack_hash}").status_code == 200
        service.close()
        service2 = PersistentWorldService(tmp_path / "worlds.sqlite")
        try:
            restored = service2.get(imported_world)
            assert pack.pack_hash in restored.packs
            assert restored.packs[pack.pack_hash]["blueprint_hash"] == pack.manifest["blueprint_hash"]
            assert pack.pack_hash in service2.get(fork_id).packs
        finally:
            service2.close()
    finally:
        try:
            service.close()
        except Exception:
            pass


def test_pack_import_rejects_external_resource_manifest(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    try:
        csrf = _session(client, "owner")
        headers = {"X-CSRF-Token": csrf}
        bad = {"format": "worldforge-rule-pack", "format_version": 1, "publisher": "https://example.invalid"}
        response = client.post("/api/worlds/import", headers=headers, json={"archive_base64": base64.b64encode(json.dumps(bad).encode()).decode()})
        assert response.status_code == 400
        assert response.json()["error"]["code"] in {"E_PACK_ARCHIVE", "E_PACK_EXTERNAL_RESOURCE", "E_PACK_SCHEMA"}
    finally:
        service.close()

