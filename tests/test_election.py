"""Semantic regressions with hand-built expected meanings and source evidence."""

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_semantics.contracts import (
    ELECTION_ID,
    SCHEMAS,
    VENUE_FILES,
    Limits,
    Projection,
    SemanticsError,
    VerifiedBundle,
    canonical_json,
    empty_policy,
    native_revision_id,
    sha256,
    strict_json,
)
from market_semantics.election import compare_policies, compile_tables, validate_curation


def empty_curation():
    return {
        "version": 1,
        "election_id": ELECTION_ID,
        "decisions": [],
        "supersedes": {"mappings": {}, "relationships": {}},
    }


def source(
    venue="polymarket",
    candidate="Donald Trump",
    market_id="market-1",
    question=None,
    description="",
):
    question = question or f"Will {candidate} win the 2024 US Presidential Election?"
    source_market = {
        "market_id": market_id,
        "market_ticker": market_id,
        "observation_id": "observation-1",
        "question": question,
        "description": description,
    }
    relations = {name: [] for name in VENUE_FILES[venue]}
    relations["markets"] = [source_market]
    relations["outcomes"] = [
        {"market_id": market_id, "observation_id": "observation-1", "outcome_label": side}
        for side in ("Yes", "No")
    ]
    bundle = VerifiedBundle(
        venue,
        Path("/synthetic"),
        {"source_release_id": "release-1"},
        "a" * 64,
        relations,
        (market_id,),
    )
    natives = []
    for index, side in enumerate(("YES", "NO"), 1):
        definition = {"market": source_market, "outcome": {"label": side.title()}, "parents": {}}
        row = dict.fromkeys(SCHEMAS["native_outcomes"].names)
        row.update(
            venue=venue,
            native_market_id=market_id,
            side=side if venue == "kalshi" else None,
            asset_kind="token" if venue == "polymarket" else None,
            asset_id=f"{market_id}-{index}" if venue == "polymarket" else None,
            outcome_label=side.title(),
            native_key_json=canonical_json({"venue": venue, "market_id": market_id, "side": side}),
            input_manifest_sha256="a" * 64,
            source_release_id="release-1",
            observation_id="observation-1",
            received_at=datetime(2024, 11, 1, tzinfo=timezone.utc),
            source_updated_at_raw_json="null",
            definition_sha256=sha256(canonical_json(definition).encode()),
            definition_json=canonical_json(definition),
            policy_json=canonical_json(empty_policy()),
            usable=True,
            diagnostics_json="[]",
        )
        row["native_outcome_revision_id"] = native_revision_id(row)
        natives.append(row)
    coverage = dict.fromkeys(SCHEMAS["coverage"].names)
    coverage.update(
        venue=venue,
        native_market_id=market_id,
        producer_status="found",
        source_usable=True,
        observed_outcome_count=2,
        usable_outcome_count=2,
        exported_outcome_count=2,
        mapped_count=0,
        unresolved_count=0,
        unsupported_count=0,
        unusable_count=0,
        semantic_disposition="pending",
        diagnostics_json="[]",
    )
    evidence = {
        "venue": venue,
        "file": "markets.json",
        "pointer": "/0/question",
        "value_pointer": None,
        "quote": question,
    }
    return Projection(natives, [coverage]), bundle, evidence


def complete_policy(evidence):
    policy = empty_policy()
    values = {
        "winner_criterion": "election_winner",
        "election_id": ELECTION_ID,
        "round": "general",
        "candidate_id": "person:donald-j-trump",
        "deadline": {"at": "2025-01-20T17:00:00Z", "timezone": "America/New_York"},
        "resolution_sources": ["official election certification"],
        "certification": "final certification",
        "recount": "final result",
        "dispute": "final adjudication",
        "postponement": "wait",
        "cancellation": "refund",
        "withdrawal": "lose",
        "payout": {"win": "1", "lose": "0", "cancel": "0.5", "withdrawal": "0"},
    }
    for key, value in values.items():
        policy["fields"][key] = {"state": "known", "value": value, "evidence": [evidence]}
    return policy


def curate(projection, evidence, policy=None):
    curation = empty_curation()
    for row in projection.native_outcomes:
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
            policy=policy or empty_policy(),
        )
        curation["decisions"].append(decision)
    return curation


