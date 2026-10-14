"""Synthetic producer-shaped targeted bundles, without producer imports."""

from market_semantics.contracts import VENUE_CONTRACTS, canonical_json, json_bytes, sha256


def relations(venue):
    provenance = {"observation_id": "observation-1", "received_at": "2024-10-01T00:00:00Z"}
    if venue == "polymarket":
        market = {
            "market_id": "1",
            "condition_id": "0x" + "a" * 64,
            "source_market_version": "v1",
            "usable": True,
            "question": "Will Donald Trump win the 2024 US presidential election?",
            "description": "Election winner",
            "source_kind": "market_direct",
            **provenance,
        }
        outcomes = [
            {
                "market_id": "1",
                "condition_id": market["condition_id"],
                "asset_kind": "ctf_token",
                "asset_id": str(10 + i),
                "clob_token_id": str(10 + i),
                "position_id": None,
                "chain_index_set": None,
                "outcome_index": i,
                "outcome_label": label,
                "usable": True,
                **provenance,
            }
            for i, label in ((1, "Yes"), (2, "No"))
        ]
        return {
            "markets": [market],
            "outcomes": outcomes,
            "memberships": [],
            "identity_history": [],
            "coverage": [{"market_id": "1", "status": "found", "error": None, "requested": True}],
        }
    series = {"ticker": "S", "title": "Presidential election"}
    event = {"event_ticker": "E", "series_ticker": "S", "title": "2024 election"}
    market = {
        "ticker": "M",
        "event_ticker": "E",
        "title": "Donald Trump",
        "rules_primary": "Wins the 2024 US presidential election",
        "yes_bid": 50,
    }
    return {
        "series": [
            {**series, "series_ticker": "S", "payload_json": canonical_json(series), **provenance}
        ],
        "events": [{**event, "payload_json": canonical_json(event), **provenance}],
        "markets": [
            {
                **market,
                "market_ticker": "M",
                "series_ticker": "S",
                "payload_json": canonical_json(market),
                "usable": True,
                "source_kind": "direct",
                **provenance,
            }
        ],
        "coverage": [{"market_ticker": "M", "status": "found", "error": None}],
    }


def write_bundle(root, venue, rows=None, newline=None):
    root.mkdir(parents=True)
    rows = relations(venue) if rows is None else rows
    inventory = {}
    for relation, values in rows.items():
        body = json_bytes(values)
        (root / (relation + ".json")).write_bytes(body)
        inventory[relation + ".json"] = {"size": len(body), "sha256": sha256(body)}
    if newline is None:
        newline = venue == "polymarket"
    release = sha256((canonical_json(inventory) + ("\n" if newline else "")).encode())
    manifest = {
        "contract": VENUE_CONTRACTS[venue],
        "source_release_id": release,
        "coverage_scope": "targeted",
        "received_at": "2024-10-01T00:00:00Z",
        "files": inventory,
    }
    body = json_bytes(manifest)
    (root / "manifest.json").write_bytes(body)
    return sha256(body), release
