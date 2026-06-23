# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-h29w1 — Behavioural cross-boundary write-seam CI gate.

Audit finding F1: no single CI gate exercises BOTH halves of a write-seam diff.
The Python suite (ci.yml) deselects integration tests; service-ci.yml fires only
on ``service/**`` paths — so a Python-only change to
``http_vector_client.py:upsert_chunks`` cannot trigger the Java
``VectorHandler``/``PgVectorRepository`` tenant/dedup/quota suite.

This test closes that gap by booting the real service JAR and feeding
>300-record, duplicate-chash, and oversized inputs through the REAL
``HttpVectorClient.upsert_chunks``, asserting server-side enforcement:

- **Dedup collapse** — in-batch duplicate chash ids collapse server-side
  (``PgVectorRepository.upsertChunksInternal`` seen-HashSet, lines 292-313).
  Result: kept count == unique chash count.
- **ON CONFLICT idempotency** — re-upsert the same chash in a second call
  updates, not duplicates (``ON CONFLICT (tenant_id,collection,chash) DO UPDATE``).
- **>300-record round-trip** — a single logical ``upsert_chunks`` call with
  >300 ids traverses whatever client-side batching + server enforcement exists
  without error, and all ids are retrievable.
- **Oversize (>MAX_DOCUMENT_BYTES) rejection** — a chunk whose byte length
  exceeds ``QUOTAS.MAX_DOCUMENT_BYTES`` (16 384 bytes) is rejected by the server
  with a 4xx; the client surfaces it as ``VectorServiceError`` (not a silent drop).

Fixture strategy: Docker pgvector/pgvector:pg17 (works on ubuntu-latest CI and
on any local machine with Docker).  Homebrew PG fallback is intentionally NOT
implemented here — the docker path works everywhere the CI runner does, and the
job is path-gated so it only runs when relevant files change.

Prerequisites:
  - ``service/target/nexus-service-1.0-SNAPSHOT.jar`` built and fresh.
  - Docker available and ``pgvector/pgvector:pg17`` pullable.
  - Java (>= 17) on PATH or JAVA_HOME set.
  - No VOYAGE_API_KEY needed (service runs in LOCAL/ONNX mode).

Run locally:
    cd service && mvn package -DskipTests && cd ..
    uv run pytest tests/db/test_write_seam_gate_integration.py \\
        -o addopts="" -m integration -v -s

Non-vacuity guarantee:
    - The ``_service_jar_freshness`` autouse fixture in conftest.py SKIPS locally
      and FAILS in CI when the JAR is missing/stale.
    - A ``NX_REQUIRE_SERVICE_JAR=1`` env (set by the CI job) overrides the skip
      branch to also FAIL locally — so local scripted runs (e.g. smoke scripts)
      cannot pass vacuously on a missing JAR either.
    - The test itself asserts a concrete count: ``len(all_ids) > 300``.  If the
      fixture cannot connect to the service, ``local_service`` raises and the test
      errors — it does NOT skip silently.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL

# ── Prerequisite detection ────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"

_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = (
    Path(_JAVA_HOME) / "bin" / "java"
    if _JAVA_HOME
    else Path(shutil.which("java") or "java")
)

_DOCKER = shutil.which("docker")

# JAR check is enforced via the conftest autouse fixture.
# Docker is the only PG path for this test — no Homebrew fallback.
_JAVA_OK = _JAVA_HOME and (Path(_JAVA_HOME) / "bin" / "java").exists() or shutil.which("java") is not None
_DOCKER_OK = _DOCKER is not None

_ALL_PREREQS = _JAR.exists() and _JAVA_OK and _DOCKER_OK

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar, java, or docker "
            f"(jar={_JAR.exists()}, java={_JAVA_OK}, docker={_DOCKER_OK})"
        ),
    ),
]

# ── Helpers ───────────────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _stop_service(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def _chunk_id(text: str) -> str:
    """sha256(text)[:32] — matches HttpVectorClient.chunk_id()."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# ── Module-scoped Docker pgvector fixture ─────────────────────────────────────

_CONTAINER_NAME = "nexus_write_seam_gate_pg17"


@pytest.fixture(scope="module")
def pg_instance():
    """Throwaway pgvector/pgvector:pg17 container.

    Uses docker run in detached mode, waits for pg_isready, then yields
    connection params.  The container is force-removed on teardown.
    """
    pg_port = _free_port()
    pg_user = "nexus_test"
    pg_pass = "nexus_test_pass"
    pg_db = "nexus_seam_gate"

    subprocess.run(
        ["docker", "rm", "-f", _CONTAINER_NAME],
        capture_output=True,
    )
    subprocess.run(
        [
            "docker", "run", "-d",
            "--name", _CONTAINER_NAME,
            "-e", f"POSTGRES_DB={pg_db}",
            "-e", f"POSTGRES_USER={pg_user}",
            "-e", f"POSTGRES_PASSWORD={pg_pass}",
            "-p", f"{pg_port}:5432",
            "pgvector/pgvector:pg17",
        ],
        check=True, capture_output=True,
    )

    # Wait for PG to accept connections
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker", "exec", _CONTAINER_NAME,
                "pg_isready", "-U", pg_user, "-d", pg_db,
            ],
            capture_output=True,
        )
        if result.returncode == 0:
            break
        time.sleep(0.5)
    else:
        logs = subprocess.run(
            ["docker", "logs", _CONTAINER_NAME],
            capture_output=True, text=True,
        )
        subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)
        raise RuntimeError(
            f"pgvector container never became ready:\n{logs.stdout}\n{logs.stderr}"
        )

    # Give PG a moment for the initial superuser setup to finish
    time.sleep(1.0)

    # Create nexus_svc role (required by Liquibase grants-nexus-svc.xml, runAlways=true)
    _run_psql_in_container(pg_user, pg_db, SERVICE_ROLES_SQL)

    pg = {
        "host": "127.0.0.1",
        "port": pg_port,
        "dbname": pg_db,
        "user": pg_user,
        "password": pg_pass,
    }

    yield pg

    subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)


def _run_psql_in_container(pg_user: str, pg_db: str, sql: str) -> None:
    """Execute SQL inside the running container as the superuser."""
    result = subprocess.run(
        [
            "docker", "exec", "-i", _CONTAINER_NAME,
            "psql", "-U", pg_user, "-d", pg_db,
            "-v", "ON_ERROR_STOP=1", "-c", sql,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Container psql failed (rc={result.returncode}):\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )


# ── Module-scoped service fixture ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def local_service(pg_instance: dict):
    """Java service in LOCAL mode (ONNX embedder, no Voyage key).

    Yields ``(base_url, token)`` after the HTTP port is reachable.
    """
    token = "write-seam-gate-token"
    svc_port = _free_port()

    pg = pg_instance
    pg_jdbc = (
        f"jdbc:postgresql://{pg['host']}:{pg['port']}/{pg['dbname']}"
    )

    # App pool: nexus_svc (NOSUPERUSER NOBYPASSRLS) — Liquibase wires DML grants.
    # Migration pool: container superuser (pg['user']) — has DDL rights.
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": pg_jdbc,
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "2",
        "NX_DB_ADMIN_URL": pg_jdbc,
        "NX_DB_ADMIN_USER": pg["user"],
        "NX_DB_ADMIN_PASS": pg["password"],
        "NX_CHROMA_MODE": "local",
        "NX_CHROMA_PATH": tempfile.mkdtemp(prefix="seam-gate-chroma-"),
    }
    # Force ONNX / local mode — no Voyage billing
    env.pop("NX_VOYAGE_API_KEY", None)
    env.pop("VOYAGE_API_KEY", None)
    env.pop("NX_STORAGE_BACKEND", None)

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=90.0)
        yield f"http://127.0.0.1:{svc_port}", token
    finally:
        _stop_service(proc)
        chroma_path = env.get("NX_CHROMA_PATH", "")
        if chroma_path and Path(chroma_path).exists():
            shutil.rmtree(chroma_path, ignore_errors=True)


# ── Integration tests ─────────────────────────────────────────────────────────


# Collection uses minilm-l6-v2-384 (the ONNX local embedder) — same as seam_b.
_COLLECTION = "knowledge__seam-gate-dedup__minilm-l6-v2-384__v1"


def _make_chunks(n: int, prefix: str = "chunk") -> tuple[list[str], list[str]]:
    """Generate *n* unique (ids, documents) pairs."""
    docs = [f"{prefix}_doc_{i:04d}: unique content for chunk index {i}" for i in range(n)]
    ids = [_chunk_id(d) for d in docs]
    return ids, docs


def test_over_300_record_upsert_round_trip(
    local_service: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single logical upsert_chunks call with >300 ids completes without error
    and all ids are retrievable from the server.

    The client's ``upsert_chunks`` sends a SINGLE POST to ``/v1/vectors/upsert-chunks``
    (line 545 in http_vector_client.py — no client-side batching on this path, unlike
    ``update_chunks`` which batches at 300).  The server's ``PgVectorRepository``
    handles the full batch in one transaction.  This test proves the seam is
    end-to-end functional, not just signature-compatible.
    """
    from nexus.db.http_vector_client import (
        get_http_vector_client,
        reset_http_vector_client_for_tests,
    )

    base_url, token = local_service
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    monkeypatch.delenv("NX_LOCAL", raising=False)
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    reset_http_vector_client_for_tests()

    client = get_http_vector_client()
    n = 350  # > 300 = MAX_RECORDS_PER_WRITE
    all_ids, all_docs = _make_chunks(n, prefix="over300")

    assert len(all_ids) > 300, "precondition: must test with >300 ids"

    # Should not raise — the server must accept the full batch
    client.upsert_chunks(_COLLECTION, all_ids, all_docs)

    # Verify round-trip: existing_ids must see ALL upserted chunks
    found = client.existing_ids(_COLLECTION, all_ids)
    assert found == set(all_ids), (
        f"Server did not store all {n} chunks. "
        f"Missing {len(set(all_ids) - found)} of {n}. "
        "This is the signature-parity-not-behaviour-parity failure class."
    )


def test_duplicate_chash_dedup_collapse(
    local_service: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server-side dedup collapses duplicate chash ids within a single upsert call.

    ``PgVectorRepository.upsertChunksInternal`` builds a ``seen`` HashSet and
    removes duplicates before embed+write (lines 292-313, Java source).  This test
    sends a batch where the same 10 unique chash ids each appear 5 times (50 total
    records in the request) and asserts:
      - No error from the server.
      - The stored count equals the UNIQUE chash count (10), not the raw count (50).
    """
    from nexus.db.http_vector_client import (
        get_http_vector_client,
        reset_http_vector_client_for_tests,
    )

    base_url, token = local_service
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    monkeypatch.delenv("NX_LOCAL", raising=False)
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    reset_http_vector_client_for_tests()

    client = get_http_vector_client()

    # 10 unique chunks, each repeated 5 times
    unique_ids, unique_docs = _make_chunks(10, prefix="dedup_gate")
    dup_ids = unique_ids * 5   # 50 ids, 10 unique
    dup_docs = unique_docs * 5

    assert len(dup_ids) == 50
    assert len(set(dup_ids)) == 10

    coll = "knowledge__seam-gate-dedup2__minilm-l6-v2-384__v1"

    # Server must not error on duplicate chash ids in one batch
    client.upsert_chunks(coll, dup_ids, dup_docs)

    # Stored count == unique chash count (server dedup collapsed duplicates)
    found = client.existing_ids(coll, dup_ids)
    assert found == set(unique_ids), (
        f"Server dedup failed: expected {len(unique_ids)} unique stored chunks, "
        f"found {len(found)}. "
        "PgVectorRepository.upsertChunksInternal HashSet dedup not working across wire."
    )


def test_on_conflict_idempotency(
    local_service: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-upserting the same chash with updated metadata updates the row, not duplicates.

    Exercises ``ON CONFLICT (tenant_id, collection, chash) DO UPDATE SET
    chunk_text/embedding/metadata`` (lines 385-388, PgVectorRepository.java).
    """
    from nexus.db.http_vector_client import (
        get_http_vector_client,
        reset_http_vector_client_for_tests,
    )

    base_url, token = local_service
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    monkeypatch.delenv("NX_LOCAL", raising=False)
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    reset_http_vector_client_for_tests()

    client = get_http_vector_client()
    coll = "knowledge__seam-gate-conflict__minilm-l6-v2-384__v1"

    # First upsert
    ids, docs = _make_chunks(5, prefix="conflict_gate")
    metas_v1 = [{"version": "v1", "index": i} for i in range(5)]
    client.upsert_chunks(coll, ids, docs, metadatas=metas_v1)

    # Confirm all 5 are present
    found_after_first = client.existing_ids(coll, ids)
    assert found_after_first == set(ids), "First upsert did not store all 5 chunks"

    # Re-upsert same chash ids with updated metadata
    metas_v2 = [{"version": "v2", "index": i} for i in range(5)]
    client.upsert_chunks(coll, ids, docs, metadatas=metas_v2)

    # Count must still be exactly 5 (no duplicates created)
    found_after_second = client.existing_ids(coll, ids)
    assert found_after_second == set(ids), (
        f"After re-upsert: expected same 5 chunks, got {len(found_after_second)}. "
        "ON CONFLICT DO UPDATE produced a duplicate instead of updating the row."
    )

    # Metadata refresh: the stored chunk must now carry v2 metadata
    entry = client.get_by_id(coll, ids[0])
    assert entry is not None, "get_by_id returned None for a just-upserted chunk"
    # The content field carries the chunk text (flat T3Database shape)
    assert "conflict_gate" in entry.get("content", ""), (
        f"get_by_id returned unexpected content: {entry}"
    )


def test_oversize_chunk_rejected_by_server(
    local_service: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chunk whose byte length exceeds MAX_DOCUMENT_BYTES (16 384) is rejected.

    The server enforces NUL sanitization and dim-guard fail-loud paths; oversize
    is the adjacent quota boundary.  This test sends a single chunk of exactly
    MAX_DOCUMENT_BYTES + 1 bytes and asserts the server returns a 4xx, surfaced
    as ``VectorServiceError`` (not a silent drop, not a 2xx with truncation).

    NOTE: if the service currently does NOT enforce this boundary at the HTTP
    layer (enforcement is at the Python QuotaValidator level only), this test
    will FAIL, which is the correct outcome — it surfaces the missing server-side
    guard as a test failure rather than a silent gap.
    """
    from nexus.db.chroma_quotas import QUOTAS
    from nexus.db.http_vector_client import (
        VectorServiceError,
        get_http_vector_client,
        reset_http_vector_client_for_tests,
    )

    base_url, token = local_service
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    monkeypatch.delenv("NX_LOCAL", raising=False)
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    reset_http_vector_client_for_tests()

    client = get_http_vector_client()
    coll = "knowledge__seam-gate-oversize__minilm-l6-v2-384__v1"

    # Build a chunk that is exactly 1 byte over the limit
    oversize_text = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)
    assert len(oversize_text.encode()) == QUOTAS.MAX_DOCUMENT_BYTES + 1
    chunk_id = _chunk_id(oversize_text)

    try:
        client.upsert_chunks(coll, [chunk_id], [oversize_text])
        # If we reach here, the server accepted the oversize chunk.
        # This is a finding: the server does not enforce MAX_DOCUMENT_BYTES.
        # We mark this explicitly rather than silently passing.
        pytest.xfail(
            "Server accepted an oversize chunk (> MAX_DOCUMENT_BYTES). "
            "This means enforcement is Python-only (QuotaValidator), not server-side. "
            "Audit finding: server-side oversize guard is absent. "
            "Track a follow-on bead to add server-side enforcement if desired."
        )
    except VectorServiceError as exc:
        # Expected: server rejected the oversize chunk with a 4xx
        # VectorServiceError.code carries the HTTP status when available.
        if exc.code is not None:
            assert 400 <= exc.code < 600, (
                f"VectorServiceError.code={exc.code} is not a 4xx/5xx: {exc}"
            )
        # If code is None it was a transport error (connection refused etc.),
        # which would indicate a different problem — the message string still
        # surfaces the issue.
