# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-service integration test: frecency metadata update via Java service (nexus-enehl).

Proves that _run_index_frecency_only in service mode:
  A) Does NOT skip — it executes the real frecency-update logic.
  B) Routes the frecency_score update through the service's /v1/vectors/update-metadata
     endpoint (not daemon-Chroma).
  C) The updated frecency_score is visible via a subsequent service store-get
     (proves no split-brain: update lands in service Chroma, not daemon).
  D) A subsequent search on the collection returns the chunk (frecency restored).

Prerequisite:
  - service/target/nexus-service-1.0-SNAPSHOT.jar must exist (build first).
  - Java (>= 17) on PATH or JAVA_HOME set.
  - /opt/homebrew/opt/postgresql@16/bin/{initdb,pg_ctl,psql,createdb} present.
  - No VOYAGE_API_KEY or NX_VOYAGE_API_KEY needed (ONNX mode).

Run:
    cd service && mvn package -DskipTests && cd ..
    uv run pytest tests/db/test_frecency_enehl_integration.py -m integration -v -s
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL

# ── Prerequisite detection ──────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN = Path("/opt/homebrew/opt/postgresql@16/bin")

_INITDB   = _PG_BIN / "initdb"
_PG_CTL   = _PG_BIN / "pg_ctl"
_PSQL     = _PG_BIN / "psql"
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

# ── Bootstrap SQL ───────────────────────────────────────────────────────────

_BOOTSTRAP_SQL = """\
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_frecency_enehl') THEN
    CREATE ROLE svc_frecency_enehl LOGIN PASSWORD 'svc_frecency_enehl_pass';
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO svc_frecency_enehl;
"""

# ── Service helpers ─────────────────────────────────────────────────────────


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


# ── Module-scoped Postgres fixture ──────────────────────────────────────────


@pytest.fixture(scope="module")
def pg_instance():
    pgdata = tempfile.mkdtemp(prefix="nexus_frecency_pg_")
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
             "-U", pg_user, "frecency_test"],
            check=True, capture_output=True,
        )
        pg = {"port": pg_port, "dbname": "frecency_test", "user": pg_user}

        def _run_sql(sql: str) -> None:
            proc = subprocess.run(
                [str(_PSQL), "-h", "127.0.0.1", "-p", str(pg_port),
                 "-U", pg_user, "-d", "frecency_test",
                 "-v", "ON_ERROR_STOP=1", "-c", sql],
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"SQL failed (rc={proc.returncode}):\n"
                    f"stdout={proc.stdout}\nstderr={proc.stderr}"
                )

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
def local_service(pg_instance: dict):
    """Java service in LOCAL mode (ONNX, no Voyage key). Yields (base_url, token)."""
    token = "frecency-enehl-test-token"
    svc_port = _free_port()
    pg_user = pg_instance["user"]

    chroma_data = tempfile.mkdtemp(prefix="frecency-enehl-chroma-")
    pg_jdbc = (
        f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}/{pg_instance['dbname']}"
    )
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL":  pg_jdbc,
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "2",
        "NX_DB_ADMIN_URL":  pg_jdbc,
        "NX_DB_ADMIN_USER": pg_user,
        "NX_DB_ADMIN_PASS": "",
        "NX_CHROMA_MODE": "local",
        "NX_CHROMA_PATH": chroma_data,
    }
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


# ── HTTP helpers ─────────────────────────────────────────────────────────────


