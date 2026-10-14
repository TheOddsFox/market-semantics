"""Recompute release invariants against retained producer evidence, without compilation."""

from __future__ import annotations

import re
from collections import Counter

import pyarrow as pa

from market_semantics.contracts import (
    CANDIDATES,
    CORE_POLICY_FIELDS,
    ELECTION_ID,
    RULE_REVISION,
    SCHEMAS,
    SCOPE,
    SORT_KEYS,
    VENUE_CONTRACTS,
    assessment_id,
    canonical_json,
    claim_payload,
    identity,
    native_revision_id,
    require,
    strict_json,
    validate_evidence,
    validate_policy,
)


def _check(condition, message):
    require(condition, message, "VALIDATION_FAILED")


def _json(value):
    parsed = strict_json(value)
    _check(canonical_json(parsed) == value, "noncanonical nested JSON")
    return parsed


def _rows(tables, limits):
    _check(isinstance(tables, dict) and set(tables) == set(SCHEMAS), "invalid relation set")
    for name, schema in SCHEMAS.items():
        rows = tables[name]
        _check(isinstance(rows, list), "relation must be a row list")
        require(
            len(rows) <= limits.max_records_per_bundle,
            "relation row limit exceeded",
            "LIMIT_EXCEEDED",
        )
        keys = set()
        for row in rows:
            _check(
                isinstance(row, dict) and set(row) == set(schema.names),
                f"row fields differ: {name}",
            )
            for field in schema:
                value = row[field.name]
                if value is None:
                    _check(field.nullable, f"null in required field: {name}.{field.name}")
                elif pa.types.is_string(field.type):
                    _check(isinstance(value, str), "expected string field")
                    if field.name.endswith("_json"):
                        _json(value)
                elif pa.types.is_boolean(field.type):
                    _check(type(value) is bool, "expected Boolean field")
                elif pa.types.is_integer(field.type):
                    _check(type(value) is int and -(2**31) <= value < 2**31, "expected int32 field")
            key = tuple(row[column] for column in SORT_KEYS[name])
            _check(key not in keys, f"duplicate relation key: {name}")
            keys.add(key)
        try:
            table = pa.Table.from_pylist(rows, schema=schema)
            require(
                table.nbytes <= limits.max_release_bytes,
                "decoded table exceeds limit",
                "LIMIT_EXCEEDED",
            )
        except (pa.ArrowException, TypeError, ValueError) as error:
            from market_semantics.contracts import SemanticsError

            raise SemanticsError("VALIDATION_FAILED", f"invalid typed relation: {name}") from error


def _supersedes(curation, mappings, relationships):
    _check(
        isinstance(curation, dict)
        and set(curation) == {"version", "election_id", "decisions", "supersedes"},
        "invalid curation schema",
    )
    _check(
        type(curation["version"]) is int
        and curation["version"] == 1
        and curation["election_id"] == ELECTION_ID,
        "unsupported curation",
    )
    supersedes = curation["supersedes"]
    _check(
        isinstance(supersedes, dict) and set(supersedes) == {"mappings", "relationships"},
        "invalid supersedes schema",
    )
    current = {
        "mappings": {row["native_outcome_revision_id"]: row for row in mappings.values()},
        "relationships": {
            canonical_json(
                [row["left_revision_id"], row["right_revision_id"], row["relationship_type"]]
            ): row
            for row in relationships
        },
    }
    for kind, rows in current.items():
        supplied = supersedes[kind]
        _check(
            isinstance(supplied, dict) and set(supplied) <= set(rows),
            "unknown current supersedes key",
        )
        for key, row in rows.items():
            prior = supplied.get(key)
            _check(
                prior is None
                or isinstance(prior, str)
                and re.fullmatch(r"(?:mapping|relationship):[0-9a-f]{64}", prior),
                "invalid prior assessment ID",
            )
            _check(
                row["supersedes_assessment_id"] == prior,
                "assessment supersedes differs from curation",
            )


