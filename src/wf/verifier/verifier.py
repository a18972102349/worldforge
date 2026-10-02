"""Offline, bounded validation worker for compiled WF-IR candidates."""
from __future__ import annotations

import copy
import json
import multiprocessing as mp
import os
import socket
import time
from pathlib import Path
from typing import Any, Mapping

from wf.compiler import compile_blueprint
from wf.core.canonical import blueprint_hash, digest, strict_loads
from wf.core.context import SUITE_VERSION, VERIFIER_VERSION, ValidationContext, default_limits
from wf.core.errors import WFError


_ROOT = Path(__file__).resolve().parents[3]
_WORLD_PATH = _ROOT / "examples" / "fixtures" / "world.demo.json"
_EXPECTED_PATH = _ROOT / "examples" / "fixtures" / "expected_demo.json"
_SUITE_CASES = ["fixture_demo", "energy_conservation", "no_source", "no_input", "output_full", "rename_visual_invariance", "shared_source_budget", "determinism"]


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _world_fixture() -> dict[str, Any]:
    return strict_loads(_WORLD_PATH.read_bytes())


def _expected_fixture() -> dict[str, Any]:
    return strict_loads(_EXPECTED_PATH.read_bytes())


def _engine_class() -> Any:
    # Keep verifier importable while the engine package is assembled.  No
    # dynamic candidate-controlled imports are performed.
    try:
        from wf.engine import Engine
    except ImportError:
        try:
            from wf.core.engine import Engine
        except ImportError as exc:
            raise WFError("E_VERIFIER_UNAVAILABLE", "simulation engine is unavailable") from exc
    return Engine


def _command(seq: int, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "server_seq": seq,
        "command_id": f"verify-{seq}",
        "actor_id": "verifier",
        "kind": kind,
        "payload": dict(payload),
    }


def _state(engine: Any) -> dict[str, Any]:
    state = getattr(engine, "state", None)
    if callable(state):
        state = state()
    if state is None and hasattr(engine, "snapshot"):
        state = engine.snapshot()
    if state is None:
        return {}
    if hasattr(state, "model_dump"):
        state = state.model_dump(mode="json")
    return _json_copy(state)


def _result_data(result: Any) -> dict[str, Any]:
    if hasattr(result, "model_dump"):
        result = result.model_dump(mode="json")
    if isinstance(result, Mapping):
        return _json_copy(dict(result))
    return {
        "tick": getattr(result, "tick", None),
        "state_hash": getattr(result, "state_hash", None),
        "events": _json_copy(getattr(result, "events", [])),
        "command_results": _json_copy(getattr(result, "command_results", [])),
        "summary": _json_copy(getattr(result, "summary", {})),
    }


def _entities_by_node(state: Mapping[str, Any]) -> dict[str, str]:
    entities = state.get("entities", {})
    result: dict[str, str] = {}
    if isinstance(entities, Mapping):
        for entity_id, entity in entities.items():
            if isinstance(entity, Mapping) and isinstance(entity.get("node_id"), str):
                result[entity["node_id"]] = str(entity_id)
    elif isinstance(entities, list):
        for entity in entities:
            if isinstance(entity, Mapping) and isinstance(entity.get("node_id"), str) and entity.get("entity_id") is not None:
                result[entity["node_id"]] = str(entity["entity_id"])
    return result


def _ledger(state: Mapping[str, Any]) -> dict[str, int]:
    value = state.get("energy_ledger", {})
    if not isinstance(value, Mapping):
        value = {}
    return {str(key): int(number) for key, number in value.items() if isinstance(number, int) and not isinstance(number, bool)}


def _summary(state: Mapping[str, Any]) -> dict[str, Any]:
    ledger = _ledger(state)
    entities = state.get("entities", {})
    active_jobs = 0
    if isinstance(entities, Mapping):
        iterable = entities.values()
    elif isinstance(entities, list):
        iterable = entities
    else:
        iterable = ()
    for entity in iterable:
        if isinstance(entity, Mapping) and entity.get("active_job") is not None:
            active_jobs += 1
    entity_summary: dict[str, Any] = {}
    if isinstance(entities, Mapping):
        for entity_id, entity in entities.items():
            if isinstance(entity, Mapping):
                entity_summary[str(entity_id)] = {
                    "node_id": entity.get("node_id"),
                    "on_hand": dict(entity.get("on_hand", {})),
                    "input_escrow": dict(entity.get("input_escrow", {})),
                    "output_reserved": dict(entity.get("output_reserved", {})),
                    "active_job": entity.get("active_job"),
                    "last_status": entity.get("last_status"),
                }
    return {
        "tick": state.get("tick"),
        "state_hash": digest("state", state),
        "active_jobs": active_jobs,
        "energy_ledger": ledger,
        "entities": entity_summary,
    }


