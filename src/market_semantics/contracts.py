"""Frozen v1 identities, relation schemas, bounds, and interchange types."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pyarrow as pa

CONTRACT = "oddsfox.market-semantics.v1"
ELECTION_ID = "election:us:president:2024:general"
RULE_REVISION = "us-presidential-general-2024.v1"
VENUE_CONTRACTS = {
    "polymarket": "oddsfox.polymarket.metadata.v1",
    "kalshi": "oddsfox.kalshi.metadata.v1",
}
VENUE_FILES = {
    "polymarket": ("markets", "outcomes", "memberships", "identity_history", "coverage"),
    "kalshi": ("series", "events", "markets", "coverage"),
}
CANDIDATES = {
    "person:donald-j-trump": ("Donald Trump", ["Donald J. Trump", "Donald Trump"]),
    "person:kamala-harris": ("Kamala Harris", ["Kamala Harris", "Kamala D. Harris"]),
}
SCOPE = {
    "office_id": "office:us:president",
    "jurisdiction_id": "jurisdiction:us",
    "election_date": "2024-11-05",
    "round": "general",
    "winner_criterion": "election_winner",
}
POLICY_FIELDS = (
    "winner_criterion",
    "election_id",
    "round",
    "candidate_id",
    "deadline",
    "resolution_sources",
    "certification",
    "recount",
    "dispute",
    "postponement",
    "cancellation",
    "withdrawal",
    "payout",
)
CORE_POLICY_FIELDS = {"winner_criterion", "election_id", "round", "candidate_id"}
EVIDENCE_KEYS = {"venue", "file", "pointer", "value_pointer", "quote"}
CURATION_DECISION_KEYS = {
    "venue",
    "native_market_id",
    "observation_id",
    "definition_sha256",
    "input_manifest_sha256",
    "candidate_id",
    "yes_polarity",
    "evidence",
    "policy",
}


class SemanticsError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require(condition: Any, message: str, code: str = "INVALID_INPUT") -> None:
    if not condition:
        raise SemanticsError(code, message)


@dataclass(frozen=True)
class Limits:
    max_markets_per_venue: int = 100
    max_bundle_bytes: int = 128 * 1024 * 1024
    max_records_per_bundle: int = 100_000
    max_comparisons: int = 40_000
    max_duration_seconds: int = 600
    max_temp_bytes: int = 1024 * 1024 * 1024
    max_release_bytes: int = 512 * 1024 * 1024

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None = None) -> Limits:
        require(value is None or isinstance(value, dict), "limits must be an object")
        value = value or {}
        defaults = asdict(cls())
        require(not set(value) - set(defaults), "unknown resource limit")
        for key, amount in value.items():
            require(type(amount) is int and 0 < amount <= defaults[key], f"invalid limit: {key}")
        return cls(**{**defaults, **value})


MAX_MANIFEST_BYTES = 1024 * 1024
MAX_INVENTORY_FILES = 64
MAX_JSON_DEPTH = 64
MAX_STRING_BYTES = 1024 * 1024
MAX_DECODED_BATCH_BYTES = 64 * 1024 * 1024


def _check_json(value: Any, depth: int = 0) -> None:
    require(depth <= MAX_JSON_DEPTH, "JSON nesting exceeds limit", "LIMIT_EXCEEDED")
    if isinstance(value, str):
        require(
            len(value.encode("utf-8")) <= MAX_STRING_BYTES,
            "JSON string exceeds limit",
            "LIMIT_EXCEEDED",
        )
    elif value is None or type(value) in (bool, int):
        return
    elif isinstance(value, list):
        for element in value:
            _check_json(element, depth + 1)
    elif isinstance(value, dict):
        for key, element in value.items():
            require(isinstance(key, str), "JSON object keys must be strings")
            _check_json(key, depth + 1)
            _check_json(element, depth + 1)
    else:
        raise SemanticsError(
            "INVALID_INPUT", "semantic JSON must not contain floats or custom values"
        )


def canonical_json(value: Any) -> str:
    _check_json(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(kind: str, value: Any) -> str:
    return f"{kind}:{sha256(canonical_json({'kind': kind, 'value': value}).encode('utf-8'))}"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, element in pairs:
        require(key not in value, f"duplicate JSON key: {key}")
        value[key] = element
    return value


def strict_json(data: bytes | str) -> Any:
    try:
        value = json.loads(
            data,
            object_pairs_hook=_unique_object,
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
        _check_json(value)
        return value
    except (ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, SemanticsError):
            raise
        raise SemanticsError("INVALID_INPUT", "invalid bounded JSON") from error


def timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def empty_policy() -> dict[str, Any]:
    return {
        "version": 1,
        "fields": {
            key: {"state": "unknown", "value": None, "evidence": []} for key in POLICY_FIELDS
        },
        "unparsed_clauses": [],
    }


def claim_payload(candidate_id: str, polarity: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "predicate": "wins_election",
        "candidate_id": candidate_id,
        "election_id": ELECTION_ID,
        "polarity": polarity,
        "scope": dict(SCOPE),
    }


def native_revision_id(row: dict[str, Any]) -> str:
    return identity(
        "native",
        {
            key: row[key]
            for key in (
                "input_manifest_sha256",
                "observation_id",
                "native_key_json",
                "definition_sha256",
            )
        },
    )


def assessment_id(kind: str, row: dict[str, Any]) -> str:
    return identity(kind, {key: value for key, value in row.items() if key != "assessment_id"})


def field(name: str, dtype: pa.DataType = pa.string(), nullable: bool = False) -> pa.Field:
    return pa.field(name, dtype, nullable=nullable)


SCHEMAS = {
    "entities": pa.schema(
        [
            field("entity_id"),
            field("entity_type"),
            field("label"),
            field("aliases_json"),
            field("identity_json"),
            field("evidence_json"),
        ]
    ),
    "claims": pa.schema(
        [
            field("claim_id"),
            field("schema_version", pa.int32()),
            field("predicate"),
            field("candidate_id"),
            field("election_id"),
            field("polarity", pa.bool_()),
            field("scope_json"),
            field("canonical_json"),
        ]
    ),
    "native_outcomes": pa.schema(
        [
            field("native_outcome_revision_id"),
            field("venue"),
            field("native_market_id"),
            field("side", nullable=True),
            field("asset_kind", nullable=True),
            field("asset_id", nullable=True),
            field("condition_id", nullable=True),
            field("source_market_version", nullable=True),
            field("outcome_index", pa.int32(), True),
            field("outcome_label"),
            field("clob_token_id", nullable=True),
            field("position_id", nullable=True),
            field("chain_index_set", pa.int32(), True),
            field("event_id", nullable=True),
            field("series_id", nullable=True),
            field("native_key_json"),
            field("input_manifest_sha256"),
            field("source_release_id"),
            field("observation_id"),
            field("received_at", pa.timestamp("us", tz="UTC")),
            field("source_updated_at", pa.timestamp("us", tz="UTC"), True),
            field("source_updated_at_raw_json"),
            field("definition_sha256"),
            field("definition_json"),
            field("policy_json"),
            field("usable", pa.bool_()),
            field("diagnostics_json"),
        ]
    ),
    "outcome_mappings": pa.schema(
        [
            field("assessment_id"),
            field("native_outcome_revision_id"),
            field("claim_id", nullable=True),
            field("status"),
            field("reason_code"),
            field("method"),
            field("rule_revision"),
            field("curation_decision_id", nullable=True),
            field("supersedes_assessment_id", nullable=True),
            field("evidence_json"),
        ]
    ),
    "relationships": pa.schema(
        [
            field("assessment_id"),
            field("left_revision_id"),
            field("right_revision_id"),
            field("relationship_type"),
            field("status"),
            field("reason_code"),
            field("comparison_json"),
            field("rule_revision"),
            field("curation_decision_id", nullable=True),
            field("supersedes_assessment_id", nullable=True),
            field("evidence_json"),
        ]
    ),
    "coverage": pa.schema(
        [
            field("venue"),
            field("native_market_id"),
            field("producer_status"),
            field("producer_reason", nullable=True),
            field("source_usable", pa.bool_(), True),
            field("observed_outcome_count", pa.int32()),
            field("usable_outcome_count", pa.int32()),
            field("exported_outcome_count", pa.int32()),
            field("mapped_count", pa.int32()),
            field("unresolved_count", pa.int32()),
            field("unsupported_count", pa.int32()),
            field("unusable_count", pa.int32()),
            field("semantic_disposition"),
            field("diagnostics_json"),
        ]
    ),
}
SORT_KEYS = {
    "entities": ("entity_id",),
    "claims": ("claim_id",),
    "native_outcomes": ("native_outcome_revision_id",),
    "outcome_mappings": ("native_outcome_revision_id",),
    "relationships": ("left_revision_id", "right_revision_id", "relationship_type"),
    "coverage": ("venue", "native_market_id"),
}


@dataclass(frozen=True)
class VerifiedBundle:
    venue: str
    root: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    relations: dict[str, list[dict[str, Any]]]
    selected_market_ids: tuple[str, ...]

    @property
    def source_release_id(self) -> str:
        return self.manifest["source_release_id"]


@dataclass
class Projection:
    native_outcomes: list[dict[str, Any]]
    coverage: list[dict[str, Any]]


@dataclass(frozen=True)
class PreparedRelease:
    path: Path
    release_id: str
    manifest_sha256: str
    coverage_summary: dict[str, Any]


def resolve_pointer(document: Any, pointer: str) -> Any:
    require(
        isinstance(pointer, str) and (pointer == "" or pointer.startswith("/")),
        "invalid JSON pointer",
    )
    current = document
    if pointer:
        for token in pointer[1:].split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            try:
                if isinstance(current, list):
                    require(token.isdigit() and str(int(token)) == token, "invalid array pointer")
                    current = current[int(token)]
                else:
                    require(isinstance(current, dict), "pointer crosses a scalar")
                    current = current[token]
            except (KeyError, IndexError) as error:
                raise SemanticsError("VALIDATION_FAILED", "unresolved evidence pointer") from error
    return current


def validate_evidence(evidence: Any, bundles: dict[str, VerifiedBundle]) -> None:
    require(isinstance(evidence, list), "evidence must be a list", "VALIDATION_FAILED")
    for item in evidence:
        require(
            isinstance(item, dict) and set(item) == EVIDENCE_KEYS,
            "invalid evidence fields",
            "VALIDATION_FAILED",
        )
        venue, filename = item["venue"], item["file"]
        require(
            venue in bundles and filename in {f"{x}.json" for x in VENUE_FILES[venue]},
            "invalid evidence source",
            "VALIDATION_FAILED",
        )
        value = resolve_pointer(
            bundles[venue].relations[filename.removesuffix(".json")], item["pointer"]
        )
        if item["value_pointer"] is not None:
            require(
                isinstance(value, str),
                "nested evidence must address encoded JSON",
                "VALIDATION_FAILED",
            )
            value = resolve_pointer(strict_json(value), item["value_pointer"])
        quote = value if isinstance(value, str) else canonical_json(value)
        require(
            isinstance(item["quote"], str) and item["quote"] == quote,
            "evidence quote differs from source",
            "VALIDATION_FAILED",
        )


def validate_policy(policy: Any, bundles: dict[str, VerifiedBundle] | None = None) -> None:
    require(
        isinstance(policy, dict) and set(policy) == {"version", "fields", "unparsed_clauses"},
        "invalid policy schema",
        "VALIDATION_FAILED",
    )
    require(
        type(policy["version"]) is int and policy["version"] == 1,
        "unsupported policy version",
        "VALIDATION_FAILED",
    )
    fields = policy["fields"]
    require(
        isinstance(fields, dict) and set(fields) == set(POLICY_FIELDS),
        "incomplete policy field schema",
        "VALIDATION_FAILED",
    )
    require(
        isinstance(policy["unparsed_clauses"], list)
        and all(isinstance(x, str) for x in policy["unparsed_clauses"]),
        "invalid unparsed clauses",
        "VALIDATION_FAILED",
    )
    for key, item in fields.items():
        require(
            isinstance(item, dict) and set(item) == {"state", "value", "evidence"},
            "invalid policy field",
            "VALIDATION_FAILED",
        )
        state, value = item["state"], item["value"]
        require(
            state in {"known", "unknown", "not_applicable"},
            "invalid policy state",
            "VALIDATION_FAILED",
        )
        require(isinstance(item["evidence"], list), "invalid policy evidence", "VALIDATION_FAILED")
        if state == "unknown":
            require(value is None, "unknown policy has a value", "VALIDATION_FAILED")
        else:
            require(bool(item["evidence"]), "asserted policy lacks evidence", "VALIDATION_FAILED")
            if state == "not_applicable":
                require(
                    key not in CORE_POLICY_FIELDS,
                    "core identity cannot be not-applicable",
                    "VALIDATION_FAILED",
                )
                require(value is None, "not-applicable policy has a value", "VALIDATION_FAILED")
            elif key == "deadline":
                require(
                    isinstance(value, dict)
                    and set(value) == {"at", "timezone"}
                    and timestamp(value["at"]) is not None
                    and isinstance(value["timezone"], str)
                    and bool(value["timezone"]),
                    "invalid policy deadline",
                    "VALIDATION_FAILED",
                )
                try:
                    ZoneInfo(value["timezone"])
                except (ValueError, ZoneInfoNotFoundError) as error:
                    raise SemanticsError("VALIDATION_FAILED", "unknown policy timezone") from error
            elif key == "resolution_sources":
                require(
                    isinstance(value, list)
                    and bool(value)
                    and all(isinstance(x, str) and bool(x) for x in value),
                    "invalid resolution sources",
                    "VALIDATION_FAILED",
                )
            elif key == "payout":
                require(
                    isinstance(value, dict)
                    and set(value) == {"win", "lose", "cancel", "withdrawal"}
                    and all(isinstance(x, str) and bool(x) for x in value.values()),
                    "invalid payout scenarios",
                    "VALIDATION_FAILED",
                )
                require(
                    all(re.fullmatch(r"(?:0|1|0\.[0-9]{0,17}[1-9])", x) for x in value.values()),
                    "payout must be a canonical fraction between zero and one",
                    "VALIDATION_FAILED",
                )
            else:
                require(
                    isinstance(value, str) and bool(value),
                    "invalid policy value",
                    "VALIDATION_FAILED",
                )
        if bundles is not None:
            validate_evidence(item["evidence"], bundles)
