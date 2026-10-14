"""Price-free native projections shared by the two explicit venue adapters."""

from ..contracts import canonical_json, empty_policy, native_revision_id, sha256, timestamp


def coverage_row(bundle, source, outcomes, usable):
    key = "market_id" if bundle.venue == "polymarket" else "market_ticker"
    reason = source.get("error")
    return {
        "venue": bundle.venue,
        "native_market_id": source[key],
        "producer_status": source["status"],
        "producer_reason": reason,
        "source_usable": usable,
        "observed_outcome_count": outcomes,
        "usable_outcome_count": 0,
        "exported_outcome_count": 0,
        "mapped_count": 0,
        "unresolved_count": 0,
        "unsupported_count": 0,
        "unusable_count": 0,
        "semantic_disposition": "unresolved",
        "diagnostics_json": canonical_json([]),
    }


def native_row(bundle, market, definition, native_key, **fields):
    updated = market.get("source_updated_at", market.get("provider_updated_at"))
    row = {
        "venue": bundle.venue,
        "native_market_id": native_key["market_id"],
        "side": None,
        "asset_kind": None,
        "asset_id": None,
        "condition_id": None,
        "source_market_version": None,
        "outcome_index": None,
        "outcome_label": "",
        "clob_token_id": None,
        "position_id": None,
        "chain_index_set": None,
        "event_id": None,
        "series_id": None,
        "native_key_json": canonical_json(native_key),
        "input_manifest_sha256": bundle.manifest_sha256,
        "source_release_id": bundle.source_release_id,
        "observation_id": market["observation_id"],
        "received_at": timestamp(market["received_at"]),
        "source_updated_at": timestamp(updated),
        "source_updated_at_raw_json": canonical_json(updated),
        "definition_sha256": sha256(canonical_json(definition).encode()),
        "definition_json": canonical_json(definition),
        "policy_json": canonical_json(empty_policy()),
        "usable": True,
        "diagnostics_json": canonical_json([]),
        **fields,
    }
    row["native_outcome_revision_id"] = native_revision_id(row)
    return row
