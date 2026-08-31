# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-xn3fr: recovery-bundle idempotency against the REAL engine.

The design's verification item (3): a re-import of the same bundle onto
the same catalog must MERGE, never duplicate — proven here against the
engine's actual duplicate-link contract (``co_discovered_by`` merge) and
its real ``/list?source_uri`` resolution, not a fake's approximation of
either.

Scope note (recorded on nexus-xn3fr): the knowledge-doc half runs with a
registration-only importer here — the full store_put chain's live
behavior (sdp0u reconcile, manifest write, fence) is
``tests/test_store_put_cli_parity.py``'s standing territory, and the
importer's exact call sequence into that chain is seam-pinned by
``tests/catalog/test_recovery_bundle.py``'s
``test_default_import_doc_drives_the_real_store_put_chain``. Duplicating
the full embedding env here would re-prove the chain, not the bundle.

Harness mirrors ``tests/db/test_http_catalog_integration.py`` (hermetic
PG via the bundled binaries, the shaded JAR with Liquibase at boot,
module-scoped, skip when the substrate is absent).
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.db._service_fixture import (
    SERVICE_ROLES_SQL,
    pg_bin_dir,
    spawn_service,
    wait_for_service,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_JAR = _REPO_ROOT / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_PG_BIN = pg_bin_dir()
_INITDB = _PG_BIN / "initdb"
_PG_CTL = _PG_BIN / "pg_ctl"
_CREATEDB = _PG_BIN / "createdb"
_JAVA_HOME = os.environ.get("JAVA_HOME", "")
_JAVA = Path(_JAVA_HOME) / "bin" / "java" if _JAVA_HOME else Path(shutil.which("java") or "java")

_SUBSTRATE_OK = _JAR.exists() and _INITDB.exists() and _PG_CTL.exists()
if not _SUBSTRATE_OK:
    pytest.skip(
        "engine substrate absent (service jar or PG bundle missing)",
        allow_module_level=True,
    )


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _psql(pg: dict, sql: str) -> None:
    subprocess.run(
        [str(_PG_BIN / "psql"), "-h", "127.0.0.1", "-p", str(pg["port"]),
         "-U", pg["user"], "-d", pg["dbname"], "-v", "ON_ERROR_STOP=1", "-c", sql],
        check=True, capture_output=True,
    )


@pytest.fixture(scope="module")
def pg_instance():
    pgdata = tempfile.mkdtemp(prefix="nexus_recovery_inttest_pg_")
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
             "-U", pg_user, "nexusrecoverytest"],
            check=True, capture_output=True,
        )
        pg = {"port": pg_port, "dbname": "nexusrecoverytest", "user": pg_user}
        _psql(pg, SERVICE_ROLES_SQL)
        yield pg
    finally:
        subprocess.run([str(_PG_CTL), "-D", pgdata, "stop", "-m", "immediate"],
                       capture_output=True)
        shutil.rmtree(pgdata, ignore_errors=True)


@pytest.fixture(scope="module")
def service(pg_instance):
    svc_port = _free_port()
    token = "recovery-inttest-bearer-secret"
    pg_jdbc = (
        f"jdbc:postgresql://127.0.0.1:{pg_instance['port']}/{pg_instance['dbname']}"
    )
    env = {
        **os.environ,
        "NX_SERVICE_PORT": str(svc_port),
        "NX_SERVICE_TOKEN": token,
        "NX_DB_URL": pg_jdbc,
        "NX_DB_USER": "nexus_svc",
        "NX_DB_PASS": "nexus_svc_pass",
        "NX_POOL_SIZE": "3",
        "NX_DB_ADMIN_URL": pg_jdbc,
        "NX_DB_ADMIN_USER": pg_instance["user"],
        "NX_DB_ADMIN_PASS": "",
    }
    env.pop("NX_STORAGE_BACKEND", None)
    env.pop("NX_STORAGE_BACKEND_CATALOG", None)
    proc, svc_log = spawn_service([str(_JAVA), "-jar", str(_JAR)], env)
    try:
        wait_for_service("127.0.0.1", svc_port, proc=proc, log_path=svc_log, timeout=60.0)
        yield f"http://127.0.0.1:{svc_port}", token
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


@pytest.fixture(scope="module")
def cat(service):
    from nexus.catalog.http_catalog_client import HttpCatalogClient

    base_url, token = service
    c = HttpCatalogClient(base_url=base_url, tenant="default", _token=token)
    yield c
    c.close()


