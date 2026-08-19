# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-090 operationalized: NDCG retrieval-drift gate against the REAL stack.

nexus-9kq3h: RDR-090 shipped as a 5-query spike (12b5285b) and was accepted
but never wired into CI — the mechanism designed to catch retrieval drift was
itself dormant. This test promotes the judged corpus (25 docs, 50 graded
queries — ``corpus.json`` / ``queries.json``) to a maintained gate over the
production-local retrieval path: a REAL Java service (shaded jar + hermetic
PG16) embedding server-side with bge-768 and ranking in Postgres, exactly the
RDR-155/160 local-mode stack. This is NOT the MiniLM smoke test in
``test_retrieval_ndcg.py`` (which pins the math, not the stack).

CI wiring: ``integration``-marked, so it rides the EXISTING nightly
local-service gate (linux, daily) and the pre-release integration run — no
new workflow, no new cadence (CI-cost directive: never add a job where an
existing gate already fits).

Baseline discipline: ``ndcg_baseline.json`` pins mean NDCG@3. The assertion
is a symmetric band (±0.05), not a floor — an unexplained IMPROVEMENT is
drift too (embedding model change, rank re-weighting) and must be looked at
and re-pinned deliberately (re-pin: NX_NDCG_PIN=1 uv run pytest
tests/benchmarks/test_retrieval_drift_gate.py -m integration). A hard
absolute floor backstops catastrophic breakage independent of the pin.

Plan-first-vs-naive A/B (the other half of RDR-090's design) is deliberately
NOT here: it requires nx_answer's ``claude -p`` planner — non-hermetic,
paid, nondeterministic. It remains a manual/soak activity.

SCOPE (nexus-j46lz, corrected 2026-08-19 — this paragraph used to overclaim):
``test_ndcg_drift_gate`` below measures ONLY the raw vector-store layer —
it calls ``HttpVectorClient.search`` directly, the same rank ``VectorHandler``
in the Java service returns before any Python client code runs. It is a real
gate on the ENGINE's ranking, but it is blind to every CLIENT-side
re-weighter: ``apply_link_boost``, topic grouping, Ward clustering, the
topic boost, and the salience boost all run in ``search_engine.py`` on top
of what this leg observes, so a regression confined to one of them (e.g.
nexus-ekn9n: the topic boost totally dead in service mode since the aqbrk
flip, unnoticed for however long) is invisible here in EITHER direction —
not "engine ranking drifted, boost held," but "nothing here can see the
boost layer at all." ``test_ndcg_drift_gate_boost_layer`` is the second
site (Hal decision 2026-07-28, T2-recorded): same judged corpus, but routed
through ``search_engine.search_cross_corpus`` with real discovered topics so
the topic boost actually fires, then sorted by final distance the way the
production ``search`` MCP tool does. A change in the boost layer must move
THAT gate even when the raw layer above holds steady.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import numpy as np
import pytest

from tests.db._service_fixture import spawn_service, wait_for_service

from tests.benchmarks.test_retrieval_ndcg import ndcg_at_k
from tests.db._service_fixture import SERVICE_ROLES_SQL, pg_bin_dir

_BENCH_DIR = Path(__file__).parent
_REPO_ROOT = _BENCH_DIR.parent.parent
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN = pg_bin_dir()

_INITDB = _PG_BIN / "initdb"
_PG_CTL = _PG_BIN / "pg_ctl"
_PSQL = _PG_BIN / "psql"
_CREATEDB = _PG_BIN / "createdb"

_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = (
    Path(_JAVA_HOME) / "bin" / "java"
    if _JAVA_HOME
    else Path(shutil.which("java") or "java")
)


def _bge_model_present() -> bool:
    from nexus.db.service_bge_model import service_bge_model_present
    return service_bge_model_present()


_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
    and _bge_model_present()
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar, PG binaries, java, or bge-768 model "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, "
            f"bge={_bge_model_present() if _PG_CTL.exists() else 'n/a'})"
        ),
    ),
]

_TOKEN = "ndcg-drift-gate-bearer-secret"
_TENANT = "ndcg-drift-tenant"
# bge-768-native collection name (same convention as test_embed_parity).
_COLLECTION = "knowledge__ndcg-drift__bge-base-en-v15-768__v1"
_BASELINE = _BENCH_DIR / "ndcg_baseline.json"
_K = 3
#: Symmetric drift band around the pinned mean — beyond it, in EITHER
#: direction, retrieval behavior changed and the pin must be revisited.
_BAND = 0.05
#: Catastrophic-breakage backstop, independent of the pin.
_ABS_FLOOR = 0.30


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 120.0) -> None:
    # Generous: the jar runs ~120 Liquibase changesets + loads the bge-768
    # ONNX model before it listens (same rationale as test_embed_parity).
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


