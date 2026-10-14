import json
from pathlib import Path

import pytest

from market_semantics.contracts import (
    SCHEMAS,
    Limits,
    SemanticsError,
    VerifiedBundle,
    canonical_json,
    claim_payload,
    empty_policy,
    identity,
    strict_json,
    validate_evidence,
    validate_policy,
)


def test_identity_ignores_object_order_but_preserves_polarity():
    positive = claim_payload("person:donald-j-trump", True)
    assert identity("claim", positive) == identity("claim", dict(reversed(list(positive.items()))))
    assert identity("claim", positive) != identity(
        "claim", claim_payload("person:donald-j-trump", False)
    )


@pytest.mark.parametrize("data", ['{"a":1,"a":2}', '{"x":NaN}', '{"x":1.2}', "[" * 1000])
def test_invalid_semantic_json(data):
    with pytest.raises(SemanticsError):
        strict_json(data)


def test_limits_can_only_be_reduced():
    assert Limits.from_dict({"max_comparisons": 1}).max_comparisons == 1
    for invalid in ({"max_comparisons": True}, {"max_duration_seconds": 601}, {"unknown": 1}):
        with pytest.raises(SemanticsError):
            Limits.from_dict(invalid)


def test_canonical_json_has_no_newline_or_ascii_replacement():
    assert canonical_json({"é": None, "a": True}) == '{"a":true,"é":null}'


def evidence_bundle():
    evidence = {
        "venue": "kalshi",
        "file": "markets.json",
        "pointer": "/0/payload_json",
        "value_pointer": "/rules_primary",
        "quote": "Synthetic rule",
    }
    bundle = VerifiedBundle(
        "kalshi",
        Path("/unused"),
        {},
        "a" * 64,
        {"markets": [{"payload_json": '{"rules_primary":"Synthetic rule"}'}]},
        ("SYNTHETIC",),
    )
    return evidence, {"kalshi": bundle}


def test_evidence_resolves_encoded_native_payload():
    evidence, bundles = evidence_bundle()
    validate_evidence([evidence], bundles)
    for changed in (
        {**evidence, "quote": "Different rule"},
        {**evidence, "pointer": "/9"},
        {**evidence, "file": "../markets.json"},
    ):
        with pytest.raises(SemanticsError):
            validate_evidence([changed], bundles)


def test_policy_rejects_unsupported_assertions():
    evidence, bundles = evidence_bundle()
    policy = empty_policy()
    validate_policy(policy, bundles)
    policy["fields"]["candidate_id"] = {
        "state": "not_applicable",
        "value": None,
        "evidence": [evidence],
    }
    with pytest.raises(SemanticsError):
        validate_policy(policy, bundles)


@pytest.mark.parametrize(
    "key,value",
    [
        ("deadline", {"at": "2025-01-20T17:00:00Z", "timezone": "Imaginary/Zone"}),
        ("payout", {"win": "1", "lose": "0", "cancel": "1.1", "withdrawal": "0"}),
        ("payout", {"win": "1.0", "lose": "0", "cancel": "0.5", "withdrawal": "0"}),
    ],
)
def test_policy_timezone_and_payout_are_not_arbitrary_strings(key, value):
    evidence, bundles = evidence_bundle()
    policy = empty_policy()
    policy["fields"][key] = {"state": "known", "value": value, "evidence": [evidence]}
    with pytest.raises(SemanticsError):
        validate_policy(policy, bundles)


def test_schema_compatibility_fixture():
    expected = json.loads((Path(__file__).parent / "fixtures/schemas-v1.json").read_text())
    actual = {
        name: [{"name": x.name, "type": str(x.type), "nullable": x.nullable} for x in schema]
        for name, schema in SCHEMAS.items()
    }
    assert actual == expected
