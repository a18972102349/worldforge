from __future__ import annotations

import pytest

from wf.chain import (
    ConflictingRegistration,
    InventionRegistry,
    InvalidRegistration,
    LocalDevelopmentChain,
    NotRegistrant,
    ParentNotFound,
    keccak256,
)

ALICE = "0x00000000000000000000000000000000000000a1"
BOB = "0x00000000000000000000000000000000000000b2"
PACK_A = "0x" + "11" * 32
PACK_B = "0x" + "22" * 32
LICENSE = "0x" + "33" * 32


def test_ethereum_keccak_vectors() -> None:
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"


def test_registration_is_idempotent_and_other_authors_can_declare_same_pack() -> None:
    registry = InventionRegistry()
    first = registry.register(registrant=ALICE, pack_hash=PACK_A, license_hash=LICENSE, content_uri="ipfs://wf/a")
    assert registry.register(registrant=ALICE, pack_hash=PACK_A, license_hash=LICENSE, content_uri="ipfs://wf/a") == first
    assert len(registry.events) == 1
    second = registry.register(registrant=BOB, pack_hash=PACK_A, license_hash=LICENSE, content_uri="ipfs://wf/a")
    assert second != first
    assert registry.get_invention(second).registrant == BOB


def test_conflicting_duplicate_and_parent_rules() -> None:
    registry = InventionRegistry()
    parent = registry.register(registrant=ALICE, pack_hash=PACK_A, license_hash=LICENSE, content_uri="https://example.test/a")
    with pytest.raises(ConflictingRegistration):
        registry.register(registrant=ALICE, pack_hash=PACK_A, license_hash=LICENSE, content_uri="ipfs://different")
    child = registry.register(registrant=ALICE, pack_hash=PACK_B, parent_ids=[parent], license_hash=LICENSE, content_uri="ipfs://wf/b")
    assert registry.get_parents(child) == (parent,)
    with pytest.raises(ParentNotFound):
        registry.register(registrant=BOB, pack_hash="0x" + "44" * 32, parent_ids=["0x" + "55" * 32], license_hash=LICENSE, content_uri="ipfs://wf/c")
    with pytest.raises(InvalidRegistration):
        registry.register(registrant=BOB, pack_hash="0x" + "44" * 32, parent_ids=[parent, parent], license_hash=LICENSE, content_uri="ipfs://wf/c")


def test_validation_boundaries_and_deprecate_authorization() -> None:
    registry = InventionRegistry()
    with pytest.raises(InvalidRegistration):
        registry.register(registrant=ALICE, pack_hash=PACK_A, license_hash=LICENSE, content_uri="file:///secret")
    with pytest.raises(InvalidRegistration):
        registry.register(registrant=ALICE, pack_hash=PACK_A, license_hash=LICENSE, content_uri="https://" + "a" * 260)
    identifier = registry.register(registrant=ALICE, pack_hash=PACK_A, license_hash=LICENSE, content_uri="ipfs://wf/a")
    with pytest.raises(NotRegistrant):
        registry.deprecate(registrant=BOB, invention_id_value=identifier)
    registry.deprecate(registrant=ALICE, invention_id_value=identifier)
    assert registry.get_invention(identifier).deprecated is True


def test_local_chain_prepare_has_no_wallet_or_rpc_and_rejects_rpc_mode() -> None:
    chain = LocalDevelopmentChain()
    tx = chain.prepare_register(registrant=ALICE, pack_hash=PACK_A, parent_ids=[], license_hash=LICENSE, content_uri="ipfs://wf/a")
    assert tx["chain_id"] == 31337
    assert "private_key" not in tx
    identifier = chain.submit_register(registrant=ALICE, pack_hash=PACK_A, license_hash=LICENSE, content_uri="ipfs://wf/a")
    assert chain.registry.get_invention(identifier).pack_hash == PACK_A
    with pytest.raises(Exception):
        LocalDevelopmentChain(rpc_enabled=True).prepare_register(registrant=ALICE, pack_hash=PACK_A, parent_ids=[], license_hash=LICENSE, content_uri="ipfs://wf/a")
