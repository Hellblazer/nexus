# Benchmarking (`scripts/bench/`)

`scripts/bench/` is the benchmark-harness package (importable as `bench`,
via `pythonpath = ["scripts"]` in `pyproject.toml`). It holds two
independent harnesses that measure different things — do not conflate
them — plus the study scripts `operator_proxy_ab.py`, `model_tier_study.py`
and `synthesis_tier_study.py`, which this page does not describe:

## Retrieval bench (RDR-090)

`scripts/bench/{runner,metrics,schema,paths}.py` + `bench/queries/*.yaml`
score **retrieval quality** (NDCG@3, multi-hop precision) across three
`nx search` / `nx_answer` paths, against hand-labeled ground truth. See
`docs/rdr/rdr-090-realistic-agenticscholar-benchmark.md` for the full
design.

```
uv run python scripts/bench/runner.py bench/queries/spike_5q.yaml
```

### Comparing numbers (nexus-3ceda.11)

A latency is a number on one machine against one engine, so the runner records both and
refuses to compare across them:

- **Every report carries a `manifest`** (`scripts/bench/manifest.py`): the checkout's git sha
  and whether its tracked files were dirty, the host class, the CPU model, the engine build,
  the Python version and the time. The host class is the lab-run grant's size (`lab-run
  8c/16384M linux-x86_64` for lab-m), or `unmanaged <cores>c <os>-<arch>` outside a grant; the
  CPU model tells apart two grants of one size on different hosts. A host name is never
  recorded, since baselines are committed. The engine build is `release X.Y.Z` from the
  engine's `/version`, or `unstamped`. Outside a git checkout (`lab-remote --no-git`) the sha
  is `unknown`.
- **`--compare OLD NEW` refuses two reports whose host class, CPU model or engine build
  differ**, a report with no manifest, and a `--pairs` report. The git sha may differ; it is
  usually what is being compared, and it is the Python checkout's, not the engine's.
- **What the keys cannot see:** every engine without a release stamp reads `unstamped`, so two
  different local builds (or no engine answering) compare as equal. For an A/B against a local
  engine, put both arms in one `--pairs` run, where they meet the same engine by construction.
- **`--pairs N` times an A/B as paired, alternating runs** (`scripts/bench/paired.py`). With
  exactly two `--paths` (the first named is arm A) and an even N, each query runs N pairs in
  ABBA order (A B, B A, A B, ...), so each arm leads half the pairs and drift during a pair
  moves both of its samples. A path that fails (an error row or an exception) fails the run
  instead of entering as a fast sample. The summary is the median of each arm and the median
  of the per-pair differences; every sample is kept in the report, and outliers past the 1.5
  IQR fences are counted, not dropped (not computed below four samples).

```
uv run python scripts/bench/runner.py bench/queries/spike_5q.yaml --paths A,C --pairs 5
uv run python scripts/bench/runner.py --compare scripts/bench/baselines/old.json scripts/bench/baselines/new.json
```

Run timing on a lab-run grant (`lab-remote -c lab-m -- ...`) rather than the laptop when the
numbers are to be kept: a grant's size is fixed, a laptop's load is not.

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

**`verify` is UNDECIDABLE even after a topped-up sample.** Its positive
control passes (n=4 pairs, mean 0.8000) but with a THIN margin (min
0.7333 vs threshold 0.70 — only 0.033 of headroom), and its graded
near-miss test shows a realistic partial loss (1-of-2 citations dropped)
still scores 0.800, above threshold. A `--topup operator_verify` run
(one more live dispatch, paired against every prior raw output) landed
two more pairs at the same 0.7333, corroborating rather than resolving
the thin margin — 3 of 4 pairs now land at exactly 0.7333, which is a
repeated observation, not a single outlier. `.p2c` may not flip `verify`
on this proxy's evidence alone — see the working T2 entry's variance
table and the bd comment on nexus-nyry9.16. Outcome: `check` and `verify` did
join the flipped set on 2026-08-21 (nexus-3mea3, Sam decision), on the
three-arm model-tier study's evidence, not on this proxy's.

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
