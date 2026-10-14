"""Synthetic self-contained releases shared by invariant and inventory tests."""

from dataclasses import asdict

from market_semantics.adapters import kalshi, polymarket
from market_semantics.contracts import ELECTION_ID, VENUE_CONTRACTS, Limits, json_bytes, sha256
from market_semantics.election import compile_tables
from market_semantics.inputs import verify_bundle

from .adapter_helpers import relations, write_bundle


def release_case(stage, empty=False):
    bundles = {}
    for venue, adapter in (("polymarket", polymarket), ("kalshi", kalshi)):
        rows = relations(venue)
        if empty:
            rows = {name: [] for name in rows}
            rows["coverage"] = [
                {
                    "market_id" if venue == "polymarket" else "market_ticker": "1"
                    if venue == "polymarket"
                    else "M",
                    "status": "absent",
                    "error": None,
                    "requested": True,
                }
            ]
        pin, release = write_bundle(stage / "inputs" / venue, venue, rows)
        bundles[venue] = verify_bundle(
            venue, stage / "inputs" / venue, pin, release, ["1"] if venue == "polymarket" else ["M"]
        )
    curation = {
        "version": 1,
        "election_id": ELECTION_ID,
        "decisions": [],
        "supersedes": {"mappings": {}, "relationships": {}},
    }
    # Preserve noncanonical operator curation bytes and its independently supplied pin.
    curation_bytes = b"  " + json_bytes(curation)
    (stage / "curation.json").write_bytes(curation_bytes)
    selection = {
        "version": 1,
        "election_id": ELECTION_ID,
        "inputs": {
            venue: {
                "path": "/operator/source/" + venue,
                "contract": VENUE_CONTRACTS[venue],
                "source_release_id": bundle.source_release_id,
                "manifest_sha256": bundle.manifest_sha256,
                "selected_market_ids": list(bundle.selected_market_ids),
            }
            for venue, bundle in bundles.items()
        },
        "curation": {"path": "/operator/curation.json", "sha256": sha256(curation_bytes)},
        "limits": asdict(Limits()),
    }
    projections = [
        adapter.adapt(bundles[venue])
        for venue, adapter in (("polymarket", polymarket), ("kalshi", kalshi))
    ]
    tables = compile_tables(projections, curation, bundles, Limits())
    return tables, bundles, curation, selection
