"""Errors shared by the WorldForge compiler and runtime.

The compiler deliberately exposes one small, serialisable error type.  Callers
can inspect ``code`` and ``path`` without parsing the human readable message.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(eq=False)
class WFError(ValueError):
    """A deterministic user/input error.

    ``path`` is a JSON pointer-like location when one is available.  It is
    intentionally optional because lexical errors (for example duplicate JSON
    members) may be detected before an object tree exists.
    """

    code: str
    message: str
    path: str = ""

    def __post_init__(self) -> None:
        # ValueError uses ``args`` for a useful default repr and for tools which
        # only know standard exception APIs.
        ValueError.__init__(self, self.__str__())

    def __str__(self) -> str:
        location = f" at {self.path}" if self.path else ""
        return f"{self.code}{location}: {self.message}"

    def as_dict(self) -> dict[str, str]:
        result = {"code": self.code, "message": self.message}
        if self.path:
            result["path"] = self.path
        return result
