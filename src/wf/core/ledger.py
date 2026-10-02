"""Small integer conservation ledgers used by the tick engine."""
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class EnergyLedger:
    external_withdrawn_eu: int = 0
    conversion_loss_eu: int = 0
    work_consumed_eu: int = 0
    unused_dissipated_eu: int = 0
    def as_dict(self): return {"external_withdrawn_eu":self.external_withdrawn_eu,"conversion_loss_eu":self.conversion_loss_eu,"work_consumed_eu":self.work_consumed_eu,"unused_dissipated_eu":self.unused_dissipated_eu}
    def assert_conserved(self):
        if self.external_withdrawn_eu != self.conversion_loss_eu + self.work_consumed_eu + self.unused_dissipated_eu:
            raise ValueError("energy ledger is not conserved")

@dataclass
class ResourceLedger:
    initial_mass_tags: dict[str,int] = field(default_factory=dict)
    construction_consumed: dict[str,int] = field(default_factory=dict)
    processed_consumed: dict[str,int] = field(default_factory=dict)
    def as_dict(self): return {"initial_mass_tags":dict(self.initial_mass_tags),"construction_consumed":dict(self.construction_consumed),"processed_consumed":dict(self.processed_consumed)}
