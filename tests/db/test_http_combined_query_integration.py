# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4nflf — Combined-query end-to-end tripwire (write via public API ->
combined-query visibility via public API, HTTP/MCP layer).

Provenance (nexus-x6kdz): on 2026-07-08 the live 6.5.0 shakeout found ALL
combined-query SQL functions returning zero rows. Root cause: NO write-time
writer stamped ``catalog_document_chunks.collection`` (the combined-query
join key) — the only rows that ever had it were stamped by the migration
leg's ``manifest_backfill()``, and every subsequent manifest REPLACE write
(``writeManifestRows``, one shared body for first and Nth call alike)
recreated the rows without the stamp. Combined queries
(``search_metadata_scoped`` / ``search_graph_hop``) inner-join on that
stamp, so re-indexed docs silently vanished from combined-query results on
a live tenant with every existing gate green:

- Every Python combined-query unit/integration test monkeypatched
  ``HttpVectorClient._post`` and asserted request SHAPE only — none of them
  round-tripped through a real Postgres-backed service, so the join could
  not actually fail in those tests.
- The Java-side ``ManifestCollectionStampTest`` covers ``CatalogRepository``
  directly (in-JVM), which proves the SQL is correct in isolation but never
  crosses the Python HTTP client -> real server -> real Postgres wire the way
  production traffic does.

This module closes that gap: it boots the real service JAR (full Liquibase,
including the catalog-006/-007/-008/-012/-014 combined-query SQL functions)
against a throwaway ``pgvector/pgvector:pg17`` Docker container, with the
service's application pool bound to ``nexus_svc`` (NOSUPERUSER NOBYPASSRLS —
production-like FORCE RLS, not the migration/superuser pool), and drives the
entire write -> read arc through PUBLIC API methods only
(``HttpCatalogClient.register`` / ``.write_manifest`` / ``.link``,
``HttpVectorClient.upsert_chunks``). No raw SQL is used for any assertion in
this module — the fixtures' role-provisioning psql call is the sole SQL
touch-point, copied verbatim from ``test_write_seam_gate_integration.py``
because Liquibase's ``grants-nexus-svc.xml`` changeset (runAlways=true)
requires the ``nexus_svc`` role to exist before the JVM boots.

Fixture strategy: same Docker pgvector/pgvector:pg17 pattern as
``test_write_seam_gate_integration.py``, own container name
(``nexus_cq_tripwire_pg17``) so the two gates never collide on a shared
Docker container when run concurrently.

Prerequisites (identical to the write-seam gate):
  - ``service/target/nexus-service-1.0-SNAPSHOT.jar`` built and fresh.
  - Docker available and ``pgvector/pgvector:pg17`` pullable.
  - Java (>= 17) on PATH or JAVA_HOME set.
  - No VOYAGE_API_KEY needed (service runs in LOCAL/ONNX bge-768 mode).

Run locally:
    cd service && mvn package -DskipTests && cd ..
    uv run pytest tests/db/test_http_combined_query_integration.py \\
        -o addopts="" -m integration -v -s

Non-vacuity guarantee:
    - The ``_service_jar_freshness`` autouse fixture in conftest.py SKIPS
      locally and FAILS in CI when the JAR is missing/stale (same mechanism
      as the write-seam gate; not duplicated here).
    - Every positive visibility assertion (a row IS returned) is paired with
      a negative control in the SAME test (a ``where`` filter that must
      return EMPTY) so the assertion cannot pass vacuously regardless of
      whether the combined-query SQL functions are wired correctly.
    - ``test_metadata_scoped_visible_after_public_api_write`` already trips
      on the shipped x6kdz defect (pre-fix, the FIRST ``write_manifest``
      call omitted the ``collection`` column entirely — zero rows from call
      one). ``test_manifest_rewrite_keeps_combined_query_visibility`` runs
      the REPLACE write TWICE on top of that: ``writeManifestRows`` is one
      shared body for first and Nth call today, so the second pass guards
      against a FUTURE first/second-write asymmetry (e.g. a diff-based
      short-circuit optimization), not a distinct door in the current code.
    - The genuinely asymmetric write seams are the ``ON CONFLICT DO
      UPDATE`` append/import paths (``appendManifestChunks`` /
      ``doImportChunk``), covered by
      ``test_append_and_import_seams_stamp_collection``: their INSERT
      branch stamping is fully falsifiable here; their DO-UPDATE re-stamp
      line (``.set(COLLECTION, EX_CHK_COLL)``) is only falsifiable when the
      pre-conflict stamp is wrong, a state the public API cannot construct
      on a correct tree — that residual is recorded on bead nexus-4nflf and
      covered in-JVM by ``ManifestCollectionStampTest``.
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
    """sha256(text)[:32] — matches HttpVectorClient's chash convention."""
    return hashlib.sha256(text.encode()).hexdigest()[:32]


