import json
import pytest

from wf.adapters import (
    FixtureProvider,
    InventionCompiler,
    InventionRequest,
    ModelBudget,
    ProviderResponse,
    RemoteProvider,
    build_m4_evaluation_suite,
    guard_request,
)


def _blueprint():
    with open("examples/blueprints/river_mill.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_suite_has_thirty_tasks_and_three_categories():
    suite = build_m4_evaluation_suite()
    assert len(suite) == 30
    assert {task.category for task in suite} == {"basic", "novel", "reject"}
    assert len({task.task_id for task in suite}) == 30


def test_fixture_provider_requires_explicit_scene_id():
    provider = FixtureProvider({"river": _blueprint()})
    unknown = provider.propose(InventionRequest("task", "harvest and process grain"))
    assert unknown.kind == "unsupported"
    assert unknown.code == "OFFLINE_FIXTURE_ONLY"


def test_remote_provider_is_disabled_by_default():
    response = RemoteProvider(model_id="example", endpoint="https://example.invalid").propose(InventionRequest("task", "harvest and process grain"))
    assert response.kind == "provider_error"
    assert response.code == "E_REMOTE_DISABLED"


def test_unknown_and_unsafe_requests_are_rejected_before_provider():
    with pytest.raises(Exception):
        guard_request(InventionRequest("task", "authorization: teleport grain"))


def test_remote_transport_can_be_injected_without_network():
    calls = []

    def transport(payload):
        calls.append(payload)
        return ProviderResponse("unsupported", code="E_UNSUPPORTED_INTENT", message="fixture")

    provider = RemoteProvider(model_id="m", endpoint="https://example.invalid", enabled=True, allowed_endpoints={"https://example.invalid"}, transport=transport)
    response = provider.propose(InventionRequest("task", "harvest and process grain"))
    assert response.kind == "unsupported"
    assert calls and calls[0]["prompt"]


def test_compiler_stops_at_three_requests_and_records_repairs():
    class BadProvider:
        kind = "manual"

        def propose(self, request):
            return ProviderResponse("candidate", candidate={"bad": True})

        def repair(self, request, candidate, report):
            return self.propose(request)

    result = InventionCompiler(BadProvider(), {}, budget=ModelBudget(max_attempts=3)).compile(InventionRequest("task", "harvest and process grain"))
    assert result.outcome == "rejected"
    assert len(result.attempts) == 3
    assert [item.attempt for item in result.attempts] == [1, 2, 3]


def test_manual_valid_candidate_is_locally_verified():
    catalog = json.loads(open("examples/fixtures/catalog.json", encoding="utf-8").read())
    result = InventionCompiler(
        __import__("wf.adapters", fromlist=["ManualProvider"]).ManualProvider(_blueprint()),
        catalog,
    ).compile(InventionRequest("task", "build the fixture mechanism"))
    assert result.outcome == "validated", result.as_dict()
    assert result.candidate_hash and result.report and result.report["outcome"] == "passed"


def test_provider_exception_is_bounded_and_recorded():
    class BrokenProvider:
        model_id = "broken"

        def propose(self, request):
            raise RuntimeError("transport broke")

    result = InventionCompiler(BrokenProvider(), {}).compile(InventionRequest("task", "build a mechanism"))
    assert result.outcome == "provider_error"
    assert result.diagnostics[0]["code"] == "E_PROVIDER_ERROR"
