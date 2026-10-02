"""Offline InventionRegistry v1 semantics.

This module mirrors the intentionally small contract surface in
``docs/BLOCKCHAIN.md``.  It is a local development-chain model, not a wallet,
RPC client, or claim of an audited deployed contract.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .keccak import keccak256

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_DOMAIN = b"WF:invention:v1"


class RegistryError(ValueError):
    code = "E_REGISTRY"


class InvalidRegistration(RegistryError):
    code = "E_INVALID_REGISTRATION"


class ParentNotFound(RegistryError):
    code = "E_PARENT_NOT_FOUND"


class ConflictingRegistration(RegistryError):
    code = "E_CONFLICTING_REGISTRATION"


class NotRegistrant(RegistryError):
    code = "E_NOT_REGISTRANT"


class ChainUnavailable(RegistryError):
    code = "E_CHAIN_UNAVAILABLE"


@dataclass(frozen=True)
class Invention:
    invention_id: str
    registrant: str
    pack_hash: str
    license_hash: str
    registered_at_block: int
    parent_ids: tuple[str, ...]
    content_uri: str
    deprecated: bool = False

    @property
    def parent_count(self) -> int:
        return len(self.parent_ids)


def _hex32(value: str, field: str) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise InvalidRegistration(f"{field} must be a 32-byte 0x-prefixed hash")
    result = value.lower()
    if result == "0x" + "0" * 64:
        raise InvalidRegistration(f"{field} must not be zero")
    return result


def _address(value: str) -> str:
    if not isinstance(value, str) or not _ADDRESS_RE.fullmatch(value):
        raise InvalidRegistration("registrant must be a 20-byte 0x-prefixed address")
    return value.lower()


def _abi_encode_id(chain_id: int, contract: str, registrant: str, pack_hash: str) -> bytes:
    if not isinstance(chain_id, int) or chain_id < 0 or chain_id >= 2**256:
        raise InvalidRegistration("chain_id is outside uint256")
    # abi.encode(string,uint256,address,address,bytes32): a 5-word head followed by
    # the length-padded dynamic string tail.
    string_tail = len(_DOMAIN).to_bytes(32, "big") + _DOMAIN + b"\0" * ((32 - len(_DOMAIN) % 32) % 32)
    head = (
        (160).to_bytes(32, "big")
        + chain_id.to_bytes(32, "big")
        + bytes.fromhex(_address(contract)[2:]).rjust(32, b"\0")
        + bytes.fromhex(_address(registrant)[2:]).rjust(32, b"\0")
        + bytes.fromhex(_hex32(pack_hash, "pack_hash")[2:])
    )
    return head + string_tail


def invention_id(chain_id: int, contract: str, registrant: str, pack_hash: str) -> str:
    return "0x" + keccak256(_abi_encode_id(chain_id, contract, registrant, pack_hash)).hex()


class InventionRegistry:
    """Deterministic local model of the v1 registry contract."""

    def __init__(self, *, chain_id: int = 31337, contract_address: str = "0x0000000000000000000000000000000000001001") -> None:
        self.chain_id = chain_id
        self.contract_address = _address(contract_address)
        self.block_number = 0
        self._records: dict[str, Invention] = {}
        self._by_author_pack: dict[tuple[str, str], str] = {}
        self.events: list[dict[str, Any]] = []

    def _uri(self, content_uri: str) -> str:
        if not isinstance(content_uri, str) or len(content_uri.encode("utf-8")) > 256:
            raise InvalidRegistration("contentURI must be at most 256 UTF-8 bytes")
        if not (content_uri.startswith("ipfs://") or content_uri.startswith("https://")):
            raise InvalidRegistration("contentURI must use ipfs:// or https://")
        if any(ch in content_uri for ch in ("\r", "\n", "\0")):
            raise InvalidRegistration("contentURI contains a control character")
        return content_uri

    def _parents(self, parent_ids: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        if not isinstance(parent_ids, (list, tuple)) or len(parent_ids) > 4:
            raise InvalidRegistration("at most four parents are allowed")
        normalized = tuple(_hex32(item, "parent_id") for item in parent_ids)
        if tuple(sorted(normalized)) != normalized or len(set(normalized)) != len(normalized):
            raise InvalidRegistration("parent IDs must be strictly ascending and unique")
        for parent in normalized:
            if parent not in self._records:
                raise ParentNotFound(parent)
        return normalized

    def register(
        self,
        *,
        registrant: str,
        pack_hash: str,
        parent_ids: list[str] | tuple[str, ...] = (),
        license_hash: str,
        content_uri: str,
        value: int = 0,
    ) -> str:
        author = _address(registrant)
        pack = _hex32(pack_hash, "pack_hash")
        license_value = _hex32(license_hash, "license_hash")
        if value != 0:
            raise InvalidRegistration("register is nonpayable")
        parents = self._parents(parent_ids)
        uri = self._uri(content_uri)
        key = (author, pack)
        existing_id = self._by_author_pack.get(key)
        if existing_id is not None:
            existing = self._records[existing_id]
            if (existing.parent_ids, existing.license_hash, existing.content_uri) != (parents, license_value, uri):
                raise ConflictingRegistration("same author and pack hash has different registration data")
            return existing_id

        self.block_number += 1
        identifier = invention_id(self.chain_id, self.contract_address, author, pack)
        record = Invention(identifier, author, pack, license_value, self.block_number, parents, uri)
        self._records[identifier] = record
        self._by_author_pack[key] = identifier
        self.events.append({"type": "InventionRegistered", "invention_id": identifier, "registrant": author, "pack_hash": pack, "parent_ids": list(parents), "license_hash": license_value, "content_uri": uri})
        return identifier

    def deprecate(self, *, registrant: str, invention_id_value: str) -> None:
        author = _address(registrant)
        record = self._records.get(invention_id_value.lower())
        if record is None:
            raise ParentNotFound(invention_id_value)
        if record.registrant != author:
            raise NotRegistrant("only the registrant may deprecate")
        if record.deprecated:
            return
        self._records[record.invention_id] = Invention(**{**record.__dict__, "deprecated": True})
        self.events.append({"type": "InventionDeprecated", "invention_id": record.invention_id, "registrant": author})

    def get_invention(self, invention_id_value: str) -> Invention:
        try:
            return self._records[invention_id_value.lower()]
        except KeyError as exc:
            raise ParentNotFound(invention_id_value) from exc

    def get_parents(self, invention_id_value: str) -> tuple[str, ...]:
        return self.get_invention(invention_id_value).parent_ids

    def get_content_uri(self, invention_id_value: str) -> str:
        return self.get_invention(invention_id_value).content_uri

    def snapshot(self) -> tuple[Invention, ...]:
        return tuple(self._records[key] for key in sorted(self._records))


class LocalDevelopmentChain:
    """Explicit local-chain façade; it never contacts an RPC endpoint."""

    chain_id = 31337

    def __init__(self, *, rpc_enabled: bool = False) -> None:
        self.rpc_enabled = rpc_enabled
        self.registry = InventionRegistry(chain_id=self.chain_id)

    def ensure_available(self) -> None:
        if self.rpc_enabled:
            raise ChainUnavailable("RPC integration is intentionally unavailable in the offline build")

    def prepare_register(self, *, registrant: str, pack_hash: str, parent_ids: list[str] | tuple[str, ...], license_hash: str, content_uri: str) -> dict[str, Any]:
        self.ensure_available()
        author = _address(registrant)
        pack = _hex32(pack_hash, "pack_hash")
        license_value = _hex32(license_hash, "license_hash")
        parents = self.registry._parents(parent_ids)
        uri = self.registry._uri(content_uri)
        return {"chain_id": self.chain_id, "contract": self.registry.contract_address, "method": "register", "registrant": author, "pack_hash": pack, "parent_ids": list(parents), "license_hash": license_value, "content_uri": uri, "value": 0}

    def submit_register(self, **kwargs: Any) -> str:
        self.ensure_available()
        return self.registry.register(**kwargs)


__all__ = ["ChainUnavailable", "ConflictingRegistration", "Invention", "InventionRegistry", "InvalidRegistration", "LocalDevelopmentChain", "NotRegistrant", "ParentNotFound", "invention_id"]