# ── Module-scoped Docker pgvector fixture ─────────────────────────────────────

_CONTAINER_NAME = "nexus_cq_tripwire_pg17"


@pytest.fixture(scope="module")
def pg_instance():
    """Throwaway pgvector/pgvector:pg17 container.

    Copied verbatim (container name + db name changed) from
    ``test_write_seam_gate_integration.py`` — see that module's docstring
    for why the role-creation step is a RETRY LOOP and not a fixed sleep.
    """
    pg_port = _free_port()
    pg_user = "nexus_test"
    pg_pass = "nexus_test_pass"
    pg_db = "nexus_cq_tripwire"

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

    # Create nexus_svc role (required by Liquibase grants-nexus-svc.xml,
    # runAlways=true). RETRY LOOP, not a fixed sleep: the postgres image's
    # entrypoint boots a TEMPORARY server for initdb, shuts it down
    # ("FATAL: the database system is shutting down"), then starts the real
    # one — and pg_isready can succeed against the temporary server, so a
    # psql issued in that window dies with exactly that FATAL.
    psql_deadline = time.monotonic() + 60.0
    while True:
        try:
            _run_psql_in_container(pg_user, pg_db, SERVICE_ROLES_SQL)
            break
        except RuntimeError:
            if time.monotonic() >= psql_deadline:
                logs = subprocess.run(
                    ["docker", "logs", _CONTAINER_NAME],
                    capture_output=True, text=True,
                )
                subprocess.run(["docker", "rm", "-f", _CONTAINER_NAME], capture_output=True)
                raise RuntimeError(
                    "role-creation psql never succeeded (PG init-restart window "
                    f"did not close within 60s):\n{logs.stdout[-2000:]}\n{logs.stderr[-2000:]}"
                ) from None
            time.sleep(1.0)

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
    """Execute SQL inside the running container as the superuser.

    Fixture-provisioning only (role creation) — no test assertion in this
    module uses raw SQL.
    """
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
    """Java service in LOCAL mode (ONNX bge-768 embedder, no Voyage key).

    App pool is ``nexus_svc`` (NOSUPERUSER NOBYPASSRLS) — production-like
    FORCE RLS, matching the write-seam gate's pool split. Full Liquibase
    runs at boot, which is what makes the combined-query SQL functions and
    the catalog-014 changeset real for this test (not a mock).

    Yields ``(base_url, token)`` after the HTTP port is reachable.
    """
    token = "cq-tripwire-gate-token"
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
        "NX_CHROMA_PATH": tempfile.mkdtemp(prefix="cq-tripwire-chroma-"),
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


