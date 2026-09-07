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
from pathlib import Path

import pytest
import yaml

from nexus.corpus import resolve_corpus
from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.limits import QUOTAS
from nexus.search_engine import _group_collections_by_embedding_model
from nexus.search_engine import search_cross_corpus as new_search_cross_corpus

pytestmark = pytest.mark.integration

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
    return HttpVectorClient()


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
    # Every corpus on the live tenant splits today (13+ collections exceed the
    # cap once the floor is multiplier-scaled), so the per-query split floor
    # alone would hold nothing to 0.9. The strict floor is kept in AGGREGATE:
    # one accepted tail swap (measured 2026-09-07: nine queries at 1.000, rdr at
    # 0.667, mean 0.967) passes; a batching regression that drags several
    # queries into the 0.6-0.9 band fails here even though no single query
    # breaches its own floor.
    mean_overlap = sum(r[3] for r in rows) / len(rows)
    assert mean_overlap >= _JACCARD_FLOOR, (
        f"mean Jaccard {mean_overlap:.3f} across {len(rows)} queries fell below "
        f"{_JACCARD_FLOOR} (nexus-atylb accepts isolated tail swaps, not a broad "
        f"drift)\n{report}"
    )
