"""Conservative, retrospective interpretation of the selected 2024 election contracts."""

from __future__ import annotations

import re
from copy import deepcopy
from decimal import Decimal
from typing import Any

from .contracts import (
    CANDIDATES,
    CORE_POLICY_FIELDS,
    CURATION_DECISION_KEYS,
    ELECTION_ID,
    POLICY_FIELDS,
    RULE_REVISION,
    SCOPE,
    SORT_KEYS,
    Limits,
    Projection,
    VerifiedBundle,
    assessment_id,
    canonical_json,
    claim_payload,
    empty_policy,
    identity,
    require,
    strict_json,
    timestamp,
    validate_evidence,
    validate_policy,
)

_UNSUPPORTED = re.compile(
    r"inaugurat|party representative|(?:other|another) [^.;]*representative|"
    r"representative of (?:the |their )?party|"
    r"popular[ -]vote|nominat|\bprimary\b|\bprimaries\b|\brunoff\b|"
    r"electoral votes? (?:margin|total)|\b(?:take|takes|taking) office\b|"
    r"\bassum(?:e|es|ing) office\b|\bsworn[ -]in\b|\bswearing[ -]in\b",
    re.I,
)
_QUESTION = re.compile(
    r"(?:will )?(Donald (?:J\.? )?Trump|Kamala (?:D\.? )?Harris) "
    r"win (?:the )?2024 (?:(?:US|U\.S\.|United States) )?presidential election\??",
    re.I,
)
_NAME = r"(?:Donald (?:J\.? )?Trump|Kamala (?:D\.? )?Harris)"
_WINNER_CONDITION = re.compile(
    rf"({_NAME}) wins? (?:the )?2024 (?:(?:US|U\.S\.|United States) )?"
    r"presidential (?:general )?election[?.]?",
    re.I,
)
_AFFIRMATIVE_RESOLUTION = re.compile(r"\byes[\"']? (?:if|when) ([^.;]+)", re.I)
_NAMED_WINNER = re.compile(rf"({_NAME} (?:wins?|does not win|will not win|loses?)\b[^.;]*)", re.I)


def _resolution_guard(texts: list[str], candidate: str) -> str | None:
    """Only the closed individual-winner clause can confirm source resolution meaning."""
    unparsed = False
    for text in texts:
        text = re.sub(r"\bU\.S\.", "US", text, flags=re.I)
        text = re.sub(r"\b(Donald J|Kamala D)\.", r"\1", text, flags=re.I)
        conditions = _AFFIRMATIVE_RESOLUTION.findall(text)
        if (
            re.search(r"\b(?:resolves?|resolution)\b", text, re.I)
            or re.search(r"\byes[\"']? unless\b", text, re.I)
        ) and not conditions:
            unparsed = True
        conditions += _NAMED_WINNER.findall(text)
        for condition in conditions:
            match = _WINNER_CONDITION.fullmatch(condition.strip())
            if not match:
                unparsed = True
                continue
            claimed = (
                "person:donald-j-trump" if "trump" in match[1].lower() else "person:kamala-harris"
            )
            if claimed != candidate:
                return "CONTRADICTORY_SOURCE_CANDIDATE"
    return "UNPARSED_RESOLUTION_CONDITION" if unparsed else None


def compare_policies(left: dict, right: dict, inverse: bool = False) -> tuple[str, str, dict]:
    """Compare documented policies; incompatibility wins over missing evidence."""
    validate_policy(left)
    validate_policy(right)
    fields = {}
    incompatible = False
    incomplete = bool(left["unparsed_clauses"] or right["unparsed_clauses"])
    for key in POLICY_FIELDS:
        a, b = left["fields"][key], right["fields"][key]
        result = "compatible"
        if a["state"] != "known" or b["state"] != "known":
            result = "unknown"
            incomplete = True
        elif key == "payout" and a["state"] == "known" and inverse:
            if any(Decimal(a["value"][x]) + Decimal(b["value"][x]) != 1 for x in a["value"]):
                result = "incompatible"
        elif key == "deadline":
            if timestamp(a["value"]["at"]) != timestamp(b["value"]["at"]) or (
                a["value"]["timezone"] != b["value"]["timezone"]
            ):
                result = "incompatible"
        elif a["value"] != b["value"]:
            result = "incompatible"
        if key in CORE_POLICY_FIELDS:
            expected = {
                "winner_criterion": "election_winner",
                "election_id": ELECTION_ID,
                "round": "general",
            }
            for item in (a, b):
                if item["state"] == "known" and (
                    (key in expected and item["value"] != expected[key])
                    or (key == "candidate_id" and item["value"] not in CANDIDATES)
                ):
                    result = "incompatible"
        incompatible |= result == "incompatible"
        fields[key] = {
            "left": {"state": a["state"], "value": a["value"]},
            "right": {"state": b["state"], "value": b["value"]},
            "result": result,
        }
    comparison = {"inverse": inverse, "complete": not incomplete, "fields": fields}
    if incompatible:
        return "rejected", "INCOMPATIBLE_POLICY", comparison
    if incomplete:
        return "unresolved", "INCOMPLETE_POLICY", comparison
    return "accepted", "COMPATIBLE_POLICY", comparison