class _NoT3:
    """The link-half round-trip needs no T3 — a fetch would be a test bug."""

    def get_by_id(self, collection: str, doc_id: str):
        raise AssertionError("T3 must not be touched by the link round-trip")


def _register_import_doc(cat, owner_t):
    """Registration-only importer (see the module docstring's scope note):
    models the chain's catalog-visible outcome — a row carrying the sdp0u
    identity — without the embedding env."""
    from nexus.aspect_readers import uri_for

    def _f(t3, rec: dict) -> None:
        uri = rec["source_uri"] or uri_for(rec["collection"], rec["title"])
        cat.register(
            owner_t, rec["title"], content_type="knowledge",
            physical_collection=rec["collection"], source_uri=uri,
        )
    return _f


def _seed_chunk(pg: dict, tenant: str, collection: str, chash_hex: str, *, dim: int = 768) -> None:
    """RDR-191 manifest-chunk FK: the manifest write 409s unless the chunk
    row exists — mirror test_http_catalog_integration._seed_chunks (zero
    vector; superuser psql bypasses FORCE RLS)."""
    vec = "[" + ",".join(["0"] * dim) + "]"
    _psql(pg, (
        f"INSERT INTO nexus.catalog_collections (tenant_id, name) VALUES ('{tenant}', '{collection}') "
        "ON CONFLICT DO NOTHING; "
        f"INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_{dim}) "
        f"VALUES ('{tenant}', '{collection}', decode('{chash_hex}', 'hex'), 'seed', '{vec}'::vector) "
        "ON CONFLICT DO NOTHING;"
    ))


def test_reimport_is_idempotent_against_the_real_engine(cat, pg_instance, tmp_path):
    """Design item (3), live: export -> import onto the SAME catalog ->
    zero net growth; the second pass merges every link."""
    from nexus.catalog.recovery_bundle import export_bundle, import_bundle

    # Seed: one store_put-shaped knowledge doc + one file-backed doc + a link.
    know_col = "knowledge__knowledge__bge-base-en-v15-768__v1"
    # register() takes an OWNER TUMBLER PREFIX, not a name — mint the owners
    # first (the store_hook does the same via curator_owner_tumbler_by_name).
    know_owner = cat.register_owner("knowledge", owner_type="curator")
    repo_owner = cat.register_owner("nexus", owner_type="repo")
    a = cat.register(
        know_owner, "recovery-note-a", content_type="knowledge",
        physical_collection=know_col,
        source_uri="chroma://knowledge/recovery-note-a",
    )
    b = cat.register(
        repo_owner, "beta.py", content_type="code",
        physical_collection="code__nexus__bge-base-en-v15-768__v1",
        file_path="src/beta.py", source_uri="file:///repo/src/beta.py",
    )
    a_t, b_t = str(a), str(b)
    assert cat.link(a_t, b_t, "cites", "recovery-inttest")

    # The knowledge doc needs a single-row manifest to be exportable —
    # and the manifest FK needs the chunk row to exist first (RDR-191).
    chash = "f" * 64
    _seed_chunk(pg_instance, "default", know_col, chash)
    cat.atomic_manifest_replace(a_t, [{"chash": chash, "position": 0}], collection=know_col)

    class _T3:
        def get_by_id(self, collection: str, doc_id: str):
            assert doc_id == chash
            return {"id": doc_id, "content": "note-a body", "tags": "", "category": ""}

    bundle = tmp_path / "bundle.jsonl"
    summary = export_bundle(cat, _T3(), bundle)
    assert summary.docs_exported == 1
    assert summary.links_exported == 1

    links_before = len(cat.link_query(limit=200))

    s1 = import_bundle(cat, cat, _NoT3(), bundle,
                       import_doc=_register_import_doc(cat, know_owner))
    s2 = import_bundle(cat, cat, _NoT3(), bundle,
                       import_doc=_register_import_doc(cat, know_owner))

    # Real by_source_uri resolution + real duplicate-link merge:
    assert s1.unresolvable_links == []
    assert s2.unresolvable_links == []
    assert s2.links_created == 0, "second import must merge, never create"
    assert s2.links_merged == 1
    links_after = len(cat.link_query(limit=200))
    assert links_after == links_before, "re-import grew the link set"

    # The registration importer reconciled (never duplicated) doc A:
    matches = [
        d for d in cat.all_documents()
        if d.title == "recovery-note-a" and d.physical_collection == know_col
    ]
    assert len(matches) == 1, f"doc duplicated on re-import: {len(matches)} rows"