def _svc_post(base_url: str, token: str, path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Nexus-Tenant": "default",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


# ── Integration test ──────────────────────────────────────────────────────────


def test_frecency_service_mode_update_lands_in_service_chroma(
    local_service: tuple[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_run_index_frecency_only in service mode updates frecency_score in the
    service's Chroma (not daemon-Chroma) and the update is visible via service
    store-get — proves no split-brain and frecency is restored.

    Steps:
      1. Seed a chunk directly via the service /v1/vectors/upsert-chunks.
      2. Set up a registry pointing to that collection.
      3. Mock batch_frecency to return a known score for the test file.
      4. Run _run_index_frecency_only in service mode.
      5. Read back the chunk via /v1/vectors/store-get and assert frecency_score
         is the expected value (proves update went through the service).
      6. Verify make_t3() was NOT called (proves no daemon-Chroma write).
    """
    base_url, token = local_service

    # Point HttpVectorClient at the live service
    monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
    monkeypatch.setenv("NX_SERVICE_URL", base_url)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token)
    monkeypatch.delenv("NX_LOCAL", raising=False)
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)

    # Reset the service client singleton so new env vars are picked up
    from nexus.db.http_vector_client import reset_http_vector_client_for_tests
    reset_http_vector_client_for_tests()

    # Choose a unique collection name for this test run
    collection = "code__frecency-enehl-test__minilm-l6-v2-384__v1"
    chunk_id = "frecency-test-chunk-001"
    chunk_text = "def frecency_score_test(): return 42"

    # Step 1: Seed a chunk directly into the service's Chroma
    upsert_result = _svc_post(base_url, token, "/v1/vectors/upsert-chunks", {
        "collection": collection,
        "ids": [chunk_id],
        "documents": [chunk_text],
        "metadatas": [{"frecency_score": 0.0, "source_path": str(tmp_path / "test_file.py")}],
    })
    assert upsert_result.get("upserted") == 1, (
        f"Failed to seed chunk: {upsert_result}"
    )

    # Step 2: Build a fake registry pointing to this collection
    fake_registry = MagicMock()
    fake_registry.get.return_value = {
        "collection": collection,
        "code_collection": collection,
        "docs_collection": None,   # no docs collection — keeps test focused
    }

    # Step 3: Set up frecency map with a known score for tmp_path/test_file.py
    test_file = tmp_path / "test_file.py"
    test_file.write_text("# test file for frecency\ndef frecency_score_test(): return 42\n")
    expected_score = 0.77
    fake_frecency = {test_file: expected_score}

    # Step 4: Run _run_index_frecency_only in service mode
    # Patch:
    #   - batch_frecency → fake_frecency (controlled score)
    #   - make_t3 → sentinel (proves daemon-Chroma is NOT written)
    #   - catalog reader → unavailable (forces legacy where-filter fallback path)
    #     so the update path is exercised without catalog dependency
    make_t3_called = []

    def sentinel_make_t3():
        make_t3_called.append("CALLED")
        raise AssertionError(
            "make_t3() was called in service mode — "
            "frecency update must go through HttpVectorClient, not daemon-Chroma"
        )

    with (
        patch("nexus.frecency.batch_frecency", return_value=fake_frecency),
        patch("nexus.db.make_t3", side_effect=sentinel_make_t3),
        patch("nexus.catalog.factory.make_catalog_reader",
              side_effect=Exception("no catalog in test")),
    ):
        from nexus.indexer import _run_index_frecency_only
        _run_index_frecency_only(tmp_path, fake_registry)

    # Verify make_t3() was NOT called
    assert not make_t3_called, (
        "make_t3() must NOT be called in service mode — "
        "this would write to daemon-Chroma, creating a split-brain."
    )

    # Step 5: Read back the chunk via the service and verify frecency_score
    get_result = _svc_post(base_url, token, "/v1/vectors/store-get", {
        "collection": collection,
        "ids": [chunk_id],
    })
    ids = get_result.get("ids", [])
    assert chunk_id in ids, (
        f"Chunk {chunk_id!r} not found in service after frecency update. "
        f"Got ids: {ids}"
    )

    metas = get_result.get("metadatas", [])
    idx = ids.index(chunk_id)
    meta = metas[idx] if idx < len(metas) else {}
    actual_score = meta.get("frecency_score")

    assert actual_score == pytest.approx(expected_score, abs=1e-6), (
        f"frecency_score in service Chroma must be {expected_score} after update, "
        f"got {actual_score}. This proves the update went through the service endpoint "
        "and is visible to service-mode search."
    )

    # Step 6: Verify the chunk is also visible via search (frecency restored = searchable)
    search_result = _svc_post(base_url, token, "/v1/vectors/search", {
        "query": "frecency_score_test function",
        "collections": [collection],
        "n_results": 5,
    })
    assert search_result, (
        "Search returned no results — chunk must be searchable after frecency update"
    )
    result_ids = [r.get("id") for r in search_result]
    assert chunk_id in result_ids, (
        f"Indexed chunk {chunk_id!r} not returned by search after frecency update. "
        f"Got: {result_ids}"
    )
