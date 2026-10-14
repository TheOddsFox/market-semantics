"""Consumer handoff proof: preserve native price grains and bind observed definitions.

The fact schemas below mirror the neighboring market-data contracts: Polymarket
feeds.PRICE_SCHEMA/books.IDENTITY_FIELDS and Kalshi normalize.SCHEMAS['book_levels'].
Revision bindings are an explicit consumer sidecar, not invented producer fields.
This fixture demonstrates a consumer join; market-semantics does not serve prices.
"""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

import pyarrow as pa
import pytest

from market_semantics.adapters import kalshi, polymarket
from market_semantics.contracts import (
    ELECTION_ID,
    Limits,
    canonical_json,
    strict_json,
    validate_evidence,
)
from market_semantics.election import compile_tables
from market_semantics.inputs import verify_bundle

from .adapter_helpers import relations, write_bundle

EXACT = pa.decimal128(38, 18)
POLY_PRICE_SCHEMA = pa.schema(
    [
        (name, pa.string())
        for name in ("venue", "asset_kind", "asset_id", "market_id", "condition_id")
    ]
    + [("outcome_index", pa.int32())]
    + [(name, pa.string()) for name in ("acquisition_id", "capture_id", "source")]
    + [(name, pa.timestamp("us", tz="UTC")) for name in ("source_at", "received_at", "ingested_at")]
    + [("price", EXACT)]
)
KALSHI_LEVEL_SCHEMA = pa.schema(
    [(name, pa.string()) for name in ("capture_id", "market_ticker", "outcome_side", "book_side")]
    + [("level_index", pa.int32()), ("price", EXACT), ("quantity", EXACT)]
)
POLY_GRAIN = (
    "source",
    "acquisition_id",
    "capture_id",
    "venue",
    "asset_kind",
    "asset_id",
    "source_at",
)
KALSHI_GRAIN = ("capture_id", "market_ticker", "outcome_side", "book_side", "level_index")
BINDING_FIELDS = ("native_key_json", "input_manifest_sha256", "observation_id", "definition_sha256")


def _compiled(root, observation="observation-1", changed_definition=False):
    bundles, projections = {}, []
    for venue, adapter, market_ids in (
        ("polymarket", polymarket, ["1", "2"]),
        ("kalshi", kalshi, ["M"]),
    ):
        rows = relations(venue)
        if venue == "polymarket":
            market = deepcopy(rows["markets"][0])
            market.update(
                market_id="2",
                condition_id="0x" + "b" * 64,
                source_market_version="v2",
                question="Will Kamala Harris win the 2024 US presidential election?",
            )
            rows["markets"].append(market)
            for original in list(rows["outcomes"]):
                outcome = deepcopy(original)
                outcome.update(
                    market_id="2",
                    condition_id=market["condition_id"],
                    asset_kind="poly_v2_position",
                    position_id=outcome["asset_id"],
                    clob_token_id=None,
                )
                rows["outcomes"].append(outcome)
            rows["coverage"].append(
                {"market_id": "2", "status": "found", "error": None, "requested": True}
            )
            if changed_definition:
                rows["markets"][0]["description"] = "Changed documented election definition."
        else:
            market = rows["markets"][0]
            payload = strict_json(market["payload_json"])
            payload["title"] = "Will Donald Trump win the 2024 US presidential election?"
            market["payload_json"] = canonical_json(payload)
        for name in ("markets", "outcomes", "events", "series"):
            for row in rows.get(name, []):
                row["observation_id"] = observation
        pin, release = write_bundle(root / venue, venue, rows)
        bundles[venue] = verify_bundle(venue, root / venue, pin, release, market_ids)
        projections.append(adapter.adapt(bundles[venue]))
    curation = {
        "version": 1,
        "election_id": ELECTION_ID,
        "decisions": [],
        "supersedes": {"mappings": {}, "relationships": {}},
    }
    tables = compile_tables(projections, curation, bundles, Limits())
    # The handoff contains validated source pointers, not only an opaque claim ID.
    for mapping in tables["outcome_mappings"]:
        assert mapping["status"] == "mapped"
        validate_evidence(strict_json(mapping["evidence_json"]), bundles)
    return tables


def _grain(fact, names):
    return tuple(fact[name] for name in names)