def _entities_claims(tables, bundles):
    entities = {row["entity_id"]: row for row in tables["entities"]}
    allowed = {
        **{key: "person" for key in CANDIDATES},
        ELECTION_ID: "election",
        SCOPE["office_id"]: "office",
        SCOPE["jurisdiction_id"]: "jurisdiction",
    }
    for key, row in entities.items():
        _check(key in allowed and row["entity_type"] == allowed[key], "unknown canonical entity")
        _check(
            bool(row["label"])
            and isinstance(_json(row["aliases_json"]), list)
            and all(isinstance(x, str) for x in _json(row["aliases_json"])),
            "invalid entity labels",
        )
        evidence = _json(row["evidence_json"])
        _check(bool(evidence), "entity assignment lacks evidence")
        validate_evidence(evidence, bundles)
        _check(isinstance(_json(row["identity_json"]), dict), "invalid entity identity")
    claims = {row["claim_id"]: row for row in tables["claims"]}
    for key, row in claims.items():
        _check(
            row["candidate_id"] in CANDIDATES and row["election_id"] == ELECTION_ID,
            "unsupported claim arguments",
        )
        payload = claim_payload(row["candidate_id"], row["polarity"])
        _check(
            _json(row["canonical_json"]) == payload
            and _json(row["scope_json"]) == SCOPE
            and row["schema_version"] == 1
            and row["predicate"] == "wins_election"
            and key == identity("claim", payload),
            "invalid claim identity",
        )
        _check(
            {row["candidate_id"], ELECTION_ID, SCOPE["office_id"], SCOPE["jurisdiction_id"]}
            <= set(entities),
            "claim references missing entities",
        )
    return entities, claims


