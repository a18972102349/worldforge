#!/usr/bin/env python3
"""Validate this DESIGN package, not an implementation of the game.

Requires jsonschema already installed. No network, engine, model, or chain calls.
The small semantic checker covers example invariants only; the full compiler must
still implement every requirement in the normative design and acceptance table.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
import unicodedata
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MAX_SAFE_INT = 2**53 - 1

class SpecError(ValueError):
    pass

def reject_float(value: str) -> Any:
    raise SpecError('E_NUMERIC_TYPE: float token ' + value)

def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SpecError('E_DUPLICATE_KEY: ' + key)
        result[key] = value
    return result

def check_canonical_value(value: Any) -> None:
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if not -MAX_SAFE_INT <= value <= MAX_SAFE_INT:
            raise SpecError('E_INTEGER_RANGE')
        return
    if isinstance(value, str):
        if unicodedata.normalize('NFC', value) != value:
            raise SpecError('E_CANONICAL_INPUT: non-NFC')
        if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise SpecError('E_CANONICAL_INPUT: surrogate')
        return
    if isinstance(value, list):
        for child in value:
            check_canonical_value(child)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or not key.isascii():
                raise SpecError('E_CANONICAL_INPUT: keys must be ASCII')
            check_canonical_value(key)
            check_canonical_value(child)
        return
    raise SpecError('E_CANONICAL_INPUT: unsupported type')

def strict_load(path: Path) -> Any:
    data = path.read_bytes()
    # Schema and docs fixtures are also small; blueprint 64KiB limit tested here.
    if 'blueprints' in path.parts or 'invalid' in path.parts:
        if len(data) > 65536:
            raise SpecError('E_COMPLEXITY_BUDGET')
    try:
        value = json.loads(data.decode('utf-8'), parse_float=reject_float,
                           parse_constant=reject_float, object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SpecError('E_PARSE: ' + str(exc)) from exc
    check_canonical_value(value)
    return value

def canonical_bytes(value: Any) -> bytes:
    check_canonical_value(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')

def blueprint_hash(value: Any) -> str:
    return hashlib.sha256(b'WF:blueprint:v1\0' + canonical_bytes(value)).hexdigest()

def check_example_semantics(value: dict[str, Any], catalog: dict[str, Any]) -> None:
    """Example conformance checks; NOT the complete production validator."""
    materials = catalog['materials']
    edges = {x['input']: x for x in catalog['process_edges']}
    seen: set[str] = set()
    cells: set[tuple[int, int]] = set()
    nodes: dict[str, Any] = {}
    for node in value['nodes']:
        if node['id'] in seen or tuple(node['offset']) in cells:
            raise SpecError('E_DUPLICATE_NODE')
        seen.add(node['id']); cells.add(tuple(node['offset'])); nodes[node['id']] = node
        for item in node['inventory_capacities']:
            if item not in materials:
                raise SpecError('E_UNKNOWN_CAPABILITY')
        h = node.get('harvest')
        if h and h['efficiency']['num'] > h['efficiency']['den']:
            raise SpecError('E_ENERGY_GAIN')
        p = node.get('process')
        if not p:
            continue
        if set(p['inputs']) & set(p['outputs']):
            raise SpecError('E_RECIPE_LAYOUT')
        for item, qty in list(p['inputs'].items()) + list(p['outputs'].items()):
            if item not in materials:
                raise SpecError('E_UNKNOWN_CAPABILITY')
            if node['inventory_capacities'].get(item, 0) < qty:
                raise SpecError('E_CAPACITY')
        def tags(items: dict[str, int]) -> dict[str, int]:
            total: dict[str, int] = {}
            for item, qty in items.items():
                for tag, mass in materials[item]['mass_tags'].items():
                    total[tag] = total.get(tag, 0) + mass * qty
            return total
        if tags(p['inputs']) != tags(p['outputs']):
            raise SpecError('E_MATERIAL_BALANCE')
        expected: dict[str, int] = {}
        min_work = 0
        for item, qty in p['inputs'].items():
            if item not in edges:
                raise SpecError('E_TRANSFORM_NOT_ALLOWED')
            edge = edges[item]
            expected[edge['output']] = expected.get(edge['output'], 0) + qty
            min_work += qty * edge['min_work_eu_per_input']
        if expected != p['outputs']:
            raise SpecError('E_TRANSFORM_NOT_ALLOWED')
        if p['work_eu'] < min_work:
            raise SpecError('E_WORK_TOO_LOW')
    parent = {key:key for key in nodes}
    def root(key: str) -> str:
        while parent[key] != key:
            key = parent[key]
        return key
    pairs = set()
    for link in value['links']:
        a,b = link['a']['node'], link['b']['node']
        if a not in nodes or b not in nodes:
            raise SpecError('E_BAD_REFERENCE')
        pair=tuple(sorted((a,b)))
        if a==b or pair in pairs or root(a)==root(b):
            raise SpecError('E_TOPOLOGY')
        pairs.add(pair)
        p1,p2=nodes[a]['offset'],nodes[b]['offset']
        if sum(abs(x-y) for x,y in zip(p1,p2)) != 1:
            raise SpecError('E_ADJACENCY')
        parent[root(a)] = root(b)

def main() -> int:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        print('BLOCKED: install an approved local copy of jsonschema; no installation attempted.', file=sys.stderr)
        return 2
    schema = strict_load(ROOT/'schemas/blueprint.schema.json')
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    catalog = strict_load(ROOT/'examples/fixtures/catalog.json')
    valid = sorted((ROOT/'examples/blueprints').glob('*.json'))
    for path in valid:
        value = strict_load(path)
        errors=list(validator.iter_errors(value))
        if errors:
            raise SpecError(f'E_SCHEMA in {path.name}: {errors[0].message}')
        check_example_semantics(value,catalog)
    invalid_cases = strict_load(ROOT/'examples/invalid_cases.json')
    for case in invalid_cases:
        path=ROOT/'examples'/case['file']
        found=None
        gate=None
        try:
            value=strict_load(path)
        except SpecError as exc:
            found=str(exc);gate='parse'
        if found is None:
            errors=list(validator.iter_errors(value))
            if errors:
                found='E_SCHEMA';gate='schema'
        if found is None:
            try:
                check_example_semantics(value,catalog)
            except SpecError as exc:
                found=str(exc);gate='semantic'
        if not found or case['error'] not in found or gate != case['gate']:
            raise SpecError(f'Invalid example mismatch: {case}, got {gate}: {found}')
    vectors=strict_load(ROOT/'test_vectors/canonical.json')
    for vector in vectors:
        if canonical_bytes(vector['input']).hex()!=vector['canonical_utf8_hex']:
            raise SpecError('canonical vector mismatch: '+vector['name'])
        if blueprint_hash(vector['input'])!=vector['blueprint_domain_sha256']:
            raise SpecError('hash vector mismatch: '+vector['name'])
    expected_hashes=strict_load(ROOT/'test_vectors/blueprint_hashes.json')
    for path in valid:
        if blueprint_hash(strict_load(path))!=expected_hashes[path.name]:
            raise SpecError('blueprint hash mismatch: '+path.name)
    # Other JSON fixtures must be syntactically readable. They are not engine runs.
    for path in (ROOT/'examples/fixtures').glob('*.json'):
        strict_load(path)
    result={
        'status':'passed',
        'scope':'design_package_only_not_game_or_contract_implementation',
        'valid_blueprints':len(valid),'rejected_invalid_blueprints':len(invalid_cases),
        'canonical_vectors':len(vectors),'stable_blueprint_hashes':len(expected_hashes),
        'engine_tests_run':False,'contract_tests_run':False,'real_ai_tests_run':False,
        'network_calls':0,
    }
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (SpecError, OSError, KeyError) as exc:
        print('FAILED: '+str(exc), file=sys.stderr)
        raise SystemExit(1)
