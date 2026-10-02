"""Bounded, replaceable invention providers and the offline M4 compiler.

The adapter boundary treats model output as hostile data.  Providers return a
small tagged response and the local compiler remains the only authority that
parses, validates, sandboxes, and accepts a candidate.  No provider performs
network I/O unless a caller explicitly injects a transport and enables it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from wf.compiler import compile_blueprint
from wf.core.canonical import blueprint_hash, canonical_bytes, digest
from wf.core.errors import WFError
from wf.verifier import verify_blueprint


@dataclass(frozen=True, init=False)
class InventionRequest:
    prompt: str
    context: Mapping[str, Any] = field(default_factory=dict)
    capabilities: Mapping[str, Any] = field(default_factory=dict)
    policy_hash: str = "policy-v0"
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    parent_hash: str | None = None
    attempt: int = 0
    diagnostics: tuple[Mapping[str, Any], ...] = ()
    request_id: str = ""

    def __init__(self, request_id_or_prompt: str = "", prompt: str | None = None, context: Mapping[str, Any] | Any = None, capabilities: Mapping[str, Any] | None = None, policy_hash: str = "policy-v0", output_schema: Mapping[str, Any] | None = None, parent_hash: str | None = None, attempt: int = 0, diagnostics: tuple[Mapping[str, Any], ...] = (), request_id: str | None = None) -> None:
        if prompt is None:
            actual_prompt, actual_request_id = request_id_or_prompt, request_id or ""
        else:
            actual_prompt, actual_request_id = prompt, request_id or request_id_or_prompt
        object.__setattr__(self, "prompt", actual_prompt)
        object.__setattr__(self, "request_id", actual_request_id)
        object.__setattr__(self, "context", context if isinstance(context, Mapping) else {})
        object.__setattr__(self, "capabilities", capabilities if isinstance(capabilities, Mapping) else {})
        object.__setattr__(self, "policy_hash", policy_hash)
        object.__setattr__(self, "output_schema", output_schema if isinstance(output_schema, Mapping) else {})
        object.__setattr__(self, "parent_hash", parent_hash)
        object.__setattr__(self, "attempt", attempt)
        object.__setattr__(self, "diagnostics", diagnostics)


@dataclass(frozen=True)
class ProviderResponse:
    kind: str
    candidate: Mapping[str, Any] | None = None
    code: str | None = None
    message: str = ""
    request_id: str = ""
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    @classmethod
    def candidate_response(cls, candidate: Mapping[str, Any], *, provider: str = "manual", request_id: str = "") -> "ProviderResponse":
        return cls("candidate", candidate=dict(candidate), request_id=request_id, model_id=provider, output_tokens=len(canonical_bytes(candidate)))


class InventionProvider(Protocol):
    def propose(self, request: InventionRequest) -> ProviderResponse: ...


@dataclass(frozen=True)
class ModelBudget:
    max_attempts: int = 3
    max_input_tokens: int = 8192
    max_output_tokens: int = 8192
    max_latency_ms: int = 30_000
    max_cost_units: int = 0
    max_requests: int | None = None


@dataclass(frozen=True)
class AttemptRecord:
    attempt: int
    response_kind: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    candidate_hash: str | None
    report_hash: str | None
    diagnostics: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class CompilationResult:
    outcome: str
    candidate: Mapping[str, Any] | None
    candidate_hash: str | None
    report: Mapping[str, Any] | None
    attempts: tuple[AttemptRecord, ...]
    diagnostics: tuple[Mapping[str, Any], ...]
    fixture: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "candidate": self.candidate,
            "candidate_hash": self.candidate_hash,
            "report": self.report,
            "attempts": [asdict(attempt) for attempt in self.attempts],
            "diagnostics": list(self.diagnostics),
            "fixture": self.fixture,
            "repair_rounds": max(0, len(self.attempts) - 1),
        }


@dataclass(frozen=True)
class EvaluationTask:
    task_id: str
    prompt: str
    category: str
    expected: str
    fixture: bool = False


@dataclass(frozen=True)
class EvaluationReport:
    suite_hash: str
    total: int
    fixture_total: int
    eligible_total: int
    results: tuple[Mapping[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {"suite_hash": self.suite_hash, "total": self.total, "fixture_total": self.fixture_total, "eligible_total": self.eligible_total, "results": list(self.results)}


def guard_request(request: InventionRequest, *, max_prompt_chars: int = 8192, max_context_bytes: int = 64 * 1024) -> ProviderResponse | None:
    if not isinstance(request.prompt, str) or not request.prompt.strip():
        raise WFError("E_MODEL_INPUT", "prompt must be non-empty")
    if len(request.prompt) > max_prompt_chars:
        raise WFError("E_MODEL_INPUT", "prompt exceeds the input budget")
    try:
        context_value = dict(request.context) if isinstance(request.context, Mapping) else {}
        context_bytes = len(canonical_bytes(context_value))
    except WFError as exc:
        raise WFError("E_MODEL_INPUT", "context is not canonical JSON") from exc
    if context_bytes > max_context_bytes:
        raise WFError("E_MODEL_INPUT", "model context exceeds the input budget")
    # Secrets are rejected before any provider boundary.  This is a guardrail,
    # not a claim that a natural-language string can be perfectly classified.
    lowered = request.prompt.lower()
    if any(marker in lowered for marker in ("authorization:", "private_key", "private key", "seed phrase", "api_key=")):
        raise WFError("E_MODEL_SENSITIVE_INPUT", "sensitive credential material is not accepted")
    if any(marker in lowered for marker in ("teleport", "arbitrary script", "source quota", "increase source", "change source", "perpetual", "infinite product", "unlimited output", "real fluid", "unsupported fluid", "global control", "all entities", "modify verifier", "write sql", "execute python", "execute sql", "传送", "永动", "无限产物", "全局控制", "流体模拟")):
        return ProviderResponse("unsupported", code="E_UNSUPPORTED_INTENT", message="request requires an unsupported primitive", request_id=request.request_id, model_id="guard")
    return None


class FixtureProvider:
    """Explicit scenario fixtures; unknown free text is never table-matched."""

    def __init__(self, fixtures: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        self.fixtures = {str(key): dict(value) for key, value in (fixtures or {}).items()}

    def propose(self, request: InventionRequest) -> ProviderResponse:
        scenario = request.context.get("scenario_id") if isinstance(request.context, Mapping) else None
        if not isinstance(scenario, str) or scenario not in self.fixtures:
            return ProviderResponse("unsupported", code="OFFLINE_FIXTURE_ONLY", message="unknown offline fixture; remote understanding is disabled", request_id=request.request_id, model_id="fixture")
        return ProviderResponse("candidate", candidate=self.fixtures[scenario], request_id=request.request_id, model_id=f"fixture:{scenario}", input_tokens=0, output_tokens=len(canonical_bytes(self.fixtures[scenario])))


class ManualProvider:
    """Provider for a user-supplied candidate; it does not bypass validation."""

    def __init__(self, candidate: Mapping[str, Any]) -> None:
        self.candidate = dict(candidate)

    def propose(self, request: InventionRequest) -> ProviderResponse:
        return ProviderResponse("candidate", candidate=dict(self.candidate), request_id=request.request_id, model_id="manual", output_tokens=len(canonical_bytes(self.candidate)))


class RemoteProvider:
    """Explicit remote adapter boundary with an injected transport only."""

    def __init__(self, *, model_id: str | None = None, endpoint: str = "", enabled: bool = False, allowed_endpoints: Sequence[str] = (), transport: Callable[[Any], Any] | None = None, model: str | None = None, remote_enabled: bool | None = None, allowed_models: Sequence[str] = ()) -> None:
        self.model_id = model_id or model or "remote"
        self.endpoint = endpoint
        self.enabled = enabled if remote_enabled is None else remote_enabled
        self.allowed_endpoints = tuple(allowed_endpoints)
        self.allowed_models = tuple(allowed_models)
        self.transport = transport

    def propose(self, request: InventionRequest) -> ProviderResponse:
        if not self.enabled:
            return ProviderResponse("provider_error", code="E_REMOTE_DISABLED", message="remote model adapter is disabled", request_id=request.request_id, model_id=self.model_id)
        if self.allowed_models and self.model_id not in self.allowed_models:
            return ProviderResponse("provider_error", code="E_REMOTE_MODEL_DENIED", message="model is not allow-listed", request_id=request.request_id, model_id=self.model_id)
        if not self.allowed_endpoints or self.endpoint not in self.allowed_endpoints:
            return ProviderResponse("provider_error", code="REMOTE_ENDPOINT_DENIED", message="provider endpoint is not allow-listed", request_id=request.request_id, model_id=self.model_id)
        if self.transport is None:
            return ProviderResponse("provider_error", code="REMOTE_TRANSPORT_UNAVAILABLE", message="no transport was injected", request_id=request.request_id, model_id=self.model_id)
        payload = {"request_id": request.request_id, "prompt": request.prompt, "context": dict(request.context) if isinstance(request.context, Mapping) else {}, "capabilities": dict(request.capabilities), "policy_hash": request.policy_hash, "output_schema": dict(request.output_schema), "parent_hash": request.parent_hash, "attempt": request.attempt, "diagnostics": list(request.diagnostics)}
        try:
            response = self.transport(payload)
        except TypeError:
            response = self.transport(request)
        if isinstance(response, Mapping):
            response = ProviderResponse(kind=str(response.get("kind", "provider_error")), candidate=response.get("candidate"), code=response.get("code"), message=str(response.get("message", "")), request_id=str(response.get("request_id", request.request_id)), model_id=str(response.get("model_id", self.model_id)), input_tokens=int(response.get("input_tokens", 0)), output_tokens=int(response.get("output_tokens", 0)), latency_ms=int(response.get("latency_ms", 0)))
        if not isinstance(response, ProviderResponse):
            return ProviderResponse("provider_error", code="REMOTE_PROTOCOL", message="transport returned an invalid response", request_id=request.request_id, model_id=self.model_id)
        return response


class InventionCompiler:
    def __init__(self, provider: InventionProvider, catalog: Mapping[str, Any], *, budget: ModelBudget | None = None, limits: Mapping[str, Any] | None = None) -> None:
        self.provider = provider
        self.catalog = dict(catalog)
        self.budget = budget or ModelBudget()
        self.limits = dict(limits or {})

    def compile(self, request: InventionRequest) -> CompilationResult:
        guard_response = guard_request(request)
        if guard_response is not None:
            detail = {"code": guard_response.code or "E_UNSUPPORTED_INTENT", "message": guard_response.message}
            return CompilationResult("unsupported", None, None, None, (AttemptRecord(1, guard_response.kind, guard_response.model_id, 0, 0, 0, None, None, (detail,)),), (detail,), False)
        max_attempts = max(1, min(3, int(self.budget.max_requests if self.budget.max_requests is not None else self.budget.max_attempts)))
        attempts: list[AttemptRecord] = []
        diagnostics: list[Mapping[str, Any]] = []
        current = request
        fixture = False
        last_candidate: Mapping[str, Any] | None = None
        last_hash: str | None = None
        last_report: Mapping[str, Any] | None = None
        total_input_tokens = 0
        total_output_tokens = 0
        for attempt_no in range(max_attempts):
            started = time.monotonic()
            try:
                if attempt_no and hasattr(self.provider, "repair") and last_candidate is not None:
                    response = self.provider.repair(current, last_candidate, last_report or {})  # type: ignore[attr-defined]
                else:
                    response = self.provider.propose(current)
            except Exception as exc:
                detail = {"code": "E_PROVIDER_ERROR", "message": str(exc)}
                diagnostics.append(detail)
                attempts.append(AttemptRecord(attempt_no + 1, "provider_error", getattr(self.provider, "model_id", "provider"), 0, 0, int((time.monotonic() - started) * 1000), None, None, (detail,)))
                return CompilationResult("provider_error", last_candidate, last_hash, last_report, tuple(attempts), tuple(diagnostics), fixture)
            if isinstance(response, Mapping):
                response = ProviderResponse(kind=str(response.get("kind", "provider_error")), candidate=response.get("candidate"), code=response.get("code"), message=str(response.get("message", "")), request_id=str(response.get("request_id", request.request_id)), model_id=str(response.get("model_id", "remote")), input_tokens=int(response.get("input_tokens", 0)), output_tokens=int(response.get("output_tokens", 0)), latency_ms=int(response.get("latency_ms", 0)))
            elapsed = int((time.monotonic() - started) * 1000)
            latency = response.latency_ms or elapsed
            fixture = fixture or response.model_id.startswith("fixture:")
            total_input_tokens += max(0, response.input_tokens)
            total_output_tokens += max(0, response.output_tokens)
            if response.input_tokens > self.budget.max_input_tokens or response.output_tokens > self.budget.max_output_tokens or total_input_tokens > self.budget.max_input_tokens or total_output_tokens > self.budget.max_output_tokens or latency > self.budget.max_latency_ms:
                detail = {"code": "E_MODEL_BUDGET", "message": "provider response exceeded model budget"}
                diagnostics.append(detail)
                attempts.append(AttemptRecord(attempt_no + 1, response.kind, response.model_id, response.input_tokens, response.output_tokens, latency, None, None, (detail,)))
                break
            if response.kind != "candidate" or not isinstance(response.candidate, Mapping):
                detail = {"code": response.code or "E_PROVIDER", "message": response.message or "provider did not return a candidate"}
                diagnostics.append(detail)
                attempts.append(AttemptRecord(attempt_no + 1, response.kind, response.model_id, response.input_tokens, response.output_tokens, latency, None, None, (detail,)))
                if response.kind in {"unsupported", "needs_clarification", "provider_error"}:
                    return CompilationResult(response.kind, None, None, None, tuple(attempts), tuple(diagnostics), fixture)
                break
            last_candidate = dict(response.candidate)
            try:
                ir = compile_blueprint(last_candidate, self.catalog)
                last_hash = blueprint_hash(ir)
                report = verify_blueprint(ir.model_dump(mode="json"), self.catalog, self.limits)
                last_report = report
                if report.get("outcome") == "passed":
                    attempts.append(AttemptRecord(attempt_no + 1, response.kind, response.model_id, response.input_tokens, response.output_tokens, latency, last_hash, str(report.get("report_hash")), ()))
                    return CompilationResult("validated", ir.model_dump(mode="json"), last_hash, report, tuple(attempts), tuple(diagnostics), fixture)
                detail = {"code": "E_CASE_FAILED", "message": "sandbox validation rejected candidate"}
                diagnostics.append(detail)
            except WFError as exc:
                detail = {"code": exc.code, "message": str(exc)}
                diagnostics.append(detail)
            attempts.append(AttemptRecord(attempt_no + 1, response.kind, response.model_id, response.input_tokens, response.output_tokens, latency, last_hash, str(last_report.get("report_hash")) if last_report else None, (diagnostics[-1],)))
            current = InventionRequest(prompt=request.prompt, context=request.context, capabilities=request.capabilities, policy_hash=request.policy_hash, output_schema=request.output_schema, parent_hash=request.parent_hash, attempt=attempt_no + 1, diagnostics=tuple(diagnostics[-4:]), request_id=request.request_id)
        return CompilationResult("rejected", last_candidate, last_hash, last_report, tuple(attempts), tuple(diagnostics), fixture)


def build_m4_evaluation_suite() -> tuple[EvaluationTask, ...]:
    tasks: list[EvaluationTask] = []
    for index in range(10):
        tasks.append(EvaluationTask(f"fixture-{index + 1:02d}", f"fixture scenario {index + 1:02d}", "basic", "candidate", True))
    for index in range(10):
        tasks.append(EvaluationTask(f"novel-{index + 1:02d}", f"combine source {index + 1} with condition {index % 4} and connection {index % 3}", "novel", "candidate", False))
    for index, prompt in enumerate(("teleport inventory", "run arbitrary script", "increase source quota", "create perpetual energy", "make infinite product", "unsupported fluid", "global control", "modify verifier", "write SQL", "send private key"), 1):
        tasks.append(EvaluationTask(f"reject-{index:02d}", prompt, "reject", "unsupported", False))
    return tuple(tasks)


def evaluate_suite(provider: InventionProvider, catalog: Mapping[str, Any], *, budget: ModelBudget | None = None) -> EvaluationReport:
    tasks = build_m4_evaluation_suite()
    results: list[Mapping[str, Any]] = []
    for task in tasks:
        request = InventionRequest(prompt=task.prompt, context={"scenario_id": task.task_id}, request_id=task.task_id)
        try:
            outcome = InventionCompiler(provider, catalog, budget=budget).compile(request)
            results.append({"task_id": task.task_id, "category": task.category, "fixture": task.fixture, "expected": task.expected, "prompt": task.prompt, "candidate": outcome.candidate, "candidate_hash": outcome.candidate_hash, "report": outcome.report, "outcome": outcome.outcome, "passed": outcome.outcome in {"validated", "candidate"} if task.expected == "candidate" else outcome.outcome in {"unsupported", "needs_clarification", "rejected"}, "diagnostics": list(outcome.diagnostics), "attempts": len(outcome.attempts), "repair_rounds": max(0, len(outcome.attempts) - 1)})
        except WFError as exc:
            rejected_input = exc.code in {"E_MODEL_INPUT", "E_MODEL_SENSITIVE_INPUT", "E_UNSUPPORTED_INTENT"}
            terminal = "unsupported" if rejected_input else "provider_error"
            results.append({"task_id": task.task_id, "category": task.category, "fixture": task.fixture, "expected": task.expected, "prompt": task.prompt, "candidate": None, "candidate_hash": None, "report": None, "outcome": terminal, "passed": bool(rejected_input and task.expected == "unsupported"), "diagnostics": [{"code": exc.code, "message": str(exc)}], "attempts": 0, "repair_rounds": 0})
    suite_hash = digest("m4-suite", [asdict(task) for task in tasks])
    fixture_total = sum(1 for task in tasks if task.fixture)
    return EvaluationReport(suite_hash, len(tasks), fixture_total, len(tasks) - fixture_total, tuple(results))


__all__ = ["AttemptRecord", "CompilationResult", "EvaluationReport", "EvaluationTask", "FixtureProvider", "InventionCompiler", "InventionProvider", "InventionRequest", "ManualProvider", "ModelBudget", "ProviderResponse", "RemoteProvider", "build_m4_evaluation_suite", "evaluate_suite", "guard_request"]
