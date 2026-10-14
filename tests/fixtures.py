"""Shared synthetic operator inputs; interoperability uses real producer executables."""

from copy import deepcopy
from pathlib import Path

from market_semantics.adapters.kalshi import adapt as kalshi_adapt
from market_semantics.adapters.polymarket import adapt as polymarket_adapt
from market_semantics.contracts import (
    ELECTION_ID,
    Limits,
    canonical_json,
    empty_policy,
    json_bytes,
    sha256,
)
from market_semantics.inputs import verify_bundle

from .adapter_helpers import relations, write_bundle

QUESTION = "Will Donald Trump win the 2024 US presidential election?"
RULES = (
    "Synthetic fixture only. Donald Trump wins the 2024 US presidential general election. "
    "The synthetic election board's final certification and final adjudication including recounts "
    "determine the result by January 20, 2025 at 17:00 UTC. Postponement waits for that deadline. "
    "YES pays 1 on win, 0 on loss, 0.5 on cancellation and 0 on withdrawal. "
    "NO pays the complementary amount in every scenario."
)


def full_policy(evidence):
    policy = empty_policy()
    values = {
        "winner_criterion": "election_winner",
        "election_id": ELECTION_ID,
        "round": "general",
        "candidate_id": "person:donald-j-trump",
        "deadline": {"at": "2025-01-20T17:00:00Z", "timezone": "UTC"},
        "resolution_sources": ["synthetic election board"],
        "certification": "final certification",
        "recount": "final result",
        "dispute": "final adjudication",
        "postponement": "wait",
        "cancellation": "split",
        "withdrawal": "lose",
        "payout": {"win": "1", "lose": "0", "cancel": "0.5", "withdrawal": "0"},
    }
    for key, value in values.items():
        policy["fields"][key] = {"state": "known", "value": value, "evidence": [deepcopy(evidence)]}
    return policy


def make_case(root: Path, *, curated=False, limits=None):
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    inputs, bundles, projections = {}, {}, []
    for venue, adapter, market_id in (
        ("polymarket", polymarket_adapt, "1"),
        ("kalshi", kalshi_adapt, "M"),
    ):
        rows = relations(venue)
        if venue == "polymarket":
            rows["markets"][0]["question"] = QUESTION
            rows["markets"][0]["description"] = RULES
        else:
            row = rows["markets"][0]
            payload = {
                "ticker": "M",
                "event_ticker": "E",
                "title": QUESTION,
                "market_type": "binary",
                "rules_primary": RULES,
                "rules_secondary": "",
            }
            row.update(payload)
            row["payload_json"] = canonical_json(payload)
        path = root / venue
        pin, release = write_bundle(path, venue, rows)
        bundles[venue] = verify_bundle(venue, path, pin, release, [market_id], Limits())
        projections.append(adapter(bundles[venue]))
        inputs[venue] = {
            "path": str(path),
            "contract": bundles[venue].manifest["contract"],
            "source_release_id": release,
            "manifest_sha256": pin,
            "selected_market_ids": [market_id],
        }
    curation = {
        "version": 1,
        "election_id": ELECTION_ID,
        "decisions": [],
        "supersedes": {"mappings": {}, "relationships": {}},
    }
    if curated:
        for projection in projections:
            for row in projection.native_outcomes:
                venue = row["venue"]
                evidence = {
                    "venue": venue,
                    "file": "markets.json",
                    "pointer": "/0/description" if venue == "polymarket" else "/0/payload_json",
                    "value_pointer": None if venue == "polymarket" else "/rules_primary",
                    "quote": RULES,
                }
                decision = {
                    key: row[key]
                    for key in (
                        "venue",
                        "native_market_id",
                        "observation_id",
                        "definition_sha256",
                        "input_manifest_sha256",
                    )
                }
                decision.update(
                    candidate_id="person:donald-j-trump",
                    yes_polarity=True,
                    evidence=[evidence],
                    policy=full_policy(evidence),
                )
                curation["decisions"].append(decision)
    curation_path = root / "reviewed-curation.json"
    curation_body = json_bytes(curation)
    curation_path.write_bytes(curation_body)
    selection = {
        "version": 1,
        "election_id": ELECTION_ID,
        "inputs": inputs,
        "curation": {"path": str(curation_path), "sha256": sha256(curation_body)},
        "limits": limits or {},
    }
    path = root / "selection.json"
    path.write_bytes(json_bytes(selection))
    return path, selection, curation
