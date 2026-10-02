from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from wf import WFError, import_rule_pack, make_rule_pack
from wf.core.canonical import strict_loads


ROOT = Path(__file__).resolve().parents[2]
CATALOG = strict_loads((ROOT / "examples" / "fixtures" / "catalog.json").read_bytes())
BLUEPRINT = strict_loads((ROOT / "examples" / "blueprints" / "river_mill.json").read_bytes())


def test_rule_pack_round_trip_is_deterministic_and_self_contained() -> None:
    first = make_rule_pack(BLUEPRINT, catalog=CATALOG, engine_artifact_hash="engine-v0", policy_hash="policy-v0", publisher="alice", license_text="CC0")
    second = make_rule_pack(BLUEPRINT, catalog=CATALOG, engine_artifact_hash="engine-v0", policy_hash="policy-v0", publisher="alice", license_text="CC0")
    assert first.pack_hash == second.pack_hash
    assert first.archive == second.archive
    loaded = import_rule_pack(first.archive, catalog=CATALOG, expected_engine_artifact_hash="engine-v0", expected_policy_hash="policy-v0")
    assert loaded.pack_hash == first.pack_hash
    assert loaded.blueprint_hash == first.manifest["blueprint_hash"]
    assert set(loaded.files) == {"manifest.json", first.manifest["blueprint_file"], "LICENSE.txt"}


def test_pack_context_mismatch_and_tamper_are_rejected() -> None:
    pack = make_rule_pack(BLUEPRINT, catalog=CATALOG)
    with pytest.raises(WFError, match="incompatible"):
        import_rule_pack(pack.archive, catalog=CATALOG, expected_engine_artifact_hash="other-engine")
    # Rebuilding a ZIP with altered blueprint bytes keeps the archive shape but
    # fails the declared file digest before the compiler can see it.
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(pack.archive), "r") as source, zipfile.ZipFile(out, "w") as target:
        for info in source.infolist():
            data = source.read(info.filename)
            if info.filename.endswith(".json") and info.filename != "manifest.json":
                data += b" "
            target.writestr(info, data)
    with pytest.raises(WFError, match="digest"):
        import_rule_pack(out.getvalue(), catalog=CATALOG)


def test_pack_rejects_traversal_symlinks_and_external_urls() -> None:
    manifest = {
        "format": "worldforge-rule-pack",
        "format_version": 1,
        "blueprint_hash": "0" * 64,
        "blueprint_file": "../escape.json",
        "files": [],
        "engine_release": "0.1.0",
        "engine_artifact_hash": "engine-v0",
        "catalog_hash": "catalog-v0",
        "policy_hash": "policy-v0",
        "ir_version": "0.1",
        "parent_pack_hashes": [],
        "publisher": "https://bad.example",
        "publisher_declaration": "",
        "license_hash": "",
    }
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("manifest.json", __import__("json").dumps(manifest))
    with pytest.raises(WFError):
        import_rule_pack(data.getvalue(), catalog=CATALOG)

