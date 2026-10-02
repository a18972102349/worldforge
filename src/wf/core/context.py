"""Versioned inputs bound into a sandbox validation report."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import digest


ENGINE_VERSION = "wf-engine-0.1"
VERIFIER_VERSION = "wf-verifier-0.1"
SUITE_VERSION = "wf-suite-m2-0.1"


@dataclass(frozen=True)
class ValidationContext:
    """Immutable hashes identifying every validation input."""

    engine_hash: str
    catalog_hash: str
    policy_hash: str
    suite_hash: str
    verifier_hash: str

    @classmethod
    def create(
        cls,
        catalog: Mapping[str, Any],
        limits: Mapping[str, Any] | None = None,
        suite: Any | None = None,
        *,
        engine_version: str = ENGINE_VERSION,
        verifier_version: str = VERIFIER_VERSION,
    ) -> "ValidationContext":
        policy = dict(limits or default_limits())
        # WF-CJSON has integer-only numbers.  Accept the ergonomic
        # ``timeout_seconds`` input but bind the policy in milliseconds.
        if "timeout_seconds" in policy:
            seconds = policy.pop("timeout_seconds")
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
                raise ValueError("timeout_seconds must be positive")
            policy["timeout_ms"] = int(seconds * 1000)
        suite_data = suite if suite is not None else {"suite_version": SUITE_VERSION}
        return cls(
            engine_hash=digest("engine", {"version": engine_version}),
            catalog_hash=digest("catalog", catalog),
            policy_hash=digest("policy", policy),
            suite_hash=digest("suite", suite_data),
            verifier_hash=digest("verifier", {"version": verifier_version}),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "engine_hash": self.engine_hash,
            "catalog_hash": self.catalog_hash,
            "policy_hash": self.policy_hash,
            "suite_hash": self.suite_hash,
            "verifier_hash": self.verifier_hash,
        }

    @property
    def context_hash(self) -> str:
        return digest("validation-context", self.as_dict())


def default_limits() -> dict[str, Any]:
    return {
        "max_cases": 10,
        "max_ticks": 120,
        "max_entities": 128,
        "timeout_ms": 2000,
    }


__all__ = [
    "ENGINE_VERSION",
    "VERIFIER_VERSION",
    "SUITE_VERSION",
    "ValidationContext",
    "default_limits",
]