def test_exact_winner_maps_both_sides_without_fabricating_settlement_policy():
    projection, bundle, _ = source()
    tables = compile_tables([projection], empty_curation(), {"polymarket": bundle}, Limits())
    assert {row["polarity"] for row in tables["claims"]} == {True, False}
    assert len(tables["entities"]) == 4
    assert [row["status"] for row in tables["outcome_mappings"]] == ["mapped", "mapped"]
    assert tables["coverage"][0]["mapped_count"] == 2
    inverse = next(row for row in tables["relationships"] if row["relationship_type"] == "inverse")
    assert inverse["status"] == "unresolved"
    assert projection.native_outcomes[0]["policy_json"] == canonical_json(empty_policy())


@pytest.mark.parametrize(
    "question",
    [
        "Will Donald Trump win the 2024 US presidential popular vote?",
        "Will Donald Trump win the 2024 Republican nomination?",
        "Will Donald Trump be the first inaugurated president in 2025?",
        "Will Donald Trump win the 2024 US Presidential Election or a party representative win?",
    ],
)
def test_excluded_predicates_cannot_be_promoted_by_curation(question):
    projection, bundle, evidence = source(question=question)
    tables = compile_tables(
        [projection], curate(projection, evidence), {"polymarket": bundle}, Limits()
    )
    assert {row["status"] for row in tables["outcome_mappings"]} == {"unsupported"}
    assert not tables["claims"]
    assert not tables["relationships"]


def test_unknown_question_stays_unresolved_and_missing_outcomes_are_not_created():
    projection, bundle, _ = source(question="Who will win?")
    projection.native_outcomes = projection.native_outcomes[:1]
    tables = compile_tables([projection], empty_curation(), {"polymarket": bundle}, Limits())
    assert len(tables["native_outcomes"]) == 1
    assert tables["outcome_mappings"][0]["status"] == "unresolved"
    assert tables["coverage"][0]["exported_outcome_count"] == 1


def test_no_candidate_a_does_not_imply_yes_candidate_b():
    first, bundle_a, _ = source(market_id="trump")
    second, bundle_b, _ = source(venue="kalshi", candidate="Kamala Harris", market_id="harris")
    tables = compile_tables(
        [first, second], empty_curation(), {"polymarket": bundle_a, "kalshi": bundle_b}, Limits()
    )
    for row in tables["relationships"]:
        assert {row["left_revision_id"], row["right_revision_id"]} <= (
            {n["native_outcome_revision_id"] for n in first.native_outcomes}
        ) or {row["left_revision_id"], row["right_revision_id"]} <= (
            {n["native_outcome_revision_id"] for n in second.native_outcomes}
        )


def test_compatible_complete_policy_and_complementary_payouts_accept():
    projection, bundle, evidence = source()
    policy = complete_policy(evidence)
    tables = compile_tables(
        [projection], curate(projection, evidence, policy), {"polymarket": bundle}, Limits()
    )
    inverse = next(row for row in tables["relationships"] if row["relationship_type"] == "inverse")
    assert inverse["status"] == "accepted"
    assert compare_policies(policy, policy)[0] == "accepted"
    assert compare_policies(policy, policy, inverse=True)[0] == "rejected"


def test_demonstrated_incompatibility_rejects_even_with_other_unknown_fields():
    _, _, evidence = source()
    policy = complete_policy(evidence)
    other = deepcopy(policy)
    other["fields"]["withdrawal"]["value"] = "refund"
    other["fields"]["certification"] = empty_policy()["fields"]["certification"]
    assert compare_policies(policy, other)[0:2] == ("rejected", "INCOMPATIBLE_POLICY")


def test_unparsed_material_clauses_prevent_acceptance():
    _, _, evidence = source()
    policy = complete_policy(evidence)
    policy["unparsed_clauses"] = ["Unhandled contingent election."]
    assert compare_policies(policy, policy)[0] == "unresolved"


def test_core_policy_must_match_claim_and_unsupported_known_values_reject():
    projection, bundle, evidence = source()
    policy = complete_policy(evidence)
    policy["fields"]["winner_criterion"]["value"] = "first_inaugurated"
    with pytest.raises(SemanticsError, match="contradicts"):
        compile_tables(
            [projection], curate(projection, evidence, policy), {"polymarket": bundle}, Limits()
        )
    assert compare_policies(policy, policy)[0] == "rejected"