def _strings(value: Any, pointer: str = ""):
    if isinstance(value, str):
        yield pointer, value
    elif isinstance(value, dict):
        for key, item in value.items():
            escaped = key.replace("~", "~0").replace("/", "~1")
            yield from _strings(item, f"{pointer}/{escaped}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{pointer}/{index}")


def _source_evidence(row: dict, bundles: dict[str, VerifiedBundle]) -> list[dict]:
    """Locate only source fields of this selected observation or its declared parents."""
    evidence = []
    definition = strict_json(row["definition_json"])
    texts = {text for _, text in _strings(definition)}
    for relation, records in bundles[row["venue"]].relations.items():
        if relation not in {"markets", "events", "series", "outcomes"}:
            continue
        for index, source in enumerate(records):
            market = source.get("market_id", source.get("market_ticker", source.get("ticker")))
            allowed = market == row["native_market_id"]
            if relation == "events":
                allowed = source.get("event_ticker", source.get("ticker")) == row["event_id"]
            elif relation == "series":
                allowed = source.get("series_ticker", source.get("ticker")) == row["series_id"]
            if not allowed or (
                source.get("observation_id") is not None
                and source["observation_id"] != row["observation_id"]
                and relation in {"markets", "outcomes"}
            ):
                continue
            for pointer, text in _strings(source):
                if pointer == "/payload_json":
                    for nested, quote in _strings(strict_json(text)):
                        if quote in texts:
                            evidence.append(
                                {
                                    "venue": row["venue"],
                                    "file": f"{relation}.json",
                                    "pointer": f"/{index}{pointer}",
                                    "value_pointer": nested,
                                    "quote": quote,
                                }
                            )
                elif text in texts:
                    evidence.append(
                        {
                            "venue": row["venue"],
                            "file": f"{relation}.json",
                            "pointer": f"/{index}{pointer}",
                            "value_pointer": None,
                            "quote": text,
                        }
                    )
    return evidence


def _binding(row: dict) -> tuple:
    return tuple(
        row[key]
        for key in (
            "venue",
            "native_market_id",
            "observation_id",
            "definition_sha256",
            "input_manifest_sha256",
        )
    )


