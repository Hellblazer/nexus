# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-152 bead nexus-gmiaf.22 — Seam B INDEXER integration test.

Proves the index→search round-trip works end-to-end with the indexer
using ONLY the Java service (no direct Chroma / voyageai on the index
write path):

  1. Start the Java nexus-service in LOCAL mode (ONNX embedder, no Voyage key).
  2. Set NX_STORAGE_BACKEND_VECTORS=service.
  3. Run the indexer on a tiny Python file corpus via doc_indexer._index_document.
  4. Search for a known phrase → must return the indexed chunk.

Prerequisite:
  - service/target/nexus-service-1.0-SNAPSHOT.jar must exist (build first).
  - Java (>= 17) on PATH or JAVA_HOME set.
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present.
  - No VOYAGE_API_KEY or NX_VOYAGE_API_KEY needed (ONNX mode).

Run:
    cd service && mvn package -DskipTests && cd ..
    uv run pytest tests/db/test_indexer_seam_b_integration.py -m integration -v -s
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Generator

import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL

# ── Prerequisite detection ──────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN = Path("/opt/homebrew/opt/postgresql@16/bin")

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

_ALL_PREREQS = (
    _JAR.exists()
    and _INITDB.exists()
    and _PG_CTL.exists()
    and _PSQL.exists()
    and _CREATEDB.exists()
    and (_JAVA.exists() if _JAVA_HOME else shutil.which("java") is not None)
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _ALL_PREREQS,
        reason=(
            "skipped: missing jar or pg16 binaries "
            f"(jar={_JAR.exists()}, pg16={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]

# ── Bootstrap SQL ────────────────────────────────────────────────────────────
#
# net63: JAR runs Liquibase at startup; grants-nexus-svc.xml (runAlways=true) issues
# GRANT ... TO nexus_svc — this role must exist BEFORE the JAR starts or the
# migration fails and System.exit(1) fires before the HTTP port opens.
# SERVICE_ROLES_SQL creates nexus_svc; this SQL creates the test service role.

_BOOTSTRAP_SQL = """\
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_seam_b') THEN
    CREATE ROLE svc_seam_b LOGIN PASSWORD 'svc_seam_b_pass';
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO svc_seam_b;
"""

# ── Service helpers ───────────────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _stop_service(proc: subprocess.Popen) -> None:
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


# ── Module-scoped Postgres fixture ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def pg_instance() -> Generator[dict, None, None]:
    """Hermetic Postgres 16 instance."""
    pgdata = tempfile.mkdtemp(prefix="nexus_seam_b_pg_")
    pg_port = _free_port()
    pglog = os.path.join(pgdata, "pg.log")
    pg_user = os.environ["USER"]

    try:
        subprocess.run(
            [str(_INITDB), "-D", pgdata, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        with open(os.path.join(pgdata, "postgresql.conf"), "a") as f:
            f.write(f"\nport = {pg_port}\nlisten_addresses = '127.0.0.1'\n")
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "-l", pglog,
             "-o", f"-p {pg_port} -k {pgdata}", "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "seambtest"],
            check=True, capture_output=True,
        )
        pg = {"port": pg_port, "dbname": "seambtest", "user": pg_user}

        def _run_sql(sql: str) -> None:
            proc = subprocess.run(
                [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                 "-U", pg_user, "-d", "seambtest",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"SQL failed (rc={proc.returncode}):\n"
                    f"stdout={proc.stdout}\nstderr={proc.stderr}"
                )

        # net63: create nexus_svc BEFORE the JAR starts (grants-nexus-svc.xml pre-condition).
        _run_sql(SERVICE_ROLES_SQL)
        _run_sql(_BOOTSTRAP_SQL)

        yield pg
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def local_service(pg_instance: dict) -> Generator[tuple[str, str], None, None]:
    """Java service in LOCAL mode (ONNX, no Voyage key). Yields (base_url, token)."""
    token = "seam-b-test-token"
    svc_port = _free_port()
    pg_user = pg_instance["user"]

    # Ephemeral Chroma data dir — the service defaults NX_CHROMA_PATH to
    # ~/.config/nexus/chroma, which persists across runs and would leave a
    # previously-indexed doc on disk, making the staleness round-trip
    # (first index > 0, second index == 0) non-idempotent. A per-module
    # tmp dir guarantees a clean Chroma on every run.
    chroma_data = tempfile.mkdtemp(prefix="seam-b-chroma-")

    pg_jdbc = (
        f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}/{pg_instance['dbname']}"
    )
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": token,
        # App pool: nexus_svc (NOSUPERUSER NOBYPASSRLS) — subject to FORCE RLS.
        # DML grants are wired by grants-nexus-svc.xml (runAlways) during Liquibase run.
        "NX_DB_URL":  pg_jdbc,
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "2",
        # Migration pool: OS superuser — has DDL rights for full Liquibase run.
        "NX_DB_ADMIN_URL":  pg_jdbc,
        "NX_DB_ADMIN_USER": pg_user,
        "NX_DB_ADMIN_PASS": "",
        "NX_CHROMA_MODE": "local",
        "NX_CHROMA_PATH": chroma_data,
    }
    # Ensure no Voyage key — forces ONNX mode
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
        _wait_tcp("127.0.0.1", svc_port, timeout=60.0)
        yield f"http://127.0.0.1:{svc_port}", token
    finally:
        _stop_service(proc)
        shutil.rmtree(chroma_data, ignore_errors=True)


# ── Integration test ────────────────────────────────────────────────────────────


def test_indexer_seam_b_index_search_round_trip(
    local_service: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Index a small document via the Java service (ONNX embedder) and search for it.

    Proves: the indexer's Seam B write path (doc_indexer._index_document) routes
    through HttpVectorClient → /v1/vectors/upsert-chunks (no direct Chroma/Voyage).
    The search result proves the service actually embedded and stored the chunk.
    """
    import json
    import urllib.request

    base_url, token = local_service

    # Point HttpVectorClient at the live service
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)

    # Do NOT set NX_LOCAL=1: the service-mode guard must activate BEFORE
    # is_local_mode() fires.  Setting NX_LOCAL=1 here would cause
    # _make_local_embed_fn() to run first (the pre-fix ordering bug), making
    # the "no Python embed" proof vacuous.  The guard fix (nexus-gmiaf.22)
    # routes service mode first so no Voyage / ONNX creds are needed.
    monkeypatch.delenv("NX_LOCAL", raising=False)
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    # Reset the singleton so new env vars are picked up
    from nexus.db.http_vector_client import reset_http_vector_client_for_tests
    reset_http_vector_client_for_tests()

    # Create a tiny document corpus
    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    test_doc = corpus_dir / "seam_b_marker.md"
    test_doc.write_text(
        "# Seam B Test Document\n\n"
        "This document is indexed via the Java nexus-service vector endpoint.\n"
        "It contains the unique phrase: quantum_flux_capacitor_seam_b_gmiaf22.\n"
    )

    collection = "knowledge__seam-b-test__minilm-l6-v2-384__v1"

    # NON-VACUITY PROOF: patch _make_local_embed_fn and _embed_with_fallback
    # to raise AssertionError.  If either fires, the service-mode guard is
    # broken and Python embedding is running instead of the service.
    from unittest.mock import patch

    def _must_not_call_local_embed(*_a, **_kw):
        raise AssertionError(
            "_make_local_embed_fn was called in service mode — the service-mode "
            "guard must prevent local ONNX from firing (guard ordering bug)"
        )

    def _must_not_call_embed_fallback(*_a, **_kw):
        raise AssertionError(
            "_embed_with_fallback was called in service mode — the service-mode "
            "guard or upsert-site stub is broken (Python embed must not run)"
        )

    with patch("nexus.doc_indexer._make_local_embed_fn", side_effect=_must_not_call_local_embed), \
         patch("nexus.doc_indexer._embed_with_fallback", side_effect=_must_not_call_embed_fallback):
        # Index via doc_indexer using the service (embed_fn=None, service mode)
        from nexus.doc_indexer import _index_document, _markdown_chunks

        chunks_indexed = _index_document(
            test_doc,
            corpus="seam-b-test",
            collection_name=collection,
            chunk_fn=_markdown_chunks,
            t3=None,          # forces service-mode routing via get_t3()
            embed_fn=None,    # no Python embed — service embeds server-side
        )

    assert chunks_indexed > 0, (
        f"Expected at least 1 chunk indexed, got {chunks_indexed}"
    )

    # Staleness check: second index call with same content must return 0
    # (service now has /v1/vectors/get so incremental-sync works).
    with patch("nexus.doc_indexer._make_local_embed_fn", side_effect=_must_not_call_local_embed), \
         patch("nexus.doc_indexer._embed_with_fallback", side_effect=_must_not_call_embed_fallback):
        from nexus.doc_indexer import _index_document as _index_document2
        chunks_reindexed = _index_document2(
            test_doc,
            corpus="seam-b-test",
            collection_name=collection,
            chunk_fn=_markdown_chunks,
            t3=None,
            embed_fn=None,
        )

    assert chunks_reindexed == 0, (
        f"Second index of identical content must be a no-op (0 upserts), "
        f"got {chunks_reindexed} — staleness check via /v1/vectors/get is broken"
    )

    # Search for the unique phrase via the service's /v1/vectors/search
    search_body = json.dumps({
        "query": "quantum_flux_capacitor_seam_b_gmiaf22",
        "collections": [collection],
        "n_results": 5,
    }).encode()
    req = urllib.request.Request(
        f"{base_url}/v1/vectors/search",
        data=search_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Nexus-Tenant": "default",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        results = json.loads(resp.read())

    assert results, "search returned no results — chunk was not indexed via service"
    result_texts = [r.get("content", "") for r in results]
    assert any("seam_b" in t.lower() or "quantum" in t.lower() for t in result_texts), (
        f"None of the search results contain the indexed phrase.\n"
        f"Got: {result_texts}"
    )
