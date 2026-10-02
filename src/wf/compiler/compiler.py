"""Strict Blueprint parser, schema gate and catalogue-aware compiler."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from pydantic import ValidationError

from wf.core.canonical import canonical_bytes, check_canonical_value, strict_loads
from wf.core.errors import WFError

from .models import Blueprint, ExprAllAny, ExprInventoryAtLeast, ExprNot, model_to_dict

_ROOT = Path(__file__).resolve().parents[3]
_SCHEMA_PATH = _ROOT / "schemas" / "blueprint.schema.json"
_CATALOG_PATH = _ROOT / "examples" / "fixtures" / "catalog.json"


def _load_input(value: Any) -> dict[str, Any]:
    if isinstance(value, Blueprint):
        return model_to_dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        parsed = strict_loads(value)
    else:
        parsed = value
        check_canonical_value(parsed)
        # Enforce the same byte budget for direct Python callers.
        if len(canonical_bytes(parsed)) > 64 * 1024:
            raise WFError("E_COMPLEXITY_BUDGET", "blueprint JSON exceeds 64 KiB")
    if not isinstance(parsed, dict):
        raise WFError("E_SCHEMA", "blueprint root must be a JSON object", "")
    return parsed


def _load_catalog(catalog: Any | None) -> dict[str, Any]:
    if catalog is None:
        try:
            catalog = strict_loads(_CATALOG_PATH.read_bytes())
        except OSError as exc:
            raise WFError("E_UNKNOWN_CAPABILITY", "default catalogue is unavailable") from exc
    elif isinstance(catalog, (str, bytes, bytearray)):
        catalog = strict_loads(catalog)
    else:
        check_canonical_value(catalog)
    if not isinstance(catalog, dict):
        raise WFError("E_SCHEMA", "catalogue root must be a JSON object")
    return catalog


def _schema_gate(data: dict[str, Any]) -> None:
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover - dependency is in the handoff runtime
        raise WFError("E_SCHEMA", "jsonschema dependency is required") from exc
    try:
        schema = strict_loads(_SCHEMA_PATH.read_bytes())
    except OSError as exc:
        raise WFError("E_SCHEMA", "blueprint schema is unavailable") from exc
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda error: list(error.path))
    if errors:
        error = errors[0]
        path = "".join(f"/{part}" for part in error.path)
        raise WFError("E_SCHEMA", error.message, path)


def _precheck_capability_tokens(data: dict[str, Any]) -> None:
    """Give unsupported capability names their stable semantic error code.

    ``source_kind`` and expression ``op`` are intentionally constrained by the
    schema too, but callers need to distinguish an unsupported capability from
    malformed structure.  This precheck only handles well-formed string tokens;
    missing/wrongly typed fields still go through E_SCHEMA.
    """
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        return
    if len(nodes) > 16:
        raise WFError("E_COMPLEXITY_BUDGET", "blueprint exceeds the 16-node budget", "/nodes")
    supported_ops = {"true", "false", "inventory_at_least", "not", "all", "any"}

    def walk_expression(expr: Any, path: str, *, depth: int = 1, count: list[int] | None = None) -> None:
        if not isinstance(expr, dict):
            return
        if count is None:
            count = [0]
        count[0] += 1
        if depth > 8 or count[0] > 64:
            raise WFError("E_COMPLEXITY_BUDGET", "enabled_when expression exceeds AST budget", path)
        op = expr.get("op")
        if isinstance(op, str) and op not in supported_ops:
            raise WFError("E_UNKNOWN_CAPABILITY", f"unknown expression primitive {op!r}", path + "/op")
        if op == "not":
            walk_expression(expr.get("arg"), path + "/arg", depth=depth + 1, count=count)
        elif op in ("all", "any") and isinstance(expr.get("args"), list):
            if len(expr["args"]) > 8:
                raise WFError("E_COMPLEXITY_BUDGET", "enabled_when expression exceeds AST budget", path + "/args")
            for index, arg in enumerate(expr["args"]):
                walk_expression(arg, f"{path}/args/{index}", depth=depth + 1, count=count)

    for index, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        harvest = node.get("harvest")
        if isinstance(harvest, dict):
            source = harvest.get("source_kind")
            if isinstance(source, str) and source != "river":
                raise WFError("E_UNKNOWN_CAPABILITY", f"unknown source kind {source!r}", f"/nodes/{index}/harvest/source_kind")
        process = node.get("process")
        if isinstance(process, dict):
            walk_expression(process.get("enabled_when"), f"/nodes/{index}/process/enabled_when")


def _model_gate(data: dict[str, Any]) -> Blueprint:
    try:
        return Blueprint.model_validate(data)
    except ValidationError as exc:
        error = exc.errors()[0]
        path = "".join(f"/{part}" for part in error.get("loc", ()))
        # Pydantic errors are structural/type failures.  Do not leak a Python
        # repr as a protocol error; details remain in the stable message.
        raise WFError("E_SCHEMA", str(error.get("msg", "invalid value")), path) from exc


def _expr_check(expr: Any, materials: Mapping[str, Any], *, depth: int = 1, count: list[int] | None = None, path: str = "") -> None:
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > 64 or depth > 8:
        raise WFError("E_COMPLEXITY_BUDGET", "enabled_when expression exceeds AST budget", path)
    op = getattr(expr, "op", None)
    if op == "inventory_at_least":
        if expr.item not in materials:
            raise WFError("E_UNKNOWN_CAPABILITY", f"unknown inventory item {expr.item!r}", path + "/item")
    elif op == "not":
        _expr_check(expr.arg, materials, depth=depth + 1, count=count, path=path + "/arg")
    elif op in ("all", "any"):
        for index, child in enumerate(expr.args):
            _expr_check(child, materials, depth=depth + 1, count=count, path=f"{path}/args/{index}")


def _material_tags(items: Mapping[str, int], materials: Mapping[str, Any]) -> dict[str, int]:
    total: dict[str, int] = {}
    for item, quantity in items.items():
        tags = materials[item].get("mass_tags", {})
        for tag, mass in tags.items():
            total[tag] = total.get(tag, 0) + int(mass) * int(quantity)
    return total


def _semantic_gate(ir: Blueprint, catalog: dict[str, Any]) -> None:
    materials = catalog.get("materials")
    source_kinds = catalog.get("source_kinds")
    limits = catalog.get("limits", {})
    edges = catalog.get("process_edges")
    if not isinstance(materials, dict) or not isinstance(source_kinds, list) or not isinstance(edges, list):
        raise WFError("E_UNKNOWN_CAPABILITY", "catalogue is missing required capability tables")
    known_materials = materials
    known_sources = set(source_kinds)
    node_by_id: dict[str, Any] = {}
    positions: dict[tuple[int, int], str] = {}
    for index, node in enumerate(ir.nodes):
        node_path = f"/nodes/{index}"
        if node.id in node_by_id:
            raise WFError("E_DUPLICATE_NODE", f"duplicate node id {node.id!r}", node_path + "/id")
        node_by_id[node.id] = node
        if tuple(node.offset) in positions:
            raise WFError("E_DUPLICATE_POSITION", f"position already used by {positions[tuple(node.offset)]!r}", node_path + "/offset")
        positions[tuple(node.offset)] = node.id
        for item in node.inventory_capacities:
            if item not in known_materials:
                raise WFError("E_UNKNOWN_CAPABILITY", f"unknown inventory item {item!r}", node_path + "/inventory_capacities")
        if node.harvest is not None:
            harvest = node.harvest
            if harvest.source_kind not in known_sources:
                raise WFError("E_UNKNOWN_CAPABILITY", f"unknown source kind {harvest.source_kind!r}", node_path + "/harvest/source_kind")
            if harvest.efficiency.num > harvest.efficiency.den:
                raise WFError("E_ENERGY_GAIN", "harvest efficiency cannot exceed 1", node_path + "/harvest/efficiency")
            max_withdraw = limits.get("max_withdraw_eu_per_tick", 8)
            if harvest.max_withdraw_eu > max_withdraw:
                raise WFError("E_UNKNOWN_CAPABILITY", "harvest request exceeds catalogue limit", node_path + "/harvest/max_withdraw_eu")
        if node.process is None:
            continue
        process = node.process
        process_path = node_path + "/process"
        if set(process.inputs) & set(process.outputs):
            raise WFError("E_RECIPE_LAYOUT", "process inputs and outputs must be disjoint", process_path)
        for item, quantity in list(process.inputs.items()) + list(process.outputs.items()):
            if item not in known_materials:
                raise WFError("E_UNKNOWN_CAPABILITY", f"unknown inventory item {item!r}", process_path)
            if node.inventory_capacities.get(item, 0) < quantity:
                raise WFError("E_CAPACITY", f"inventory capacity cannot hold one batch of {item!r}", process_path)
        expected: dict[str, int] = {}
        min_work = 0
        for item, quantity in process.inputs.items():
            candidates = [edge for edge in edges if isinstance(edge, dict) and edge.get("input") == item]
            if len(candidates) != 1:
                raise WFError("E_TRANSFORM_NOT_ALLOWED", f"no unique catalogue transform for {item!r}", process_path)
            edge = candidates[0]
            output = edge.get("output")
            expected[output] = expected.get(output, 0) + quantity
            min_work += quantity * int(edge.get("min_work_eu_per_input", 0))
        # A different output material is a forbidden transform.  Check this
        # before quantities so grain->stone reports the more useful transform
        # error, while grain->2 flour still reports mass creation below.
        if set(expected) != set(process.outputs):
            raise WFError("E_TRANSFORM_NOT_ALLOWED", "recipe output material is not a catalogue transform", process_path)
        if _material_tags(process.inputs, known_materials) != _material_tags(process.outputs, known_materials):
            raise WFError("E_MATERIAL_BALANCE", "process mass tags are not conserved", process_path)
        if expected != dict(process.outputs):
            raise WFError("E_TRANSFORM_NOT_ALLOWED", "recipe does not match a catalogue transform", process_path)
        if process.work_eu < min_work:
            raise WFError("E_WORK_TOO_LOW", "recipe work is below catalogue minimum", process_path + "/work_eu")
        _expr_check(process.enabled_when, known_materials, path=process_path + "/enabled_when")
    # A link graph is a forest.  This also catches duplicate undirected edges.
    parent = {node_id: node_id for node_id in node_by_id}

    def root(value: str) -> str:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    seen_pairs: set[tuple[str, str]] = set()
    for index, link in enumerate(ir.links):
        path = f"/links/{index}"
        a, b = link.a.node, link.b.node
        if a not in node_by_id or b not in node_by_id:
            raise WFError("E_BAD_REFERENCE", "link references a missing node", path)
        if a == b:
            raise WFError("E_TOPOLOGY", "self-links are not allowed", path)
        pair = tuple(sorted((a, b)))
        if pair in seen_pairs:
            raise WFError("E_TOPOLOGY", "duplicate link", path)
        seen_pairs.add(pair)
        pa, pb = node_by_id[a].offset, node_by_id[b].offset
        if abs(pa[0] - pb[0]) + abs(pa[1] - pb[1]) != 1:
            raise WFError("E_ADJACENCY", "shaft endpoints must be adjacent", path)
        if root(a) == root(b):
            raise WFError("E_TOPOLOGY", "mechanical links must be acyclic", path)
        parent[root(a)] = root(b)


def compile_blueprint(raw: Any, catalog: Any | None = None) -> Blueprint:
    """Strictly parse, validate and compile a blueprint into immutable IR."""
    if isinstance(raw, Blueprint):
        ir = raw
        data = model_to_dict(ir)
    else:
        data = _load_input(raw)
        _precheck_capability_tokens(data)
        _schema_gate(data)
        ir = _model_gate(data)
    catalogue = _load_catalog(catalog)
    _semantic_gate(ir, catalogue)
    return ir


def validate_blueprint(raw: Any, catalog: Any | None = None) -> Blueprint:
    """Compatibility entry point; validation returns the compiled typed IR."""
    return compile_blueprint(raw, catalog)


__all__ = ["compile_blueprint", "validate_blueprint"]