def validate_curation(
    curation: dict, native_rows: list[dict], bundles: dict[str, VerifiedBundle]
) -> dict[str, dict]:
    """Validate closed, source-bound decisions and return their current revision owners."""
    require(
        isinstance(curation, dict)
        and set(curation) == {"version", "election_id", "decisions", "supersedes"},
        "invalid curation schema",
    )
    require(
        type(curation["version"]) is int
        and curation["version"] == 1
        and curation["election_id"] == ELECTION_ID,
        "unsupported curation scope",
    )
    require(isinstance(curation["decisions"], list), "curation decisions must be a list")
    supersedes = curation["supersedes"]
    require(
        isinstance(supersedes, dict)
        and set(supersedes) == {"mappings", "relationships"}
        and all(isinstance(x, dict) for x in supersedes.values()),
        "invalid supersession schema",
    )
    revisions = {row["native_outcome_revision_id"] for row in native_rows}
    for kind, references in supersedes.items():
        for current, previous in references.items():
            require(
                isinstance(previous, str)
                and re.fullmatch(
                    rf"{'mapping' if kind == 'mappings' else 'relationship'}:[0-9a-f]{{64}}",
                    previous,
                ),
                "invalid prior assessment ID",
            )
            if kind == "mappings":
                require(current in revisions, "unknown superseded native revision")
            else:
                pair = strict_json(current)
                require(
                    isinstance(pair, list)
                    and len(pair) == 3
                    and all(isinstance(x, str) for x in pair)
                    and pair[0] < pair[1]
                    and pair[0] in revisions
                    and pair[1] in revisions
                    and pair[2] in {"related", "equivalent", "inverse"}
                    and canonical_json(pair) == current,
                    "invalid superseded relationship key",
                )
    decisions = {}
    for decision in curation["decisions"]:
        require(
            isinstance(decision, dict) and set(decision) == CURATION_DECISION_KEYS,
            "invalid curation decision fields",
        )
        require(
            isinstance(decision["candidate_id"], str)
            and decision["candidate_id"] in CANDIDATES
            and type(decision["yes_polarity"]) is bool,
            "unsupported curated candidate or polarity",
        )
        matching = [row for row in native_rows if _binding(row) == _binding(decision)]
        require(bool(matching), "stale or unselected curation decision")
        require(bool(decision["evidence"]), "curated assignment lacks evidence")
        validate_evidence(decision["evidence"], bundles)
        validate_policy(decision["policy"], bundles)
        for row in matching:
            allowed = {canonical_json(x) for x in _source_evidence(row, bundles)}
            cited = decision["evidence"] + [
                e for field in decision["policy"]["fields"].values() for e in field["evidence"]
            ]
            require(
                all(canonical_json(e) in allowed for e in cited),
                "curation cites a different native definition or observation",
            )
            revision = row["native_outcome_revision_id"]
            require(
                revision not in decisions or decisions[revision] == decision,
                "conflicting curated decisions",
            )
            decisions[revision] = decision
    return decisions


def interpret_outcome(
    row: dict, bundles: dict[str, VerifiedBundle], decision: dict | None = None
) -> dict:
    """Pure interpretation shared with verification; curation cannot promote excluded meanings."""
    result = {
        "candidate_id": None,
        "polarity": None,
        "status": "unresolved",
        "reason_code": "UNRECOGNIZED_MEANING",
        "method": "deterministic",
        "evidence": [],
        "policy": empty_policy(),
        "curation_decision_id": None,
    }
    if not row["usable"]:
        return {**result, "status": "unusable", "reason_code": "UNUSABLE_NATIVE_IDENTITY"}
    definition = strict_json(row["definition_json"])
    market = definition["market"]
    if row["venue"] == "polymarket":
        outcomes = [
            item
            for item in bundles["polymarket"].relations["outcomes"]
            if item.get("market_id") == row["native_market_id"]
            and item.get("observation_id", row["observation_id"]) == row["observation_id"]
        ]
        if len(outcomes) != 2 or {
            str(item.get("outcome_label", "")).upper() for item in outcomes
        } != {"YES", "NO"}:
            return {**result, "status": "unsupported", "reason_code": "NON_BINARY_MARKET"}
    # Price and realized outcomes never participate in semantic interpretation.
    semantics = [
        value
        for pointer, value in _strings(definition)
        if any(
            part in pointer.lower()
            for part in ("question", "title", "description", "rules", "mve_selected_legs")
        )
    ]
    if any(_UNSUPPORTED.search(text) for text in semantics) or market.get("mve_selected_legs"):
        return {**result, "status": "unsupported", "reason_code": "UNSUPPORTED_PREDICATE"}
    evidence = _source_evidence(row, bundles)
    questions = [
        value
        for key, value in market.items()
        if key in {"question", "title"} and isinstance(value, str)
    ]
    if any(re.search(r"\b(?:and|or)\b", question, re.I) for question in questions):
        return {**result, "status": "unsupported", "reason_code": "COMPOSITE_QUESTION"}
    matches = [_QUESTION.fullmatch(text.strip()) for text in questions]
    names = {match.group(1).lower() for match in matches if match}
    candidates = {
        "person:donald-j-trump" if "trump" in name else "person:kamala-harris" for name in names
    }
    if len(candidates) > 1:
        return {**result, "reason_code": "AMBIGUOUS_CANDIDATE"}
    candidate = next(iter(candidates), None)
    meaning_evidence = [item for item in evidence if _QUESTION.fullmatch(item["quote"].strip())]
    if candidate is not None and not meaning_evidence:
        return {**result, "reason_code": "MISSING_MEANING_EVIDENCE"}
    if decision:
        if candidate is not None:
            require(
                candidate == decision["candidate_id"] and decision["yes_polarity"],
                "curation conflicts with recognized individual-winner meaning",
            )
        else:
            require(
                decision["yes_polarity"],
                "curated negative YES orientation has no supported source interpretation",
            )
            # Reviewed disambiguation still needs explicit candidate/election/winner source text.
            text = " ".join(item["quote"] for item in decision["evidence"]).lower()
            surname = "trump" if decision["candidate_id"].endswith("trump") else "harris"
            require(
                surname in text
                and "2024" in text
                and ("presiden" in text)
                and ("win" in text or "elected" in text),
                "curation does not evidence the supported individual-winner claim",
            )
        candidate = decision["candidate_id"]
        meaning_evidence = decision["evidence"]
    if candidate is None:
        return result
    conflict = _resolution_guard(semantics, candidate)
    if conflict:
        return {
            **result,
            "status": "unsupported"
            if conflict == "CONTRADICTORY_SOURCE_CANDIDATE"
            else "unresolved",
            "reason_code": conflict,
        }
    label = row["side"] if row["venue"] == "kalshi" else row["outcome_label"]
    require(isinstance(label, str), "invalid native outcome label")
    side = label.upper()
    if side not in {"YES", "NO"}:
        return {**result, "status": "unsupported", "reason_code": "UNSUPPORTED_OUTCOME_LABEL"}
    polarity = (side == "YES") == (decision["yes_polarity"] if decision else True)
    policy = deepcopy(decision["policy"]) if decision else empty_policy()
    expected = {
        "winner_criterion": "election_winner",
        "election_id": ELECTION_ID,
        "round": "general",
        "candidate_id": candidate,
    }
    for key, value in expected.items():
        field = policy["fields"][key]
        require(
            field["state"] != "known" or field["value"] == value,
            "curated policy identity contradicts the supported claim",
        )
        policy["fields"][key] = {"state": "known", "value": value, "evidence": meaning_evidence}
    if side == "NO" and policy["fields"]["payout"]["state"] == "known":
        policy["fields"]["payout"]["value"] = {
            key: format((1 - Decimal(value)).normalize(), "f")
            for key, value in policy["fields"]["payout"]["value"].items()
        }
    validate_policy(policy, bundles)
    return {
        **result,
        "candidate_id": candidate,
        "polarity": polarity,
        "status": "mapped",
        "reason_code": "SUPPORTED_INDIVIDUAL_WINNER",
        "evidence": meaning_evidence,
        "policy": policy,
        "method": "curated" if decision else "deterministic",
        "curation_decision_id": identity("curation", decision) if decision else None,
    }