def _setup_engine(candidate: Mapping[str, Any], candidate_hash: str, catalog: Mapping[str, Any], world: Mapping[str, Any]) -> Any:
    Engine = _engine_class()
    config = _json_copy(world)
    engine = Engine(config, _json_copy(catalog))
    engine.register_blueprint(candidate_hash, _json_copy(candidate))
    return engine


def _run_ticks(
    candidate: Mapping[str, Any], candidate_hash: str, catalog: Mapping[str, Any], *,
    world: Mapping[str, Any], ticks: int, deposit: bool = True, fill_outputs: bool = False,
) -> dict[str, Any]:
    run_world = _json_copy(world)
    if fill_outputs:
        run_world.setdefault("warehouse", {})
        for node in candidate.get("nodes", []):
            if not isinstance(node, Mapping) or not isinstance(node.get("process"), Mapping):
                continue
            for item, amount in node["process"].get("outputs", {}).items():
                capacity = int(node.get("inventory_capacities", {}).get(item, amount))
                run_world["warehouse"][item] = max(int(run_world["warehouse"].get(item, 0)), capacity * int(amount))
    engine = _setup_engine(candidate, candidate_hash, catalog, run_world)
    seq = 1
    first = [_command(seq, "INSTALL_VALIDATED_BLUEPRINT", {"blueprint_hash": candidate_hash})]
    seq += 1
    first.append(_command(seq, "PLACE", {"blueprint_hash": candidate_hash, "origin": [2, 1]}))
    seq += 1
    # Placement IDs are deterministic (`e{placement_server_seq}/{node_id}`),
    # so fixture commands can deposit in the same tick's command phase after
    # PLACE.  This preserves the scenario's at_tick=1 semantics and avoids
    # granting the candidate an extra tick merely because this worker has no
    # alias resolver.
    if deposit:
        for node in candidate.get("nodes", []):
            if not isinstance(node, Mapping) or not isinstance(node.get("process"), Mapping):
                continue
            item_maps = [node["process"].get("outputs", {}), node["process"].get("inputs", {})] if fill_outputs else [node["process"].get("inputs", {})]
            for item_map in item_maps:
                for item, amount in item_map.items():
                    amount = int(amount) * (int(node.get("inventory_capacities", {}).get(item, amount)) if fill_outputs and item in node["process"].get("outputs", {}) else 10)
                    first.append(_command(seq, "DEPOSIT", {"entity_id": f"e2/{node['id']}", "item": item, "amount": amount}))
                    seq += 1
    result_log: list[dict[str, Any]] = []
    result_log.append(_result_data(engine.step(first)))
    entity_ids = _entities_by_node(_state(engine))
    if deposit and not any(c.get("kind") == "DEPOSIT" for c in first):
        deposits: list[dict[str, Any]] = []
        node_map = {node["id"]: node for node in candidate.get("nodes", []) if isinstance(node, Mapping)}
        for node_id, node in node_map.items():
            process = node.get("process")
            if not isinstance(process, Mapping) or node_id not in entity_ids:
                continue
            item_maps = [process.get("outputs", {}), process.get("inputs", {})] if fill_outputs else [process.get("inputs", {})]
            for item_map in item_maps:
                for item, amount in item_map.items():
                    amount = int(amount) * (int(node.get("inventory_capacities", {}).get(item, amount)) if fill_outputs and item in process.get("outputs", {}) else 10)
                    deposits.append(_command(seq, "DEPOSIT", {"entity_id": entity_ids[node_id], "item": item, "amount": amount}))
                    seq += 1
        if deposits:
            result_log.append(_result_data(engine.step(deposits)))
    current_tick = int(_state(engine).get("tick", len(result_log)))
    while current_tick < ticks:
        result_log.append(_result_data(engine.step([])))
        current_tick = int(_state(engine).get("tick", current_tick + 1))
    state = _state(engine)
    return {"state": state, "summary": _summary(state), "results": result_log}


def _variant_rename(candidate: Mapping[str, Any]) -> dict[str, Any]:
    variant = _json_copy(candidate)
    variant["name"] = (str(variant.get("name", "candidate")) + " renamed")[:64]
    variant["description"] = (str(variant.get("description", "")) + " renamed")[:512]
    shapes = ["wheel", "mill", "block", "shaft"]
    for node in variant.get("nodes", []):
        visual = node.get("visual", {})
        if isinstance(visual, dict) and visual.get("shape") in shapes:
            visual["shape"] = shapes[(shapes.index(visual["shape"]) + 1) % len(shapes)]
    return variant


