# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-196 .p2c retrieval-quality non-regression check (nexus-nyry9.16
round-2 review fix, T2 [23144] VERIFICATION #6).

The bead's own VERIFICATION list requires the RDR-090/179 retrieval
bench (NDCG@3, spike_5q) run with the tiering opt-in OFF and ON, shown
NOT regressed. The first attempt at this (round 1) ran Path A against
the LIVE production corpus and got 0/5 non-error queries (a corpus-name
mismatch unrelated to .p2c) — a VACUOUS check, not a real one.

This makes it real: indexes the real RDR files spike_5q's ground_truth
references into the self-provisioned ephemeral test engine (LOCAL
bge-768 embeddings — no paid API calls, ``t2_service_env``'s
``NX_LOCAL=1`` posture), then runs Path A (``nx search`` CLI, itself
zero LLM cost) against that freshly-indexed corpus with
``NX_OPERATOR_MODEL_TIERING`` unset and set, comparing NDCG@3.

Marked ``integration`` only (NOT ``lived_in`` — no real money spent
here, unlike the sibling A/B measurement file). Run explicitly:
    uv run pytest -m integration -p no:xdist -s \
        tests/integration/test_rdr_196_p2c_retrieval_bench.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

pytestmark = [pytest.mark.integration]

_BENCH_PARENT = Path(__file__).resolve().parent.parent.parent / "scripts"
if str(_BENCH_PARENT) not in sys.path:
    sys.path.insert(0, str(_BENCH_PARENT))

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OUT_PATH = Path(
    os.environ.get(
        "NX_P2C_BENCH_OUT_PATH",
        str(_REPO_ROOT / "bench" / "out" / "nyry9_16_retrieval_bench_result.json"),
    )
)

# The exact RDR-number prefixes spike_5q.yaml's ground_truth references
# (bench/queries/spike_5q.yaml) -- indexing only these, not all 201
# docs/rdr/*.md files, keeps this test's wall-clock/embedding-compute
# bounded.
_GT_RDR_PREFIXES = (
    "049", "053", "086", "061", "075", "070", "067",
    "095", "089", "078", "080", "088", "091", "092", "079",
)


def test_p2c_retrieval_bench_not_regressed(tmp_path) -> None:
    from bench.paths import run_path_a
    from bench.schema import load_queries
    from nexus.cli import main

    import re as _re

    _rdr_num_re = _re.compile(r"^rdr-(\d+)-")
    rdr_dir = _REPO_ROOT / "docs" / "rdr"
    files = sorted(
        f for f in rdr_dir.glob("*.md")
        if (m := _rdr_num_re.match(f.name)) and m.group(1) in _GT_RDR_PREFIXES
    )
    assert len(files) >= 15, (
        f"expected >=15 ground-truth RDR files, found {len(files)}: "
        f"{[f.name for f in files]} -- spike_5q.yaml's ground_truth "
        f"prefixes may have drifted from docs/rdr/'s actual filenames"
    )

    runner = CliRunner()
    for f in files:
        result = runner.invoke(main, ["index", "rdr", str(f)])
        assert result.exit_code == 0, (
            f"nx index rdr {f.name} failed: {result.output}"
        )

    # Discover the collection the indexer actually created -- catalog-
    # aware indexing in service mode uses tumbler-based naming
    # (RDR-103), not the legacy ``rdr__<repo>-<hash8>`` flat form the
    # bench runner's ``--corpus`` default assumes, so it must be
    # resolved from the live catalog rather than hardcoded.
    from nexus.mcp_infra import get_collection_names

    collections = [c for c in get_collection_names() if c.startswith("rdr__")]
    assert collections, (
        "no rdr__* collection exists after indexing -- "
        f"nx index rdr appears to have silently no-opped; "
        f"all collections: {get_collection_names()}"
    )
    corpus = collections[0]

    queries = load_queries(_REPO_ROOT / "bench" / "queries" / "spike_5q.yaml")

    def _run_all(*, tiering: bool) -> tuple[dict[str, float], dict[str, str]]:
        if tiering:
            os.environ["NX_OPERATOR_MODEL_TIERING"] = "1"
        else:
            os.environ.pop("NX_OPERATOR_MODEL_TIERING", None)
        scores: dict[str, float] = {}
        errors: dict[str, str] = {}
        for q in queries:
            row = run_path_a(q, corpus=corpus)
            scores[q.qid] = row["ndcg_at_3"]
            if row.get("error"):
                errors[q.qid] = row["error"]
        os.environ.pop("NX_OPERATOR_MODEL_TIERING", None)
        return scores, errors

    scores_off, errors_off = _run_all(tiering=False)
    scores_on, errors_on = _run_all(tiering=True)

    # Diagnostic (not part of the pass/fail claim): capture ONE query's
    # raw retrieved chunks so a 0.0 NDCG can be told apart from "search
    # returned nothing at all" vs. "search returned real chunks that
    # just don't match ground_truth's expected source_paths".
    _diag_row = run_path_a(queries[0], corpus=corpus)

    report = {
        "corpus": corpus,
        "n_queries": len(queries),
        "scores_opt_in_off": scores_off,
        "scores_opt_in_on": scores_on,
        "errors_opt_in_off": errors_off,
        "errors_opt_in_on": errors_on,
        "non_error_queries_off": len(queries) - len(errors_off),
        "non_error_queries_on": len(queries) - len(errors_on),
        "identical": scores_off == scores_on,
        "diagnostic_q1_raw_chunk_count": _diag_row.get("raw_chunk_count"),
        "diagnostic_q1_deduped_source_paths": [
            c.get("source_path") for c in _diag_row.get("chunks", [])
        ],
        "diagnostic_q1_ground_truth_keys": list(queries[0].ground_truth.keys()),
    }
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))

    # Non-vacuity: this check means nothing if every query errored.
    assert report["non_error_queries_off"] > 0, (
        f"every query errored against freshly-indexed corpus {corpus!r} "
        f"-- this is not a real non-regression check; errors: {errors_off}"
    )
    # The actual claim: opt-in off vs on must be IDENTICAL (retrieval
    # tools are outside NX_OPERATOR_MODEL_TIERING's scope by
    # construction), not merely "not worse".
    assert scores_off == scores_on, (
        f"retrieval scores differ between opt-in off/on -- this should "
        f"be structurally impossible (search/query never consult "
        f"model_tiers); off={scores_off} on={scores_on}"
    )
