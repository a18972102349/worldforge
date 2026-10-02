"""WF-CJSON-1 parsing, canonicalisation and domain separated hashes."""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from typing import Any

from .errors import WFError

MAX_SAFE_INT = 2**53 - 1
MAX_JSON_BYTES = 64 * 1024


def _reject_float(token: str) -> Any:
    raise WFError("E_NUMERIC_TYPE", f"floating point token {token!r}")


def _reject_constant(token: str) -> Any:
    # NaN and infinities are JSON extensions and are rejected at the parse gate.
    raise WFError("E_PARSE", f"non-JSON numeric constant {token!r}")


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise WFError("E_DUPLICATE_KEY", f"duplicate object key {key!r}")
        out[key] = value
    return out


def _check_string(value: str, *, key: bool = False, path: str = "") -> None:
    if key and not value.isascii():
        raise WFError("E_CANONICAL_INPUT", "object keys must contain ASCII only", path)
    if unicodedata.normalize("NFC", value) != value:
        raise WFError("E_CANONICAL_INPUT", "string must be NFC normalized", path)
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in value):
        raise WFError("E_CANONICAL_INPUT", "isolated surrogate is not allowed", path)


def check_canonical_value(value: Any, *, path: str = "") -> None:
    """Validate the in-memory subset accepted by WF-CJSON-1.

    ``type(value)`` checks are intentional: bool is a subclass of int in
    Python, but the wire format treats them as distinct JSON types.
    """
    # Compiled Pydantic IR is intentionally accepted as a convenience for
    # callers calculating a blueprint hash after compilation.  The conversion
    # is read-only and still passes through every canonical check below.
    if hasattr(value, "model_dump") and callable(value.model_dump):
        value = value.model_dump(mode="json")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise WFError("E_INTEGER_RANGE", "integer exceeds safe JSON range", path)
        return
    if type(value) is float:
        # Keep this explicit for callers passing Python values directly rather
        # than through strict_loads.
        raise WFError("E_NUMERIC_TYPE", "floating point values are not allowed", path)
    if isinstance(value, str):
        _check_string(value, path=path)
        return
    if isinstance(value, list) or isinstance(value, tuple):
        for index, child in enumerate(value):
            check_canonical_value(child, path=f"{path}/{index}")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                raise WFError("E_CANONICAL_INPUT", "object keys must be strings", path)
            child_path = f"{path}/{key}" if path else f"/{key}"
            _check_string(key, key=True, path=child_path)
            check_canonical_value(child, path=child_path)
        return
    raise WFError("E_CANONICAL_INPUT", f"unsupported value type {type(value).__name__}", path)


def strict_loads(source: str | bytes | bytearray) -> Any:
    """Parse strict UTF-8 JSON and return only the WF-CJSON-1 value subset."""
    if isinstance(source, str):
        # UTF-8 has no encoding step for Python text, so measure the actual wire
        # representation just as a file reader would.
        try:
            raw = source.encode("utf-8", "strict")
        except UnicodeError as exc:
            raise WFError("E_PARSE", "input is not valid UTF-8 text") from exc
    elif isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    else:
        raise WFError("E_PARSE", "strict_loads expects str or bytes")
    if len(raw) > MAX_JSON_BYTES:
        raise WFError("E_COMPLEXITY_BUDGET", "JSON input exceeds 64 KiB")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise WFError("E_PARSE", "input is not valid UTF-8") from exc
    try:
        value = json.loads(
            text,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_pairs,
        )
    except WFError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise WFError("E_PARSE", str(exc)) from exc
    check_canonical_value(value)
    return value


def canonical_bytes(data: Any) -> bytes:
    """Return deterministic WF-CJSON-1 UTF-8 bytes for ``data``."""
    if hasattr(data, "model_dump") and callable(data.model_dump):
        data = data.model_dump(mode="json")
    check_canonical_value(data)
    try:
        encoded = json.dumps(
            data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8", "strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise WFError("E_CANONICAL_INPUT", str(exc)) from exc
    return encoded


def digest(domain: str, data: Any) -> str:
    """SHA-256 over ``WF:{domain}:v1\\0`` followed by canonical bytes."""
    if not isinstance(domain, str) or not domain or not domain.isascii():
        raise WFError("E_CANONICAL_INPUT", "digest domain must be non-empty ASCII")
    prefix = f"WF:{domain}:v1\0".encode("ascii")
    return hashlib.sha256(prefix + canonical_bytes(data)).hexdigest()


def blueprint_hash(data: Any) -> str:
    return digest("blueprint", data)


__all__ = [
    "MAX_SAFE_INT",
    "MAX_JSON_BYTES",
    "check_canonical_value",
    "strict_loads",
    "canonical_bytes",
    "digest",
    "blueprint_hash",
]