def _invariant_case(name: str, actual: dict[str, Any], expected: Mapping[str, Any]) -> tuple[bool, str]:
    state = actual["state"]
    ledger = _ledger(state)
    if name == "energy_conservation":
        lhs = ledger.get("external_withdrawn_eu", 0)
        rhs = ledger.get("conversion_loss_eu", 0) + ledger.get("unused_dissipated_eu", 0) + ledger.get("work_consumed_eu", 0)
        return lhs == rhs, f"external={lhs}, accounted={rhs}"
    if name == "no_source":
        got = ledger.get("external_withdrawn_eu", 0)
        return got == 0, f"external_withdrawn_eu={got}"
    if name == "no_input":
        work = ledger.get("work_consumed_eu", 0)
        return work == 0 and actual["summary"]["active_jobs"] == 0, f"work={work},active_jobs={actual['summary']['active_jobs']}"
    if name == "output_full":
        work = ledger.get("work_consumed_eu", 0)
        return work == 0, f"work={work}"
    if name == "determinism":
        return bool(expected.get("same_state_hashes")), str(expected)
    if name == "fixture_demo":
        entities = state.get("entities", {})
        # The expected fixture is interpreted by stable field names; no
        # candidate/node name is used to decide behavior.
        target = expected
        for entity in (entities.values() if isinstance(entities, Mapping) else entities if isinstance(entities, list) else []):
            if not isinstance(entity, Mapping):
                continue
            on_hand = entity.get("on_hand", {})
            process = entity.get("node_id")
            if isinstance(on_hand, Mapping) and "flour" in on_hand and "grain" in on_hand:
                expected_mill = target.get("mill", {})
                got = {"on_hand": dict(on_hand), "active_job": entity.get("active_job")}
                exp = {"on_hand": expected_mill.get("on_hand", {}), "active_job": expected_mill.get("active_job")}
                return got == exp, f"got={got},expected={exp}"
        return False, "no processor entity with grain/flour inventory"
    return True, ""


def _behavior_signature(state: Mapping[str, Any]) -> dict[str, Any]:
    entities = state.get("entities", {})
    values = entities.values() if isinstance(entities, Mapping) else entities if isinstance(entities, list) else []
    rows = []
    for entity in values:
        if isinstance(entity, Mapping):
            rows.append({"node_id": entity.get("node_id"), "on_hand": dict(entity.get("on_hand", {})), "input_escrow": dict(entity.get("input_escrow", {})), "output_reserved": dict(entity.get("output_reserved", {})), "active_job": entity.get("active_job"), "last_status": entity.get("last_status")})
    rows.sort(key=lambda row: str(row.get("node_id")))
    return {"entities": rows, "energy_ledger": _ledger(state), "tick": state.get("tick")}


