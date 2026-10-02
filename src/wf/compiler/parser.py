"""Compatibility exports for strict JSON parsing."""
from wf.core.canonical import canonical_bytes, strict_loads
from .compiler import compile_blueprint

__all__ = ["strict_loads", "canonical_bytes", "compile_blueprint"]
