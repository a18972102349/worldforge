"""Deterministic, self-contained WorldForge rule-pack archives.

The pack format deliberately contains data only: a canonical manifest, one
canonical compiled blueprint and optional license text.  Import never reads a
path named by the archive and never executes archive content.  The same
manifest bytes produce the same ``pack_hash`` regardless of ZIP timestamps or
compression implementation.
"""
from __future__ import annotations

import hashlib
import io
import posixpath
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping

from wf.compiler import Blueprint, compile_blueprint, model_to_dict
from wf.core.canonical import blueprint_hash, canonical_bytes, check_canonical_value, digest, strict_loads
from wf.core.errors import WFError


PACK_FORMAT = "worldforge-rule-pack"
PACK_VERSION = 1
MAX_PACK_BYTES = 16 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 128
MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _raw_input(value: bytes | bytearray | str) -> bytes:
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
    elif isinstance(value, str):
        # A string is accepted as a filesystem path for ergonomic CLI/library
        # callers; the path helper still exists for callers that want a more
        # explicit name.
        try:
            from pathlib import Path
            raw = Path(value).read_bytes()
        except OSError as exc:
            raise WFError("E_PACK_ARCHIVE", "rule-pack file cannot be read") from exc
    else:
        raise WFError("E_PACK_SCHEMA", "pack input must be bytes")
    if len(raw) > MAX_PACK_BYTES:
        raise WFError("E_PACK_LIMIT", "pack exceeds the 16 MiB archive limit")
    return raw