@pytest.fixture(scope="module")
def pg_instance():
    """Hermetic PostgreSQL with the service schema (Liquibase runs in-jar)."""
    pgdata = tempfile.mkdtemp(prefix="nexus_ndcg_gate_pg_")
    pg_port = _free_port()
    pg_user = os.environ["USER"]
    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", os.path.join(pgdata, "pg.log"),
             "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexusndcggate"],
            check=True, capture_output=True,
        )
        pg = {"port": pg_port, "dbname": "nexusndcggate", "user": pg_user}
        proc = subprocess.run(
            [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port), "-U", pg_user,
             "-d", "nexusndcggate", "-v", "ON_ERROR_STOP=1", "-c", SERVICE_ROLES_SQL],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"roles SQL failed: {proc.stderr}")
        yield pg
    finally:
        subprocess.run([str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
                       capture_output=True)
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def java_service(pg_instance):
    """The shaded jar in local-embed mode (server-side bge-768)."""
    svc_port = _free_port()
    chroma_data = tempfile.mkdtemp(prefix="nexus-ndcg-gate-chroma-")
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": _TOKEN,
        "NX_DB_URL": (
            f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
            f"/{pg_instance['dbname']}"
        ),
        "NX_DB_USER": pg_instance["user"],
        "NX_DB_PASS": "",
        "NX_POOL_SIZE": "3",
        "NX_CHROMA_PATH": chroma_data,
    }
    env.pop("NX_STORAGE_BACKEND", None)
    # nexus-lom9g: FILE-backed output via the shared primitive; the old
    # stdout=PIPE/stderr=PIPE form wedged the service once 64KB of Logback
    # output accumulated before the port bound (nexus-j0nec). This file sits
    # OUTSIDE tests/db/ and was missed by the first sweep's grep scope.
    proc, _svc_log = spawn_service([str(_JAVA), "-jar", str(_JAR)], env)
    try:
        # 120s preserved: this jar also loads the bge-768 ONNX model before
        # listening, per the local _wait_tcp's own note.
        wait_for_service(
            "127.0.0.1", svc_port, proc=proc, log_path=_svc_log, timeout=120.0,
        )
        yield f"http://127.0.0.1:{svc_port}", _TOKEN
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        shutil.rmtree(chroma_data, ignore_errors=True)


@pytest.fixture(scope="module")
def seeded_client(java_service):
    """HttpVectorClient with the judged corpus upserted (server-side embed)."""
    base_url, token = java_service
    saved = {
        k: os.environ.get(k)
        for k in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN")
    }
    os.environ["NX_SERVICE_URL"] = base_url
    os.environ["NX_SERVICE_TOKEN"] = token

    from nexus.db.http_vector_client import HttpVectorClient

    client = HttpVectorClient(tenant=_TENANT)
    corpus = json.loads((_BENCH_DIR / "corpus.json").read_text())
    # RDR-180 strict boundary (octet_length(chash)=32 bytes): ids must be
    # the canonical FULL 64-hex sha256 content address. Keep chash ->
    # judged doc_id for scoring.
    ids, docs, chash_to_doc = [], [], {}
    for d in corpus:
        chash = hashlib.sha256(d["content"].encode("utf-8")).hexdigest()
        ids.append(chash)
        docs.append(d["content"])
        chash_to_doc[chash] = d["id"]
    client.upsert_chunks(_COLLECTION, ids, docs,
                         metadatas=[{"doc_id": d["id"]} for d in corpus])
    yield client, chash_to_doc
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture
def _service_env(java_service, monkeypatch):
    """Re-pin ``NX_SERVICE_URL``/``NX_SERVICE_TOKEN`` to THIS module's
    hermetic service for the current test (nexus-j46lz debugging,
    2026-08-19).

    ``HttpVectorClient``'s module-level HTTP helpers (``count``,
    ``list_collections``, ``search``, ...) call ``_resolve_endpoint()``
    FRESH on every request (``http_vector_client.py``, env-first) — unlike
    ``HttpTaxonomyStore``/``RefreshableHttpStoreMixin``, which pin
    ``base_url``/``token`` once at construction. ``seeded_client``'s own
    ``os.environ[...]`` pin is MODULE-scoped: it fires exactly once, during
    module fixture setup, which happens BEFORE any test's function-scoped
    fixtures run — including the repo-wide autouse ``_pin_t2_substrate``
    (``tests/conftest.py``), which re-points every test at the STANDARD
    shared T2 substrate. So `seeded_client`'s pin wins for nothing: by the
    time a test BODY runs, `_pin_t2_substrate` has already overwritten the
    env vars, and every subsequent env-resolved call (search included)
    silently talks to a DIFFERENT, empty substrate — no error, just zero
    rows back, which read as "retrieval broken" with no clue why.

    The fix is the same pattern ``tests/db/test_http_combined_query_
    integration.py``'s ``vec_client`` fixture already uses: a NON-autouse,
    explicitly-requested function-scoped fixture. Non-autouse fixtures
    resolve AFTER every autouse fixture of the same scope (documented
    pytest ordering), so requesting this in a test's signature is what
    makes its ``monkeypatch.setenv`` win the race and stick for that
    test's body. ``HttpTaxonomyStore``/``HttpCentroidStore`` calls are
    unaffected either way — ``taxonomy_store`` constructs them with an
    explicit pinned ``base_url``/``_token``, never touching env.
    """
    base_url, token = java_service
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)


