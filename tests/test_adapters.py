import pytest

from market_semantics.adapters import kalshi, polymarket
from market_semantics.contracts import SCHEMAS, strict_json
from market_semantics.inputs import verify_bundle

from .adapter_helpers import relations, write_bundle


def project(tmp_path, venue, rows=None):
    pin, release = write_bundle(tmp_path / venue, venue, rows)
    selected = ["1"] if venue == "polymarket" else ["M"]
    bundle = verify_bundle(venue, tmp_path / venue, pin, release, selected)
    return (polymarket if venue == "polymarket" else kalshi).adapt(bundle)


@pytest.mark.parametrize("venue", ["polymarket", "kalshi"])
def test_projection_exact_schema_and_empty_policy(tmp_path, venue):
    result = project(tmp_path, venue)
    assert len(result.native_outcomes) == 2
    assert set(result.native_outcomes[0]) == set(SCHEMAS["native_outcomes"].names)
    assert set(result.coverage[0]) == set(SCHEMAS["coverage"].names)
    for native in result.native_outcomes:
        assert set(strict_json(native["definition_json"])) == {"market", "outcome", "parents"}
        assert all(
            f["state"] == "unknown" for f in strict_json(native["policy_json"])["fields"].values()
        )
    assert result.coverage[0]["usable_outcome_count"] == 2


def test_poly_version_ordinal_and_asset_kind(tmp_path):
    result = project(tmp_path, "polymarket")
    native = result.native_outcomes[0]
    assert native["source_market_version"] == "v1"
    assert native["outcome_index"] == 1
    assert native["chain_index_set"] is None
    assert "outcome_label" not in strict_json(native["native_key_json"])
    rows = relations("polymarket")
    rows["markets"][0]["source_market_version"] = "v2"
    for outcome in rows["outcomes"]:
        outcome["position_id"] = outcome["asset_id"]
        outcome["asset_kind"] = "poly_v2_position"
    changed = project(tmp_path / "changed", "polymarket", rows)
    assert changed.native_outcomes[0]["asset_kind"] == "poly_v2_position"
    assert changed.native_outcomes[0]["native_key_json"] != native["native_key_json"]


def test_unusable_and_missing_remain_coverage_only(tmp_path):
    rows = relations("polymarket")
    rows["markets"][0]["usable"] = False
    rows["markets"][0]["identity_error"] = "invalid source identity"
    result = project(tmp_path, "polymarket", rows)
    assert result.native_outcomes == []
    assert result.coverage[0]["source_usable"] is False
    assert "invalid source identity" in result.coverage[0]["diagnostics_json"]


def test_kalshi_definition_retains_rules_composites_but_not_prices(tmp_path):
    rows = relations("kalshi")
    from market_semantics.contracts import canonical_json

    payload = strict_json(rows["markets"][0]["payload_json"])
    payload["mve_selected_legs"] = [{"market_ticker": "LEG", "side": "yes"}]
    rows["markets"][0]["payload_json"] = canonical_json(payload)
    result = project(tmp_path, "kalshi", rows)
    assert {r["side"] for r in result.native_outcomes} == {"YES", "NO"}
    definition = strict_json(result.native_outcomes[0]["definition_json"])
    assert definition["market"]["mve_selected_legs"]
    assert definition["market"]["rules_primary"]
    assert "yes_bid" not in definition["market"]
    assert definition["parents"]["event"]["series_ticker"] == "S"


def test_receipt_changes_revision_without_changing_definition(tmp_path):
    original = project(tmp_path, "polymarket").native_outcomes[0]
    rows = relations("polymarket")
    for source in rows["markets"] + rows["outcomes"]:
        source["received_at"] = "2024-10-02T00:00:00Z"
        source["observation_id"] = "new-observation"
    later = project(tmp_path / "later", "polymarket", rows).native_outcomes[0]
    assert original["definition_sha256"] == later["definition_sha256"]
    assert original["native_outcome_revision_id"] != later["native_outcome_revision_id"]
