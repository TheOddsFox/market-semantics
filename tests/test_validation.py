from copy import deepcopy

import pytest

from market_semantics.contracts import (
    Limits,
    SemanticsError,
    assessment_id,
    canonical_json,
    strict_json,
)
from market_semantics.validation import validate_tables

from .release_helpers import release_case


def test_report_counts_are_recomputed(tmp_path):
    tables, bundles, curation, _ = release_case(tmp_path)
    report = validate_tables(tables, bundles, curation, Limits())
    assert report["valid"]
    assert report["mapping_status_counts"] == {"mapped": 2, "unresolved": 2}
    assert report["selected_counts"] == {"kalshi": 1, "polymarket": 1}


@pytest.mark.parametrize("change", ["link", "counts", "status", "native", "policy", "entity"])
def test_rejects_forged_invariants(tmp_path, change):
    tables, bundles, curation, _ = release_case(tmp_path)
    bad = deepcopy(tables)
    if change == "link":
        bad["outcome_mappings"][0]["native_outcome_revision_id"] = "missing"
    elif change == "counts":
        bad["coverage"][0]["mapped_count"] += 1
    elif change == "status":
        mapping = next(row for row in bad["outcome_mappings"] if row["status"] == "mapped")
        mapping["status"] = "unsupported"
        mapping["claim_id"] = None
        mapping["assessment_id"] = assessment_id("mapping", mapping)
    elif change == "native":
        bad["native_outcomes"][0]["outcome_label"] = "invented"
    elif change == "entity":
        entity = bad["entities"][0]
        entity["identity_json"] = canonical_json({"scope": "invented"})
    else:
        outcome = next(row for row in bad["native_outcomes"] if row["venue"] == "polymarket")
        policy = strict_json(outcome["policy_json"])
        policy["fields"]["round"]["value"] = "primary"
        outcome["policy_json"] = canonical_json(policy)
    with pytest.raises(SemanticsError):
        validate_tables(bad, bundles, curation, Limits())


def test_missing_and_unusable_selections_never_manufacture_outcomes(tmp_path):
    tables, bundles, curation, _ = release_case(tmp_path, empty=True)
    report = validate_tables(tables, bundles, curation, Limits())
    assert report["row_counts"]["native_outcomes"] == 0
    assert report["disposition_counts"] == {"no_outcomes": 2}
    assert len(tables["coverage"]) == 2


def test_closed_policy_and_json_schema(tmp_path):
    tables, bundles, curation, _ = release_case(tmp_path)
    tables["native_outcomes"][0]["policy_json"] = '{"version":1}'
    with pytest.raises(SemanticsError):
        validate_tables(tables, bundles, curation, Limits())


def test_rehashed_accepted_relationship_still_recomputes_incomplete_policy(tmp_path):
    tables, bundles, curation, _ = release_case(tmp_path)
    row = next(row for row in tables["relationships"] if row["relationship_type"] == "inverse")
    assert row["status"] == "unresolved"
    row["status"] = "accepted"
    row["reason_code"] = "COMPATIBLE_POLICY"
    row["assessment_id"] = assessment_id("relationship", row)
    with pytest.raises(SemanticsError, match="policy comparison differs"):
        validate_tables(tables, bundles, curation, Limits())


def test_source_quote_alone_does_not_authorize_uncurated_policy(tmp_path):
    tables, bundles, curation, _ = release_case(tmp_path)
    tables["relationships"] = []
    outcome = next(row for row in tables["native_outcomes"] if row["venue"] == "polymarket")
    mapping = next(
        row
        for row in tables["outcome_mappings"]
        if row["native_outcome_revision_id"] == outcome["native_outcome_revision_id"]
    )
    policy = strict_json(outcome["policy_json"])
    policy["fields"]["withdrawal"] = {
        "state": "known",
        "value": "manufactured treatment",
        "evidence": strict_json(mapping["evidence_json"]),
    }
    outcome["policy_json"] = canonical_json(policy)
    with pytest.raises(SemanticsError, match="differs from evidenced interpretation"):
        validate_tables(tables, bundles, curation, Limits())


def test_relationship_assessment_limit_exact_and_one_over(tmp_path):
    from dataclasses import replace

    tables, bundles, curation, _ = release_case(tmp_path)
    count = len(tables["relationships"])
    assert count == 2
    assert validate_tables(tables, bundles, curation, replace(Limits(), max_comparisons=count))[
        "valid"
    ]
    with pytest.raises(SemanticsError, match="relationship limit"):
        validate_tables(tables, bundles, curation, replace(Limits(), max_comparisons=count - 1))