def _run_benchmark(client, chash_to_doc) -> dict:
    queries = json.loads((_BENCH_DIR / "queries.json").read_text())
    per_query = []
    for q in queries:
        judgments = {e["doc_id"]: e["relevance"] for e in q["expected"]}
        rows = client.search(q["query"], [_COLLECTION], n_results=_K)
        relevances = [
            judgments.get(chash_to_doc.get(r.get("id", ""), ""), 0) for r in rows
        ]
        ideal = sorted(judgments.values(), reverse=True)
        per_query.append({
            "query": q["query"],
            "ndcg_at_3": round(ndcg_at_k(relevances, ideal, _K), 4),
        })
    mean = sum(p["ndcg_at_3"] for p in per_query) / len(per_query)
    return {"mean_ndcg_at_3": round(mean, 4), "k": _K, "queries": per_query}


def test_ndcg_drift_gate(seeded_client, _service_env):
    """Mean NDCG@3 over the 50 judged queries stays inside the pinned band."""
    client, chash_to_doc = seeded_client
    result = _run_benchmark(client, chash_to_doc)

    if os.environ.get("NX_NDCG_PIN") == "1":
        _BASELINE.write_text(json.dumps(result, indent=1) + "\n")
        pytest.skip(f"baseline pinned: mean={result['mean_ndcg_at_3']}")

    if not _BASELINE.exists():
        # Backlogged (nexus-9kq3h): the gate is built but not yet armed — no
        # baseline pinned. Skip (never fail) until the bead is resumed:
        # NX_NDCG_PIN=1 uv run pytest tests/benchmarks/test_retrieval_drift_gate.py -m integration
        pytest.skip("ndcg_baseline.json not pinned yet (nexus-9kq3h backlogged)")
    baseline = json.loads(_BASELINE.read_text())
    mean = result["mean_ndcg_at_3"]
    pinned = baseline["mean_ndcg_at_3"]

    assert mean >= _ABS_FLOOR, (
        f"CATASTROPHIC: mean NDCG@3={mean} below absolute floor {_ABS_FLOOR} "
        f"(pinned {pinned}) — retrieval is broken, not drifted"
    )
    assert abs(mean - pinned) <= _BAND, (
        f"RETRIEVAL DRIFT: mean NDCG@3={mean} vs pinned {pinned} "
        f"(|Δ|={abs(mean - pinned):.4f} > band {_BAND}). Either direction "
        "means embedding/chunking/ranking behavior changed. Diagnose, then "
        "re-pin deliberately (NX_NDCG_PIN=1) with the cause in the commit. "
        f"Worst queries: "
        + ", ".join(
            f"{q['query'][:40]!r}={q['ndcg_at_3']}"
            for q in sorted(result["queries"], key=lambda x: x["ndcg_at_3"])[:3]
        )
    )


# ── Second site: boost-layer gate (nexus-j46lz) ──────────────────────────────
#
# Same judged corpus and same Java service/PG instance as the raw-layer gate
# above, routed through search_cross_corpus (with real discovered topics, so
# apply_topic_boost actually fires) instead of HttpVectorClient.search
# directly — the client-side ranking layer the raw gate above cannot see.