def _binding(native, applicability="as_observed"):
    return {**{key: native[key] for key in BINDING_FIELDS}, "applicability": applicability}


def _identity(native):
    if native["venue"] == "polymarket":
        return ("polymarket", native["asset_kind"], native["asset_id"])
    return ("kalshi", native["native_market_id"], native["side"])


def _left_annotate(facts, venue, tables, bindings):
    """Reference left join after a consumer explicitly selects capture and revision."""
    names = POLY_GRAIN if venue == "polymarket" else KALSHI_GRAIN
    mappings = {
        row["native_outcome_revision_id"]: row
        for row in tables["outcome_mappings"]
        if row["status"] == "mapped"
    }
    annotated = []
    for fact in facts:
        key = (
            ("polymarket", fact["asset_kind"], fact["asset_id"])
            if venue == "polymarket"
            else ("kalshi", fact["market_ticker"], fact["outcome_side"])
        )
        binding = bindings.get(_grain(fact, names))
        matches = []
        if (
            binding
            and binding["applicability"] == "as_observed"
            and all(binding.get(field) is not None for field in BINDING_FIELDS)
        ):
            matches = [
                row
                for row in tables["native_outcomes"]
                if _identity(row) == key
                and all(row[field] == binding[field] for field in BINDING_FIELDS)
                and row["native_outcome_revision_id"] in mappings
            ]
        assert len(matches) <= 1, "consumer must select one compatible observed revision"
        match = matches[0] if matches else None
        annotated.append(
            {
                **fact,
                "semantic_claim_id": (
                    mappings[match["native_outcome_revision_id"]]["claim_id"] if match else None
                ),
                "semantic_status": "as_observed" if match else "unresolved",
                "effective_validity": "unknown",
            }
        )
    return annotated


def _poly_fact(native, minute=0, **changes):
    stamp = datetime(2024, 10, 1, 0, minute, tzinfo=timezone.utc)
    return {
        **{
            key: native[key]
            for key in ("venue", "asset_kind", "asset_id", "condition_id", "outcome_index")
        },
        "market_id": native["native_market_id"],
        "source": "clob-history",
        "acquisition_id": "chosen-acquisition",
        "capture_id": "chosen-capture",
        "source_at": stamp,
        "received_at": stamp,
        "ingested_at": stamp,
        "price": Decimal("0.54"),
        **changes,
    }


def test_polymarket_left_join_preserves_fact_grain_and_typed_id_collisions(tmp_path):
    tables = _compiled(tmp_path / "release")
    natives = [
        row
        for row in tables["native_outcomes"]
        if row["venue"] == "polymarket" and row["asset_id"] == "11"
    ]
    assert {row["asset_kind"] for row in natives} == {"ctf_token", "poly_v2_position"}
    facts = [_poly_fact(native) for native in natives]
    facts += [
        _poly_fact(natives[0], minute=1, price=None),
        _poly_fact(natives[0], minute=2, asset_id="999"),
    ]
    facts = pa.Table.from_pylist(facts, schema=POLY_PRICE_SCHEMA).to_pylist()
    bindings = {
        _grain(fact, POLY_GRAIN): _binding(native)
        for fact, native in zip(facts[:2], natives, strict=True)
    }
    bindings[_grain(facts[2], POLY_GRAIN)] = _binding(natives[0])
    # A binding alone cannot turn an unselected asset into the bound native asset.
    bindings[_grain(facts[3], POLY_GRAIN)] = _binding(natives[0])
    joined = _left_annotate(facts, "polymarket", tables, bindings)
    assert len(joined) == len(facts) == 4
    assert [_grain(row, POLY_GRAIN) for row in joined] == [_grain(row, POLY_GRAIN) for row in facts]
    assert len(set(_grain(row, POLY_GRAIN) for row in joined)) == len(facts)
    assert all(
        {key: row[key] for key in POLY_PRICE_SCHEMA.names} == fact
        for row, fact in zip(joined, facts, strict=True)
    )
    assert joined[0]["semantic_claim_id"] != joined[1]["semantic_claim_id"]
    assert joined[2]["price"] is None and joined[2]["semantic_claim_id"] is not None
    assert joined[3]["semantic_claim_id"] is None and joined[3]["semantic_status"] == "unresolved"


