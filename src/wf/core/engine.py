"""Deterministic integer tick engine for the mechanical-v0 IR."""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping

from .allocator import capped_fair_share
from .canonical import digest
from .errors import WFError


@dataclass(frozen=True)
class TickResult:
    tick: int
    state_hash: str
    events: list[dict[str, Any]]
    command_results: list[dict[str, Any]]
    summary: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"tick": self.tick, "state_hash": self.state_hash, "events": self.events, "command_results": self.command_results, "summary": self.summary}


# Compatibility names used by callers that model the mutable simulation as a
# World/WorldState pair.  The authoritative value remains the engine's plain
# JSON state, which makes snapshots and replay portable.

def _as_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return deepcopy(value)


class Engine:
    """Single-writer simulation actor.

    The engine never branches on a display name or visual shape.  Only the
    compiled IR primitives and catalog data influence state transitions.
    """
    def __init__(self, config: Mapping[str, Any], catalog: Mapping[str, Any], state: Mapping[str, Any] | None = None, blueprints: Mapping[str, Any] | None = None):
        self.config = deepcopy(dict(config))
        self.catalog = deepcopy(dict(catalog))
        self.blueprints: dict[str, dict[str, Any]] = {k: _as_dict(v) for k, v in (blueprints or {}).items()}
        self.frozen = False
        self.state: dict[str, Any] = deepcopy(dict(state)) if state is not None else self._initial_state()
        if isinstance(self.state.get("entities"), list):
            self.state["entities"] = {e["entity_id"]: e for e in self.state["entities"] if isinstance(e, dict) and "entity_id" in e}
        # Snapshots from older callers may omit optional ledgers; fill only
        # deterministic defaults so a malformed command cannot mutate globals.
        self.state.setdefault("tick", 0); self.state.setdefault("rules_epoch", 1)
        self.state.setdefault("installed_blueprint_hashes", []); self.state.setdefault("disabled_blueprint_hashes", [])
        self.state.setdefault("map", deepcopy(self.config.get("map", {}))); self.state.setdefault("sources", deepcopy(self.config.get("sources", [])))
        self.state.setdefault("warehouse", deepcopy(self.config.get("warehouse", {}))); self.state.setdefault("entities", {})
        self.state.setdefault("mechanical_links", []); self.state.setdefault("resource_ledger", {"initial_mass_tags": {}, "construction_consumed": {}, "processed_consumed": {}})
        self.state.setdefault("energy_ledger", {"external_withdrawn_eu": 0, "conversion_loss_eu": 0, "work_consumed_eu": 0, "unused_dissipated_eu": 0})
        self.state.setdefault("deterministic_counters", {"placement_server_seq": 0})

    def _normalize_entities(self, entities: Any) -> dict[str, Any]:
        if isinstance(entities, dict):
            return deepcopy(entities)
        if isinstance(entities, list):
            return {str(e.get("entity_id")): deepcopy(e) for e in entities if isinstance(e, dict) and e.get("entity_id") is not None}
        return {}

    def _initial_state(self) -> dict[str, Any]:
        warehouse = deepcopy(self.config.get("warehouse", {}))
        state = {
            "tick": int(self.config.get("tick", 0)),
            "rules_epoch": int(self.config.get("rules_epoch", 1)),
            "installed_blueprint_hashes": [],
            "disabled_blueprint_hashes": [],
            "map": deepcopy(self.config.get("map", {"width": 32, "height": 32, "default_tile": "land", "overrides": []})),
            "sources": deepcopy(self.config.get("sources", [])),
            "warehouse": warehouse,
            "entities": self._normalize_entities(self.config.get("entities", {})),
            "mechanical_links": [list(x) for x in self.config.get("mechanical_links", [])],
            "resource_ledger": {"initial_mass_tags": {}, "construction_consumed": {}, "processed_consumed": {}},
            "energy_ledger": {"external_withdrawn_eu": 0, "conversion_loss_eu": 0, "work_consumed_eu": 0, "unused_dissipated_eu": 0},
            "deterministic_counters": {"placement_server_seq": 0},
        }
        if isinstance(state["entities"], list):
            state["entities"] = {e["entity_id"]: e for e in state["entities"] if isinstance(e, dict) and "entity_id" in e}
        # Record initial material mass tags once; all subsequent transformations
        # are tracked by the deterministic ledger rather than inferred from UI.
        tags = state["resource_ledger"].setdefault("initial_mass_tags", {})
        materials = self.catalog.get("materials", {})
        for item, amount in state.get("warehouse", {}).items():
            for tag, weight in materials.get(item, {}).get("mass_tags", {}).items():
                tags[tag] = tags.get(tag, 0) + int(amount) * int(weight)
        for ent in state["entities"].values():
            for item, amount in ent.get("on_hand", {}).items():
                for tag, weight in materials.get(item, {}).get("mass_tags", {}).items():
                    tags[tag] = tags.get(tag, 0) + int(amount) * int(weight)
        return state

    def register_blueprint(self, blueprint_hash: str, compiled_or_dict: Any) -> None:
        self.blueprints[str(blueprint_hash)] = _as_dict(compiled_or_dict)

    def snapshot(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def state_hash(self) -> str:
        try:
            return digest("state", self.state)
        except WFError as exc:
            # Blueprint inputs have a 64 KiB lexical budget; complete snapshots
            # may exceed it, so hash the same WF-CJSON bytes without that input
            # gate while retaining strict JSON and domain separation.
            if exc.code != "E_COMPLEXITY_BUDGET":
                raise
            import hashlib, json
            raw = json.dumps(self.state, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
            return hashlib.sha256(b"WF:state:v1\0" + raw).hexdigest()

    def _entity(self, entity_id: str) -> dict[str, Any]:
        try:
            return self.state["entities"][entity_id]
        except KeyError as exc:
            raise WFError("E_BAD_REFERENCE", f"unknown entity {entity_id}") from exc

    def _blueprint(self, h: str) -> dict[str, Any]:
        if h not in self.blueprints:
            raise WFError("E_UNKNOWN_BLUEPRINT", f"blueprint {h} is not registered")
        return self.blueprints[h]

    def _occupied(self, cell: list[int] | tuple[int, int]) -> bool:
        return any(tuple(e.get("cell", [])) == tuple(cell) for e in self.state["entities"].values())

    def _tile(self, cell: list[int] | tuple[int, int]) -> str:
        for override in self.state["map"].get("overrides", []):
            if tuple(override["cell"]) == tuple(cell): return str(override["tile"])
        return str(self.state["map"].get("default_tile", "land"))

    def _construction_cost(self, bp: dict[str, Any]) -> dict[str, int]:
        cost: dict[str, int] = {}
        table = self.catalog.get("construction_costs", {})
        for node in bp.get("nodes", []):
            kind = "harvest" if "harvest" in node else "process" if "process" in node else "connector"
            for item, quantity in table.get(kind, {}).items(): cost[item] = cost.get(item, 0) + int(quantity)
        return cost

    def _command(self, command: Mapping[str, Any]) -> dict[str, Any]:
        kind = str(command.get("kind", "")); p = command.get("payload", {}) or {}
        expected_epoch = command.get("expected_rules_epoch")
        if expected_epoch is not None and int(expected_epoch) != int(self.state.get("rules_epoch", 1)):
            raise WFError("E_RULES_EPOCH", "rules epoch mismatch")
        if kind in ("INSTALL_BLUEPRINT", "INSTALL_VALIDATED_BLUEPRINT", "INSTALL_PACK"):
            h = str(p.get("blueprint_hash", "")); self._blueprint(h)
            if h not in self.state["installed_blueprint_hashes"]: self.state["installed_blueprint_hashes"].append(h); self.state["installed_blueprint_hashes"].sort()
            return {"ok": True, "kind": kind, "blueprint_hash": h}
        if kind == "DISABLE_FUTURE_PLACEMENT":
            h = str(p.get("blueprint_hash", "")); self._blueprint(h)
            if h not in self.state["disabled_blueprint_hashes"]: self.state["disabled_blueprint_hashes"].append(h); self.state["disabled_blueprint_hashes"].sort()
            return {"ok": True, "kind": kind}
        if kind == "PLACE":
            h = str(p.get("blueprint_hash", "")); bp = self._blueprint(h)
            if h not in self.state["installed_blueprint_hashes"]: raise WFError("E_NOT_INSTALLED", "blueprint is not installed")
            if h in self.state["disabled_blueprint_hashes"]: raise WFError("E_DISABLED", "blueprint placement is disabled")
            origin = p.get("origin");
            if not (isinstance(origin, (list, tuple)) and len(origin) == 2 and all(type(x) is int for x in origin)): raise WFError("E_SCHEMA", "origin must be integer [x,y]")
            cells = [[origin[0] + n["offset"][0], origin[1] + n["offset"][1]] for n in bp["nodes"]]
            width, height = int(self.state["map"].get("width", 32)), int(self.state["map"].get("height", 32))
            if len({tuple(c) for c in cells}) != len(cells) or any(c[0] < 0 or c[1] < 0 or c[0] >= width or c[1] >= height or self._occupied(c) or self._tile(c) != "land" for c in cells): raise WFError("E_POSITION", "placement cell is occupied, out of bounds, or not land")
            cost = self._construction_cost(bp)
            if any(self.state["warehouse"].get(i, 0) < q for i, q in cost.items()): raise WFError("E_COST", "construction materials are insufficient")
            for i, q in cost.items(): self.state["warehouse"][i] = self.state["warehouse"].get(i, 0) - q
            seq = int(command.get("server_seq", 0))
            if seq <= 0:
                seq = int(self.state["deterministic_counters"].get("placement_server_seq", 0)) + 1
            self.state["deterministic_counters"]["placement_server_seq"] = max(seq, self.state["deterministic_counters"].get("placement_server_seq", 0))
            ids: list[str] = []; by_node: dict[str, str] = {}
            for node, cell in zip(bp["nodes"], cells):
                eid = f"e{seq}/{node['id']}"
                if eid in self.state["entities"]: raise WFError("E_ID_CONFLICT", "placement entity id already exists")
                by_node[node["id"]] = eid; ids.append(eid)
                capacities = deepcopy(node.get("inventory_capacities", {}))
                self.state["entities"][eid] = {"entity_id": eid, "placement_command_id": str(command.get("command_id", "")), "node_id": node["id"], "blueprint_hash": h, "cell": cell, "on_hand": {}, "input_escrow": {}, "output_reserved": {}, "active_job": None, "last_status": "idle", "job_counter": 0, "capacities": capacities, "node": node}
            for link in bp.get("links", []):
                a, b = by_node[link["a"]["node"]], by_node[link["b"]["node"]]
                self.state["mechanical_links"].append(sorted([a, b]))
            self.state["mechanical_links"] = sorted({tuple(x) for x in self.state["mechanical_links"]}); self.state["mechanical_links"] = [list(x) for x in self.state["mechanical_links"]]
            for i, q in cost.items(): self.state["resource_ledger"]["construction_consumed"][i] = self.state["resource_ledger"]["construction_consumed"].get(i, 0) + q
            return {"ok": True, "kind": kind, "entity_ids": ids}
        if kind == "DEPOSIT":
            e = self._entity(str(p.get("entity_id"))); item = str(p.get("item")); amount = p.get("amount")
            if type(amount) is not int or amount <= 0: raise WFError("E_NUMERIC_TYPE", "amount must be a positive integer")
            if self.state["warehouse"].get(item, 0) < amount: raise WFError("E_INVENTORY", "warehouse inventory is insufficient")
            if e.get("on_hand", {}).get(item, 0) + e.get("input_escrow", {}).get(item, 0) + e.get("output_reserved", {}).get(item, 0) + amount > e.get("capacities", {}).get(item, 0): raise WFError("E_CAPACITY", "entity inventory capacity exceeded")
            self.state["warehouse"][item] -= amount; e["on_hand"][item] = e["on_hand"].get(item, 0) + amount
            return {"ok": True, "kind": kind}
        if kind == "WITHDRAW":
            e = self._entity(str(p.get("entity_id"))); item = str(p.get("item")); amount = p.get("amount")
            if type(amount) is not int or amount <= 0 or e.get("on_hand", {}).get(item, 0) < amount: raise WFError("E_INVENTORY", "on-hand inventory is insufficient")
            e["on_hand"][item] -= amount; self.state["warehouse"][item] = self.state["warehouse"].get(item, 0) + amount
            return {"ok": True, "kind": kind}
        if kind == "CONNECT":
            a, b = str(p.get("a")), str(p.get("b")); ea, eb = self._entity(a), self._entity(b)
            if sum(abs(x-y) for x,y in zip(ea["cell"], eb["cell"])) != 1: raise WFError("E_ADJACENCY", "mechanical endpoints must be adjacent")
            def has_shaft(ent):
                node = ent.get("node", {})
                return "shaft" in node.get("ports", [])
            if not has_shaft(ea) or not has_shaft(eb): raise WFError("E_BAD_REFERENCE", "both endpoints require shaft ports")
            pair = sorted([a,b]); links = {tuple(x) for x in self.state["mechanical_links"]}
            if tuple(pair) in links: raise WFError("E_TOPOLOGY", "duplicate link")
            if self._connected(a,b): raise WFError("E_TOPOLOGY", "link would create a cycle")
            self.state["mechanical_links"].append(pair); self.state["mechanical_links"].sort(); return {"ok": True, "kind": kind}
        if kind == "DISCONNECT":
            pair = sorted([str(p.get("a")), str(p.get("b"))]); links = [x for x in self.state["mechanical_links"] if x != pair]
            if len(links) == len(self.state["mechanical_links"]): raise WFError("E_BAD_REFERENCE", "link does not exist")
            self.state["mechanical_links"] = links; return {"ok": True, "kind": kind}
        if kind == "CANCEL_JOB":
            e = self._entity(str(p.get("entity_id"))); job = e.get("active_job")
            if job is None: raise WFError("E_NO_JOB", "entity has no active job")
            for item, q in job["inputs"].items(): e["on_hand"][item] = e["on_hand"].get(item, 0) + q; e["input_escrow"][item] = e["input_escrow"].get(item, 0) - q
            for item, q in job["outputs"].items(): e["output_reserved"][item] = e["output_reserved"].get(item, 0) - q
            e["active_job"] = None; e["last_status"] = "cancelled"; return {"ok": True, "kind": kind}
        raise WFError("E_UNKNOWN_COMMAND", f"unsupported command {kind}")

    def _connected(self, a: str, b: str) -> bool:
        seen = {a}; todo = [a]
        links = self.state["mechanical_links"]
        while todo:
            cur = todo.pop()
            for x, y in links:
                nxt = y if x == cur else x if y == cur else None
                if nxt is not None and nxt not in seen:
                    if nxt == b: return True
                    seen.add(nxt); todo.append(nxt)
        return False

    def _components(self) -> list[list[str]]:
        ids = sorted(self.state["entities"]); remaining = set(ids); out=[]
        links = self.state["mechanical_links"]
        while remaining:
            root = min(remaining); comp={root}; todo=[root]
            while todo:
                cur=todo.pop()
                for a,b in links:
                    nxt=b if a==cur else a if b==cur else None
                    if nxt is not None and nxt not in comp: comp.add(nxt); todo.append(nxt)
            remaining -= comp; out.append(sorted(comp))
        return out

    def _enabled(self, node: dict[str, Any], entity: dict[str, Any]) -> bool:
        def ev(x: dict[str, Any]) -> bool:
            op=x["op"]
            if op == "true": return True
            if op == "false": return False
            if op == "inventory_at_least": return entity.get("on_hand", {}).get(x["item"], 0) >= x["amount"]
            if op == "not": return not ev(x["arg"])
            if op == "all": return all(ev(a) for a in x["args"])
            if op == "any": return any(ev(a) for a in x["args"])
            raise WFError("E_UNKNOWN_CAPABILITY", f"unsupported expression {op}")
        return ev(node["process"]["enabled_when"])

    def _check_invariants(self) -> None:
        limit = 2**53 - 1
        for value in self.state.get("warehouse", {}).values():
            if type(value) is not int or value < 0 or value > limit: raise WFError("E_INVARIANT", "warehouse integer range invariant failed")
        for key, value in self.state.get("energy_ledger", {}).items():
            if type(value) is not int or value < 0 or value > limit: raise WFError("E_INVARIANT", "energy integer range invariant failed")
        for e in self.state["entities"].values():
            for item, cap in e.get("capacities", {}).items():
                used=e.get("on_hand", {}).get(item,0)+e.get("input_escrow", {}).get(item,0)+e.get("output_reserved", {}).get(item,0)
                if type(cap) is not int or cap < 0 or cap > limit: raise WFError("E_INVARIANT", "inventory capacity integer range invariant failed")
                if used < 0 or used > cap or used > limit: raise WFError("E_INVARIANT", "inventory capacity invariant failed")
                for bucket in (e.get("on_hand", {}), e.get("input_escrow", {}), e.get("output_reserved", {})):
                    if any(type(v) is not int or v < 0 or v > limit for v in bucket.values()): raise WFError("E_INVARIANT", "inventory integer range invariant failed")
        el=self.state["energy_ledger"]; 
        if el["external_withdrawn_eu"] != el["conversion_loss_eu"] + el["work_consumed_eu"] + el["unused_dissipated_eu"]: raise WFError("E_INVARIANT", "energy conservation failed")
        # Material mass tags are conserved through processing.  Construction
        # consumption is the only deliberate removal from the initial stock.
        materials = self.catalog.get("materials", {})
        current: dict[str, int] = {}
        def add_materials(items: Mapping[str, Any]) -> None:
            for item, amount in items.items():
                for tag, weight in materials.get(item, {}).get("mass_tags", {}).items():
                    current[tag] = current.get(tag, 0) + int(amount) * int(weight)
        add_materials(self.state.get("warehouse", {}))
        for entity in self.state["entities"].values():
            add_materials(entity.get("on_hand", {})); add_materials(entity.get("input_escrow", {}))
        for item, amount in self.state.get("resource_ledger", {}).get("construction_consumed", {}).items():
            for tag, weight in materials.get(item, {}).get("mass_tags", {}).items():
                current[tag] = current.get(tag, 0) + int(amount) * int(weight)
        initial = self.state.get("resource_ledger", {}).get("initial_mass_tags", {})
        if initial and current != initial:
            raise WFError("E_INVARIANT", "material mass tags are not conserved")

    def step(self, commands: list[Mapping[str, Any]] | None = None) -> TickResult:
        if self.frozen: raise WFError("E_WORLD_FROZEN", "world is frozen after a severe invariant failure")
        working = deepcopy(self.state); old = self.state; self.state = working
        next_tick = int(working["tick"]) + 1; working["tick"] = next_tick; events: list[dict[str, Any]]=[]; results=[]; cancelled_this_tick: set[str] = set()
        try:
            for command in sorted(commands or [], key=lambda c: (int(c.get("server_seq", 0)), str(c.get("command_id", "")))):
                if int(command.get("effective_tick", next_tick)) > next_tick: results.append({"command_id":command.get("command_id"),"status":"deferred"}); continue
                before_command = deepcopy(working)
                try:
                    result=self._command(command); results.append({"command_id":command.get("command_id"),"status":"executed","result":result})
                    if command.get("kind") == "CANCEL_JOB":
                        cancelled_this_tick.add(str(command.get("payload", {}).get("entity_id", "")))
                except WFError as exc:
                    # Every command is atomic within the tick work copy.
                    working.clear(); working.update(before_command); self.state = working
                    results.append({"command_id":command.get("command_id"),"status":"rejected","error":exc.as_dict()})
            pools: dict[str,int] = {}
            for comp in self._components(): pools["|".join(comp)] = 0
            comp_by = {eid:key for key, comp in [("|".join(c),c) for c in self._components()] for eid in comp}
            for eid in sorted(working["entities"]):
                e=working["entities"][eid]; node=e.get("node", {}); proc=node.get("process")
                if proc is None or eid in cancelled_this_tick: continue
                enabled=self._enabled(node,e)
                if e.get("active_job") is None and enabled:
                    if all(e.get("on_hand",{}).get(i,0)>=q for i,q in proc["inputs"].items()) and all(e.get("on_hand",{}).get(i,0)+e.get("input_escrow",{}).get(i,0)+e.get("output_reserved",{}).get(i,0)+q<=e.get("capacities",{}).get(i,0) for i,q in proc["outputs"].items()):
                        for i,q in proc["inputs"].items(): e["on_hand"][i]-=q; e["input_escrow"][i]=e["input_escrow"].get(i,0)+q
                        for i,q in proc["outputs"].items(): e["output_reserved"][i]=e["output_reserved"].get(i,0)+q
                        e["job_counter"]+=1; e["active_job"]={"job_id":e["job_counter"],"inputs":deepcopy(proc["inputs"]),"outputs":deepcopy(proc["outputs"]),"work_required_eu":proc["work_eu"],"progress_eu":0,"rate_eu_per_tick":proc["rate_eu_per_tick"]}; events.append({"type":"job_started","entity_id":eid})
                    else:
                        has_inputs = all(e.get("on_hand",{}).get(i,0) >= q for i,q in proc["inputs"].items())
                        e["last_status"] = "output_full" if has_inputs else "no_input"
                elif e.get("active_job") is not None and not enabled: e["last_status"]="gated"
                elif e.get("active_job") is None and not enabled: e["last_status"]="gated"
            source_requests = {str(source.get("id")): {} for source in working.get("sources", [])}
            source_by_id = {str(source.get("id")): source for source in working.get("sources", [])}
            for eid, entity in sorted(working["entities"].items()):
                harvest = entity.get("node", {}).get("harvest")
                if not harvest:
                    continue
                choices = [source for source in source_by_id.values() if source.get("kind") == harvest.get("source_kind") and sum(abs(x-y) for x,y in zip(entity["cell"], source.get("cell", []))) == 1]
                if choices:
                    selected = sorted(choices, key=lambda source: str(source.get("id")))[0]
                    source_requests[str(selected.get("id"))][eid] = harvest["max_withdraw_eu"]
            for source in sorted(working.get("sources", []), key=lambda x:str(x.get("id"))):
                requests = source_requests.get(str(source.get("id")), {})
                allocations=capped_fair_share(requests,int(source.get("supply_eu_per_tick",0)),next_tick)
                for eid,withdraw in allocations.items():
                    if not withdraw: continue
                    h=working["entities"][eid]["node"]["harvest"]; converted=withdraw*h["efficiency"]["num"]//h["efficiency"]["den"]; loss=withdraw-converted; key=comp_by.get(eid,"")
                    pools[key]=pools.get(key,0)+converted; working["energy_ledger"]["external_withdrawn_eu"]+=withdraw; working["energy_ledger"]["conversion_loss_eu"]+=loss; events.append({"type":"energy_withdrawn","source_id":source.get("id"),"entity_id":eid,"amount":withdraw})
            for key, pool in pools.items():
                ids=key.split("|") if key else []; requests={}
                for eid in ids:
                    job=working["entities"][eid].get("active_job");
                    if job is not None and self._enabled(working["entities"][eid].get("node",{}),working["entities"][eid]): requests[eid]=min(job["rate_eu_per_tick"],job["work_required_eu"]-job["progress_eu"])
                allocations=capped_fair_share(requests,pool,next_tick)
                used=0
                for eid,amount in allocations.items():
                    if not amount: continue
                    e=working["entities"][eid]; job=e["active_job"]; job["progress_eu"]+=amount; used+=amount; working["energy_ledger"]["work_consumed_eu"]+=amount
                    if job["progress_eu"]>=job["work_required_eu"]:
                        for i,q in job["inputs"].items(): e["input_escrow"][i]-=q
                        for i,q in job["outputs"].items(): e["output_reserved"][i]-=q; e["on_hand"][i]=e["on_hand"].get(i,0)+q
                        consumed = working["resource_ledger"].setdefault("processed_consumed", {})
                        for i,q in job["inputs"].items(): consumed[i] = consumed.get(i, 0) + int(q)
                        e["active_job"]=None; e["last_status"]="idle"; events.append({"type":"job_completed","entity_id":eid})
                    else: e["last_status"]="running"
                working["energy_ledger"]["unused_dissipated_eu"] += pool-used
                if used == 0:
                    for eid in ids:
                        ent = working["entities"][eid]
                        if ent.get("active_job") is not None and self._enabled(ent.get("node",{}), ent):
                            ent["last_status"] = "no_power"
            self._check_invariants(); self.state=working
        except Exception:
            self.state=old; self.frozen=True; raise
        summary={"tick":next_tick,"warehouse":deepcopy(working["warehouse"]),"entities":{eid:{"on_hand":deepcopy(e.get("on_hand",{})),"active_job":deepcopy(e.get("active_job")),"last_status":e.get("last_status")} for eid,e in sorted(working["entities"].items())},"energy_ledger":deepcopy(working["energy_ledger"])}
        return TickResult(next_tick,self.state_hash(),events,results,summary)


class WorldState:
    def __init__(self, data=None): self.data = deepcopy(dict(data or {}))
    def to_dict(self): return deepcopy(self.data)
    @property
    def tick(self): return self.data.get("tick", 0)
    @property
    def state_hash(self):
        try: return digest("state", self.data)
        except Exception:
            import hashlib, json
            return hashlib.sha256(b"WF:state:v1\\0" + json.dumps(self.data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
class World:
    def __init__(self, data): self.state=WorldState(data)
    @classmethod
    def from_scenario(cls, scenario): return cls(scenario)
    def to_dict(self): return self.state.to_dict()

__all__ = ["Engine", "TickResult", "World", "WorldState"]