def compile_tables(
    projections: list[Projection],
    curation: dict,
    bundles: dict[str, VerifiedBundle],
    limits: Limits,
) -> dict[str, list[dict]]:
    """Compile only actual observed outcomes; missing selections remain coverage rows."""
    tables = {name: [] for name in SORT_KEYS}
    natives = deepcopy([row for projection in projections for row in projection.native_outcomes])
    decisions = validate_curation(curation, natives, bundles)
    claims, assignments, market_mappings = {}, {}, {}
    for row in natives:
        revision = row["native_outcome_revision_id"]
        interpreted = interpret_outcome(row, bundles, decisions.get(revision))
        row["policy_json"] = canonical_json(interpreted["policy"])
        claim_id = None
        if interpreted["status"] == "mapped":
            candidate, polarity = interpreted["candidate_id"], interpreted["polarity"]
            payload = claim_payload(candidate, polarity)
            claim_id = identity("claim", payload)
            claims[claim_id] = {
                "claim_id": claim_id,
                "schema_version": 1,
                "predicate": "wins_election",
                "candidate_id": candidate,
                "election_id": ELECTION_ID,
                "polarity": polarity,
                "scope_json": canonical_json(SCOPE),
                "canonical_json": canonical_json(payload),
            }
            assignments.setdefault(candidate, []).extend(interpreted["evidence"])
        mapping = {
            "native_outcome_revision_id": revision,
            "claim_id": claim_id,
            "status": interpreted["status"],
            "reason_code": interpreted["reason_code"],
            "method": interpreted["method"],
            "rule_revision": RULE_REVISION,
            "curation_decision_id": interpreted["curation_decision_id"],
            "supersedes_assessment_id": curation["supersedes"]["mappings"].get(revision),
            "evidence_json": canonical_json(interpreted["evidence"]),
        }
        mapping["assessment_id"] = assessment_id("mapping", mapping)
        tables["outcome_mappings"].append(mapping)
        market_mappings.setdefault((row["venue"], row["native_market_id"]), []).append(mapping)
    tables["native_outcomes"] = natives
    tables["claims"] = list(claims.values())
    if assignments:
        all_evidence = [e for values in assignments.values() for e in values]
        entities = [
            (ELECTION_ID, "election", "2024 US presidential general election", [], SCOPE),
            (
                "office:us:president",
                "office",
                "President of the United States",
                [],
                {"jurisdiction_id": "jurisdiction:us"},
            ),
            (
                "jurisdiction:us",
                "jurisdiction",
                "United States",
                ["US", "USA"],
                {"country_code": "US"},
            ),
        ]
        entities += [
            (candidate, "person", *CANDIDATES[candidate], {"curated_key": candidate})
            for candidate in assignments
        ]
        for entity_id, kind, label, aliases, scoped in entities:
            evidence = assignments.get(entity_id, all_evidence)
            tables["entities"].append(
                {
                    "entity_id": entity_id,
                    "entity_type": kind,
                    "label": label,
                    "aliases_json": canonical_json(aliases),
                    "identity_json": canonical_json(scoped),
                    "evidence_json": canonical_json(
                        sorted(
                            {canonical_json(e): e for e in evidence}.values(), key=canonical_json
                        )
                    ),
                }
            )
    mapped = {
        m["native_outcome_revision_id"]: m
        for m in tables["outcome_mappings"]
        if m["status"] == "mapped"
    }
    selected = sorted(
        (row for row in natives if row["native_outcome_revision_id"] in mapped),
        key=lambda row: row["native_outcome_revision_id"],
    )
    for index, left in enumerate(selected):
        for right in selected[index + 1 :]:
            a = claims[mapped[left["native_outcome_revision_id"]]["claim_id"]]
            b = claims[mapped[right["native_outcome_revision_id"]]["claim_id"]]
            if a["candidate_id"] != b["candidate_id"]:
                continue
            kinds = ["related", "equivalent" if a["polarity"] == b["polarity"] else "inverse"]
            for kind in kinds:
                require(
                    len(tables["relationships"]) < limits.max_comparisons,
                    "relationship comparison limit exceeded",
                    "LIMIT_EXCEEDED",
                )
                status, reason, comparison = ("accepted", "SHARED_CANDIDATE_ELECTION", {})
                if kind != "related":
                    status, reason, comparison = compare_policies(
                        strict_json(left["policy_json"]),
                        strict_json(right["policy_json"]),
                        inverse=kind == "inverse",
                    )
                pair = [
                    left["native_outcome_revision_id"],
                    right["native_outcome_revision_id"],
                    kind,
                ]
                evidence = sorted(
                    {
                        canonical_json(e): e
                        for row in (left, right)
                        for e in strict_json(
                            mapped[row["native_outcome_revision_id"]]["evidence_json"]
                        )
                    }.values(),
                    key=canonical_json,
                )
                relation = {
                    "left_revision_id": pair[0],
                    "right_revision_id": pair[1],
                    "relationship_type": kind,
                    "status": status,
                    "reason_code": reason,
                    "comparison_json": canonical_json(comparison),
                    "rule_revision": RULE_REVISION,
                    "curation_decision_id": None,
                    "supersedes_assessment_id": curation["supersedes"]["relationships"].get(
                        canonical_json(pair)
                    ),
                    "evidence_json": canonical_json(evidence),
                }
                relation["assessment_id"] = assessment_id("relationship", relation)
                tables["relationships"].append(relation)
    current_pairs = {
        canonical_json([r["left_revision_id"], r["right_revision_id"], r["relationship_type"]])
        for r in tables["relationships"]
    }
    require(
        set(curation["supersedes"]["relationships"]) <= current_pairs,
        "superseded relationship was not assessed",
    )
    for projection in projections:
        for original in projection.coverage:
            row = deepcopy(original)
            mappings = market_mappings.get((row["venue"], row["native_market_id"]), [])
            for status in ("mapped", "unresolved", "unsupported", "unusable"):
                row[f"{status}_count"] = sum(m["status"] == status for m in mappings)
            row["exported_outcome_count"] = len(mappings)
            statuses = {m["status"] for m in mappings}
            row["semantic_disposition"] = (
                next(iter(statuses))
                if len(statuses) == 1
                else "mixed"
                if statuses
                else "no_outcomes"
            )
            tables["coverage"].append(row)
    for name, keys in SORT_KEYS.items():
        tables[name].sort(key=lambda row: tuple(row[key] for key in keys))
    return tables