@pytest.fixture
def cat_client(local_service: tuple[str, str]):
    """HttpCatalogClient bound to the live local_service, closed on teardown."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    base_url, token = local_service
    with HttpCatalogClient(base_url=base_url, tenant="default", _token=token) as c:
        yield c


@pytest.fixture
def vec_client(local_service: tuple[str, str], monkeypatch: pytest.MonkeyPatch):
    """HttpVectorClient singleton re-resolved against the live local_service."""
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

    return get_http_vector_client()


# ── Integration tests ─────────────────────────────────────────────────────────

# Collection uses bge-base-en-v15-768 (the ONNX local embedder) — service
# embeds server-side into chunks_768 tables, exercised through the
# search_*_768 combined-query SQL functions.
_COLLECTION = "knowledge__cq-tripwire__bge-base-en-v15-768__v1"


def test_metadata_scoped_visible_after_public_api_write(
    cat_client, vec_client
) -> None:
    """Register -> upsert -> write_manifest -> search_metadata_scoped, all
    through public API methods, must make the chunk combined-query-visible.

    Pre-x6kdz-fix this path returned zero rows: the manifest writer never
    stamped ``document_chunks.collection`` on first write, so
    ``search_metadata_scoped``'s inner join against the catalog manifest
    excluded the row entirely.

    Also proves the catalog-012 ``where`` equality shape is a REAL predicate,
    not a no-op: a matching ``where`` returns the row, a mismatching
    ``where`` on the SAME query returns empty — the negative control lives
    in this same test so it cannot pass vacuously.
    """
    text = (
        "cqtripwire test1 alpha unique content about combined query "
        "visibility through the public catalog and vector client API"
    )
    chash = _chunk_id(text)

    t = cat_client.register(
        "1.1",
        "CQ Tripwire Doc A",
        content_type="knowledge",
        author="cq-tripwire-suite",
        corpus="cq-tripwire",
        physical_collection=_COLLECTION,
        source_uri="file:///cq-tripwire/docA.md",
    )

    vec_client.upsert_chunks(
        _COLLECTION,
        [chash],
        [text],
        metadatas=[{"kind": "cq-tripwire"}],
    )

    cat_client.write_manifest(
        str(t),
        [{"position": 0, "chash": chash, "line_start": 1, "line_end": 10}],
    )

    query = "cqtripwire test1 alpha combined query visibility"

    rows = vec_client.search_metadata_scoped(query, [_COLLECTION])
    matching = [r for r in rows if r["id"] == str(t)]
    assert matching, (
        f"search_metadata_scoped returned no row for tumbler {t!s} after a "
        "public-API register+upsert+write_manifest write — this is the "
        "x6kdz zero-rows regression (manifest writer never stamped "
        "document_chunks.collection, so the combined-query inner join "
        f"excluded the doc). Rows returned: {rows!r}"
    )
    assert matching[0]["chash"] == chash, (
        f"matched row's chash {matching[0]['chash']!r} does not equal the "
        f"upserted chunk's chash {chash!r} — combined query matched the "
        "wrong chunk or a stale row"
    )

    # Non-vacuity negative control: a mismatching `where` on the identical
    # query MUST return empty, proving `where` is a live predicate.
    where_match = vec_client.search_metadata_scoped(
        query, [_COLLECTION], where={"kind": "cq-tripwire"}
    )
    assert any(r["id"] == str(t) for r in where_match), (
        "matching where={'kind': 'cq-tripwire'} did not return the doc — "
        "catalog-012 where-equality shape is broken for the positive case"
    )
    where_mismatch = vec_client.search_metadata_scoped(
        query, [_COLLECTION], where={"kind": "no-such-value"}
    )
    assert where_mismatch == [], (
        f"mismatching where={{'kind': 'no-such-value'}} on the SAME query "
        f"returned {where_mismatch!r} instead of []  — the where filter is "
        "a no-op server-side, which would let the positive assertion above "
        "pass vacuously regardless of whether filtering actually works"
    )


def test_manifest_rewrite_keeps_combined_query_visibility(
    cat_client, vec_client
) -> None:
    """The x6kdz core regression test: a repeat ``write_manifest`` REPLACE
    pass over the same document must NOT wipe combined-query visibility.

    This is self-contained (its own doc, its own chunks) so it does not
    depend on test execution order. Precision note (substantive-critic,
    2026-07-09): ``writeManifestRows`` is ONE shared DELETE+re-INSERT body
    for first and Nth call alike — pre-fix, the FIRST call already omitted
    the ``collection`` column, so test 1 alone catches the shipped defect.
    What THIS test adds is a guard against a future first/second-write
    asymmetry in that shared body (e.g. a diff-based short-circuit that
    skips the re-INSERT, or a REPLACE variant that clears the stamp a
    migration-backfill had repaired) — the "visible, then re-write, then
    invisible" shape the live tenant actually experienced.
    """
    texts = [
        (
            "cqtripwire test2 first pass unique content about manifest "
            "rewrite regression chunk zero"
        ),
        (
            "cqtripwire test2 first pass unique content about manifest "
            "rewrite regression chunk one"
        ),
    ]
    ids = [_chunk_id(t) for t in texts]

    t = cat_client.register(
        "1.1",
        "CQ Tripwire Doc Rewrite",
        content_type="knowledge",
        author="cq-tripwire-suite",
        corpus="cq-tripwire",
        physical_collection=_COLLECTION,
        source_uri="file:///cq-tripwire/doc-rewrite.md",
    )

    vec_client.upsert_chunks(_COLLECTION, ids, texts)

    manifest_rows = [
        {"position": i, "chash": ids[i], "line_start": i * 10 + 1, "line_end": i * 10 + 10}
        for i in range(len(texts))
    ]

    # First write_manifest pass.
    cat_client.write_manifest(str(t), manifest_rows)

    query = "cqtripwire test2 first pass manifest rewrite regression"

    rows_after_first = vec_client.search_metadata_scoped(query, [_COLLECTION])
    matching_first = [r for r in rows_after_first if r["id"] == str(t)]
    assert matching_first, (
        f"search_metadata_scoped returned no row for {t!s} after the FIRST "
        f"write_manifest call — cannot test the rewrite regression if the "
        f"baseline write itself is not visible. Rows: {rows_after_first!r}"
    )
    assert matching_first[0]["chash"] in ids, (
        f"first-write matched chash {matching_first[0]['chash']!r} is not "
        f"one of the upserted chunk ids {ids!r}"
    )

    # SECOND write_manifest pass — same doc, same rows. Same shared REPLACE
    # body as the first call today; must keep stamping the collection even
    # if the two calls ever diverge.
    cat_client.write_manifest(str(t), manifest_rows)

    rows_after_second = vec_client.search_metadata_scoped(query, [_COLLECTION])
    matching_second = [r for r in rows_after_second if r["id"] == str(t)]
    assert matching_second, (
        f"search_metadata_scoped returned NO row for {t!s} after a SECOND "
        "write_manifest REPLACE pass over the SAME document — this is "
        "exactly the x6kdz regression: the manifest REPLACE writer wiped "
        "document_chunks.collection, silently emptying combined-query "
        f"results on a live tenant. Rows before second write: "
        f"{rows_after_first!r}; rows after: {rows_after_second!r}"
    )
    assert matching_second[0]["chash"] in ids, (
        f"post-rewrite matched chash {matching_second[0]['chash']!r} is not "
        f"one of the upserted chunk ids {ids!r} — visibility survived but "
        "matched the wrong chunk"
    )


def test_graph_hop_visible_through_link(cat_client, vec_client) -> None:
    """Register doc A + doc B, link A -> B, then search_graph_hop from seed A
    must surface doc B — proving the catalog link graph and combined-query
    hop traversal compose correctly through the public API.

    Also carries the catalog-012 where match/mismatch pair extended to the
    graph-hop path (non-vacuity: a positive-only assertion here would pass
    identically whether or not the hop's where filter is wired at all).
    """
    text_a = (
        "cqtripwire test3 doc a seed content about graph hop traversal "
        "starting from a linked seed document"
    )
    text_b = (
        "cqtripwire test3 doc b linked target content about graph hop "
        "reachability through a catalog link"
    )
    chash_a = _chunk_id(text_a)
    chash_b = _chunk_id(text_b)

    doc_a = cat_client.register(
        "1.1",
        "CQ Tripwire Doc A (hop seed)",
        content_type="knowledge",
        author="cq-tripwire-suite",
        corpus="cq-tripwire",
        physical_collection=_COLLECTION,
        source_uri="file:///cq-tripwire/hop-a.md",
    )
    doc_b = cat_client.register(
        "1.1",
        "CQ Tripwire Doc B (hop target)",
        content_type="knowledge",
        author="cq-tripwire-suite",
        corpus="cq-tripwire",
        physical_collection=_COLLECTION,
        source_uri="file:///cq-tripwire/hop-b.md",
    )

    vec_client.upsert_chunks(_COLLECTION, [chash_a], [text_a])
    vec_client.upsert_chunks(
        _COLLECTION, [chash_b], [text_b], metadatas=[{"kind": "cq-tripwire-hop"}]
    )

    cat_client.write_manifest(
        str(doc_a),
        [{"position": 0, "chash": chash_a, "line_start": 1, "line_end": 10}],
    )
    cat_client.write_manifest(
        str(doc_b),
        [{"position": 0, "chash": chash_b, "line_start": 1, "line_end": 10}],
    )

    created = cat_client.link(
        str(doc_a), str(doc_b), "relates", created_by="cq-tripwire-test"
    )
    assert created, f"expected a NEW link to be created between {doc_a!s} and {doc_b!s}"

    query = "cqtripwire test3 doc b linked target graph hop reachability"

    hop_rows = vec_client.search_graph_hop(
        query, seeds=[str(doc_a)], collection_names=[_COLLECTION], depth=1
    )
    assert any(r["id"] == str(doc_b) for r in hop_rows), (
        f"search_graph_hop from seed {doc_a!s} did not surface linked doc "
        f"{doc_b!s} — graph hop traversal through the public link/search API "
        f"is broken. Rows: {hop_rows!r}"
    )

    # Non-vacuity negative control for the graph-hop where shape.
    hop_where_match = vec_client.search_graph_hop(
        query,
        seeds=[str(doc_a)],
        collection_names=[_COLLECTION],
        depth=1,
        where={"kind": "cq-tripwire-hop"},
    )
    assert any(r["id"] == str(doc_b) for r in hop_where_match), (
        "matching where={'kind': 'cq-tripwire-hop'} did not return doc B "
        "through the graph hop — catalog-012 where-equality shape is broken "
        "on the graph-hop path for the positive case"
    )
    hop_where_mismatch = vec_client.search_graph_hop(
        query,
        seeds=[str(doc_a)],
        collection_names=[_COLLECTION],
        depth=1,
        where={"kind": "no-such-value"},
    )
    assert hop_where_mismatch == [], (
        f"mismatching where={{'kind': 'no-such-value'}} on the SAME graph "
        f"hop returned {hop_where_mismatch!r} instead of [] — the hop's "
        "where filter is a no-op server-side, which would let the positive "
        "assertion above pass vacuously regardless of whether filtering "
        "actually works"
    )


def test_append_and_import_seams_stamp_collection(cat_client, vec_client) -> None:
    """The other two public write seams the x6kdz fix touched must ALSO
    stamp ``document_chunks.collection`` (substantive-critic finding 2,
    2026-07-09): ``appendManifestChunks`` (via
    ``HttpCatalogClient.append_manifest_chunks``) and ``importChunksBatch``
    (via ``POST /v1/catalog/import/chunk`` — the exact envelope the
    migration orchestrator sends, see
    ``src/nexus/migration/orchestrator.py`` fill_missing_document_chunks
    import_fn).

    Both are ``ON CONFLICT DO UPDATE`` writers, unlike ``write_manifest``'s
    DELETE+re-INSERT: dropping ``COLLECTION`` from their INSERT column list
    is a one-line regression this test trips on (the append/import call is
    each doc's FIRST manifest write here, so the INSERT branch runs and an
    unstamped row is combined-query-invisible). Each seam is then re-called
    with the same rows to walk the DO-UPDATE branch; sensitivity caveat: on
    a correct tree the pre-conflict stamp is already right, so a dropped
    DO-UPDATE ``.set(COLLECTION, ...)`` alone would not flip this test —
    that narrower line is covered in-JVM by ``ManifestCollectionStampTest``
    and recorded as a residual on bead nexus-4nflf.
    """
    # ── Seam 1: append_manifest_chunks (POST /v1/catalog/manifest/append) ──
    text_app = (
        "cqtripwire test4 append seam unique content stamped through the "
        "manifest append on-conflict writer"
    )
    chash_app = _chunk_id(text_app)

    doc_app = cat_client.register(
        "1.1",
        "CQ Tripwire Doc Append",
        content_type="knowledge",
        author="cq-tripwire-suite",
        corpus="cq-tripwire",
        physical_collection=_COLLECTION,
        source_uri="file:///cq-tripwire/doc-append.md",
    )
    vec_client.upsert_chunks(_COLLECTION, [chash_app], [text_app])

    append_rows = [
        {"position": 0, "chash": chash_app, "line_start": 1, "line_end": 10}
    ]
    # First append = INSERT branch: must stamp collection or the doc is
    # combined-query-invisible (the x6kdz defect class on this seam).
    cat_client.append_manifest_chunks(str(doc_app), append_rows)

    query_app = "cqtripwire test4 append seam manifest on-conflict writer"
    rows = vec_client.search_metadata_scoped(query_app, [_COLLECTION])
    matching = [r for r in rows if r["id"] == str(doc_app)]
    assert matching, (
        f"search_metadata_scoped returned no row for {doc_app!s} after "
        "append_manifest_chunks — the append seam's INSERT branch is not "
        f"stamping document_chunks.collection. Rows: {rows!r}"
    )
    assert matching[0]["chash"] == chash_app, (
        f"append-seam matched chash {matching[0]['chash']!r} != upserted "
        f"chash {chash_app!r}"
    )

    # Re-append the same rows = ON CONFLICT DO UPDATE branch. Visibility
    # must survive (see docstring for what this is and isn't sensitive to).
    cat_client.append_manifest_chunks(str(doc_app), append_rows)
    rows2 = vec_client.search_metadata_scoped(query_app, [_COLLECTION])
    assert any(r["id"] == str(doc_app) for r in rows2), (
        f"doc {doc_app!s} vanished from combined-query results after a "
        "REPEAT append_manifest_chunks call — the append seam's ON CONFLICT "
        f"DO UPDATE branch broke the collection stamp. Rows: {rows2!r}"
    )

    # ── Seam 2: import chunks (POST /v1/catalog/import/chunk) ─────────────
    text_imp = (
        "cqtripwire test4 import seam unique content stamped through the "
        "migration import chunk batch writer"
    )
    chash_imp = _chunk_id(text_imp)

    doc_imp = cat_client.register(
        "1.1",
        "CQ Tripwire Doc Import",
        content_type="knowledge",
        author="cq-tripwire-suite",
        corpus="cq-tripwire",
        physical_collection=_COLLECTION,
        source_uri="file:///cq-tripwire/doc-import.md",
    )
    vec_client.upsert_chunks(_COLLECTION, [chash_imp], [text_imp])

    import_rows = [
        {"position": 0, "chash": chash_imp, "line_start": 1, "line_end": 10}
    ]
    # Same wire envelope the migration orchestrator's verify-fill leg sends.
    cat_client._post("/import/chunk", {"doc_id": str(doc_imp), "rows": import_rows})

    query_imp = "cqtripwire test4 import seam migration chunk batch writer"
    rows = vec_client.search_metadata_scoped(query_imp, [_COLLECTION])
    matching = [r for r in rows if r["id"] == str(doc_imp)]
    assert matching, (
        f"search_metadata_scoped returned no row for {doc_imp!s} after "
        "/import/chunk — the import seam's INSERT branch is not stamping "
        "document_chunks.collection, so migration-imported manifests would "
        f"be combined-query-invisible. Rows: {rows!r}"
    )
    assert matching[0]["chash"] == chash_imp, (
        f"import-seam matched chash {matching[0]['chash']!r} != upserted "
        f"chash {chash_imp!r}"
    )

    # Re-import the same rows = ON CONFLICT DO UPDATE branch.
    cat_client._post("/import/chunk", {"doc_id": str(doc_imp), "rows": import_rows})
    rows2 = vec_client.search_metadata_scoped(query_imp, [_COLLECTION])
    assert any(r["id"] == str(doc_imp) for r in rows2), (
        f"doc {doc_imp!s} vanished from combined-query results after a "
        "REPEAT /import/chunk call — the import seam's ON CONFLICT DO "
        f"UPDATE branch broke the collection stamp. Rows: {rows2!r}"
    )