def test_kalshi_native_side_and_book_level_grain_remain_explicit(tmp_path):
    tables = _compiled(tmp_path / "release")
    natives = {row["side"]: row for row in tables["native_outcomes"] if row["venue"] == "kalshi"}
    facts = [
        {
            "capture_id": "chosen-capture",
            "market_ticker": "M",
            "outcome_side": side,
            "book_side": "bid",
            "level_index": index,
            "price": Decimal(price),
            "quantity": Decimal("12"),
        }
        for side, index, price in [("YES", 0, "0.54"), ("YES", 1, "0.52"), ("NO", 0, "0.45")]
    ]
    facts.append({**facts[0], "market_ticker": "UNSELECTED"})
    facts = pa.Table.from_pylist(facts, schema=KALSHI_LEVEL_SCHEMA).to_pylist()
    bindings = {
        _grain(fact, KALSHI_GRAIN): _binding(natives[fact["outcome_side"]]) for fact in facts
    }
    joined = _left_annotate(facts, "kalshi", tables, bindings)
    assert len(joined) == len(facts) == 4
    assert [_grain(row, KALSHI_GRAIN) for row in joined] == [
        _grain(row, KALSHI_GRAIN) for row in facts
    ]
    assert joined[0]["semantic_claim_id"] == joined[1]["semantic_claim_id"]
    assert joined[0]["semantic_claim_id"] != joined[2]["semantic_claim_id"]
    assert joined[3]["semantic_claim_id"] is None
    assert [row["price"] for row in joined] == [fact["price"] for fact in facts]


@pytest.mark.parametrize(
    "missing_or_wrong",
    ["native_key_json", "input_manifest_sha256", "observation_id", "definition_sha256"],
)
def test_incompatible_or_missing_revision_evidence_remains_visible_unresolved(
    tmp_path, missing_or_wrong
):
    first = _compiled(tmp_path / "first")
    later = _compiled(tmp_path / "later", observation="observation-2")
    native = next(row for row in first["native_outcomes"] if row["venue"] == "polymarket")
    same_key = next(
        row
        for row in later["native_outcomes"]
        if row["native_key_json"] == native["native_key_json"]
    )
    assert native["definition_sha256"] == same_key["definition_sha256"]
    assert native["native_outcome_revision_id"] != same_key["native_outcome_revision_id"]
    combined = {name: first[name] + later[name] for name in first}
    fact = _poly_fact(native)
    binding = _binding(native)
    grain = _grain(fact, POLY_GRAIN)
    assert (
        _left_annotate([fact], "polymarket", combined, {grain: binding})[0]["semantic_claim_id"]
        is not None
    )
    broken = {**binding, missing_or_wrong: None}
    joined = _left_annotate([fact], "polymarket", combined, {grain: broken})
    assert len(joined) == 1 and joined[0]["semantic_claim_id"] is None
    broken = {**binding, missing_or_wrong: "mismatched"}
    assert (
        _left_annotate([fact], "polymarket", combined, {grain: broken})[0]["semantic_status"]
        == "unresolved"
    )


def test_changed_definition_and_unknown_historical_applicability_cannot_reuse_current_mapping(
    tmp_path,
):
    observed = _compiled(tmp_path / "observed")
    changed = _compiled(tmp_path / "changed", changed_definition=True)
    native = next(
        row
        for row in observed["native_outcomes"]
        if row["venue"] == "polymarket" and row["native_market_id"] == "1"
    )
    fact = _poly_fact(native)
    key = _grain(fact, POLY_GRAIN)
    assert (
        _left_annotate([fact], "polymarket", changed, {key: _binding(native)})[0][
            "semantic_claim_id"
        ]
        is None
    )
    old_fact = _poly_fact(native, source_at=datetime(2020, 11, 3, tzinfo=timezone.utc))
    joined = _left_annotate(
        [old_fact],
        "polymarket",
        observed,
        {_grain(old_fact, POLY_GRAIN): _binding(native, applicability="historical")},
    )
    assert len(joined) == 1
    assert joined[0]["semantic_status"] == "unresolved"
    assert joined[0]["semantic_claim_id"] is None
    assert joined[0]["effective_validity"] == "unknown"
    # Receipt time and exact identity matching still do not establish valid_from.
    assert not {"valid_from", "valid_to"} & set(native)
    assert all(row["effective_validity"] == "unknown" for row in joined)
