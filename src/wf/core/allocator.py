"""Deterministic capped fair share allocation."""
from __future__ import annotations
from collections.abc import Mapping

def capped_fair_share(requests: Mapping[str,int], supply: int, tick: int = 1) -> dict[str,int]:
    """Allocate integer ``supply`` among requests using WF 4.3.

    IDs are sorted lexicographically. Remaining one-unit remainders rotate by
    ``(tick-1) % n``; every request is capped at its requested amount.
    """
    if type(supply) is not int or supply < 0: raise ValueError("supply must be non-negative integer")
    if type(tick) is not int or tick < 1: raise ValueError("tick must be positive integer")
    source = requests.items() if hasattr(requests, "items") else requests
    ordered = sorted((str(k), v) for k,v in source)
    if any(type(v) is not int or v < 0 for _,v in ordered): raise ValueError("requests must be non-negative integers")
    out = {k: 0 for k,_ in ordered}
    remaining = supply
    active = [(k,v) for k,v in ordered if v > 0]
    while active and remaining:
        n = len(active)
        base, rem = divmod(remaining, n)
        if base:
            taken = 0
            nxt=[]
            for k,need in active:
                q = min(need, base)
                out[k] += q; taken += q
                left = need-q
                if left: nxt.append((k,left))
            remaining -= taken
            active = nxt
            if not active: break
            if taken == 0: break
        # If less than one complete round, assign one in rotating order.
        if remaining and active:
            n = len(active)
            start = (tick - 1) % n
            progressed = False
            for j in range(n):
                if not remaining: break
                i = (start+j) % n
                k,need = active[i]
                if need:
                    out[k] += 1; remaining -= 1; progressed = True
                    active[i] = (k,need-1)
                    if active[i][1] == 0: active[i] = (k,0)
            active = [(k,need) for k,need in active if need]
            if not progressed: break
    return out

# aliases used by early callers
allocate = capped_fair_share
fair_share = capped_fair_share
