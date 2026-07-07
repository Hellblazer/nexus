# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live-service integration test: xnz0o analytics methods (nexus-xnz0o).

Proves that the 7 new HttpCatalogClient methods added in nexus-xnz0o work
correctly end-to-end against the real Java service + real PostgreSQL.  Each
test seeds known data and asserts EXACT values, making the tests non-vacuous:
a wrong HTTP param, wrong response-field name, or wrong parsing will produce
wrong values and fail the assertions.

Methods exercised:
  1.  list_owners()              -> GET /owners/list
  2.  distinct_doc_collections() -> GET /docs/distinct-collections
  3.  owners_with_roots()        -> GET /owners/all-with-roots
  4.  orphaned_docs()            -> GET /docs/orphaned
  5.  docs_with_absolute_paths() -> GET /docs/absolute-paths
  6.  get_collection_owner_root()-> GET /collections/owner-root?name=X
  7.  collection_doc_counts()    -> GET /docs/collection-counts
  8.  find_by_file_path()        -> GET /list?file_path=X  (owner-agnostic)

Spot-checks for ported COMMON commands:
  - stats (by_content_type)     -> cat.stats()
  - owners path                 -> cat.list_owners()
  - orphans path                -> cat.orphaned_docs()
  - prune-stale path            -> cat.distinct_doc_collections() + owners_with_roots()
  - collections-drift path      -> cat.distinct_doc_collections()

Also verifies cat._db raises RuntimeError (no SQLite access).

Requires (darwin with JDK):
  - PostgreSQL binaries discoverable (NEXUS_PG_BIN / Homebrew / system dirs / PATH)
  - service/target/nexus-service-1.0-SNAPSHOT.jar built
      (cd service && mvn package -DskipTests)
  - Java on PATH (or JAVA_HOME env set)

Marked @pytest.mark.integration — skipped when prerequisites absent.

Run locally:
    cd service && mvn package -DskipTests
    uv run pytest tests/db/test_xnz0o_commands_integration.py -m integration -q
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

import pytest

from tests.db._service_fixture import SERVICE_ROLES_SQL, pg_bin_dir

# ── Prerequisite paths ────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR       = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN    = pg_bin_dir()

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
            "skipped: missing jar or PG binaries "
            f"(jar={_JAR.exists()}, pg={_PG_CTL.exists()}, java={_JAVA})"
        ),
    ),
]

_TOKEN  = "xnz0o-inttest-bearer-secret"
_TENANT = "xnz0o-tenant"


# ── Port + process helpers ─────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_tcp(host: str, port: int, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.15)
    raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")