@pytest.mark.parametrize("mutation", ["stale", "extra", "quote", "conflict", "unknown_revision"])
def test_curation_fails_closed(mutation):
    projection, bundle, evidence = source()
    curation = curate(projection, evidence)
    if mutation == "stale":
        curation["decisions"][0]["definition_sha256"] = "b" * 64
    elif mutation == "extra":
        curation["decisions"][0]["force_accept"] = True
    elif mutation == "quote":
        curation["decisions"][0]["evidence"][0]["quote"] = "fabricated"
    elif mutation == "conflict":
        curation["decisions"].append({**curation["decisions"][0], "yes_polarity": False})
    else:
        curation["supersedes"]["mappings"]["native:" + "b" * 64] = "mapping:" + "c" * 64
    with pytest.raises(SemanticsError):
        validate_curation(curation, projection.native_outcomes, {"polymarket": bundle})


def test_curation_cannot_cite_a_different_market_with_identical_question():
    projection, bundle, evidence = source()
    bundle.relations["markets"].append(
        {**bundle.relations["markets"][0], "market_id": "other", "market_ticker": "other"}
    )
    evidence["pointer"] = "/1/question"
    with pytest.raises(SemanticsError, match="different native definition"):
        validate_curation(
            curate(projection, evidence), projection.native_outcomes, {"polymarket": bundle}
        )


def test_explicit_supersession_and_relationship_bounds():
    projection, bundle, _ = source()
    curation = empty_curation()
    revision = projection.native_outcomes[0]["native_outcome_revision_id"]
    curation["supersedes"]["mappings"][revision] = "mapping:" + "b" * 64
    ids = sorted(row["native_outcome_revision_id"] for row in projection.native_outcomes)
    curation["supersedes"]["relationships"][canonical_json([*ids, "inverse"])] = (
        "relationship:" + "c" * 64
    )
    tables = compile_tables([projection], curation, {"polymarket": bundle}, Limits())
    assert any(
        row["supersedes_assessment_id"] == "mapping:" + "b" * 64
        for row in tables["outcome_mappings"]
    )
    with pytest.raises(SemanticsError, match="comparison limit"):
        compile_tables([projection], curation, {"polymarket": bundle}, Limits(max_comparisons=1))


@pytest.mark.parametrize("extra_outcome", [None, {"outcome_label": "Other"}])
def test_nonbinary_or_missing_source_outcomes_cannot_be_curated(extra_outcome):
    projection, bundle, evidence = source()
    if extra_outcome is None:
        bundle.relations["outcomes"].pop()
    else:
        bundle.relations["outcomes"].append({"market_id": "market-1", **extra_outcome})
    tables = compile_tables(
        [projection], curate(projection, evidence), {"polymarket": bundle}, Limits()
    )
    assert {row["reason_code"] for row in tables["outcome_mappings"]} == {"NON_BINARY_MARKET"}


def test_compound_question_cannot_be_curated_into_atomic_claim():
    projection, bundle, evidence = source(
        question="Will Donald Trump win the 2024 US Presidential Election and control Congress?"
    )
    tables = compile_tables(
        [projection], curate(projection, evidence), {"polymarket": bundle}, Limits()
    )
    assert {row["reason_code"] for row in tables["outcome_mappings"]} == {"COMPOSITE_QUESTION"}


def test_not_applicable_payout_does_not_prove_complementarity():
    _, _, evidence = source()
    policy = complete_policy(evidence)
    policy["fields"]["payout"] = {"state": "not_applicable", "value": None, "evidence": [evidence]}
    assert compare_policies(policy, policy, inverse=True)[0] == "unresolved"


def test_negative_payout_complements_have_canonical_decimal_representation():
    projection, bundle, evidence = source()
    policy = complete_policy(evidence)
    policy["fields"]["payout"]["value"]["cancel"] = "0.25"
    tables = compile_tables(
        [projection], curate(projection, evidence, policy), {"polymarket": bundle}, Limits()
    )
    no = next(row for row in tables["native_outcomes"] if row["outcome_label"] == "No")
    assert '"cancel":"0.75"' in no["policy_json"]


def test_kalshi_native_encoded_payload_uses_exact_nested_source_evidence():
    projection, original, _ = source(venue="kalshi")
    payload = {"title": original.relations["markets"][0]["question"]}
    original.relations["markets"][0] = {
        "market_ticker": "market-1",
        "observation_id": "observation-1",
        "payload_json": canonical_json(payload),
    }
    for row in projection.native_outcomes:
        definition = {"market": payload, "outcome": {"side": row["side"]}, "parents": {}}
        row["definition_json"] = canonical_json(definition)
        row["definition_sha256"] = sha256(canonical_json(definition).encode())
        row["native_outcome_revision_id"] = native_revision_id(row)
    tables = compile_tables([projection], empty_curation(), {"kalshi": original}, Limits())
    assert {row["status"] for row in tables["outcome_mappings"]} == {"mapped"}
    assert all(
        '"value_pointer":"/title"' in row["evidence_json"] for row in tables["outcome_mappings"]
    )


