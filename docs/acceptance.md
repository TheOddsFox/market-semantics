# Acceptance evidence

This checklist maps the specification's ten acceptance criteria to executable
checks. A passing synthetic check does not imply real-data readiness. Exact final
command results belong in the PR validation evidence and CI for its revision.

| Spec criterion | Evidence | Gate |
| --- | --- | --- |
| 1. Installed offline producer/consumer interoperability | `uv build`; `python scripts/check-installed.py`: pinned producer wheels in separate environments, actual controlled endpoint exports, servers stopped, isolated installed consumer, copied release verified | Required |
| 2. Reviewed real 2024 shared claim | Bounded real captures from both producers verified in ignored operator storage. Selected Kalshi party-replacement/inauguration semantics do not establish the supported individual-winner predicate. No shared claim has been accepted. | **BLOCKED; draft PR; no v1 readiness** |
| 3. Positive claims, polarity and policy relationships | `pytest -q tests/test_election.py tests/test_runtime.py` | Synthetic |
| 4. Wrong scope and materially different predicates/policies | `pytest -q tests/test_election.py tests/test_contracts.py` | Synthetic |
| 5. Missing/ambiguous evidence, unusable identities and visible coverage | `pytest -q tests/test_inputs.py tests/test_adapters.py tests/test_election.py tests/test_validation.py` | Synthetic |
| 6. Full native identities and parent/source observations | `pytest -q tests/test_inputs.py tests/test_adapters.py` | Synthetic |
| 7. Determinism and correction lineage | `pytest -q tests/test_contracts.py tests/test_election.py tests/test_release.py tests/test_runtime.py` | Synthetic |
| 8. Input safety, limits, concurrent publication and interruptions | `pytest -q tests/test_inputs.py tests/test_release.py tests/test_runtime.py` | Synthetic; Linux/macOS CI |
| 9. Independent verification beyond hashes | `pytest -q tests/test_validation.py tests/test_release.py`; rehashed corrupt artifacts with test-controlled new external pins | Synthetic |
| 10. Grain-preserving downstream join and historical refusal | `pytest -q tests/test_downstream.py` | Synthetic |

## Delivery checks

- [x] Local locked full gate passes: 169 tests, Ruff, lock/sync, build and installed handoff.
- [x] Wheel and sdist audits exclude snapshots, credential markers, caches and checkout dependencies.
- [x] Installed actual-producer handoff passes with semantic runtime networking denied.
- [x] Independent test, security and data reviews are reconciled; aggregate copy budgets, contradictory settlement text and equivalent deadline encodings have regression coverage.
- [x] Independent verifier inspected the complete change and reran `scripts/verify`: 169 tests and all build/audit/installed checks passed; approval is scoped to draft implementation delivery.
- [ ] Blocking Linux and macOS CI checks pass for the delivered revision.
- [ ] Draft product PR is linked; no merge, deployment or package publication.
- [ ] Real reviewed shared-claim gate passes. **Currently blocked.**

Real evidence is deliberately retained outside public Git and distributions.
Operator review must either obtain a genuine supported shared candidate/election
claim from both venues or explicitly revise scope in a later approved decision.
Known election results, prices, and similar titles cannot satisfy that gate.
