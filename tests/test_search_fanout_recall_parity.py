# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-d9xt2 code-review recall-parity gate (T2 code-review-nexus-d9xt2 [24739]
Critical finding + critique-nexus-d9xt2 [24738]): the model-grouped batched
fan-out must not silently drop relevant results relative to the pre-fix
one-call-per-collection design.

Runs a fixed set of real queries against the LIVE cloud engine (reads
only -- search_cross_corpus never writes), spanning knowledge, code,
docs, rdr, and a mixed "all" corpus, and compares the top-10 id set the
OLD per-collection fan-out returns (loaded standalone from
``git show ab837d219:src/nexus/search_engine.py`` -- the exact pre-fix
commit, the same falsification technique the reviewer used to confirm
the original request-count tests) against the NEW batched fan-out,
asserting Jaccard overlap >= 0.9 per query.

Marked ``@pytest.mark.integration``: skipped by default, and further
self-skips if this tenant's real collections don't cover all five corpus
specs (a content precondition, not a code assertion -- this test proves
recall parity on whatever is actually indexed here, it does not seed
its own fixture corpus).

Deliberate, explicit live-cloud opt-in (distinct from the tests/conftest.py
``_blackhole_default_managed_endpoint`` guard the nexus-d5aye suite
enforces, which exists to catch an ACCIDENTAL fall-through to the real
default when nothing was configured -- this test explicitly reads this
box's OWN real ``~/.config/nexus/config.yml`` credentials, bypassing only
the autouse ``NEXUS_CONFIG_DIR`` test-isolation redirect via env vars
(``get_credential`` checks env before ``nexus_config_dir()``'s config.yml),
the same read-only real-config-dir access pattern ``conftest.py``'s own
``_real_config_dir_for_guard`` already uses for guard verification. Never
writes to the real config dir.

Run::

    uv run pytest -m integration tests/test_search_fanout_recall_parity.py -s
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from nexus.corpus import resolve_corpus
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.limits import QUOTAS
from nexus.search_engine import _group_collections_by_embedding_model
from nexus.search_engine import search_cross_corpus as new_search_cross_corpus
from tests import _route_parity as _rp

pytestmark = [pytest.mark.integration, pytest.mark.lived_in]

#: credentials.<key> in config.yml -> the env var get_credential() checks
#: FIRST (nexus.config.CREDENTIALS) -- setting these bypasses the autouse
#: NEXUS_CONFIG_DIR test-isolation redirect without touching it.
_CREDENTIAL_ENV_MAP = {
    "service_url": "NX_SERVICE_URL",
    "service_token": "NX_SERVICE_TOKEN",
    "voyage_api_key": "VOYAGE_API_KEY",
    "mint_token": "NX_MINT_TOKEN",
    "mint_tenant": "NX_MINT_TENANT",
}


@pytest.fixture()
def _real_cloud_credentials(monkeypatch: pytest.MonkeyPatch):
    """Bootstrap live-cloud auth from the real, on-disk
    ``~/.config/nexus/config.yml`` -- READ ONLY, never written -- via env
    vars, since the autouse per-test ``NEXUS_CONFIG_DIR`` isolation
    redirect (tests/conftest.py) would otherwise make every credential
    resolve empty.

    Deliberately FUNCTION-scoped and NOT autouse, using the test's own
    ``monkeypatch`` fixture rather than a private ``MonkeyPatch()``
    instance. Two conftest.py autouse fixtures fight over exactly these
    env vars: ``_pin_t2_substrate`` (points them at the session's LOCAL
    self-provisioned engine substrate) and ``_isolate_service_endpoint_env``
    (unconditionally ``delenv``s them as an ambient-pollution guard) --
    both are function-scoped, so a MODULE-scoped version of this fixture
    loses the race (its setup runs before either, so both undo it for
    every test). ``_isolate_service_endpoint_env``'s own docstring names
    the fix: "non-autouse fixtures resolve after autouse ones... the
    explicit setenv lands after this delenv and wins" -- a plain,
    explicitly-requested function-scoped fixture using the test's shared
    monkeypatch instance is exactly that pattern.
    """
    # nexus-pfuns: the unit suite fences $HOME to a throwaway mirror, so
    # Path.home() answers the FENCE, not the operator's real home, even
    # without any explicit NEXUS_CONFIG_DIR override. NX_REAL_HOME
    # (tests/_fence_home.py) carries the true home across that fence for
    # exactly this kind of deliberate real-path read; falls back to
    # Path.home() when the suite isn't fenced (e.g. run outside the
    # xdist/session-fixture harness).
    real_home = Path(os.environ["NX_REAL_HOME"]) if os.environ.get("NX_REAL_HOME") else Path.home()
    config_path = real_home / ".config" / "nexus" / "config.yml"
    if not config_path.exists():
        pytest.skip(
            f"no real nexus config at {config_path} -- cannot reach the "
            "live cloud engine this test requires",
        )
    data = yaml.safe_load(config_path.read_text()) or {}
    creds = data.get("credentials", {}) if isinstance(data, dict) else {}

    # This test's whole subject is T3 (HttpVectorClient.search /
    # list_collections) -- it needs no T2 store at all. `=none` is
    # `_pin_t2_substrate`'s documented opt-out for exactly that case.
    monkeypatch.setenv("NX_TEST_T2_SUBSTRATE", "none")
    for key, env_name in _CREDENTIAL_ENV_MAP.items():
        value = creds.get(key)
        if value:
            monkeypatch.setenv(env_name, str(value))

# The exact pre-nexus-d9xt2 commit (base of both c4f9b57a5 and a68a2f8be) --
# the last commit where search_cross_corpus used the one-call-per-collection
# fan-out. Same SHA the code reviewer used to falsify the request-count tests.
_PRE_FIX_SHA = "ab837d219"

_REPO_ROOT = Path(__file__).resolve().parents[1]

# 10 (query, corpus_spec) pairs spanning all five corpus specs -- generic
# software-engineering queries chosen to have a reasonable chance of
# matching content in each of this tenant's real corpora. 3 of the 10 hit
# corpus="all" (critique round 2 Significant: only 1/8 exercised "all",
# the flagship, highest-risk shape -- the one whose 44-collection
# knowledge+docs+rdr group actually splits across multiple combined
# calls, and the one every quarantine/non-conformant singleton also
# lands in).
_QUERIES: list[tuple[str, str]] = [
    ("vector embeddings for semantic search", "knowledge"),
    ("neural network attention mechanism", "knowledge"),
    ("database connection pooling implementation", "code"),
    ("error handling and retry logic", "code"),
    ("architecture overview and module map", "docs"),
    ("release process and versioning", "docs"),
    ("decision record for a search fan-out change", "rdr"),
    ("test coverage for search functionality", "all"),
    ("embedding model dimension mismatch across collections", "all"),
    ("cross-corpus fan-out batching and candidate sizing", "all"),
]

_JACCARD_FLOOR = 0.9
# Floor for a corpus whose collection group SPLITS into combined sub-batches
# (desired candidates > QUOTAS.MAX_QUERY_RESULTS). nexus-atylb, Sam's ruling
# 2026-09-07 (accepted, revisit later): a split partitions which collections
# share one filtered HNSW call, and near-tied tail candidates can rank
# differently per partition. Measured live on the 9-collection rdr group:
# 0.667 reproducibly (8 shared ids of a 12-id union, all four differences at
# ranks 7-10 within a 0.005 distance band). 0.6 admits exactly that measured
# state and fails on one more swap (7 shared of 13 = 0.538).
_JACCARD_FLOOR_SPLIT = 0.6
_LIMIT = 10


def _floor_for(cols: list[str]) -> float:
    """The floor a corpus is held to: the split floor when its batching
    would divide any embedding-model group into sub-batches, else the
    strict one. Mirrors search_engine's own split decision so the
    tolerance tracks the code, not a hand-kept corpus list."""
    import nexus.search_engine as _se  # noqa: PLC0415 -- resolve at call time so tests can patch it

    _desired = _se._desired_candidate_count
    for group in _group_collections_by_embedding_model(cols):
        if len(group) > 1 and _desired(group, _LIMIT) > QUOTAS.MAX_QUERY_RESULTS:
            return _JACCARD_FLOOR_SPLIT
    return _JACCARD_FLOOR


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def _load_old_search_cross_corpus():
    """Load the pre-nexus-d9xt2 ``search_cross_corpus`` standalone from
    ``git show ab837d219:src/nexus/search_engine.py``, executed into a
    fresh module namespace so it resolves imports (``nexus.config``,
    ``nexus.db.http_vector_client``, ``nexus.types``) against the CURRENT
    installed package -- none of those changed shape between the pre-fix
    commit and today, only ``search_cross_corpus``'s own body did.
    """
    old_source = subprocess.run(
        ["git", "show", f"{_PRE_FIX_SHA}:src/nexus/search_engine.py"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True,
    ).stdout
    spec = importlib.util.spec_from_loader(
        "nexus_search_engine_prefix_d9xt2", loader=None,
    )
    old_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old_module
    try:
        exec(  # noqa: S102 — trusted source: our own git history, not user input
            compile(old_source, f"<git show {_PRE_FIX_SHA}:src/nexus/search_engine.py>", "exec"),
            old_module.__dict__,
        )
    finally:
        del sys.modules[spec.name]
    return old_module.search_cross_corpus


def _top_ids(results, limit: int) -> set[str]:
    ordered = sorted(results, key=lambda r: r.distance)
    return {r.id for r in ordered[:limit]}


@pytest.fixture()
def _live_client(_real_cloud_credentials: None) -> HttpVectorClient:
    client = HttpVectorClient()
    # nexus-tu8wp.2: these cells measure the BATCHED path (their floor swap
    # patches ``_desired_candidate_count``, which the per-collection route
    # never reads, and they count ``client.search`` calls, which the route
    # does not make). Once the cloud engine serves
    # POST /v1/vectors/search-per-collection, a client that still asks for
    # it would compare the route with itself and pass vacuously. The
    # route-parity leg (reference = this batched path, candidate = the route,
    # asserting the route was served) is nexus-tu8wp.3's.
    client.supports_per_collection_search = False  # type: ignore[misc]
    return client


@pytest.fixture()
def _corpus_collections(_live_client: HttpVectorClient) -> dict[str, list[str]]:
    all_collections = [c["name"] for c in _live_client.list_collections()]
    specs = {
        "knowledge": resolve_corpus("knowledge", all_collections),
        "code": resolve_corpus("code", all_collections),
        "docs": resolve_corpus("docs", all_collections),
        "rdr": resolve_corpus("rdr", all_collections),
        "all": all_collections,
    }
    missing = [name for name, cols in specs.items() if not cols]
    if missing:
        pytest.skip(
            f"content precondition unmet: this tenant has no collections for "
            f"corpus spec(s) {missing!r} -- recall parity needs real content "
            "in all five specs to mean anything",
        )
    return specs


def _one_comparison(old_search_cross_corpus, client, query, cols):
    kwargs = {"cluster_by": None, "threshold_override": float("inf")}
    old_results = old_search_cross_corpus(query, cols, _LIMIT, client, **kwargs)
    new_results = new_search_cross_corpus(query, cols, _LIMIT, client, **kwargs)
    old_ids = _top_ids(old_results, _LIMIT)
    new_ids = _top_ids(new_results, _LIMIT)
    return _jaccard(old_ids, new_ids), old_ids, new_ids


def _is_confirmed_regression(
    first_overlap: float, retry_overlap: float | None, floor: float = _JACCARD_FLOOR,
) -> bool:
    """Decide whether a below-floor measurement is a REAL regression that
    must fail this test, or unreproduced jitter (nexus-d9xt2 critique
    round 2 Significant).

    ``first_overlap >= _JACCARD_FLOOR`` -- the common case -- is never a
    failure, and ``retry_overlap`` is irrelevant (no retry is run at all
    in that case; the live test's own call site only invokes this after
    conditionally retrying). When the first measurement IS below floor:
    OLD and NEW each embed the same query text via two separate live
    calls and query the ANN (HNSW) index independently, and both
    embedding generation and approximate-nearest-neighbor traversal
    carry known run-to-run floating-point/ordering jitter for candidates
    near the rank-10 boundary -- measured directly during this bead's
    work: a query that scored 0.818 on one run scored a PERFECT 1.0
    (byte-identical top-10 ids) on an immediate rerun of the exact same
    comparison. One bounded retry distinguishes that from a real
    regression:

    - ``retry_overlap is None`` -- the caller recorded a below-floor
      first measurement but never actually retried. That is a caller
      bug, not evidence of jitter: treated as a confirmed failure rather
      than silently passed.
    - ``retry_overlap >= _JACCARD_FLOOR`` -- the dip did NOT reproduce;
      jitter, not a regression. Not a failure.
    - ``retry_overlap < _JACCARD_FLOOR`` -- the dip DID reproduce on an
      independent second measurement. A confirmed failure (the critique's
      "a second failure fails": retrying and then silently accepting
      whichever value came back, win or lose, is what previously let a
      genuinely intermittent regression report green most of the time).
    """
    if first_overlap >= floor:
        return False
    if retry_overlap is None:
        return True
    return retry_overlap < floor


def test_recall_parity_old_vs_batched_fan_out(
    _live_client: HttpVectorClient, _corpus_collections: dict[str, list[str]],
):
    old_search_cross_corpus = _load_old_search_cross_corpus()

    rows = []
    for query, corpus_name in _QUERIES:
        cols = _corpus_collections[corpus_name]
        floor = _floor_for(cols)
        overlap, old_ids, new_ids = _one_comparison(
            old_search_cross_corpus, _live_client, query, cols,
        )
        retried = False
        retry_overlap = None
        if overlap < floor:
            retry_overlap, old_ids, new_ids = _one_comparison(
                old_search_cross_corpus, _live_client, query, cols,
            )
            retried = True
        confirmed_regression = _is_confirmed_regression(overlap, retry_overlap, floor)
        reported_overlap = retry_overlap if retried else overlap
        rows.append((
            query, corpus_name, len(cols), reported_overlap, retried,
            confirmed_regression, old_ids, new_ids, floor,
        ))

    report_lines = [
        "\nnexus-d9xt2 recall parity (old fan-out vs batched fan-out):",
        f"{'corpus':<10} {'#cols':>5} {'jaccard':>8} {'floor':>6}  {'retried':>7}  query",
    ]
    for query, corpus_name, n_cols, overlap, retried, _confirmed, _old_ids, _new_ids, floor in rows:
        report_lines.append(
            f"{corpus_name:<10} {n_cols:>5} {overlap:>8.3f} {floor:>6.2f}  {str(retried):>7}  {query!r}",
        )
    report = "\n".join(report_lines)
    print(report)  # noqa: T201 — explicit ask: "print the per-query numbers"

    failures = [
        (query, corpus_name, overlap, floor)
        for query, corpus_name, _n_cols, overlap, _retried, confirmed_regression, _old_ids, _new_ids, floor in rows
        if confirmed_regression
    ]
    assert not failures, (
        f"{len(failures)}/{len(rows)} queries fell below their Jaccard floor "
        f"({_JACCARD_FLOOR} unsplit, {_JACCARD_FLOOR_SPLIT} split, nexus-atylb): "
        f"{failures}\n{report}"
    )
    # nexus-abdp2 (2026-10-04): the lean floor max(5, n // 2) is what moves
    # four of the five corpora (knowledge, code, docs, rdr) from the split
    # floor (0.6) to the strict 0.9: at _LIMIT=10 only the 107-collection
    # "all" corpus still splits. Under the pre-abdp2 floor every live corpus
    # split (13+ collections exceeded the cap), so the per-query split floor
    # alone held nothing to 0.9.
    #
    # The strict floor is exposed to the OLD fan-out's own run-to-run jitter:
    # OLD against OLD measured 0.818 on two of these queries (T2
    # nexus/measurements-abdp2-floor-sweep-2026-10-04), so one reference-side
    # swap can fail a run no batching change caused. The retry above absorbs
    # most of it; the rest is nexus-e9sux (the gate is flaky against its own
    # baseline).
    #
    # This gate also scores only the raw top-10 by distance at n=10, with no
    # rerank and no boosts. The page a user reads is measured by
    # test_final_order_parity_old_vs_new_floor below.
    #
    # The strict floor is also kept in AGGREGATE: one accepted tail swap
    # (measured 2026-09-07: nine queries at 1.000, rdr at 0.667, mean 0.967)
    # passes; a batching regression that drags several queries into the
    # 0.6-0.9 band fails here even though no single query breaches its own
    # floor.
    mean_overlap = sum(r[3] for r in rows) / len(rows)
    assert mean_overlap >= _JACCARD_FLOOR, (
        f"mean Jaccard {mean_overlap:.3f} across {len(rows)} queries fell below "
        f"{_JACCARD_FLOOR} (nexus-atylb accepts isolated tail swaps, not a broad "
        f"drift)\n{report}"
    )


# ── User-visible final order (nexus-abdp2 fix round, 2026-10-05) ────────────
#
# The gate above scores the RAW top-10 by vector distance: n=10, no threshold,
# no rerank, no boosts. It never exercises what a user reads: the default fetch
# is n=10 on the CLI and n=30 on the MCP search tool (limit 10 plus two
# lookahead pages), the candidate pool then feeds server rerank and
# ``apply_ranking_boosts``, and the page is cut after both. Shrinking the pool
# (the per-collection floor) can change that final page without moving the
# distance top-10 at all. The tests below compare THE FINAL PAGE a user sees
# between the pre-abdp2 floor and the current one, each against a
# reference-vs-reference noise control, parametrized over the fetch size, the
# threshold and the rerank switch.

_PAGE = 10
#: A cell passes when the new floor's mean Jaccard against the old floor is no
#: worse than the old floor's mean Jaccard against ITSELF by more than this.
_NOISE_MARGIN = 0.05
#: ``NX_ABDP2_FLOOR_VARIANT`` picks the floor under test for the measurement
#: runs: ``current`` is the code as shipped; the others are the candidates
#: raised to when the final order degrades.
_FLOOR_VARIANT_ENV = "NX_ABDP2_FLOOR_VARIANT"
#: ``NX_ABDP2_DEEP_PATH=0`` runs the path check WITHOUT ``deep_candidates``,
#: which is how the lean-floor yield loss it guards against was measured.
_DEEP_PATH_ENV = "NX_ABDP2_DEEP_PATH"


def _old_desired(cols: list[str], n: int, *, deep: bool = False) -> int:
    """The pre-abdp2 ``_desired_candidate_count``: every collection's floor
    is its full ``n * mult``."""
    import nexus.search_engine as _se  # noqa: PLC0415

    mult = max((_se._overfetch_multiplier(c) for c in cols), default=2)
    return max(n * mult, len(cols) * max(5, n * mult))


def _variant_desired(variant: str):
    """A ``_desired_candidate_count`` replacement for floor *variant*, or
    ``None`` for the shipped code."""
    import nexus.search_engine as _se  # noqa: PLC0415

    if variant == "current":
        return None
    floors = {
        "n": lambda n, mult: max(5, n),
        "n*mult//2": lambda n, mult: max(5, n * mult // 2),
    }
    if variant not in floors:
        raise ValueError(f"unknown floor variant {variant!r}")
    floor = floors[variant]

    def _desired(cols: list[str], n: int, *, deep: bool = False) -> int:
        mult = max((_se._overfetch_multiplier(c) for c in cols), default=2)
        return max(n * mult, len(cols) * floor(n, mult))

    return _desired


def _spearman(a: list[str], b: list[str]) -> float | None:
    """Spearman rank correlation of the ids two pages share, ranked within
    the shared set; ``None`` when fewer than three are shared."""
    in_b = set(b)
    shared = [i for i in a if i in in_b]
    m = len(shared)
    if m < 3:
        return None
    rank_a = {i: r for r, i in enumerate(shared)}
    in_shared = set(shared)
    rank_b = {i: r for r, i in enumerate(x for x in b if x in in_shared)}
    d2 = sum((rank_a[i] - rank_b[i]) ** 2 for i in shared)
    return 1 - 6 * d2 / (m * (m * m - 1))


def _user_page(
    results, *, rerank: bool, path: str | None = None, page: int = _PAGE,
) -> list[str]:
    """The ids a user reads on page one, in order: the CLI/MCP post-retrieval
    pipeline (path scope, ``apply_ranking_boosts``, then either the server
    rerank-score sort plus the file-diversity cap when rerank ran, or the
    boosts' own hybrid_score order), cut to *page*. Clustering is off, as in
    the gate above."""
    from nexus.config import get_tuning_config  # noqa: PLC0415
    from nexus.search_engine import (  # noqa: PLC0415
        apply_file_diversity_cap,
        apply_ranking_boosts,
    )

    if path is not None:
        results = [
            r for r in results
            if (r.metadata.get("_display_path") or "").startswith(path)
        ]
    results = apply_ranking_boosts(
        results, hybrid=False, tuning=get_tuning_config(), catalog=None,
    )
    if rerank:
        scored = [r for r in results if "rerank_score" in r.metadata]
        scored.sort(key=lambda r: float(r.metadata["rerank_score"]), reverse=True)
        unscored = [r for r in results if "rerank_score" not in r.metadata]
        results = apply_file_diversity_cap(scored + unscored)
    return [r.id for r in results[:page]]


def _run_floor(
    search, query, cols, n, client, *, floor, threshold, rerank, catalog=None,
    deep_candidates=False,
):
    """One live search under *floor* (``None`` = shipped code, ``'old'`` =
    the pre-abdp2 floor, else a variant name). Returns the raw result list
    and the number of ``t3.search`` calls it issued."""
    from unittest import mock  # noqa: PLC0415

    import nexus.search_engine as _se  # noqa: PLC0415

    if floor == "old":
        desired = _old_desired
    elif floor is None:
        desired = None
    else:
        desired = _variant_desired(floor)
    calls: list[int] = []
    real_search = client.search

    def _counting(*a, **kw):
        calls.append(1)
        return real_search(*a, **kw)

    patches = [mock.patch.object(client, "search", _counting)]
    if desired is not None:
        patches.append(mock.patch.object(_se, "_desired_candidate_count", desired))
    for p in patches:
        p.start()
    try:
        results = search(
            query, cols, n, client, cluster_by=None, threshold_override=threshold,
            rerank=rerank, catalog=catalog, deep_candidates=deep_candidates,
        )
    finally:
        for p in reversed(patches):
            p.stop()
    return results, len(calls)


_CELLS = [
    pytest.param(
        n, thr, rerank,
        id=f"n{n}-{'inf' if thr == float('inf') else 'default'}-{'rerank' if rerank else 'norerank'}",
    )
    for n in (10, 30)
    for thr in (float("inf"), None)
    for rerank in (False, True)
]


@pytest.mark.parametrize(("n", "threshold", "rerank"), _CELLS)
def test_final_order_parity_old_vs_new_floor(
    n, threshold, rerank,
    _live_client: HttpVectorClient, _corpus_collections: dict[str, list[str]],
):
    """The page a user reads must not degrade beyond reference noise.

    Per query: OLD floor, the floor under test, OLD again (the noise control),
    interleaved so cloud drift hits all three alike. Fails when the mean
    top-10 Jaccard of new-vs-old falls more than ``_NOISE_MARGIN`` under the
    mean of old-vs-old. A rerank cell skips when the backend cannot rerank
    server-side (the comparison would be vacuous)."""
    if rerank and not getattr(_live_client, "supports_server_rerank", False):
        pytest.skip("backend has no server-side rerank")
    variant = os.environ.get(_FLOOR_VARIANT_ENV, "current")
    new_floor = None if variant == "current" else variant

    rows = []
    for query, corpus_name in _QUERIES:
        cols = _corpus_collections[corpus_name]
        old1, calls_old = _run_floor(
            new_search_cross_corpus, query, cols, n, _live_client,
            floor="old", threshold=threshold, rerank=rerank,
        )
        new, calls_new = _run_floor(
            new_search_cross_corpus, query, cols, n, _live_client,
            floor=new_floor, threshold=threshold, rerank=rerank,
        )
        old2, _ = _run_floor(
            new_search_cross_corpus, query, cols, n, _live_client,
            floor="old", threshold=threshold, rerank=rerank,
        )
        p_old1 = _user_page(old1, rerank=rerank)
        p_new = _user_page(new, rerank=rerank)
        p_old2 = _user_page(old2, rerank=rerank)
        rows.append({
            "corpus": corpus_name, "query": query,
            "j_new": _jaccard(set(p_new), set(p_old1)),
            "j_old": _jaccard(set(p_old2), set(p_old1)),
            "rho_new": _spearman(p_old1, p_new),
            "rho_old": _spearman(p_old1, p_old2),
            "calls_old": calls_old, "calls_new": calls_new,
            "pool_old": len(old1), "pool_new": len(new),
        })

    def _mean(key):
        vals = [r[key] for r in rows if r[key] is not None]
        return sum(vals) / len(vals) if vals else float("nan")

    def _fmt(v):
        return "   n/a" if v is None else f"{v:>6.3f}"

    thr_label = "inf" if threshold == float("inf") else "default"
    cell = f"n={n} threshold={thr_label} rerank={rerank} floor={variant}"
    lines = [
        f"\nnexus-abdp2 final-order parity, {cell}",
        f"{'corpus':<10} {'j_new':>6} {'j_old':>6} {'rho_new':>8} {'rho_old':>8} "
        f"{'calls o/n':>10} {'pool o/n':>10}  query",
    ]
    for r in rows:
        lines.append(
            f"{r['corpus']:<10} {r['j_new']:>6.3f} {r['j_old']:>6.3f} "
            f"{_fmt(r['rho_new']):>8} {_fmt(r['rho_old']):>8} "
            f"{r['calls_old']:>4}/{r['calls_new']:<4} {r['pool_old']:>4}/{r['pool_new']:<4}  {r['query']!r}",
        )
    lines.append(
        f"MEAN {cell}: j_new={_mean('j_new'):.3f} j_old={_mean('j_old'):.3f} "
        f"rho_new={_mean('rho_new'):.3f} rho_old={_mean('rho_old'):.3f} "
        f"calls_old={_mean('calls_old'):.1f} calls_new={_mean('calls_new'):.1f}",
    )
    print("\n".join(lines))  # noqa: T201 — the table is the measurement

    assert len(rows) == len(_QUERIES), "vacuous run: not every query was measured"
    assert _mean("j_new") >= _mean("j_old") - _NOISE_MARGIN, (
        f"the final page under the new floor drifted past reference noise: {cell}\n"
        + "\n".join(lines)
    )


#: (query, corpus spec, path prefix selecting a minority collection of that
#: corpus). The prefixes are repo-relative ``_display_path`` heads on this
#: tenant; a pair whose prefix matches nothing in the old pool is reported as
#: skipped, because it measures nothing.
_PATH_PAIRS = [
    ("database connection handling", "code", "sql-state/"),
    ("database connection handling", "code", "control-plane/"),
    ("database connection handling", "docs", "docs/adr/"),
    ("release process and versioning", "code", "scripts/release/"),
    ("release process and versioning", "docs", "docs/RELEASE.md"),
    ("release process and versioning", "docs", "RELEASING.md"),
]


@pytest.mark.parametrize("rerank", [False, True], ids=["norerank", "rerank"])
def test_path_scoped_yield_old_vs_new_floor(
    rerank, _live_client: HttpVectorClient, _corpus_collections: dict[str, list[str]],
):
    """A path filter that selects a minority collection runs AFTER retrieval
    and sees only the fetched pool, so a smaller pool can leave it fewer rows.
    Measures, per pair, the rows surviving the filter and the final page under
    the old floor, the floor under test and old again."""
    if rerank and not getattr(_live_client, "supports_server_rerank", False):
        pytest.skip("backend has no server-side rerank")
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415

    variant = os.environ.get(_FLOOR_VARIANT_ENV, "current")
    new_floor = None if variant == "current" else variant
    catalog = make_catalog_reader()
    n = _PAGE
    rows = []
    for query, corpus_name, path in _PATH_PAIRS:
        cols = _corpus_collections[corpus_name]
        runs = {}
        for label, floor in (("old1", "old"), ("new", new_floor), ("old2", "old")):
            runs[label] = _run_floor(
                new_search_cross_corpus, query, cols, n, _live_client,
                floor=floor, threshold=None, rerank=rerank, catalog=catalog,
                # what `nx search --path` passes: the pool is filtered after
                # retrieval, so it asks for the deep floor.
                deep_candidates=label == "new" and os.environ.get(_DEEP_PATH_ENV) != "0",
            )
        surv = {
            k: sum(1 for r in res if (r.metadata.get("_display_path") or "").startswith(path))
            for k, (res, _c) in runs.items()
        }
        if surv["old1"] == 0 and surv["old2"] == 0:
            rows.append({"query": query, "path": path, "skipped": True})
            continue
        pages = {k: _user_page(res, rerank=rerank, path=path) for k, (res, _c) in runs.items()}
        rows.append({
            "query": query, "path": path, "skipped": False,
            "surv": surv, "page_len": {k: len(v) for k, v in pages.items()},
            "j_new": _jaccard(set(pages["new"]), set(pages["old1"])),
            "j_old": _jaccard(set(pages["old2"]), set(pages["old1"])),
        })

    lines = [
        f"\nnexus-abdp2 path-scoped yield, rerank={rerank} floor={variant}",
        f"{'path':<18} {'survivors o1/n/o2':>18} {'page len o1/n/o2':>17} {'j_new':>6} {'j_old':>6}  query",
    ]
    for r in rows:
        if r["skipped"]:
            lines.append(f"{r['path']:<18} (no survivors in the old pool; skipped)  {r['query']!r}")
            continue
        s, p = r["surv"], r["page_len"]
        lines.append(
            f"{r['path']:<18} {s['old1']:>5}/{s['new']:>3}/{s['old2']:<5} "
            f"{p['old1']:>5}/{p['new']:>3}/{p['old2']:<5} {r['j_new']:>6.3f} {r['j_old']:>6.3f}  {r['query']!r}",
        )
    print("\n".join(lines))  # noqa: T201 — the table is the measurement

    measured = [r for r in rows if not r["skipped"]]
    assert measured, "vacuous run: no path pair selected anything in the old pool"
    mean_new = sum(r["j_new"] for r in measured) / len(measured)
    mean_old = sum(r["j_old"] for r in measured) / len(measured)
    assert mean_new >= mean_old - _NOISE_MARGIN, (
        "path-scoped final page drifted past reference noise\n" + "\n".join(lines)
    )


# ── Route parity (nexus-tu8wp.3) ────────────────────────────────────────────
#
# Everything above measures the BATCHED path (``_live_client`` pins
# ``supports_per_collection_search = False``). The cells below measure the
# candidate that engine-service-v0.1.147 makes possible: the same
# ``search_cross_corpus`` over a client that reaches
# ``POST /v1/vectors/search-per-collection``, against two references, the
# batched path (deep floor, run twice: its own noise, nexus-e9sux) and, OPT-IN
# only (``NX_TU8WP3_LOADER=1``), the pre-batching ab837d219 fan-out. The machinery, and the guard that a route
# which was not served fails loudly instead of comparing the batched path with
# itself, is tests/_route_parity.py, tested hermetically in
# tests/test_route_parity_helpers.py.
#
# Run (needs the operator's cloud config; the route must be served, i.e. the
# cloud engine is >= engine-service-v0.1.147)::
#
#     uv run pytest -m integration tests/test_search_fanout_recall_parity.py -k route_parity -s
#
# ``-s`` shows the per-cell tables, which are the measurement. Each cell prints
# the requests it expects to send BEFORE sending any, a pause of
# ``NX_TU8WP3_PAUSE_S`` seconds (default 1) separates queries, and it ends with
# its UTC window and a ``TU8WP3_PARITY_JSON`` line. Cells run one at a time (no
# ``-n``: the cell refuses under xdist). LOAD: a cell is about (queries x
# (2 batched + 1 route runs)) requests, the opt-in loader adds one per
# collection per query (~98); select cells with ``-k`` rather than running all
# 12 back to back (nexus-abdp2's runs of the per-collection reference sent
# ~10k searches in three hours and pushed the live engine's plain_search mean
# from ~230 ms to 516 ms).
#
# QUERY STRINGS PER LEG. Each leg sends its own, distinct strings: the base
# ``_QUERIES`` with a leg marker appended, so a request in the engine, edge or
# gateway logs can be attributed to a leg (the engine's own
# ``event=search_per_collection`` line also carries ``per_collection_k`` and
# ``limit``, which tell the rerank sweep legs apart). ``NX_TU8WP3_NO_MARKERS=1``
# runs the bare base queries instead. The existing floor cells above keep the
# bare ``_QUERIES``.
#
#   leg norerank  rerank off, the route's shipped sizing        marker tu8wp3-nr
#   leg rr1000    rerank on, the route's shipped limit (1000)   marker tu8wp3-r1000
#   leg rr300     rerank on, ``limit`` patched to 300           marker tu8wp3-r300

_ASSERT_RERANK_ENV = "NX_TU8WP3_ASSERT_RERANK"
_NO_MARKERS_ENV = "NX_TU8WP3_NO_MARKERS"
#: The ab837d219 one-/search-per-collection reference is OPT-IN (``=1``): it is
#: ~98 requests per query. The nexus-abdp2 runs of it (about 10k searches in
#: three hours) pushed the live engine's plain_search mean from ~230 ms to
#: 516 ms and into the 30 s statement timeout. The default reference is the
#: batched path (deep floor), a handful of requests per query.
_LOADER_ENV = "NX_TU8WP3_LOADER"
#: Seconds to pause between queries (default 1.0); the cells run one after
#: another and never in parallel.
_PAUSE_ENV = "NX_TU8WP3_PAUSE_S"

#: The query strings each leg sends, spelled out (the base list with the leg's
#: marker appended; the hermetic tests pin that no string is in two legs and
#: none equals a base query).
_QUERIES_NORERANK = _rp.mark_queries(_QUERIES, _rp.LEG_MARKERS["norerank"])
_QUERIES_RR1000 = _rp.mark_queries(_QUERIES, _rp.LEG_MARKERS["rr1000"])
_QUERIES_RR300 = _rp.mark_queries(_QUERIES, _rp.LEG_MARKERS["rr300"])
_LEG_QUERIES = {
    "norerank": _QUERIES_NORERANK, "rr1000": _QUERIES_RR1000, "rr300": _QUERIES_RR300,
}


def _leg_queries(leg: str) -> list[tuple[str, str]]:
    return _QUERIES if os.environ.get(_NO_MARKERS_ENV) == "1" else _LEG_QUERIES[leg]


_ROUTE_CELLS = [
    pytest.param(
        leg, n, thr,
        id=f"{leg}-n{n}-{'inf' if thr == float('inf') else 'default'}",
    )
    for leg in ("norerank", "rr1000", "rr300")
    for n in (10, 30)
    for thr in (float("inf"), None)
]


@pytest.fixture()
def _route_client(_real_cloud_credentials: None, monkeypatch) -> HttpVectorClient:
    """A FRESH client that asks for the route (its 404 memo starts empty), with
    the kill switch cleared so a stray ``NX_SEARCH_PER_COLLECTION=0`` cannot
    turn the candidate into the reference."""
    from nexus.db.http_vector_client import (  # noqa: PLC0415
        PER_COLLECTION_ROUTE_ENV,
        per_collection_route_enabled,
    )

    monkeypatch.delenv(PER_COLLECTION_ROUTE_ENV, raising=False)
    assert per_collection_route_enabled()
    client = HttpVectorClient()
    assert client.supports_per_collection_search is True
    return client


@pytest.mark.parametrize(("leg", "n", "threshold"), _ROUTE_CELLS)
def test_route_parity_vs_batched_and_pre_batching(
    leg, n, threshold,
    _route_client: HttpVectorClient, _live_client: HttpVectorClient,
    _corpus_collections: dict[str, list[str]],
):
    """The route's FINAL page (and, rerank off, its raw top-10) must match the
    batched reference as closely as the reference matches itself.

    Per query, interleaved: batched reference, route, (opt-in) ab837d219 loader,
    batched reference again. A route that was not served for every model group, or a
    reference that never called ``/search``, raises ``RouteNotServedError``
    instead of passing. Rerank off is asserted (per query: >= 0.9 or within the
    reference's own noise, nexus-e9sux; and in the mean); rerank on is reported
    (the reranked pool legitimately differs: the route reranks the merged
    top-limit of each model group once, the batched path reranked each batch),
    unless ``NX_TU8WP3_ASSERT_RERANK=1``. Leg ``rr300`` patches the route's
    rerank ``limit`` to 300 against the shipped 1000 to show whether the deeper
    pool brings the reranked page closer to the reference."""
    import json  # noqa: PLC0415
    from unittest import mock  # noqa: PLC0415

    import nexus.search_engine as _se  # noqa: PLC0415

    rerank = leg != "norerank"
    if rerank and not getattr(_live_client, "supports_server_rerank", False):
        pytest.skip("backend has no server-side rerank")
    if os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail(
            "the route-parity cells measure the live engine and must run one at a time "
            "(no -n): parallel cells are the load that nexus-abdp2's runs put on it",
        )
    use_loader = os.environ.get(_LOADER_ENV) == "1"
    loader = _load_old_search_cross_corpus() if use_loader else None
    pause_s = float(os.environ.get(_PAUSE_ENV, "1.0"))
    sized = _rp.sized_with_rerank_limit(
        _se._per_collection_request_sizes, _rp.LEG_RERANK_LIMIT[leg],
    )
    rows = []
    queries = _leg_queries(leg)
    print(_rp.expected_cell_calls(  # noqa: T201 — the load this cell will put on the engine, before it starts
        queries, _corpus_collections, n, loader=use_loader,
    ) + f"; pause {pause_s}s between queries")
    with mock.patch.object(_se, "_per_collection_request_sizes", sized):
        for i, (query, corpus_name) in enumerate(queries):
            if i:
                time.sleep(pause_s)
            rows.append(_rp.measure_query(
                query=query, corpus=corpus_name, cols=_corpus_collections[corpus_name],
                n=n, threshold=threshold, rerank=rerank,
                route_client=_route_client, ref_client=_live_client,
                search=new_search_cross_corpus, user_page=_user_page, loader=loader,
            ))

    thr_label = "inf" if threshold == float("inf") else "default"
    label = f"leg={leg} n={n} threshold={thr_label} rerank={rerank}"
    print(_rp.format_cell(label, rows))  # noqa: T201 — the table is the measurement
    print("TU8WP3_PARITY_JSON " + json.dumps({  # noqa: T201
        "leg": leg, "n": n, "threshold": thr_label, "rerank": rerank,
        "rerank_limit_patched": _rp.LEG_RERANK_LIMIT[leg],
        "rows": [{
            "query": r.query, "corpus": r.corpus, "page_route": r.page_route,
            "page_noise": r.page_noise, "raw_route": r.raw_route, "raw_noise": r.raw_noise,
            "route_vs_loader": r.page_route_loader, "ref_vs_loader": r.page_ref_loader,
            "route_pool": r.route_pool, "ref_pool": r.ref_pool,
            "route_shapes": r.route_shapes, "ref_search_calls": r.ref_search_calls,
            "loader_search_calls": r.loader_search_calls,
            "start_utc": r.start_utc, "end_utc": r.end_utc,
        } for r in rows],
    }))

    assert len(rows) == len(_QUERIES), "vacuous run: not every query was measured"
    problems = _rp.cell_failures(
        rows, rerank=rerank, assert_rerank=os.environ.get(_ASSERT_RERANK_ENV) == "1",
    )
    assert not problems, (
        f"the route's page drifted past reference noise ({label}):\n  "
        + "\n  ".join(problems) + "\n" + _rp.format_cell(label, rows)
    )
