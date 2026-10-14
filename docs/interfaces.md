# Frozen implementation interfaces

The coordinator owns `contracts.py`; workers must request changes rather than
editing it. Every relation uses `SCHEMAS` and `SORT_KEYS`. Nested canonical JSON
is validated, never arbitrary untyped payload.

## Lane A

`inputs.load_bundle(venue, source, destination, expected_manifest_sha256,
source_release_id, selected_market_ids, limits) -> VerifiedBundle` copies into a
new staging destination, validates the retained files and returns parsed arrays.

`inputs.verify_bundle(venue, root, expected_manifest_sha256, source_release_id,
selected_market_ids, limits) -> VerifiedBundle` verifies an existing copied
bundle without writing. Both use the producer-specific release inventory hash.

`adapters.polymarket.adapt(bundle) -> Projection` and
`adapters.kalshi.adapt(bundle) -> Projection` produce native rows and coverage.
`definition_json` is an object with exactly `market`, `outcome`, and `parents`;
parent keys and projected source fields are venue-specific. Include every field
used for interpretation, exclude prices, and retain source identity/provenance
in typed columns. Initialize `policy_json` with `empty_policy()`; Lane B fills
interpreted policy. Native revision IDs use `native_revision_id(row)`.

Coverage rows initialize counts from actual outcomes, with zero mapping counts
and explicit producer status. Missing/unusable identities have coverage, never
fabricated native outcomes. No adapter mutates source rows.

## Lane B

`election.compile_tables(projections, curation, bundles, limits) -> dict[str,
list[dict]]` returns all six relations. Inputs are a list of Projection and a
venue-keyed dictionary of VerifiedBundle. It fills mappings, claims, entities,
relationships, interpreted native policies, and coverage dispositions.

`election.compare_policies(left, right, inverse=False) -> tuple[str, str, dict]`
is a pure supported-policy comparator reused by independent invariant checking;
it returns status, reason code and a comparison document. Verification must
recompute comparisons and never trust the stored status/report alone.

Curation has exactly `version: 1`, `election_id`, `decisions`, and `supersedes`.
Supersedes has `mappings` and `relationships` objects, empty when unused. Mapping
keys are current native revision IDs and values are prior mapping assessment IDs.
Relationship keys are canonical JSON `[left_revision_id,right_revision_id,type]`
and values are prior relationship assessment IDs. Reject unknown current keys,
invalid prior ID formats, or attempts to supersede the same current assessment
conflictingly. These references declare correction lineage, not proof of older
rule applicability; previous releases are not implicitly loaded or modified.
Each decision
has exactly `CURATION_DECISION_KEYS`. Bind venue, market, observation, definition
and input manifest. `yes_polarity` is Boolean. Candidate keys must be supported.
`policy` follows `validate_policy`; each claimed value and claim assignment must
have source evidence. Multiple conflicting decisions fail. Stale decisions fail
rather than being applied to a different definition.

Evidence objects have exactly `EVIDENCE_KEYS`: venue, literal relation filename,
RFC6901 pointer, nullable `value_pointer` into JSON encoded in a string (Kalshi
payloads), and exact source quote. Use `validate_evidence` against retained
inputs. Polymarket outcomes lacking source version inherit it only through their
selected `(market_id, observation_id)` parent.

Only supported binary individual-election-winner meanings map. Party replacement,
first-inaugurated, popular-vote, primary and nomination meanings must not be
curated into individual election-winner claims. Unknown semantics remain visible.
Core policy identity fields cannot be not-applicable. Accepted comparisons require
known identity fields equal to mapped claims, a recognized IANA timezone, and
canonical decimal payout fractions in [0,1] with at most 18 decimal places.

## Lane C

`validation.validate_tables(tables, bundles, curation, limits) -> dict` checks
all row schemas, keys, IDs, nested JSON, references, native identities against
source arrays, evidence, policies, mapping/relationship statuses and coverage
counts. Return the recomputed validation report. Do not invoke compile_tables.

`release.prepare_release(tables, bundles, curation, selection, stage, limits)
-> PreparedRelease` writes sorted deterministic Parquet, `curation.json`, a
sanitized `selection.json`, `validation_report.json`, and `manifest.json` into
the existing staging root containing `inputs/<venue>`. Retain no absolute paths
or run timestamps. Manifest inventory includes all retained files except itself.
Include source-code content revision, package version, Python/PyArrow versions,
dependency lock digest, rule and curation identities, limits and sorted selection.
The coordinator supplies packaged `build.json` with lock digest.

`release.verify_release(root, expected_manifest_sha256, limits=Limits())
-> PreparedRelease` independently checks bounded metadata/files/decoded batches,
producer bundles, row invariants, report equality and release identity. Recreate
native projections if necessary, but never invoke semantic compilation. This
operation does not write or publish.

The coordinator supervises processing in a child, holds the persistent destination
lock and alone publishes by rename. Lane C never writes the final destination.

## Selection and bounded runtime

Selection has exactly `version: 1`, `election_id`, `inputs`, `curation`, `limits`.
Inputs is a venue-keyed object. Each source has exactly `path`, `contract`,
`source_release_id`, `manifest_sha256`, `selected_market_ids`. Curation has `path`
and `sha256`. The coordinator copies/validates the curation, then passes the
parsed object. Retained selection replaces paths with release-relative paths;
manifest identity binds its content, not operator absolute locations.

All agents use existing SemanticsError/require and no producer runtime imports.
No cross-lane tests or global fixture files are edited without coordination.