def _reject_external_urls(value: Any, path: str = "") -> None:
    if isinstance(value, str):
        lower = value.strip().lower()
        if lower.startswith(("http://", "https://", "file://", "ftp://", "data:")):
            raise WFError("E_PACK_EXTERNAL_RESOURCE", "external resource URLs are not allowed", path)
        return
    if isinstance(value, Mapping):
        for key, child in value.items():
            _reject_external_urls(child, f"{path}/{key}" if path else f"/{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_external_urls(child, f"{path}/{index}")


def _safe_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise WFError("E_PACK_PATH", "archive member name is invalid")
    # ZIP names are slash separated.  Reject absolute, drive-qualified and
    # traversal names before any extraction operation.
    if name.startswith("/") or name.startswith("\\") or len(name) >= 2 and name[1] == ":":
        raise WFError("E_PACK_PATH", "absolute archive paths are not allowed")
    normalized = posixpath.normpath(name)
    if normalized != name or normalized in (".", "..") or normalized.startswith("../"):
        raise WFError("E_PACK_PATH", "archive member escapes its root")
    if "//" in name:
        raise WFError("E_PACK_PATH", "archive member path is not normalized")
    return name


def _manifest_without_hash(manifest: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(manifest)
    # pack_hash is never an identity input.  Accept it in imported metadata as
    # a convenience, but always recompute and compare it ourselves.
    out.pop("pack_hash", None)
    return out


@dataclass(frozen=True)
class RulePack:
    manifest: dict[str, Any]
    blueprint: dict[str, Any]
    pack_hash: str
    archive: bytes


@dataclass(frozen=True)
class ImportedRulePack:
    manifest: dict[str, Any]
    blueprint: Blueprint
    pack_hash: str
    files: dict[str, bytes]

    @property
    def blueprint_hash(self) -> str:
        return str(self.manifest["blueprint_hash"])


def make_rule_pack(
    blueprint: Mapping[str, Any] | Blueprint,
    *,
    catalog: Mapping[str, Any] | None = None,
    engine_release: str = "0.1.0",
    engine_artifact_hash: str = "engine-v0",
    catalog_hash: str | None = None,
    policy_hash: str = "policy-v0",
    parent_pack_hashes: list[str] | tuple[str, ...] = (),
    publisher: str = "",
    license_text: str = "",
    publisher_declaration: str = "",
) -> RulePack:
    """Compile and package a blueprint with a fixed execution context."""
    if not isinstance(engine_release, str) or not engine_release:
        raise WFError("E_PACK_SCHEMA", "engine_release must be non-empty")
    for name, value in (("engine_artifact_hash", engine_artifact_hash), ("policy_hash", policy_hash)):
        if not isinstance(value, str) or not value:
            raise WFError("E_PACK_SCHEMA", f"{name} must be non-empty")
    if not isinstance(publisher, str) or not isinstance(license_text, str) or not isinstance(publisher_declaration, str):
        raise WFError("E_PACK_SCHEMA", "publisher and license fields must be strings")
    if not isinstance(parent_pack_hashes, (list, tuple)) or any(not isinstance(x, str) or len(x) != 64 or any(ch not in "0123456789abcdef" for ch in x) for x in parent_pack_hashes):
        raise WFError("E_PACK_SCHEMA", "parent_pack_hashes must contain 64-character hashes")
    parents = sorted(set(parent_pack_hashes))
    if len(parents) > 4:
        raise WFError("E_PACK_SCHEMA", "at most four parent packs are allowed")
    if isinstance(blueprint, Blueprint):
        ir = blueprint
    else:
        ir = compile_blueprint(dict(blueprint), catalog)
    blueprint_value = model_to_dict(ir)
    bp_hash = blueprint_hash(blueprint_value)
    bp_path = f"blueprints/{bp_hash}.json"
    bp_bytes = canonical_bytes(blueprint_value)
    if catalog_hash is None:
        catalog_hash = digest("catalog", dict(catalog or {})) if catalog is not None else "catalog-v0"
    if not isinstance(catalog_hash, str) or not catalog_hash:
        raise WFError("E_PACK_SCHEMA", "catalog_hash must be non-empty")
    license_bytes = license_text.encode("utf-8")
    files = [{"path": bp_path, "sha256": _sha256(bp_bytes), "size": len(bp_bytes)}]
    if license_text:
        files.append({"path": "LICENSE.txt", "sha256": _sha256(license_bytes), "size": len(license_bytes)})
    manifest: dict[str, Any] = {
        "format": PACK_FORMAT,
        "format_version": PACK_VERSION,
        "blueprint_hash": bp_hash,
        "blueprint_file": bp_path,
        "files": files,
        "engine_release": engine_release,
        "engine_artifact_hash": engine_artifact_hash,
        "catalog_hash": catalog_hash,
        "policy_hash": policy_hash,
        "ir_version": str(blueprint_value.get("ir_version", "")),
        "parent_pack_hashes": parents,
        "publisher": publisher,
        "publisher_declaration": publisher_declaration,
        "license_hash": _sha256(license_bytes) if license_text else "",
    }
    check_canonical_value(manifest)
    _reject_external_urls(manifest)
    pack_hash = digest("pack", manifest)
    archive = _write_archive(manifest, {bp_path: bp_bytes, **({"LICENSE.txt": license_bytes} if license_text else {})})
    return RulePack(manifest=manifest, blueprint=blueprint_value, pack_hash=pack_hash, archive=archive)


def _write_archive(manifest: Mapping[str, Any], files: Mapping[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        entries = {"manifest.json": canonical_bytes(dict(manifest)), **files}
        for name in sorted(entries):
            _safe_name(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            archive.writestr(info, entries[name])
    return buf.getvalue()


def export_rule_pack(pack: RulePack | Mapping[str, Any] | Blueprint, **kwargs: Any) -> bytes:
    """Return deterministic archive bytes for a RulePack or blueprint."""
    if isinstance(pack, RulePack):
        return bytes(pack.archive)
    return make_rule_pack(pack, **kwargs).archive


def import_rule_pack(
    source: bytes | bytearray,
    *,
    catalog: Mapping[str, Any] | None = None,
    expected_engine_artifact_hash: str | None = None,
    expected_catalog_hash: str | None = None,
    expected_policy_hash: str | None = None,
    expected_ir_version: str = "0.1",
) -> ImportedRulePack:
    """Safely validate and load an offline rule-pack archive."""
    raw = _raw_input(source)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw), "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise WFError("E_PACK_ARCHIVE", "invalid ZIP rule pack") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise WFError("E_PACK_LIMIT", "pack contains too many members")
        names: set[str] = set()
        total = 0
        for info in infos:
            name = _safe_name(info.filename)
            if name in names:
                raise WFError("E_PACK_PATH", "duplicate archive member")
            names.add(name)
            # Reject Unix symlink entries and directory entries.  The importer
            # reads bytes in memory and never extracts files to disk.
            mode = (info.external_attr >> 16) & 0xFFFF
            if (mode & 0o170000) == 0o120000 or name.endswith("/"):
                raise WFError("E_PACK_PATH", "symlink and directory members are not allowed")
            if info.file_size > MAX_MEMBER_BYTES:
                raise WFError("E_PACK_LIMIT", "archive member exceeds size limit")
            if info.compress_size and info.file_size > info.compress_size * MAX_COMPRESSION_RATIO:
                raise WFError("E_PACK_LIMIT", "archive compression ratio is too large")
            total += int(info.file_size)
            if total > MAX_UNCOMPRESSED_BYTES:
                raise WFError("E_PACK_LIMIT", "pack exceeds uncompressed size limit")
        if "manifest.json" not in names:
            raise WFError("E_PACK_SCHEMA", "manifest.json is required")
        try:
            files = {name: archive.read(name) for name in names}
        except (RuntimeError, zipfile.BadZipFile, OSError) as exc:
            raise WFError("E_PACK_ARCHIVE", "archive member cannot be read") from exc
    try:
        manifest = strict_loads(files["manifest.json"])
    except WFError:
        raise
    if not isinstance(manifest, dict):
        raise WFError("E_PACK_SCHEMA", "manifest must be an object")
    _reject_external_urls(manifest)
    if manifest.get("format") != PACK_FORMAT or manifest.get("format_version") != PACK_VERSION:
        raise WFError("E_PACK_SCHEMA", "unsupported rule-pack format")
    declared_hash = manifest.get("pack_hash")
    manifest_no_hash = _manifest_without_hash(manifest)
    computed_hash = digest("pack", manifest_no_hash)
    if declared_hash is not None and declared_hash != computed_hash:
        raise WFError("E_PACK_HASH", "manifest pack_hash does not match content")
    if expected_engine_artifact_hash is not None and manifest.get("engine_artifact_hash") != expected_engine_artifact_hash:
        raise WFError("E_PACK_CONTEXT", "engine artifact hash is incompatible")
    if expected_catalog_hash is not None and manifest.get("catalog_hash") != expected_catalog_hash:
        raise WFError("E_PACK_CONTEXT", "catalog hash is incompatible")
    if expected_policy_hash is not None and manifest.get("policy_hash") != expected_policy_hash:
        raise WFError("E_PACK_CONTEXT", "policy hash is incompatible")
    if expected_ir_version is not None and manifest.get("ir_version") != expected_ir_version:
        raise WFError("E_PACK_CONTEXT", "IR version is incompatible")
    declared_files = manifest.get("files")
    bp_path = manifest.get("blueprint_file")
    bp_hash = manifest.get("blueprint_hash")
    if not isinstance(declared_files, list) or not isinstance(bp_path, str) or not isinstance(bp_hash, str):
        raise WFError("E_PACK_SCHEMA", "manifest file list and blueprint identity are required")
    _safe_name(bp_path)
    expected_paths = {"manifest.json"} | {str(item.get("path")) for item in declared_files if isinstance(item, dict)}
    if names != expected_paths:
        raise WFError("E_PACK_SCHEMA", "archive members do not match the manifest")
    for item in declared_files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str) or type(item.get("size")) is not int:
            raise WFError("E_PACK_SCHEMA", "invalid manifest file entry")
        name = item["path"]
        _safe_name(name)
        if sum(1 for other in declared_files if isinstance(other, dict) and other.get("path") == name) != 1:
            raise WFError("E_PACK_SCHEMA", "manifest contains duplicate file entries")
        if name not in files or _sha256(files[name]) != item["sha256"] or len(files[name]) != item["size"]:
            raise WFError("E_PACK_HASH", f"file digest mismatch for {name}")
    try:
        blueprint_value = strict_loads(files[bp_path])
        ir = compile_blueprint(blueprint_value, catalog)
    except KeyError as exc:
        raise WFError("E_PACK_SCHEMA", "manifest blueprint file is missing") from exc
    if blueprint_hash(ir) != bp_hash:
        raise WFError("E_PACK_HASH", "blueprint hash does not match manifest")
    if manifest.get("license_hash"):
        if "LICENSE.txt" not in files or _sha256(files["LICENSE.txt"]) != manifest["license_hash"]:
            raise WFError("E_PACK_HASH", "license hash does not match content")
    return ImportedRulePack(manifest=manifest_no_hash, blueprint=ir, pack_hash=computed_hash, files=files)


def import_rule_pack_file(path: str, **kwargs: Any) -> ImportedRulePack:
    """Read a user-selected local file, retaining the same safe importer."""
    from pathlib import Path

    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise WFError("E_PACK_ARCHIVE", "rule-pack file cannot be read") from exc
    return import_rule_pack(raw, **kwargs)


__all__ = [
    "PACK_FORMAT",
    "PACK_VERSION",
    "RulePack",
    "ImportedRulePack",
    "make_rule_pack",
    "export_rule_pack",
    "import_rule_pack",
    "import_rule_pack_file",
]