def test_first_inauguration_or_party_replacement_remains_outside_winner_claims():
    projection, bundle, evidence = source(
        venue="kalshi",
        description="Pays if Donald Trump or another Republican representative "
        "is the first person inaugurated in 2025.",
    )
    tables = compile_tables(
        [projection], curate(projection, evidence), {"kalshi": bundle}, Limits()
    )
    assert not tables["claims"]
    assert {row["reason_code"] for row in tables["outcome_mappings"]} == {"UNSUPPORTED_PREDICATE"}


@pytest.mark.parametrize("field", ["deadline", "resolution_sources", "withdrawal"])
def test_not_applicable_is_diagnostic_not_a_compatibility_proof(field):
    _, _, evidence = source()
    policy = complete_policy(evidence)
    policy["fields"][field] = {"state": "not_applicable", "value": None, "evidence": [evidence]}
    assert compare_policies(policy, policy)[0] == "unresolved"


def test_curation_cannot_reverse_positive_source_rules_with_an_unrecognized_question():
    description = (
        "Donald Trump wins the 2024 US presidential election. YES pays 1 if he wins and NO pays 0."
    )
    projection, bundle, _ = source(question="Donald Trump", description=description)
    evidence = {
        "venue": "polymarket",
        "file": "markets.json",
        "pointer": "/0/description",
        "value_pointer": None,
        "quote": description,
    }
    curation = curate(projection, evidence)
    for decision in curation["decisions"]:
        decision["yes_polarity"] = False
    with pytest.raises(SemanticsError, match="negative YES orientation"):
        compile_tables([projection], curation, {"polymarket": bundle}, Limits())


def test_reviewed_positive_disambiguation_keeps_native_no_inversion():
    description = "Donald Trump wins the 2024 US presidential election. YES pays 1 if he wins."
    projection, bundle, _ = source(question="Donald Trump", description=description)
    evidence = {
        "venue": "polymarket",
        "file": "markets.json",
        "pointer": "/0/description",
        "value_pointer": None,
        "quote": description,
    }
    tables = compile_tables(
        [projection], curate(projection, evidence), {"polymarket": bundle}, Limits()
    )
    claims = {row["claim_id"]: row for row in tables["claims"]}
    mapping = {row["native_outcome_revision_id"]: row for row in tables["outcome_mappings"]}
    for native in tables["native_outcomes"]:
        claim = claims[mapping[native["native_outcome_revision_id"]]["claim_id"]]
        assert claim["polarity"] is (native["outcome_label"] == "Yes")


def test_curation_cannot_reverse_a_recognized_positive_question():
    projection, bundle, evidence = source()
    curation = curate(projection, evidence)
    for decision in curation["decisions"]:
        decision["yes_polarity"] = False
    with pytest.raises(SemanticsError, match="conflicts with recognized"):
        compile_tables([projection], curation, {"polymarket": bundle}, Limits())


def test_deadline_equivalence_uses_instant_with_declared_timezone_preserved():
    _, _, evidence = source()
    left = complete_policy(evidence)
    right = deepcopy(left)
    right["fields"]["deadline"]["value"]["at"] = "2025-01-20T12:00:00-05:00"
    assert compare_policies(left, right)[0] == "accepted"
    right["fields"]["deadline"]["value"]["at"] = "2025-01-20T12:01:00-05:00"
    assert compare_policies(left, right)[0] == "rejected"
    right["fields"]["deadline"]["value"]["at"] = "2025-01-20T12:00:00-05:00"
    right["fields"]["deadline"]["value"]["timezone"] = "UTC"
    assert compare_policies(left, right)[0] == "rejected"