def _sandbox_worker(candidate: dict[str, Any], candidate_hash: str, catalog: dict[str, Any], limits: dict[str, Any], queue: Any) -> None:
    """Spawn target; all candidate execution is data-only and network denied."""
    try:
        class _DeniedSocket:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                raise RuntimeError("network disabled in verifier worker")
        socket.socket = _DeniedSocket  # type: ignore[assignment]
        max_ticks = min(int(limits.get("max_ticks", 120)), 120)
        cases: list[dict[str, Any]] = []
        base_world = _world_fixture()
        expected_fixture = _expected_fixture()
        base_actual: dict[str, Any] | None = None
        run_specs = [
            ("fixture_demo", base_world, max_ticks if max_ticks < 12 else 12, True, False, expected_fixture),
            ("energy_conservation", base_world, max_ticks, True, False, {}),
            ("no_source", {**base_world, "sources": []}, min(max_ticks, 8), True, False, {}),
            ("no_input", base_world, min(max_ticks, 8), False, False, {}),
            ("output_full", base_world, min(max_ticks, 8), True, True, {}),
        ]
        if int(limits.get("max_cases", 10)) < len(run_specs):
            run_specs = run_specs[: int(limits["max_cases"])]
        for name, world, ticks, deposit, full, expected in run_specs:
            started = time.monotonic()
            try:
                actual = _run_ticks(candidate, candidate_hash, catalog, world=world, ticks=ticks, deposit=deposit, fill_outputs=full)
                entities = actual["state"].get("entities", {})
                entity_count = len(entities) if isinstance(entities, (Mapping, list)) else 0
                if entity_count > int(limits.get("max_entities", 128)):
                    raise WFError("E_COMPLEXITY_BUDGET", "sandbox entity budget exceeded")
                if name == "fixture_demo":
                    base_actual = actual
                passed, diagnostic = _invariant_case(name, actual, expected)
                cases.append({"name": name, "input": {"world": world, "ticks": ticks, "deposit": deposit, "output_full": full}, "expected": expected, "actual": actual["summary"], "passed": passed, "diagnostic": diagnostic, "elapsed_ms": int((time.monotonic() - started) * 1000)})
            except Exception as exc:
                cases.append({"name": name, "input": {"world": world, "ticks": ticks, "deposit": deposit, "output_full": full}, "expected": expected, "actual": None, "passed": False, "diagnostic": f"{type(exc).__name__}: {exc}", "elapsed_ms": int((time.monotonic() - started) * 1000)})
        # Changing display-only fields must not change primitive behavior.  The
        # variant is compiled and executed independently, so this case catches
        # accidental name/visual dispatch in the engine.
        try:
            renamed = _variant_rename(candidate)
            renamed_ir = compile_blueprint(renamed, catalog)
            renamed_hash = blueprint_hash(renamed_ir)
            renamed_run = _run_ticks(renamed_ir.model_dump(mode="json"), renamed_hash, catalog, world=base_world, ticks=min(max_ticks, 12), deposit=True)
            first_sig = _behavior_signature(base_actual["state"]) if base_actual else {}
            renamed_sig = _behavior_signature(renamed_run["state"])
            # Installed/package hashes are deliberately excluded from the
            # signature; identity changes while physical behavior must remain.
            cases.append({"name": "rename_visual_invariance", "input": {"name_suffix": " renamed", "visual_rotation": True}, "expected": {"same_behavior_signature": True}, "actual": {"original": first_sig, "renamed": renamed_sig}, "passed": first_sig == renamed_sig, "diagnostic": "" if first_sig == renamed_sig else "display fields changed behavior"})
        except Exception as exc:
            cases.append({"name": "rename_visual_invariance", "input": {"name_suffix": " renamed", "visual_rotation": True}, "expected": {"same_behavior_signature": True}, "actual": None, "passed": False, "diagnostic": f"{type(exc).__name__}: {exc}"})
        # Multiple harvesters, when present, must share the source's finite
        # budget.  A single harvester is still checked against that same cap.
        try:
            shared_ticks = min(max_ticks, 8)
            shared_run = _run_ticks(candidate, candidate_hash, catalog, world=base_world, ticks=shared_ticks, deposit=False)
            supplied = sum(int(source.get("supply_eu_per_tick", 0)) for source in base_world.get("sources", [])) * shared_ticks
            withdrawn = _ledger(shared_run["state"]).get("external_withdrawn_eu", 0)
            cases.append({"name": "shared_source_budget", "input": {"ticks": shared_ticks, "source_count": len(base_world.get("sources", []))}, "expected": {"withdrawn_leq_supply": True}, "actual": {"withdrawn": withdrawn, "supply_bound": supplied}, "passed": withdrawn <= supplied, "diagnostic": "" if withdrawn <= supplied else "source budget was duplicated"})
        except Exception as exc:
            cases.append({"name": "shared_source_budget", "input": {"ticks": min(max_ticks, 8)}, "expected": {"withdrawn_leq_supply": True}, "actual": None, "passed": False, "diagnostic": f"{type(exc).__name__}: {exc}"})
        # Determinism is deliberately a separate run, with no reuse of state.
        world = _world_fixture()
        one = _run_ticks(candidate, candidate_hash, catalog, world=world, ticks=min(max_ticks, 12), deposit=True)
        two = _run_ticks(candidate, candidate_hash, catalog, world=world, ticks=min(max_ticks, 12), deposit=True)
        one_hash = digest("state", one["state"])
        two_hash = digest("state", two["state"])
        cases.append({"name": "determinism", "input": {"ticks": min(max_ticks, 12)}, "expected": {"same_state_hashes": True}, "actual": {"first_state_hash": one_hash, "second_state_hash": two_hash}, "passed": one_hash == two_hash, "diagnostic": "" if one_hash == two_hash else "state hashes differ"})
        cases = cases[: max(0, min(int(limits.get("max_cases", 10)), 10))]
        queue.put({"cases": cases})
    except BaseException as exc:
        queue.put({"worker_error": f"{type(exc).__name__}: {exc}"})


def report_hash(report: Mapping[str, Any]) -> str:
    body = dict(report)
    body.pop("report_hash", None)
    return digest("validation-report", body)


