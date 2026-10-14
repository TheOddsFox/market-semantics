# Operator guide

Use Python 3.12 on a local Linux or macOS filesystem. Install the built wheel or
run `uv sync --locked` in this checkout. Network access is used only to install
dependencies or explicitly run producer acquisition outside this application.

## Prepare and pin inputs

Run each event repository's targeted metadata export or refresh separately.
Copy its complete metadata bundle to operator storage. Record the manifest's
SHA-256 through an independent trusted channel and keep the producer release ID.
Do not derive a trusted pin from an untrusted release immediately before verifying
it. Pin the reviewed curation bytes in the same way. Do not reformat producer JSON:
the pins and native revision identities bind its exact bytes.

The selection is a closed versioned JSON document. All operator paths must be
absolute and may not traverse symlinks. Each bundle's coverage must account for
exactly its declared market selection. A structural example follows; placeholders
must be replaced with verified values, not guessed identifiers:

```json
{
  "version": 1,
  "election_id": "election:us:president:2024:general",
  "inputs": {
    "polymarket": {
      "path": "/operator/inputs/polymarket",
      "contract": "oddsfox.polymarket.metadata.v1",
      "source_release_id": "64_LOWERCASE_HEX_CHARACTERS",
      "manifest_sha256": "64_LOWERCASE_HEX_CHARACTERS",
      "selected_market_ids": ["VERIFIED_NATIVE_MARKET_ID"]
    },
    "kalshi": {
      "path": "/operator/inputs/kalshi",
      "contract": "oddsfox.kalshi.metadata.v1",
      "source_release_id": "64_LOWERCASE_HEX_CHARACTERS",
      "manifest_sha256": "64_LOWERCASE_HEX_CHARACTERS",
      "selected_market_ids": ["VERIFIED_NATIVE_MARKET_TICKER"]
    }
  },
  "curation": {
    "path": "/operator/reviewed-curation.json",
    "sha256": "64_LOWERCASE_HEX_CHARACTERS"
  },
  "limits": {}
}
```

Default ceilings are 100 selected markets per venue, 128 MiB and 100,000 parsed
records per bundle, 40,000 candidate relationship assessments, 600 seconds,
1 GiB temporary storage, and 512 MiB published bytes including retained inputs.
The `limits` object accepts only reductions using the exact `Limits` field names
in `contracts.py`. Verification also bounds manifest bytes, inventory entries,
JSON depth, Parquet metadata, row groups, and decoded batches. Limit exhaustion
fails the operation instead of truncating a selection.

## Review curation

An empty review is valid and explicitly leaves policy evidence unknown:

```json
{"version":1,"election_id":"election:us:president:2024:general","decisions":[],"supersedes":{"mappings":{},"relationships":{}}}
```

A reviewed decision binds the exact venue, market, observation, manifest digest,
definition digest, candidate identity, YES polarity, and closed policy document.
See `CURATION_DECISION_KEYS`, `POLICY_FIELDS`, `validate_policy`, and the
synthetic fixture in `tests/fixtures.py`. Evidence contains venue, relation
filename, RFC 6901 pointer, optional pointer into an encoded JSON value, and an
exact retained source quote. Every asserted policy field requires evidence.
Unknown values stay explicit; not-applicable annotations cannot certify payout
compatibility in this version. Unparsed material clauses prevent acceptance.

Human review must establish that each cited passage actually supports the
assigned value. Integrity verification checks pointers, binding, closed schemas,
and recorded invariants; it cannot prove the factual truth of a human reading.
Curation cannot turn an unusable identity into a usable one or turn a party
replacement, inauguration, primary, or popular-vote condition into an individual
general-election winner claim. Conflicting or stale decisions fail closed.

Corrections use `supersedes` as described in [the frozen interfaces](interfaces.md).
Prior assessment references declare lineage without implying earlier historical
validity. Retain previous releases; the compiler never rewrites them.

## Compile, publish, and verify

```sh
market-semantics compile --selection /operator/selection.json \
  --output /operator/releases/new-release
market-semantics verify --release /operator/releases/new-release \
  --expected-manifest-sha256 INDEPENDENTLY_RECORDED_DIGEST
```

Successful commands print one JSON object to stdout with the release ID,
manifest digest, output path, validation status, and coverage summary. Diagnostics
go to stderr. Argument parsing errors return 2, processing failures return 1,
timeouts return 124, and keyboard interruption returns 130.

The parent holds a destination-scoped persistent lock, supervises one child, and
publishes by an atomic rename after validation and sync. Existing destinations,
input/output overlap, symlink paths, nonregular files, failed pins, and exceeded
limits are refused. Lock files remain beside releases intentionally: deleting a
lock file can let two publishers lock different inodes. Child processes finish
or are terminated and reaped before staging cleanup.

Before rename, failure leaves no completed release. After rename, an interruption
or sync failure can leave a valid completed release without a success result.
Verify that destination using the independently recorded manifest pin. Do not
delete or overwrite it to retry. Choose a new destination for a new attempt.
There is no mutable current pointer or resume database.

Copy the entire release and preserve the manifest pin separately. Verification
needs only that copy and the installed package, and does not call the compiler,
write into the release, access producer state, or use networking.

## Consume relations

The six typed relations and their keys are frozen in `contracts.py`; nested JSON
documents have versioned closed schemas. Native keys preserve Polymarket
`ctf_token` versus `poly_v2_position`, and Kalshi ticker plus explicit YES/NO.
Labels are not identifiers. All mappings are observed definitions with unknown
historical applicability. Join chosen price facts using the complete native key
and compatible definition evidence, preserving unmatched facts and the original
fact grain. `tests/test_downstream.py` demonstrates the handoff boundary.

Shared claims and `related` assessments do not authorize price arbitrage.
Equivalent or inverse relationships require accepted claims and complete
compatible supported policies. NO on one candidate does not imply YES on another.

## Development and installed handoff

Run `scripts/verify-fast` while developing and `scripts/verify` before delivery.
The full gate builds wheel/sdist artifacts, audits their inventories, and runs
the separately installed actual producer and consumer CLIs. Set
`POLYMARKET_EVENTS_SOURCE` and `KALSHI_EVENTS_SOURCE` when the pinned producer
checkouts are not at the default sibling paths. The harness checks their pinned
revisions, uses controlled synthetic endpoints, stops those endpoints, removes
temporary producer state, and denies networking and producer/Graph imports for
the installed semantic runtime. Dependency installation may use the network.

Hosted CI runs on Python 3.12 Linux and macOS. Its permissions are read-only and
action and producer revisions are pinned. It never runs the local enrollment
gate. Real metadata, quoted live evidence, credentials, private consumer logic,
and workspace-management content belong outside Git and distributions.