@pytest.mark.parametrize("curated", [False, True])
@pytest.mark.parametrize(
    "description,reason",
    [
        (
            "Resolves Yes if Kamala Harris wins the 2024 US presidential election.",
            "CONTRADICTORY_SOURCE_CANDIDATE",
        ),
        (
            "Resolves Yes if Donald Trump does not win the 2024 US presidential election.",
            "UNPARSED_RESOLUTION_CONDITION",
        ),
        (
            "Resolves Yes if Donald Trump is sworn in as President on January 20, 2025, "
            "regardless of who won the 2024 presidential election.",
            "UNSUPPORTED_PREDICATE",
        ),
        ("Resolves Yes if Donald Trump takes office in January 2025.", "UNSUPPORTED_PREDICATE"),
        (
            "Resolves Yes if Donald Trump wins the 2028 US presidential election.",
            "UNPARSED_RESOLUTION_CONDITION",
        ),
        (
            "Resolves Yes if Donald Trump wins the 2024 Florida gubernatorial election.",
            "UNPARSED_RESOLUTION_CONDITION",
        ),
        (
            "Resolves Yes if Donald Trump wins the 2024 US presidential special election.",
            "UNPARSED_RESOLUTION_CONDITION",
        ),
        (
            "Resolves Yes if the electoral commission names its preferred candidate.",
            "UNPARSED_RESOLUTION_CONDITION",
        ),
    ],
)
def test_material_resolution_rules_override_positive_title(description, reason, curated):
    projection, bundle, evidence = source(description=description)
    curation = curate(projection, evidence) if curated else empty_curation()
    tables = compile_tables([projection], curation, {"polymarket": bundle}, Limits())
    assert not tables["claims"]
    assert {row["reason_code"] for row in tables["outcome_mappings"]} == {reason}
    assert all(row["status"] != "mapped" for row in tables["outcome_mappings"])


@pytest.mark.parametrize(
    "description",
    [
        "Resolves Yes if Donald Trump wins the 2024 US presidential election.",
        'This market will resolve to "Yes" if Donald J. Trump wins the 2024 U.S. presidential election.',
        "Donald Trump wins the 2024 United States presidential general election.",
    ],
)
def test_closed_supported_resolution_clause_confirms_individual_winner(description):
    projection, bundle, _ = source(description=description)
    tables = compile_tables([projection], empty_curation(), {"polymarket": bundle}, Limits())
    assert {row["status"] for row in tables["outcome_mappings"]} == {"mapped"}


@pytest.mark.parametrize(
    "description",
    [
        "If the commission names its preferred candidate, this market resolves Yes.",
        "YES when the commission chooses its candidate.",
        "YES unless the commission objects to the result.",
    ],
)
def test_other_conditional_resolution_word_orders_remain_unresolved(description):
    projection, bundle, evidence = source(description=description)
    tables = compile_tables(
        [projection], curate(projection, evidence), {"polymarket": bundle}, Limits()
    )
    assert not tables["claims"]
    assert {row["reason_code"] for row in tables["outcome_mappings"]} == {
        "UNPARSED_RESOLUTION_CONDITION"
    }


@pytest.mark.parametrize(
    "description",
    [
        "This market resolves according to the commission's preferred candidate.",
        "The resolution uses a separate appointment process.",
    ],
)
def test_unparsed_resolution_without_an_if_clause_cannot_be_promoted(description):
    projection, bundle, evidence = source(description=description)
    tables = compile_tables(
        [projection], curate(projection, evidence), {"polymarket": bundle}, Limits()
    )
    assert not tables["claims"]
    assert {row["reason_code"] for row in tables["outcome_mappings"]} == {
        "UNPARSED_RESOLUTION_CONDITION"
    }


@pytest.mark.parametrize("parent", ["events", "series"])
def test_authoritative_parent_rule_contradiction_cannot_be_overridden_by_market_title(
    tmp_path, parent
):
    from market_semantics.adapters import kalshi
    from market_semantics.inputs import verify_bundle

    from .adapter_helpers import relations, write_bundle

    rows = relations("kalshi")
    payload = {
        "ticker": "M",
        "event_ticker": "E",
        "title": "Will Donald Trump win the 2024 US presidential election?",
        "rules_primary": "Donald Trump wins the 2024 US presidential election.",
    }
    rows["markets"][0]["payload_json"] = canonical_json(payload)
    parent_payload = strict_json(rows[parent][0]["payload_json"])
    parent_payload["rules_primary"] = (
        "Resolves Yes if Kamala Harris wins the 2024 US presidential election."
    )
    rows[parent][0]["payload_json"] = canonical_json(parent_payload)
    pin, release = write_bundle(tmp_path / "input", "kalshi", rows)
    bundle = verify_bundle("kalshi", tmp_path / "input", pin, release, ["M"])
    tables = compile_tables([kalshi.adapt(bundle)], empty_curation(), {"kalshi": bundle}, Limits())
    assert not tables["claims"]
    assert {row["reason_code"] for row in tables["outcome_mappings"]} == {
        "CONTRADICTORY_SOURCE_CANDIDATE"
    }
