from pathlib import Path

import pytest

from wf import WFError, blueprint_hash, compile_blueprint, strict_loads

ROOT = Path(__file__).resolve().parents[2]


def test_example_blueprints_compile_to_golden_hashes() -> None:
    expected = strict_loads((ROOT / "test_vectors" / "blueprint_hashes.json").read_bytes())
    for name, value in expected.items():
        ir = compile_blueprint((ROOT / "examples" / "blueprints" / name).read_bytes())
        assert blueprint_hash(ir.model_dump(mode="json")) == value


def test_compiled_model_dump_matches_normative_schema() -> None:
    from jsonschema import Draft202012Validator

    schema = strict_loads((ROOT / "schemas" / "blueprint.schema.json").read_bytes())
    checker = Draft202012Validator(schema)
    for path in sorted((ROOT / "examples" / "blueprints").glob("*.json")):
        ir = compile_blueprint(path.read_bytes())
        assert list(checker.iter_errors(ir.model_dump(mode="json"))) == []


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("code_field.json", "E_SCHEMA"),
        ("duplicate_key.json", "E_DUPLICATE_KEY"),
        ("float_token.json", "E_NUMERIC_TYPE"),
        ("mass_creation.json", "E_MATERIAL_BALANCE"),
        ("overunity.json", "E_ENERGY_GAIN"),
        ("unknown_item.json", "E_UNKNOWN_CAPABILITY"),
    ],
)
def test_invalid_examples_are_rejected(name: str, code: str) -> None:
    with pytest.raises(WFError) as caught:
        compile_blueprint((ROOT / "examples" / "invalid" / name).read_bytes())
    assert caught.value.code == code


def test_compiled_ir_is_immutable() -> None:
    ir = compile_blueprint((ROOT / "examples" / "blueprints" / "river_mill.json").read_bytes())
    with pytest.raises((TypeError, ValueError)):
        ir.nodes[0].inventory_capacities["grain"] = 1
    with pytest.raises((TypeError, ValueError)):
        ir.nodes = ()