def _failed_report(code: str, message: str, *, candidate_hash_value: str | None = None) -> dict[str, Any]:
    report = {"verifier_version": VERIFIER_VERSION, "outcome": "rejected", "diagnostics": [{"code": code, "message": message}], "cases": []}
    if candidate_hash_value:
        report["candidate_hash"] = candidate_hash_value
    report["report_hash"] = report_hash(report)
    return report


def verify_blueprint(candidate: Any, catalog: Mapping[str, Any], limits: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Compile and run bounded, offline cases in a fresh spawn worker."""
    policy = default_limits()
    if limits:
        policy.update(dict(limits))
    if "timeout_seconds" in policy:
        policy["timeout_ms"] = int(float(policy.pop("timeout_seconds")) * 1000)
    policy["max_cases"] = min(int(policy.get("max_cases", 10)), 10)
    policy["max_ticks"] = min(int(policy.get("max_ticks", 120)), 120)
    policy["max_entities"] = min(int(policy.get("max_entities", 128)), 128)
    if policy["max_cases"] < 1 or policy["max_ticks"] < 1 or policy["max_entities"] < 1 or int(policy.get("timeout_ms", 2000)) < 1:
        return _failed_report("E_COMPLEXITY_BUDGET", "verification limits must allow at least one bounded case/tick")
    try:
        ir = compile_blueprint(candidate, catalog)
        candidate_hash_value = blueprint_hash(ir)
        candidate_data = ir.model_dump(mode="json")
    except WFError as exc:
        return _failed_report(exc.code, str(exc))
    context = ValidationContext.create(catalog, policy, {"suite_version": SUITE_VERSION, "cases": _SUITE_CASES})
    queue: Any = mp.get_context("spawn").Queue()
    process = mp.get_context("spawn").Process(target=_sandbox_worker, args=(candidate_data, candidate_hash_value, dict(catalog), policy, queue))
    process.start()
    process.join(max(0.001, int(policy.get("timeout_ms", 2000)) / 1000))
    if process.is_alive():
        process.terminate()
        process.join(1)
        return _failed_report("E_VERIFIER_TIMEOUT", "sandbox worker exceeded wall-clock limit", candidate_hash_value=candidate_hash_value)
    try:
        worker = queue.get(timeout=1)
    except Exception:
        return _failed_report("E_VERIFIER_WORKER", "sandbox worker returned no report", candidate_hash_value=candidate_hash_value)
    if worker.get("worker_error"):
        return _failed_report("E_VERIFIER_WORKER", worker["worker_error"], candidate_hash_value=candidate_hash_value)
    cases = worker.get("cases", [])
    report = {
        "verifier_version": VERIFIER_VERSION,
        "candidate_hash": candidate_hash_value,
        "context": context.as_dict(),
        "context_hashes": context.as_dict(),
        "context_hash": context.context_hash,
        "policy": policy,
        "suite_version": SUITE_VERSION,
        "cases": cases,
        "outcome": "passed" if cases and all(case.get("passed") for case in cases) else "rejected",
        "diagnostics": [],
    }
    if report["outcome"] != "passed":
        report["diagnostics"].append({"code": "E_CASE_FAILED", "message": "one or more sandbox cases failed"})
    report["report_hash"] = report_hash(report)
    return report


def check_report(candidate: Any, report: Mapping[str, Any], catalog: Mapping[str, Any], limits: Mapping[str, Any] | None = None) -> None:
    """Reject a report whose candidate or validation context was replaced."""
    if not isinstance(report, Mapping):
        raise WFError("E_REPORT_BINDING", "validation report must be an object")
    if report_hash(report) != report.get("report_hash"):
        raise WFError("E_REPORT_BINDING", "validation report hash mismatch")
    if report.get("outcome") != "passed":
        raise WFError("E_REPORT_REJECTED", "only a report with passed outcome can authorize installation")
    if report.get("outcome") != "passed":
        raise WFError("E_REPORT_REJECTED", "validation report did not pass its case suite")
    ir = compile_blueprint(candidate, catalog)
    expected_candidate = blueprint_hash(ir)
    if report.get("candidate_hash") != expected_candidate:
        raise WFError("E_REPORT_BINDING", "candidate hash does not match report")
    policy = default_limits()
    if limits:
        policy.update(dict(limits))
    context = ValidationContext.create(catalog, policy, {"suite_version": SUITE_VERSION, "cases": _SUITE_CASES})
    if report.get("context_hash") != context.context_hash or report.get("context") != context.as_dict():
        raise WFError("E_REPORT_BINDING", "engine/catalog/policy/suite/verifier context mismatch")


__all__ = ["verify_blueprint", "check_report", "report_hash"]
