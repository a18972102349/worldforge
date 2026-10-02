"""Optional, offline-only rule-pack registration primitives.

The chain package deliberately contains no RPC client, wallet, signing key, or
network dependency.  :class:`InventionRegistry` is a deterministic local
development-chain model used to test the v1 contract semantics.
"""

from .keccak import keccak256
from .registry import (
    ChainUnavailable,
    ConflictingRegistration,
    Invention,
    InventionRegistry,
    InvalidRegistration,
    LocalDevelopmentChain,
    NotRegistrant,
    ParentNotFound,
)

__all__ = [
    "ChainUnavailable",
    "ConflictingRegistration",
    "Invention",
    "InventionRegistry",
    "InvalidRegistration",
    "LocalDevelopmentChain",
    "NotRegistrant",
    "ParentNotFound",
    "keccak256",
]
