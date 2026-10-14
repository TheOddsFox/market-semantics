# market-semantics

Shared entities, claims, and evidence-backed mappings across Polymarket and
Kalshi, starting with binary election winner markets.

An offline Python 3.12 CLI consumes independently pinned metadata bundles from
`polymarket-events` and `kalshi-events`. It publishes immutable, self-contained
Parquet releases with claims, native outcome mappings, evidence, policy
comparisons, and explicit coverage. PyArrow is its only runtime dependency.

```sh
uv sync --locked
uv run --locked market-semantics compile \
  --selection /operator/selection.json --output /operator/releases/new-release
uv run --locked market-semantics verify \
  --release /operator/releases/new-release \
  --expected-manifest-sha256 INDEPENDENTLY_RECORDED_DIGEST
```

The initial scope is individual candidate winner contracts for the 2024 U.S.
presidential general election, Donald Trump and Kamala Harris. **V1 readiness is
blocked:** the captured real contracts do not yet establish a reviewed shared
claim. Synthetic tests demonstrate supported behavior; they do not satisfy that
release gate. A shared claim alone never establishes settlement equivalence.

Definitions describe the pinned observations. Historical applicability stays
unknown, so current mappings must not be reused for backtests by native ID alone.

Read the [operator guide](docs/operations.md), [specification](SPEC.md),
[frozen interfaces](docs/interfaces.md), and [acceptance checklist](docs/acceptance.md).

The event repositories own acquisition and native definitions. Market-data
repositories own price facts and their grains. Consumers verify releases and
join complete native keys with compatible definition evidence. This package
does not import producer applications or Hypertrial's Graph package.

Development checks: `scripts/verify-fast`. Full verification: `scripts/verify`.
The installed handoff also needs pinned producer checkouts; see the operator guide.