_BOOST_BASELINE = _BENCH_DIR / "ndcg_baseline_boost_layer.json"

# corpus.json is grouped thematically in blocks of 3 (auth, caching, search,
# db, api, testing, reliability, concurrency/logging/config). Groups of 3 are
# below HDBSCAN's min_cluster_size=5, so running discover_for_collection's
# real clustering on this 25-doc corpus would reliably find zero real
# clusters (all noise) — vacuous for this gate's purpose before it even
# starts. Topics are seeded directly instead, through the SAME production
# persist path (HttpTaxonomyStore.persist_discovered_topics + centroid
# upsert) that real discovery uses — only the clustering step is skipped —
# so the boost machinery downstream (get_assignments_for_docs,
# get_topic_link_pairs, apply_topic_boost) is exercised for real against a
# deterministic, hermetic topic layout.
_TOPIC_GROUP_SIZE = 3


@pytest.fixture(scope="module")
def taxonomy_store(java_service):
    """HttpTaxonomyStore pointed at the same hermetic Java service."""
    from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore

    base_url, token = java_service
    store = HttpTaxonomyStore(base_url=base_url, tenant=_TENANT, _token=token)
    yield store
    store.close()


@pytest.fixture(scope="module")
def topics_seeded(seeded_client, taxonomy_store):
    """Seed deterministic topics over the judged corpus so the topic boost
    (apply_topic_boost) has real assignments to act on — see the module
    comment above for why real HDBSCAN discovery is skipped for this 25-doc
    corpus."""
    client, chash_to_doc = seeded_client
    ids = list(chash_to_doc.keys())
    embeddings = np.asarray(client.get_embeddings(_COLLECTION, ids))
    assert embeddings is not None and len(embeddings) == len(ids), (
        "boost-layer gate setup: could not fetch stored embeddings for the "
        "judged corpus — cannot seed topics"
    )

    specs = []
    for start in range(0, len(ids), _TOPIC_GROUP_SIZE):
        group_ids = ids[start:start + _TOPIC_GROUP_SIZE]
        if len(group_ids) < 2:
            continue  # a lone trailing doc can't be "same topic" with anything
        centroid = embeddings[start:start + _TOPIC_GROUP_SIZE].mean(axis=0).tolist()
        specs.append({
            "label": f"seed-topic-{start // _TOPIC_GROUP_SIZE}",
            "terms": "[]",
            "doc_count": len(group_ids),
            "doc_ids": group_ids,
            "centroid": centroid,
            "assigned_by": "gate-seed",
        })
    assert specs, "boost-layer gate setup: no topic specs built"

    topic_ids = taxonomy_store.persist_discovered_topics(_COLLECTION, specs)
    assert topic_ids and len(topic_ids) == len(specs), (
        f"boost-layer gate setup: persist_discovered_topics returned "
        f"{len(topic_ids)} ids for {len(specs)} specs"
    )
    records = taxonomy_store._centroid_records_for_port(_COLLECTION, specs, topic_ids)
    if records:
        taxonomy_store._centroid.upsert(records)

    yield taxonomy_store


def _run_benchmark_boosted(client, taxonomy, chash_to_doc) -> dict:
    from nexus.search_engine import search_cross_corpus

    queries = json.loads((_BENCH_DIR / "queries.json").read_text())
    per_query = []
    for q in queries:
        judgments = {e["doc_id"]: e["relevance"] for e in q["expected"]}
        # Mirrors the production `search` MCP tool's default (non-clustered)
        # call: link_boost off (no catalog seeded here), cluster_by=None so
        # ordering is driven purely by distance — including whatever the
        # topic boost did to it — then an explicit final sort by distance,
        # exactly the step mcp/core.py takes after search_cross_corpus
        # returns for a non-clustered request.
        rows = search_cross_corpus(
            q["query"], [_COLLECTION], n_results=_K, t3=client,
            taxonomy=taxonomy, link_boost=False, cluster_by=None,
        )
        rows.sort(key=lambda r: r.distance)
        rows = rows[:_K]
        relevances = [
            judgments.get(chash_to_doc.get(r.id, ""), 0) for r in rows
        ]
        ideal = sorted(judgments.values(), reverse=True)
        per_query.append({
            "query": q["query"],
            "ndcg_at_3": round(ndcg_at_k(relevances, ideal, _K), 4),
        })
    mean = sum(p["ndcg_at_3"] for p in per_query) / len(per_query)
    return {"mean_ndcg_at_3": round(mean, 4), "k": _K, "queries": per_query}


