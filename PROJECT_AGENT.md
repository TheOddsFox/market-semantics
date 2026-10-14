# Project agent notes

Public offline prediction-market semantic compiler. Python 3.12, uv, PyArrow, pytest and Ruff. Read SPEC.md before changing behavior.

## Invariants

- Consume pinned copied Polymarket and Kalshi metadata bundles; never acquire implicitly or import producer/Hypertrial Graph packages.
- Preserve typed native identities, source observations, honest coverage and unknown historical applicability.
- Shared claims never imply settlement equivalence. Reviewed curation cannot bypass unusable identities or missing policy evidence.
- Publish bounded immutable releases through supervised staging and parent-owned atomic publication.
- Keep real datasets, source evidence, credentials, Pad content and private trading work outside Git and distributions.
- Synthetic development is allowed; real 2024 US presidential shared-claim evidence remains a v1 readiness gate.

## Verification

Use scripts/verify-fast for feedback and scripts/verify for completion. Tests and installed handoffs are offline; CI never runs the local Universal Pad enrollment gate. Do not edit generated managed files directly.