def validate_tables(tables, bundles, curation, limits):
    from market_semantics.adapters import kalshi, polymarket
    from market_semantics.election import compare_policies

    _rows(tables, limits)
    _check(set(bundles) == set(VENUE_CONTRACTS), "both source bundles required")
    projections = [
        adapter.adapt(bundles[venue])
        for venue, adapter in (("polymarket", polymarket), ("kalshi", kalshi))
    ]
    expected_native = {
        row["native_outcome_revision_id"]: row
        for projection in projections
        for row in projection.native_outcomes
    }
    native = {row["native_outcome_revision_id"]: row for row in tables["native_outcomes"]}
    _check(
        set(native) == set(expected_native),
        "native outcomes differ from eligible source identities",
    )
    for key, row in native.items():
        _check(
            {k: v for k, v in row.items() if k != "policy_json"}
            == {k: v for k, v in expected_native[key].items() if k != "policy_json"},
            "native projection differs from retained source",
        )
        _check(key == native_revision_id(row), "native revision ID differs")
        validate_policy(_json(row["policy_json"]), bundles)
    entities, claims = _entities_claims(tables, bundles)
    mappings = {row["native_outcome_revision_id"]: row for row in tables["outcome_mappings"]}
    _check(set(mappings) == set(native), "every native outcome requires one mapping assessment")
    used_claims = set()
    for key, row in mappings.items():
        _check(
            row["assessment_id"] == assessment_id("mapping", row), "mapping assessment ID differs"
        )
        _check(row["rule_revision"] == RULE_REVISION, "unknown mapping rule")
        _check(
            row["status"] in {"mapped", "unresolved", "unsupported", "unusable"}
            and bool(row["reason_code"])
            and bool(row["method"]),
            "invalid mapping status",
        )
        evidence = _json(row["evidence_json"])
        validate_evidence(evidence, bundles)
        _check(
            (row["claim_id"] is not None) == (row["status"] == "mapped"),
            "only mapped assessments may reference claims",
        )
        if row["status"] == "mapped":
            _check(
                native[key]["usable"] and row["claim_id"] in claims and bool(evidence),
                "accepted mapping requires usable identity, claim and evidence",
            )
            claim = claims[row["claim_id"]]
            policy = _json(native[key]["policy_json"])
            for field, expected in {
                "candidate_id": claim["candidate_id"],
                "election_id": claim["election_id"],
                "round": "general",
                "winner_criterion": "election_winner",
            }.items():
                value = policy["fields"][field]
                _check(
                    value["state"] == "known" and value["value"] == expected,
                    "mapped policy identity differs from claim",
                )
            used_claims.add(row["claim_id"])
        elif row["status"] == "unusable":
            _check(not native[key]["usable"], "usable outcome marked unusable")
    _check(used_claims == set(claims), "orphan claim")
    relationships = tables["relationships"]
    require(
        len(relationships) <= limits.max_comparisons,
        "relationship limit exceeded",
        "LIMIT_EXCEEDED",
    )
    for row in relationships:
        left, right = row["left_revision_id"], row["right_revision_id"]
        kind = row["relationship_type"]
        _check(
            left < right and left in native and right in native, "invalid relationship endpoints"
        )
        _check(
            row["assessment_id"] == assessment_id("relationship", row)
            and row["rule_revision"] == RULE_REVISION,
            "relationship revision differs",
        )
        _check(
            kind in {"related", "equivalent", "inverse"}
            and row["status"] in {"accepted", "rejected", "unresolved"},
            "invalid relationship status",
        )
        evidence = _json(row["evidence_json"])
        validate_evidence(evidence, bundles)
        lm, rm = mappings[left], mappings[right]
        _check(lm["status"] == rm["status"] == "mapped", "relationship requires mapped outcomes")
        lc, rc = claims[lm["claim_id"]], claims[rm["claim_id"]]
        _check(lc["election_id"] == rc["election_id"], "unrelated elections assessed")
        if kind in {"equivalent", "inverse"}:
            _check(
                lc["candidate_id"] == rc["candidate_id"]
                and (lc["polarity"] != rc["polarity"]) == (kind == "inverse"),
                "relationship claim identities differ",
            )
            status, reason, comparison = compare_policies(
                _json(native[left]["policy_json"]),
                _json(native[right]["policy_json"]),
                inverse=kind == "inverse",
            )
            _check(
                row["status"] == status
                and row["reason_code"] == reason
                and _json(row["comparison_json"]) == comparison,
                "policy comparison differs from recomputed result",
            )
        else:
            _check(
                lc["candidate_id"] == rc["candidate_id"]
                and row["status"] == "accepted"
                and row["reason_code"] == "SHARED_CANDIDATE_ELECTION"
                and _json(row["comparison_json"]) == {},
                "invalid related assessment",
            )
        expected_evidence = sorted(
            {
                canonical_json(item): item
                for mapping in (lm, rm)
                for item in _json(mapping["evidence_json"])
            }.values(),
            key=canonical_json,
        )
        _check(
            evidence == expected_evidence and row["curation_decision_id"] is None,
            "relationship evidence differs from its mappings",
        )
        if row["status"] == "accepted":
            _check(bool(evidence), "accepted relationship lacks evidence")
            for revision in (left, right):
                fields = _json(native[revision]["policy_json"])["fields"]
                _check(
                    all(fields[field]["state"] == "known" for field in CORE_POLICY_FIELDS),
                    "accepted relationship has unknown identity",
                )
    _supersedes(curation, mappings, relationships)
    _semantic_assignments(native, mappings, claims, entities, bundles, curation)
    expected_coverage = {
        (row["venue"], row["native_market_id"]): row
        for projection in projections
        for row in projection.coverage
    }
    coverage = {(row["venue"], row["native_market_id"]): row for row in tables["coverage"]}
    _check(
        set(coverage)
        == set(expected_coverage)
        == {
            (venue, market)
            for venue, bundle in bundles.items()
            for market in bundle.selected_market_ids
        },
        "coverage differs from exact selection",
    )
    for key, row in coverage.items():
        baseline = expected_coverage[key]
        for field in (
            "producer_status",
            "producer_reason",
            "source_usable",
            "observed_outcome_count",
            "usable_outcome_count",
            "exported_outcome_count",
        ):
            _check(row[field] == baseline[field], "coverage source counts differ")
        statuses = Counter(
            mappings[revision]["status"]
            for revision, outcome in native.items()
            if (outcome["venue"], outcome["native_market_id"]) == key
        )
        for status in ("mapped", "unresolved", "unsupported", "unusable"):
            _check(row[f"{status}_count"] == statuses[status], "coverage mapping counts differ")
        _check(
            sum(statuses.values()) == row["exported_outcome_count"],
            "coverage exported count differs",
        )
        _check(
            _coverage_disposition(row, statuses) == row["semantic_disposition"],
            "coverage disposition differs",
        )
        _check(
            _json(row["diagnostics_json"]) == _json(baseline["diagnostics_json"]),
            "coverage diagnostics differ",
        )
    return {
        "valid": True,
        "coverage_scope": "bounded_selection",
        "selected_counts": {
            venue: len(bundle.selected_market_ids) for venue, bundle in sorted(bundles.items())
        },
        "row_counts": {name: len(rows) for name, rows in sorted(tables.items())},
        "mapping_status_counts": dict(
            sorted(Counter(row["status"] for row in mappings.values()).items())
        ),
        "relationship_status_counts": dict(
            sorted(Counter(row["status"] for row in relationships).items())
        ),
        "relationship_type_status_counts": dict(
            sorted(
                Counter(
                    f"{row['relationship_type']}:{row['status']}" for row in relationships
                ).items()
            )
        ),
        "disposition_counts": dict(
            sorted(Counter(row["semantic_disposition"] for row in coverage.values()).items())
        ),
        "reason_counts": dict(
            sorted(
                Counter(
                    row["reason_code"] for row in mappings.values() if row["status"] != "mapped"
                ).items()
            )
        ),
        "producer_status_counts": dict(
            sorted(
                Counter(
                    f"{row['venue']}:{row['producer_status']}" for row in coverage.values()
                ).items()
            )
        ),
        "relationship_reason_counts": dict(
            sorted(
                Counter(
                    row["reason_code"] for row in relationships if row["status"] != "accepted"
                ).items()
            )
        ),
        "conflict_count": 0,
    }