def _psql(pg: dict, sql: str) -> None:
    proc = subprocess.run(
        [
            str(_PSQL),
            "-h", "127.0.0.1",
            "-p", str(pg["port"]),
            "-U", pg["user"],
            "-d", pg["dbname"],
            "-v", "ON_ERROR_STOP=1",
            "-c", sql,
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"psql failed (rc={proc.returncode}):\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )


# ── Module-scoped fixtures ────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pg_instance():
    """Hermetic PostgreSQL 16 instance (distinct DB from other integration tests)."""
    pgdata  = tempfile.mkdtemp(prefix="nexus_xnz0o_inttest_pg_")
    pg_port = _free_port()
    pglog   = os.path.join(pgdata, "pg.log")
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
             "-o", f"-p {pg_port} -k {pgdata}",
             "start", "-w"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [str(_CREATEDB), "-h", "127.0.0.1", "-p", str(pg_port),
             "-U", pg_user, "nexuscatxnz0o"],
            check=True, capture_output=True,
        )

        pg = {"port": pg_port, "dbname": "nexuscatxnz0o", "user": pg_user, "pgdata": pgdata}

        # nexus_svc role must exist before the JAR starts Liquibase
        _psql(pg, SERVICE_ROLES_SQL)

        yield pg
    finally:
        subprocess.run(
            [str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
            capture_output=True,
        )
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def java_service(pg_instance):
    """Launch the shaded JAR against pg_instance."""
    svc_port    = _free_port()
    chroma_data = tempfile.mkdtemp(prefix="nexus-xnz0o-chroma-")

    pg_user = pg_instance["user"]
    pg_jdbc = (
        f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}"
        f"/{pg_instance['dbname']}"
    )
    env = {
        **os.environ,
        "NX_SERVICE_PORT":  str(svc_port),
        "NX_SERVICE_TOKEN": _TOKEN,
        "NX_DB_URL":        pg_jdbc,
        "NX_DB_USER":       pg_user,
        "NX_DB_PASS":       "",
        "NX_POOL_SIZE":     "3",
        "NX_CHROMA_PATH":   chroma_data,
    }
    env.pop("NX_STORAGE_BACKEND",         None)
    env.pop("NX_STORAGE_BACKEND_CATALOG", None)

    proc = subprocess.Popen(
        [str(_JAVA), "-jar", str(_JAR)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=os.setsid,
    )
    try:
        _wait_tcp("127.0.0.1", svc_port, timeout=40.0)
        yield f"http://127.0.0.1:{svc_port}", _TOKEN, proc
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
def cat(java_service):
    """HttpCatalogClient against the real Java service (no ._db / no SQLite)."""
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    base_url, token, _ = java_service
    _saved_token = os.environ.get("NX_SERVICE_TOKEN")
    os.environ["NX_SERVICE_TOKEN"] = token
    c = HttpCatalogClient(base_url=base_url, tenant=_TENANT, _token=token)
    yield c
    c.close()
    # Restore: leaking this module's token past its service's lifetime poisons
    # later modules that resolve the endpoint/token from env (nexus-edwlp:
    # this exact leak 401'd every T3 round-trip after this module under the
    # local-service gate).
    if _saved_token is None:
        os.environ.pop("NX_SERVICE_TOKEN", None)
    else:
        os.environ["NX_SERVICE_TOKEN"] = _saved_token


@pytest.fixture(scope="module")
def seeded(cat):
    """Seed owners, documents, links, and collections for all analytics tests.

    Returns a dict of known tumblers / collection names so assertions are exact.
    """
    # ── Owners ────────────────────────────────────────────────────────────────
    repo_a = cat.register_owner(
        name="xnz0o-repo-a",
        owner_type="repo",
        tumbler_prefix="201",
        repo_root="/projects/xnz0o-a",
        head_hash="aaaa1111",
    )
    repo_b = cat.register_owner(
        name="xnz0o-repo-b",
        owner_type="repo",
        tumbler_prefix="202",
        repo_root="/projects/xnz0o-b",
        head_hash="bbbb2222",
    )
    # curator has NO repo_root — must not appear in owners_with_roots
    curator = cat.register_owner(
        name="xnz0o-curator",
        owner_type="curator",
        tumbler_prefix="203",
    )

    # ── Collections ───────────────────────────────────────────────────────────
    coll_paper = "xnz0o__knowledge__voyage-context-3__v1"
    coll_code  = "xnz0o__code__voyage-code-3__v1"
    cat.register_collection(
        coll_paper,
        content_type="knowledge",
        owner_id=str(repo_a),
        embedding_model="voyage-context-3",
    )
    cat.register_collection(
        coll_code,
        content_type="code",
        owner_id=str(repo_a),
        embedding_model="voyage-code-3",
    )

    # ── Documents ─────────────────────────────────────────────────────────────
    # Three docs in coll_paper
    doc_p1 = cat.register(
        str(repo_a), "XNZ0O Paper 1",
        content_type="paper",
        corpus="knowledge",
        physical_collection=coll_paper,
        file_path="papers/xnz0o-p1.pdf",
        source_uri="file:///projects/xnz0o-a/papers/xnz0o-p1.pdf",
        chunk_count=10,
    )
    doc_p2 = cat.register(
        str(repo_a), "XNZ0O Paper 2",
        content_type="paper",
        corpus="knowledge",
        physical_collection=coll_paper,
        file_path="papers/xnz0o-p2.pdf",
        source_uri="file:///projects/xnz0o-a/papers/xnz0o-p2.pdf",
        chunk_count=5,
    )
    doc_p3 = cat.register(
        str(repo_a), "XNZ0O Paper 3",
        content_type="paper",
        corpus="knowledge",
        physical_collection=coll_paper,
        file_path="papers/xnz0o-p3.pdf",
        source_uri="file:///projects/xnz0o-a/papers/xnz0o-p3.pdf",
        chunk_count=8,
    )
    # One doc in coll_code
    doc_c1 = cat.register(
        str(repo_a), "XNZ0O Code 1",
        content_type="code",
        corpus="code",
        physical_collection=coll_code,
        file_path="src/xnz0o.py",
        source_uri="file:///projects/xnz0o-a/src/xnz0o.py",
        chunk_count=3,
    )
    # One doc with an ABSOLUTE file_path — for docs_with_absolute_paths
    doc_abs = cat.register(
        str(repo_a), "XNZ0O Absolute Path",
        content_type="paper",
        corpus="knowledge",
        physical_collection=coll_paper,
        file_path="/absolute/path/to/doc.pdf",
        source_uri="file:///absolute/path/to/doc.pdf",
        chunk_count=2,
    )
    # One doc with no physical_collection — must NOT appear in distinct_doc_collections
    doc_no_coll = cat.register(
        str(repo_a), "XNZ0O No Collection",
        content_type="paper",
        corpus="knowledge",
        source_uri="file:///projects/xnz0o-a/no-coll.pdf",
    )
    # One doc under repo_b (for find_by_file_path cross-owner test)
    doc_b1 = cat.register(
        str(repo_b), "XNZ0O Repo-B Doc",
        content_type="rdr",
        corpus="rdr",
        physical_collection=coll_paper,  # same collection name, different owner
        file_path="rdrs/xnz0o-b1.md",
        source_uri="file:///projects/xnz0o-b/rdrs/xnz0o-b1.md",
    )

    # ── Links ─────────────────────────────────────────────────────────────────
    # doc_p1 -> doc_p2 (cites) — both have links, neither is orphaned
    cat.link(doc_p1, doc_p2, "cites",   created_by="inttest-xnz0o")
    # doc_p1 -> doc_c1 (relates) — doc_p1 has 2 outbound links
    cat.link(doc_p1, doc_c1, "relates", created_by="inttest-xnz0o")
    # doc_abs is orphaned (no links in or out)
    # doc_no_coll is orphaned
    # doc_b1 is orphaned

    return {
        "repo_a":       str(repo_a),
        "repo_b":       str(repo_b),
        "curator":      str(curator),
        "coll_paper":   coll_paper,
        "coll_code":    coll_code,
        "doc_p1":       str(doc_p1),
        "doc_p2":       str(doc_p2),
        "doc_p3":       str(doc_p3),
        "doc_c1":       str(doc_c1),
        "doc_abs":      str(doc_abs),
        "doc_no_coll":  str(doc_no_coll),
        "doc_b1":       str(doc_b1),
    }


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestNoSQLiteAccess:
    """HttpCatalogClient sentinel: ._db raises RuntimeError (service mode)."""

    def test_client_is_http_catalog_client(self, cat) -> None:
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        assert isinstance(cat, HttpCatalogClient), type(cat)

    def test_db_property_raises_runtime_error(self, cat) -> None:
        """._db must raise RuntimeError with 'service mode' in the message.

        Non-vacuous: a real SQLite Catalog handle would NOT raise — the test
        fails if someone wires the wrong backend.
        """
        with pytest.raises(RuntimeError, match="service mode"):
            _ = cat._db


class TestListOwners:
    """list_owners() -> GET /owners/list: all owners for the tenant."""

    def test_exact_owners_returned(self, cat, seeded) -> None:
        owners = cat.list_owners()
        names = {o["name"] for o in owners}
        # All three seeded owners must appear
        assert "xnz0o-repo-a" in names, f"repo-a missing; got {names}"
        assert "xnz0o-repo-b" in names, f"repo-b missing; got {names}"
        assert "xnz0o-curator" in names, f"curator missing; got {names}"

    def test_owner_dicts_have_required_fields(self, cat, seeded) -> None:
        owners = cat.list_owners()
        for o in owners:
            assert "tumbler_prefix" in o, f"tumbler_prefix missing from {o}"
            assert "name" in o, f"name missing from {o}"
            assert "owner_type" in o, f"owner_type missing from {o}"

    def test_repo_owner_has_correct_type(self, cat, seeded) -> None:
        owners = cat.list_owners()
        repo_a = next((o for o in owners if o["name"] == "xnz0o-repo-a"), None)
        assert repo_a is not None
        assert repo_a["owner_type"] == "repo"
        assert repo_a["repo_root"] == "/projects/xnz0o-a"


class TestDistinctDocCollections:
    """distinct_doc_collections() -> GET /docs/distinct-collections."""

    def test_exact_collections_returned(self, cat, seeded) -> None:
        colls = cat.distinct_doc_collections()
        assert seeded["coll_paper"] in colls, (
            f"coll_paper {seeded['coll_paper']!r} missing from {colls}"
        )
        assert seeded["coll_code"] in colls, (
            f"coll_code {seeded['coll_code']!r} missing from {colls}"
        )

    def test_empty_collection_excluded(self, cat, seeded) -> None:
        """doc_no_coll has no physical_collection — must not appear."""
        colls = cat.distinct_doc_collections()
        assert "" not in colls, f"empty string appeared in distinct collections: {colls}"
        assert not any(c is None for c in colls), "None appeared in distinct collections"

    def test_returns_list_of_strings(self, cat, seeded) -> None:
        colls = cat.distinct_doc_collections()
        assert isinstance(colls, list)
        for c in colls:
            assert isinstance(c, str), f"non-string in collections: {c!r}"


class TestOwnersWithRoots:
    """owners_with_roots() -> GET /owners/all-with-roots."""

    def test_repos_with_roots_appear(self, cat, seeded) -> None:
        result = cat.owners_with_roots()
        # Dict[tumbler_prefix -> repo_root]
        assert isinstance(result, dict), f"expected dict, got {type(result)}"
        assert seeded["repo_a"] in result, (
            f"repo_a {seeded['repo_a']!r} missing from owners_with_roots: {result}"
        )
        assert seeded["repo_b"] in result, (
            f"repo_b {seeded['repo_b']!r} missing from owners_with_roots: {result}"
        )

    def test_exact_repo_roots(self, cat, seeded) -> None:
        result = cat.owners_with_roots()
        assert result[seeded["repo_a"]] == "/projects/xnz0o-a"
        assert result[seeded["repo_b"]] == "/projects/xnz0o-b"

    def test_curator_without_root_excluded(self, cat, seeded) -> None:
        result = cat.owners_with_roots()
        # curator has no repo_root — must not appear
        assert seeded["curator"] not in result, (
            f"curator appeared in owners_with_roots: {result}"
        )


class TestOrphanedDocs:
    """orphaned_docs() -> GET /docs/orphaned."""

    def test_linked_docs_not_orphaned(self, cat, seeded) -> None:
        orphans = cat.orphaned_docs()
        orphan_tumblers = {o["tumbler"] for o in orphans}
        # doc_p1 (has outbound links) must not be an orphan
        assert seeded["doc_p1"] not in orphan_tumblers, (
            f"doc_p1 (has outbound links) appeared in orphans: {orphan_tumblers}"
        )
        # doc_p2 (has inbound link) must not be an orphan
        assert seeded["doc_p2"] not in orphan_tumblers, (
            f"doc_p2 (has inbound link) appeared in orphans: {orphan_tumblers}"
        )
        # doc_c1 (has inbound link) must not be an orphan
        assert seeded["doc_c1"] not in orphan_tumblers, (
            f"doc_c1 (has inbound link) appeared in orphans: {orphan_tumblers}"
        )

    def test_unlinked_doc_is_orphaned(self, cat, seeded) -> None:
        """doc_abs (no links at all) must appear as an orphan."""
        orphans = cat.orphaned_docs()
        orphan_tumblers = {o["tumbler"] for o in orphans}
        assert seeded["doc_abs"] in orphan_tumblers, (
            f"doc_abs (no links) not found in orphans: {orphan_tumblers}"
        )

    def test_orphan_dicts_have_required_fields(self, cat, seeded) -> None:
        orphans = cat.orphaned_docs()
        for o in orphans:
            assert "tumbler" in o, f"tumbler missing from orphan: {o}"
            # title must be present (content check)
            assert "title" in o, f"title missing from orphan: {o}"


class TestDocsWithAbsolutePaths:
    """docs_with_absolute_paths() -> GET /docs/absolute-paths."""

    def test_absolute_path_doc_returned(self, cat, seeded) -> None:
        result = cat.docs_with_absolute_paths()
        tumblers = {d["tumbler"] for d in result}
        assert seeded["doc_abs"] in tumblers, (
            f"doc_abs (/absolute/path/to/doc.pdf) missing from abs-paths result: {tumblers}"
        )

    def test_relative_path_docs_excluded(self, cat, seeded) -> None:
        result = cat.docs_with_absolute_paths()
        tumblers = {d["tumbler"] for d in result}
        # Docs with relative paths must NOT appear
        assert seeded["doc_p1"] not in tumblers, (
            f"doc_p1 (relative path) appeared in abs-paths result: {tumblers}"
        )
        assert seeded["doc_c1"] not in tumblers, (
            f"doc_c1 (relative path) appeared in abs-paths result: {tumblers}"
        )

    def test_result_dicts_have_file_path(self, cat, seeded) -> None:
        result = cat.docs_with_absolute_paths()
        for d in result:
            assert "file_path" in d, f"file_path missing from {d}"
            assert d["file_path"].startswith("/"), (
                f"file_path {d['file_path']!r} does not start with '/'"
            )


class TestGetCollectionOwnerRoot:
    """get_collection_owner_root() -> GET /collections/owner-root?name=X."""

    def test_known_collection_returns_owner_and_root(self, cat, seeded) -> None:
        owner_id, repo_root = cat.get_collection_owner_root(seeded["coll_paper"])
        assert owner_id == seeded["repo_a"], (
            f"owner_id: expected {seeded['repo_a']!r}, got {owner_id!r}"
        )
        assert repo_root == "/projects/xnz0o-a", (
            f"repo_root: expected '/projects/xnz0o-a', got {repo_root!r}"
        )

    def test_absent_collection_returns_empty_strings(self, cat, seeded) -> None:
        owner_id, repo_root = cat.get_collection_owner_root("no-such-collection-xyz")
        assert owner_id == "", f"expected '', got {owner_id!r}"
        assert repo_root == "", f"expected '', got {repo_root!r}"

    def test_returns_tuple_of_strings(self, cat, seeded) -> None:
        result = cat.get_collection_owner_root(seeded["coll_code"])
        assert isinstance(result, tuple), f"expected tuple, got {type(result)}"
        assert len(result) == 2, f"expected 2-tuple, got {len(result)}"
        owner_id, repo_root = result
        assert isinstance(owner_id, str)
        assert isinstance(repo_root, str)


class TestCollectionDocCounts:
    """collection_doc_counts() -> GET /docs/collection-counts."""

    def test_exact_counts(self, cat, seeded) -> None:
        counts = cat.collection_doc_counts()
        # coll_paper has: doc_p1, doc_p2, doc_p3, doc_abs, doc_b1 = 5 docs
        assert seeded["coll_paper"] in counts, (
            f"coll_paper missing from collection_doc_counts: {counts}"
        )
        assert counts[seeded["coll_paper"]] == 5, (
            f"coll_paper count: expected 5, got {counts[seeded['coll_paper']]}"
        )
        # coll_code has: doc_c1 = 1 doc
        assert seeded["coll_code"] in counts, (
            f"coll_code missing from collection_doc_counts: {counts}"
        )
        assert counts[seeded["coll_code"]] == 1, (
            f"coll_code count: expected 1, got {counts[seeded['coll_code']]}"
        )

    def test_no_collection_docs_excluded(self, cat, seeded) -> None:
        """doc_no_coll has no physical_collection — must not contribute any key."""
        counts = cat.collection_doc_counts()
        assert "" not in counts, "empty-string key in collection_doc_counts"
        assert not any(k is None for k in counts), "None key in collection_doc_counts"

    def test_returns_int_values(self, cat, seeded) -> None:
        counts = cat.collection_doc_counts()
        for k, v in counts.items():
            assert isinstance(v, int), f"count for {k!r} is not int: {v!r} ({type(v)})"


class TestFindByFilePath:
    """find_by_file_path() -> GET /list?file_path=X (owner-agnostic)."""

    def test_finds_existing_doc_by_relative_path(self, cat, seeded) -> None:
        result = cat.find_by_file_path("papers/xnz0o-p1.pdf")
        assert result is not None, "find_by_file_path('papers/xnz0o-p1.pdf') returned None"
        assert str(result.tumbler) == seeded["doc_p1"], (
            f"expected tumbler {seeded['doc_p1']!r}, got {result.tumbler!r}"
        )

    def test_finds_doc_with_absolute_path(self, cat, seeded) -> None:
        result = cat.find_by_file_path("/absolute/path/to/doc.pdf")
        assert result is not None, "find_by_file_path('/absolute/path/to/doc.pdf') returned None"
        assert str(result.tumbler) == seeded["doc_abs"], (
            f"expected tumbler {seeded['doc_abs']!r}, got {result.tumbler!r}"
        )

    def test_absent_path_returns_none(self, cat, seeded) -> None:
        result = cat.find_by_file_path("no/such/file/anywhere.pdf")
        assert result is None, f"expected None for absent path, got {result!r}"

    def test_finds_across_owners(self, cat, seeded) -> None:
        """find_by_file_path is owner-agnostic — finds docs under any owner."""
        # doc_b1 is under repo_b, not repo_a
        result = cat.find_by_file_path("rdrs/xnz0o-b1.md")
        assert result is not None, "find_by_file_path cross-owner returned None"
        assert str(result.tumbler) == seeded["doc_b1"], (
            f"expected tumbler {seeded['doc_b1']!r}, got {result.tumbler!r}"
        )


class TestStatsByContentType:
    """cat.stats() returns by_content_type and links_by_type (COMMON commands spot-check)."""

    def test_by_content_type_exact_values(self, cat, seeded) -> None:
        stats = cat.stats()
        assert "by_content_type" in stats, f"by_content_type missing from stats: {stats.keys()}"
        by_type = stats["by_content_type"]
        # Module-scoped tenant: 5 paper (p1/p2/p3/abs/no_coll) + 1 code + 1 rdr
        paper_count = by_type.get("paper", 0)
        assert paper_count == 5, (
            f"expected 5 paper docs, got {paper_count}; by_content_type={by_type}"
        )
        code_count = by_type.get("code", 0)
        assert code_count == 1, (
            f"expected 1 code doc, got {code_count}; by_content_type={by_type}"
        )

    def test_by_content_type_is_dict(self, cat, seeded) -> None:
        stats = cat.stats()
        assert isinstance(stats["by_content_type"], dict)

    def test_links_by_type_exact_values(self, cat, seeded) -> None:
        """Two links were seeded: cites(1) + relates(1)."""
        stats = cat.stats()
        assert "links_by_type" in stats, f"links_by_type missing from stats: {stats.keys()}"
        by_link = stats["links_by_type"]
        assert by_link.get("cites", 0) == 1, (
            f"expected 1 cites link, got {by_link.get('cites', 0)}"
        )
        assert by_link.get("relates", 0) == 1, (
            f"expected 1 relates link, got {by_link.get('relates', 0)}"
        )


class TestCommonCommandSpotChecks:
    """Spot-checks for ported COMMON commands that use the new analytics methods."""

    def test_prune_stale_path_distinct_collections(self, cat, seeded) -> None:
        """prune_stale_cmd uses distinct_doc_collections to find collections to scan."""
        colls = cat.distinct_doc_collections()
        # Both seeded collections must appear (prune-stale would scan them)
        assert seeded["coll_paper"] in colls
        assert seeded["coll_code"] in colls

    def test_prune_stale_path_owners_with_roots(self, cat, seeded) -> None:
        """prune_stale_cmd uses owners_with_roots to find stale-check candidates."""
        owr = cat.owners_with_roots()
        # Both repo owners have roots
        assert seeded["repo_a"] in owr
        assert seeded["repo_b"] in owr
        # Curator has no root (excluded from prune-stale logic)
        assert seeded["curator"] not in owr

    def test_collections_drift_distinct_collections(self, cat, seeded) -> None:
        """collections_drift uses distinct_doc_collections as the T3 reference set."""
        colls = set(cat.distinct_doc_collections())
        # Drift check: expected collections are present in the document set
        assert seeded["coll_paper"] in colls
        assert seeded["coll_code"] in colls

    def test_orphans_cmd_path(self, cat, seeded) -> None:
        """orphans_cmd uses orphaned_docs to enumerate unlinked documents."""
        orphans = cat.orphaned_docs()
        tumblers = {o["tumbler"] for o in orphans}
        # doc_p3 is in coll_paper but has no links — must be in orphans
        assert seeded["doc_p3"] in tumblers, (
            f"doc_p3 (no links) should be an orphan; orphan tumblers: {tumblers}"
        )

    def test_owners_cmd_path_list_owners(self, cat, seeded) -> None:
        """owners_cmd uses list_owners() to enumerate tenant owners."""
        owners = cat.list_owners()
        # Module-scoped tenant: exactly 3 seeded owners (repo_a, repo_b, curator)
        assert len(owners) == 3, f"expected 3 owners, got {len(owners)}: {[o['name'] for o in owners]}"
        types = {o["owner_type"] for o in owners}
        assert "repo" in types
        assert "curator" in types