def test_ndcg_drift_gate_boost_layer(seeded_client, topics_seeded, _service_env):
    """Mean NDCG@3 through the FULL client ranking path (topic boost
    included) stays inside the pinned band — the leg the raw-layer gate
    above cannot provide (nexus-j46lz)."""
    client, chash_to_doc = seeded_client
    result = _run_benchmark_boosted(client, topics_seeded, chash_to_doc)

    if os.environ.get("NX_NDCG_PIN") == "1":
        _BOOST_BASELINE.write_text(json.dumps(result, indent=1) + "\n")
        pytest.skip(f"boost-layer baseline pinned: mean={result['mean_ndcg_at_3']}")

    if not _BOOST_BASELINE.exists():
        pytest.skip(
            "ndcg_baseline_boost_layer.json not pinned yet — "
            "NX_NDCG_PIN=1 uv run pytest "
            "tests/benchmarks/test_retrieval_drift_gate.py -m integration"
        )
    baseline = json.loads(_BOOST_BASELINE.read_text())
    mean = result["mean_ndcg_at_3"]
    pinned = baseline["mean_ndcg_at_3"]

    assert mean >= _ABS_FLOOR, (
        f"CATASTROPHIC: boost-layer mean NDCG@3={mean} below absolute floor "
        f"{_ABS_FLOOR} (pinned {pinned}) — retrieval is broken, not drifted"
    )
    assert abs(mean - pinned) <= _BAND, (
        f"BOOST-LAYER RETRIEVAL DRIFT: mean NDCG@3={mean} vs pinned {pinned} "
        f"(|Δ|={abs(mean - pinned):.4f} > band {_BAND}). This leg includes "
        "apply_link_boost, topic grouping, the topic boost, and the salience "
        "boost on top of the raw-layer gate above — diagnose which one "
        "moved, then re-pin deliberately (NX_NDCG_PIN=1) with the cause in "
        "the commit. Worst queries: "
        + ", ".join(
            f"{q['query'][:40]!r}={q['ndcg_at_3']}"
            for q in sorted(result["queries"], key=lambda x: x["ndcg_at_3"])[:3]
        )
    )


def test_boost_layer_gate_is_sensitive_to_topic_boost_disable(
    seeded_client, topics_seeded, monkeypatch, _service_env,
):
    """Non-vacuity (nexus-j46lz): the boost-layer leg must actually see the
    topic boost. Reproduces the ekn9n failure class directly — a broken
    ``get_topic_link_pairs`` shape makes ``apply_topic_boost``'s call site
    raise inside ``search_engine``'s best-effort ``except``, so the ENTIRE
    topic boost (same-topic included) silently stops firing — and asserts
    the boost-layer benchmark's per-query results actually change when that
    happens. A gate that scores identically with the boost alive or dead
    would be exactly as blind as the raw-layer gate this leg exists to
    complement.
    """
    client, chash_to_doc = seeded_client

    with_boost = _run_benchmark_boosted(client, topics_seeded, chash_to_doc)

    def _broken_get_topic_link_pairs(self, topic_ids):  # noqa: ARG001
        # nexus-ekn9n's actual regression shape: get_topic_link_pairs
        # returning/raising something the caller cannot consume, caught by
        # search_cross_corpus's blanket `except Exception` around the whole
        # topic-boost block — the boost silently never runs again.
        raise RuntimeError("simulated ekn9n: get_topic_link_pairs shape broken")

    monkeypatch.setattr(
        type(topics_seeded), "get_topic_link_pairs", _broken_get_topic_link_pairs,
    )
    without_boost = _run_benchmark_boosted(client, topics_seeded, chash_to_doc)

    per_query_deltas = [
        abs(a["ndcg_at_3"] - b["ndcg_at_3"])
        for a, b in zip(with_boost["queries"], without_boost["queries"])
    ]
    assert any(d > 0 for d in per_query_deltas), (
        "boost-layer gate is VACUOUS: disabling the topic boost (simulated "
        "ekn9n) changed no query's NDCG@3 at all — this leg would not have "
        "caught nexus-ekn9n either. with_boost="
        f"{with_boost['mean_ndcg_at_3']} without_boost="
        f"{without_boost['mean_ndcg_at_3']}"
    )
