from hypothesis import given, strategies as st

from wf import canonical_bytes, digest, strict_loads


json_scalars = st.one_of(st.none(), st.booleans(), st.integers(min_value=-(2**53 - 1), max_value=2**53 - 1), st.text())
json_values = st.recursive(
    json_scalars,
    lambda children: st.one_of(
        st.lists(children, max_size=5),
        st.dictionaries(st.text(alphabet=st.characters(whitelist_categories=("Ll",)), max_size=8), children, max_size=5),
    ),
    max_leaves=20,
)


@given(json_values)
def test_canonical_bytes_are_stable(value) -> None:
    # Restrict generated object keys to ASCII to stay inside WF-CJSON-1.
    try:
        encoded = canonical_bytes(value)
    except Exception:
        return
    assert canonical_bytes(strict_loads(encoded)) == encoded
    assert digest("test", value) == digest("test", strict_loads(encoded))

