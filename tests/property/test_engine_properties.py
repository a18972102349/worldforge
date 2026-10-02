"""Property checks for deterministic scarce-energy allocation.

The allocator is the shared primitive used by the engine for environment and
mechanical pools.  Keeping these checks independent of the CLI catches an
over-allocation bug before a full world run is attempted.
"""
from __future__ import annotations

from pathlib import Path

from hypothesis import given, strategies as st

from wf.compiler import compile_blueprint
from wf.core.allocator import capped_fair_share
from wf.core import Engine, blueprint_hash, strict_loads


ROOT = Path(__file__).resolve().parents[2]


def _setup_engine(*, renamed: bool = False, reshaped: bool = False) -> Engine:
    world = strict_loads((ROOT / "examples" / "fixtures" / "world.demo.json").read_bytes())
    catalog = strict_loads((ROOT / "examples" / "fixtures" / "catalog.json").read_bytes())
    raw = strict_loads((ROOT / "examples" / "blueprints" / "river_mill.json").read_bytes())
    if renamed:
        raw["name"] = "arbitrary-display-label"
    if reshaped:
        raw["nodes"][0]["visual"]["shape"] = "block"
        raw["nodes"][1]["visual"]["shape"] = "wheel"
    bp = compile_blueprint(raw, catalog).model_dump(mode="json")
    h = blueprint_hash(bp)
    engine = Engine(world, catalog)
    engine.register_blueprint(h, bp)
    engine.step([
        {"command_id": "install", "server_seq": 1, "effective_tick": 1, "kind": "INSTALL_BLUEPRINT", "payload": {"blueprint_hash": h}},
        {"command_id": "place", "server_seq": 2, "effective_tick": 1, "kind": "PLACE", "payload": {"blueprint_hash": h, "origin": [2, 1]}},
        {"command_id": "deposit", "server_seq": 3, "effective_tick": 1, "kind": "DEPOSIT", "payload": {"entity_id": "e2/mill", "item": "grain", "amount": 10}},
    ])
    return engine


@st.composite
def request_maps(draw):
    # IDs intentionally come from a small fixed set so insertion order can be
    # varied independently of lexicographic order.
    ids = draw(st.lists(st.sampled_from(["e3", "e1", "e2", "e9"]), min_size=1, max_size=4, unique=True))
    values = draw(st.lists(st.integers(min_value=0, max_value=32), min_size=len(ids), max_size=len(ids)))
    return dict(zip(ids, values))


@given(requests=request_maps(), supply=st.integers(min_value=0, max_value=128), tick=st.integers(min_value=1, max_value=200))
def test_capped_share_conserves_supply_and_respects_caps(requests, supply, tick):
    result = capped_fair_share(requests, supply, tick)

    assert set(result) == set(requests)
    assert all(0 <= result[key] <= requests[key] for key in requests)
    assert sum(result.values()) <= supply
    if sum(requests.values()) >= supply:
        assert sum(result.values()) == supply


@given(requests=request_maps(), supply=st.integers(min_value=0, max_value=128), tick=st.integers(min_value=1, max_value=200))
def test_capped_share_is_insertion_order_independent(requests, supply, tick):
    # Dict insertion order is deliberately reversed; only sorted request IDs
    # and the tick rotation are allowed to affect allocation.
    reversed_requests = dict(reversed(list(requests.items())))
    assert capped_fair_share(requests, supply, tick) == capped_fair_share(reversed_requests, supply, tick)


def test_remainder_priority_rotates_by_tick():
    requests = {"e1": 1, "e2": 1, "e3": 1}
    assert capped_fair_share(requests, 1, tick=1) == {"e1": 1, "e2": 0, "e3": 0}
    assert capped_fair_share(requests, 1, tick=2) == {"e1": 0, "e2": 1, "e3": 0}
    assert capped_fair_share(requests, 1, tick=3) == {"e1": 0, "e2": 0, "e3": 1}


@given(prefix=st.integers(min_value=0, max_value=24))
def test_any_empty_tick_prefix_preserves_energy_conservation(prefix):
    engine = _setup_engine()
    for _ in range(prefix):
        result = engine.step([])
        ledger = result.summary["energy_ledger"]
        assert ledger["external_withdrawn_eu"] == ledger["conversion_loss_eu"] + ledger["work_consumed_eu"] + ledger["unused_dissipated_eu"]


def test_ten_thousand_ticks_keep_energy_ledger_conserved():
    engine = _setup_engine()
    for _ in range(9_999):
        engine.step([])
    ledger = engine.snapshot()["energy_ledger"]
    assert ledger["external_withdrawn_eu"] == ledger["conversion_loss_eu"] + ledger["work_consumed_eu"] + ledger["unused_dissipated_eu"]


def test_display_name_and_shape_do_not_change_simulation_outcome():
    plain = _setup_engine()
    renamed = _setup_engine(renamed=True, reshaped=True)
    for _ in range(11):
        plain.step([])
        renamed.step([])
    left, right = plain.snapshot(), renamed.snapshot()
    assert left["energy_ledger"] == right["energy_ledger"]
    assert left["entities"]["e2/mill"]["on_hand"] == right["entities"]["e2/mill"]["on_hand"]
    assert left["entities"]["e2/mill"]["active_job"] == right["entities"]["e2/mill"]["active_job"]

