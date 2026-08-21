# Benchmarking (`scripts/bench/`)

`scripts/bench/` is the benchmark-harness package (importable as `bench`,
via `pythonpath = ["scripts"]` in `pyproject.toml`). It holds two
independent harnesses that measure different things — do not conflate
them:

## Retrieval bench (RDR-090)

`scripts/bench/{runner,metrics,schema,paths}.py` + `bench/queries/*.yaml`
score **retrieval quality** (NDCG@3, multi-hop precision) across three
`nx search` / `nx_answer` paths, against hand-labeled ground truth. See
`docs/rdr/rdr-090-realistic-agenticscholar-benchmark.md` for the full
design.

```
uv run python scripts/bench/runner.py bench/queries/spike_5q.yaml
```

## Operator quality proxy (RDR-196 .p2a)

`scripts/bench/operator_proxy.py` + `scripts/bench/operator_proxy_metrics.py`
score **operator OUTPUT quality** — a different axis retrieval bench does
not touch. It exists to gate RDR-196's cost-tier routing decision (should
an operator default to a cheap or a strong model?) with real evidence
instead of assuming the paper's cheap-model-tolerance result transfers.

Method: run an operator TWICE at the strong tier on the same fixed input
(the **positive control** — self-agreement) and once more against a
synthetically DEGRADED copy of one of those runs (the **negative
control** — no extra dispatch). Score structured agreement between the
two outputs with an operator-specific metric:

| operator | metric |
| --- | --- |
| `filter` | exact-membership Jaccard over kept item ids |
| `groupby` | pairwise co-clustering agreement (Rand Index) — group LABEL strings aren't compared across independent runs, only "are these two items co-grouped?" |
| `rank` | Spearman rank correlation, matched by an embedded `[id]` tag in each ranked string |
| `extract` | flattened (field, value) bag F1 |
| `check` | 0.5 × boolean(`ok`) exact-match + 0.5 × Jaccard over evidence `(item_id, role)` pairs |
| `verify` | 0.5 × boolean(`verified`) exact-match + 0.5 × token-Jaccard over citation strings |

`summarize`, `aggregate`, `compare`, `generate` are explicitly OUT of this
proxy — their substantive output is free text, and LLM-as-judge alone is
rejected as the sole scorer for a tier-routing decision. Their tier
assignment in `nexus.operators.model_tiers.OPERATOR_MODEL_TIER` is
therefore unvalidated by this gate.

**Thresholds are fixed BEFORE measurement, always.** A threshold chosen
after seeing a run's numbers is not a gate, it's a rationalization — the
whole point of the positive/negative control pair is that the proxy can
be shown to work (or shown to be wrong) on evidence, not asserted. Each
threshold, its metric, and its justification are recorded in the FROZEN,
write-once T2 title `nexus_rdr/196-phase2-quality-proxy-REGISTERED-2026-08-21`
— written exactly once, never upserted again, so its own "Updated"
timestamp is a mechanically-checkable proof of when pre-registration
happened. All results, revisions, and variance measurements go to the
separate, mutable working entry `nexus_rdr/196-phase2-quality-proxy`
instead — including the record of the one revision made after a live
positive-control failure surfaced a genuine metric flaw (whole-string vs
token-level citation matching), corrected transparently, not
retroactively erased.

**`verify` is UNDECIDABLE at its current sample size.** Its positive
control passes (n=2 pairs, mean 0.8667) but with a THIN margin (min
0.7333 vs threshold 0.70 — only 0.033 of headroom), and its graded
near-miss test shows a realistic partial loss (1-of-2 citations dropped)
still scores 0.800, above threshold. `.p2c` may not flip `verify` on this
proxy's evidence alone — see the working T2 entry's variance table and
the bd comment on nexus-nyry9.16. A larger sample (`--topup`, below) is
the recommended next step before treating `verify`'s threshold as
validated.

```
uv run python scripts/bench/operator_proxy.py --model sonnet
uv run python scripts/bench/operator_proxy.py --model sonnet --only operator_rank,operator_extract
uv run pytest tests/test_operator_proxy_metrics.py tests/test_operator_proxy_degrade.py tests/test_operator_proxy_builder_fidelity.py tests/test_operator_proxy_main_exit_code.py   # pure, no dispatch
uv run pytest -m integration tests/test_operator_proxy_controls.py                        # live, costs money
```

### Variance measurement (`operator_proxy_variance.py`)

A single strong-vs-strong pair (n=1) is a point estimate, not a variance
measurement — `scripts/bench/operator_proxy_variance.py` dispatches
multiple independent strong-tier runs per operator and reports pairwise
agreement (mean/min/max), so a threshold can be checked against real
run-to-run noise rather than one sample. `--topup OPERATOR` adds one more
independent run to an operator already measured, pairing it against every
previously stored raw output, without re-paying for the pairs already on
file:

```
uv run python scripts/bench/operator_proxy_variance.py
uv run python scripts/bench/operator_proxy_variance.py --topup operator_verify
```

Reports (raw per-dispatch outputs + `DispatchUsage`, not just scalar
scores — needed to re-score against a future metric change without a
fresh dispatch) are written under `bench/out/` by default — repo-relative
and **gitignored**: reproducible on demand from real, costed `claude -p`
calls, not a source-of-truth artifact to commit or diff.
