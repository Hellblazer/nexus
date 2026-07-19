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

import pytest

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
    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port)
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


def test_ndcg_drift_gate(seeded_client):
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
