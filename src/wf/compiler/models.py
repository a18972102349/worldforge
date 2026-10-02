"""Frozen, typed WF-IR 0.1 models.

The JSON Schema remains the structural source of truth.  These models add
strict Python types and make the value handed to the engine immutable.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator, model_validator


class FrozenDict(dict[str, Any]):
    """A JSON-compatible mapping that rejects mutation after compilation."""

    __slots__ = ("_wf_frozen",)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        dict.__init__(self, *args, **kwargs)
        object.__setattr__(self, "_wf_frozen", True)

    def _deny(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("compiled IR mappings are immutable")

    __setitem__ = _deny
    __delitem__ = _deny
    clear = _deny
    pop = _deny
    popitem = _deny
    setdefault = _deny
    update = _deny
    __ior__ = _deny


def _freeze_mapping(value: Mapping[str, Any]) -> FrozenDict:
    return FrozenDict(value)


class WFModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_assignment=True,
        populate_by_name=True,
    )

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        # Optional primitives are omitted in the source schema.  Keeping this
        # default makes round-tripping a compiled IR hash-identical to the
        # strict source document.
        kwargs.setdefault("exclude_none", True)
        return super().model_dump(*args, **kwargs)


class ExprTrue(WFModel):
    op: Literal["true"]


class ExprFalse(WFModel):
    op: Literal["false"]


class ExprInventoryAtLeast(WFModel):
    op: Literal["inventory_at_least"]
    item: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    amount: StrictInt = Field(ge=0, le=1000)


class ExprNot(WFModel):
    op: Literal["not"]
    arg: "Expression"


class ExprAllAny(WFModel):
    op: Literal["all", "any"]
    args: tuple["Expression", ...] = Field(min_length=1, max_length=8)

    @field_validator("args", mode="before")
    @classmethod
    def _tuple_args(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


Expression = Annotated[
    Union[ExprTrue, ExprFalse, ExprInventoryAtLeast, ExprNot, ExprAllAny],
    Field(discriminator="op"),
]


class Efficiency(WFModel):
    num: StrictInt = Field(ge=1, le=16)
    den: StrictInt = Field(ge=1, le=16)


class Harvest(WFModel):
    source_kind: Literal["river"]
    max_withdraw_eu: StrictInt = Field(ge=1, le=8)
    efficiency: Efficiency


class Process(WFModel):
    inputs: Mapping[StrictStr, StrictInt] = Field(min_length=1, max_length=8)
    outputs: Mapping[StrictStr, StrictInt] = Field(min_length=1, max_length=8)
    work_eu: StrictInt = Field(ge=1, le=1_000_000)
    rate_eu_per_tick: StrictInt = Field(ge=1, le=16)
    enabled_when: Expression

    _freeze_inputs = field_validator("inputs", mode="after")(_freeze_mapping)
    _freeze_outputs = field_validator("outputs", mode="after")(_freeze_mapping)


class Visual(WFModel):
    shape: Literal["wheel", "mill", "block", "shaft"]


class Node(WFModel):
    id: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    offset: tuple[StrictInt, StrictInt]
    ports: tuple[Literal["shaft"], ...] = Field(min_length=1, max_length=1)
    inventory_capacities: Mapping[StrictStr, StrictInt] = Field(default_factory=dict, max_length=8)
    harvest: Harvest | None = None
    process: Process | None = None
    visual: Visual

    _freeze_capacities = field_validator("inventory_capacities", mode="after")(_freeze_mapping)

    @field_validator("offset", "ports", mode="before")
    @classmethod
    def _tuple_fields(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _single_active_primitive(self) -> "Node":
        if self.harvest is not None and self.process is not None:
            raise ValueError("a node may contain only one active primitive")
        return self


class Endpoint(WFModel):
    node: StrictStr = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    port: Literal["shaft"]


class Link(WFModel):
    a: Endpoint
    b: Endpoint


class Blueprint(WFModel):
    ir_version: Literal["0.1"]
    capability_profile: Literal["mechanical-v0"]
    name: StrictStr = Field(min_length=1, max_length=64)
    description: StrictStr = Field(max_length=512)
    nodes: tuple[Node, ...] = Field(min_length=1, max_length=16)
    links: tuple[Link, ...] = Field(max_length=32)

    @field_validator("nodes", "links", mode="after")
    @classmethod
    def _freeze_sequence(cls, value: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(value)

    @field_validator("nodes", "links", mode="before")
    @classmethod
    def _tuple_collections(cls, value: Any) -> Any:
        return tuple(value) if isinstance(value, list) else value


for _model in (ExprNot, ExprAllAny):
    _model.model_rebuild()


def model_to_dict(model: WFModel) -> dict[str, Any]:
    """Schema-shaped serialisation for callers and hash verification."""
    return model.model_dump(mode="json", by_alias=True)


# Keep the public aliases discoverable for code that expects straightforward
# names rather than the implementation's expression subclasses.
Expr = Expression

__all__ = [
    "FrozenDict",
    "WFModel",
    "Expression",
    "Expr",
    "ExprTrue",
    "ExprFalse",
    "ExprInventoryAtLeast",
    "ExprNot",
    "ExprAllAny",
    "Efficiency",
    "Harvest",
    "Process",
    "Visual",
    "Node",
    "Endpoint",
    "Link",
    "Blueprint",
    "model_to_dict",
]
