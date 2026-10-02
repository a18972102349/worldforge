from pathlib import Path

import pytest

from wf import WFError, blueprint_hash, strict_loads
from wf.compiler import compile_blueprint
from wf.verifier import check_report, report_hash, verify_blueprint

ROOT = Path(__file__).resolve().parents[2]


def _inputs():
    catalog = strict_loads((ROOT / "examples" / "fixtures" / "catalog.json").read_bytes())
    source = (ROOT / "examples" / "blueprints" / "river_mill.json").read_bytes()
    return source, catalog


def test_verifier_runs_real_fixture_cases() -> None:
    source, catalog = _inputs()
    report = verify_blueprint(source, catalog)
    assert report["outcome"] == "passed"
    assert len(report["cases"]) == 8
    assert all(case["actual"] is not None for case in report["cases"])
    assert report_hash(report) == report["report_hash"]
    demo = next(case for case in report["cases"] if case["name"] == "fixture_demo")
    entities = demo["actual"]["entities"]
    mill = next(value for value in entities.values() if value["node_id"] == "mill")
    assert mill["on_hand"] == {"grain": 6, "flour": 4}


def test_report_binding_rejects_tampering() -> None:
    source, catalog = _inputs()
    report = verify_blueprint(source, catalog)
    tampered = dict(report)
    tampered["cases"] = list(report["cases"])
    tampered["cases"][0] = dict(tampered["cases"][0], passed=False)
    with pytest.raises(WFError) as caught:
        check_report(source, tampered, catalog)
    assert caught.value.code == "E_REPORT_BINDING"


def test_report_binding_accepts_same_candidate_and_context() -> None:
    source, catalog = _inputs()
    report = verify_blueprint(source, catalog)
    check_report(source, report, catalog)


def test_verifier_honors_wall_clock_limit() -> None:
    source, catalog = _inputs()
    report = verify_blueprint(source, catalog, {"timeout_seconds": 0.001})
    assert report["outcome"] == "rejected"
    assert report["diagnostics"][0]["code"] in {"E_VERIFIER_TIMEOUT", "E_VERIFIER_WORKER"}