def _coverage_disposition(row, statuses):
    if not statuses:
        return "no_outcomes"
    if len(statuses) == 1:
        return next(iter(statuses))
    return "mixed"


def _semantic_assignments(native, mappings, claims, entities, bundles, curation):
    """Check the interpretation contract without running the table compiler."""
    from market_semantics.election import interpret_outcome, validate_curation

    decisions = validate_curation(curation, list(native.values()), bundles)
    assignments = {}
    for revision, outcome in native.items():
        interpreted = interpret_outcome(outcome, bundles, decisions.get(revision))
        mapping = mappings[revision]
        _check(
            _json(outcome["policy_json"]) == interpreted["policy"],
            "native policy differs from evidenced interpretation",
        )
        for key in ("status", "reason_code", "method", "curation_decision_id"):
            _check(
                mapping[key] == interpreted[key], "mapping differs from evidenced interpretation"
            )
        _check(
            _json(mapping["evidence_json"]) == interpreted["evidence"],
            "mapping evidence differs from interpretation",
        )
        if interpreted["status"] == "mapped":
            payload = claim_payload(interpreted["candidate_id"], interpreted["polarity"])
            _check(
                mapping["claim_id"] == identity("claim", payload),
                "mapped claim differs from native meaning",
            )
            assignments.setdefault(interpreted["candidate_id"], []).extend(interpreted["evidence"])
    expected_entities = set(assignments)
    if assignments:
        expected_entities.update({ELECTION_ID, SCOPE["office_id"], SCOPE["jurisdiction_id"]})
    _check(set(entities) == expected_entities, "canonical entity assignments differ")
    all_evidence = [item for evidence in assignments.values() for item in evidence]
    identities = {
        ELECTION_ID: SCOPE,
        SCOPE["office_id"]: {"jurisdiction_id": SCOPE["jurisdiction_id"]},
        SCOPE["jurisdiction_id"]: {"country_code": "US"},
        **{candidate: {"curated_key": candidate} for candidate in assignments},
    }
    for key, row in entities.items():
        evidence = assignments.get(key, all_evidence)
        unique = sorted(
            {canonical_json(item): item for item in evidence}.values(), key=canonical_json
        )
        _check(
            _json(row["evidence_json"]) == unique
            and _json(row["identity_json"]) == identities[key],
            "entity assignment differs from evidenced claims",
        )
