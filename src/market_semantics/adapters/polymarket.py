"""Preserve current typed Polymarket assets without nominating raw history."""

from ..contracts import Projection, canonical_json
from . import coverage_row, native_row

_PROVENANCE = {
    "observation_id",
    "page_id",
    "capture_id",
    "received_at",
    "source_kind",
    "json_pointer",
    "payload_sha256",
    "source_updated_at",
}


def adapt(bundle):
    markets = {r["market_id"]: r for r in bundle.relations["markets"]}
    native, coverage = [], []
    for source in bundle.relations["coverage"]:
        market_id = source["market_id"]
        market = markets.get(market_id)
        outcomes = [r for r in bundle.relations["outcomes"] if r["market_id"] == market_id]
        usable = market["usable"] if market else None
        row = coverage_row(bundle, source, len(outcomes), usable)
        diagnostics = []
        if market and not usable:
            diagnostics.append(market.get("identity_error") or "producer identity unusable")
        history = [
            r for r in bundle.relations["identity_history"] if r.get("market_id") == market_id
        ]
        for historical in history:
            if historical.get("usable") is not True or historical.get("identity_error"):
                diagnostics.append(
                    {
                        "historical_identity_error": historical.get("identity_error"),
                        "observation_id": historical.get("observation_id"),
                    }
                )
        if market and usable and source["status"] == "found":
            for outcome in outcomes:
                if not outcome["usable"]:
                    diagnostics.append(outcome.get("identity_error") or "unusable outcome")
                    continue
                version = outcome.get("source_market_version", market["source_market_version"])
                typed = {
                    key: outcome.get(key)
                    for key in (
                        "asset_kind",
                        "asset_id",
                        "condition_id",
                        "outcome_index",
                        "outcome_label",
                        "clob_token_id",
                        "position_id",
                        "chain_index_set",
                    )
                }
                key = {
                    "venue": "polymarket",
                    "market_id": market_id,
                    "source_market_version": version,
                    **{k: v for k, v in typed.items() if k != "outcome_label"},
                }
                definition = {
                    "market": {
                        k: v
                        for k, v in market.items()
                        if k not in _PROVENANCE | {"tick_size", "minimum_order_size"}
                    },
                    "outcome": {
                        **{k: v for k, v in outcome.items() if k not in _PROVENANCE},
                        "source_market_version": version,
                    },
                    "parents": {
                        "memberships": [
                            {"event_id": r["event_id"]}
                            for r in bundle.relations["memberships"]
                            if r["market_id"] == market_id
                        ]
                    },
                }
                native.append(
                    native_row(
                        bundle, market, definition, key, source_market_version=version, **typed
                    )
                )
                row["usable_outcome_count"] += 1
        row["exported_outcome_count"] = row["usable_outcome_count"]
        row["diagnostics_json"] = canonical_json(diagnostics)
        coverage.append(row)
    return Projection(native, coverage)
