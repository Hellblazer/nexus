# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/sql/livec_census.sql`` (RDR-192, bead nexus-wbfpw.32): the
v0.1.136 deploy gate for live(c). Runs the standalone census file, via
psql, exactly as conexus will run it against production -- as ``nexus_svc``
(NOSUPERUSER NOBYPASSRLS), with FORCE RLS live, the tenant set only through
``set_config('nexus.tenant', ...)`` inside the file's own transaction.

Real engine substrate (``t2_service_env``; see ``tests/_engine_substrate.py``
and ``tests/test_wbfpw31_nxexp_owner.py``): chunks and catalog rows are
created through ``HttpVectorClient.upsert_chunks_with_embeddings`` and the
real catalog writer (``register``/``write_manifest``/``delete_document``),
the same paths production writes through -- never a raw INSERT that could
seed a shape the engine itself cannot produce.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

import nexus.db.http_vector_client as hvc
from tests._catalog_fixture_ops import ActiveCatalog
from tests._engine_substrate import ensure_engine, mint_test_tenant

pytestmark = [pytest.mark.integration]

_SQL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "sql" / "livec_census.sql"

_MODEL = "bge-base-en-v15-768"


def _coll(name: str) -> str:
    return f"knowledge__wbfpw32-{name}__{_MODEL}__v1"


def _chash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _seed_one_chunk_per_cause(coll: str, coll2: str) -> dict[str, str]:
    """Seed one chunk per RDR-192 non-live cause, plus one live chunk, all
    physically stored in *coll* (the collection the census is asked about).

    ``other-collection-only`` needs a second, live-owned manifest row for
    the SAME chash in a DIFFERENT collection (*coll2*) -- and the manifest
    FK (``fk_catalog_chunks_chunk``, catalog-029, VALIDATED) targets
    ``nexus.chunks (tenant_id, collection, chash)``, so that chash must
    ALSO be physically stored under *coll2* or the manifest write 409s.
    This mirrors production's own "rename-COPY leftover" shape
    (``manifest_less_census.sql``'s ``dead-owner`` bucket comment) rather
    than being a test-only shortcut.
    """
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant="unused-client-side-tag")
    owner = cat.register_owner(f"{coll}-owner", "curator")

    chash_live = _chash(f"{coll}:live")
    chash_tomb = _chash(f"{coll}:tomb")
    chash_other = _chash(f"{coll}:other")
    chash_none = _chash(f"{coll}:none")

    db.upsert_chunks_with_embeddings(
        coll,
        ids=[chash_live, chash_tomb, chash_other, chash_none],
        documents=[
            "livec census live chunk", "livec census tombstoned-owner chunk",
            "livec census other-collection-only chunk", "livec census no-manifest chunk",
        ],
        embeddings=[],
        metadatas=[
            {"chunk_text_hash": chash_live, "title": "live.txt:1-1"},
            {"chunk_text_hash": chash_tomb, "title": "tomb.txt:1-1"},
            {"chunk_text_hash": chash_other, "title": "other.txt:1-1"},
            {"chunk_text_hash": chash_none, "title": "none.txt:1-1"},
        ],
    )
    # FK-satisfying copy: same chash, physically stored under coll2 too.
    db.upsert_chunks_with_embeddings(
        coll2,
        ids=[chash_other],
        documents=["livec census other-collection-only chunk"],
        embeddings=[],
        metadatas=[{"chunk_text_hash": chash_other, "title": "other.txt:1-1"}],
    )

    tumbler_live = str(cat.register(
        owner, f"{coll}-live-doc", content_type="knowledge",
        physical_collection=coll, file_path=f"/tmp/{coll}/live.txt",
    ))
    cat.write_manifest(tumbler_live, [{"chash": chash_live, "position": 0}], collection=coll)

    tumbler_tomb = str(cat.register(
        owner, f"{coll}-tomb-doc", content_type="knowledge",
        physical_collection=coll, file_path=f"/tmp/{coll}/tomb.txt",
    ))
    cat.write_manifest(tumbler_tomb, [{"chash": chash_tomb, "position": 0}], collection=coll)
    cat.delete_document(tumbler_tomb)

    tumbler_other = str(cat.register(
        owner, f"{coll}-other-doc", content_type="knowledge",
        physical_collection=coll2, file_path=f"/tmp/{coll2}/other.txt",
    ))
    cat.write_manifest(tumbler_other, [{"chash": chash_other, "position": 0}], collection=coll2)

    # chash_none: physically stored, manifested nowhere.

    return {
        "live": chash_live, "tombstoned-owner": chash_tomb,
        "other-collection-only": chash_other, "no-manifest": chash_none,
    }


def _run_census(state: dict, tenant: str, sql_path: Path = _SQL_PATH) -> list[tuple[str, str | None, str, int]]:
    """Run *sql_path* via ``psql -f``, as ``nexus_svc``, scoped to *tenant*.

    Returns ``(row_kind, collection, cause, chunk_count)`` tuples. The
    ``SELECT set_config(...)`` preamble's own one-field output line, and
    every non-SELECT command's status line (``BEGIN``, ``SET``, ``COMMIT``),
    are filtered out by field count -- neither carries three commas.
    """
    psql = Path(state["pg_bin"]) / "psql"
    proc = subprocess.run(
        [
            str(psql),
            "-h", "127.0.0.1",
            "-p", str(state["pg_port"]),
            "-U", "nexus_svc",
            "-d", state["pg_dbname"],
            "-v", f"tenant={tenant}",
            "-A", "-F,", "-t",
            "-f", str(sql_path),
        ],
        capture_output=True, text=True, timeout=130,
    )
    assert proc.returncode == 0, (
        f"livec_census.sql failed ({proc.returncode}) for tenant {tenant!r} "
        f"via {sql_path}:\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    rows: list[tuple[str, str | None, str, int]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) != 4:
            continue
        row_kind, collection, cause, chunk_count = parts
        rows.append((row_kind, collection or None, cause, int(chunk_count)))
    return rows


@pytest.fixture(scope="module")
def substrate_state() -> dict:
    return ensure_engine()


def test_livec_census_sql_file_exists() -> None:
    assert _SQL_PATH.is_file(), f"census script missing: {_SQL_PATH}"


@pytest.mark.integration
def test_livec_census_classifies_each_cause_and_scopes_by_tenant(
    t2_service_env: str, substrate_state: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_a = t2_service_env
    coll_a, coll_a2 = _coll("a"), _coll("a2")
    _seed_one_chunk_per_cause(coll_a, coll_a2)

    # A second, independently-minted tenant with its own, differently-named
    # collection -- the RLS-scoping half of this test.
    tenant_b, token_b = mint_test_tenant(substrate_state)
    monkeypatch.setenv("NX_SERVICE_TOKEN", token_b)
    coll_b, coll_b2 = _coll("b"), _coll("b2")
    _seed_one_chunk_per_cause(coll_b, coll_b2)

    rows_a = _run_census(substrate_state, tenant_a)
    by_key_a = {(r[1], r[2]): r[3] for r in rows_a if r[0] == "collection"}

    assert by_key_a[(coll_a, "live")] == 1
    assert by_key_a[(coll_a, "tombstoned-owner")] == 1
    assert by_key_a[(coll_a, "other-collection-only")] == 1
    assert by_key_a[(coll_a, "no-manifest")] == 1
    # coll_a2 only ever holds the FK-satisfying copy, which IS live there.
    assert by_key_a[(coll_a2, "live")] == 1
    assert by_key_a[(coll_a2, "tombstoned-owner")] == 0
    assert by_key_a[(coll_a2, "other-collection-only")] == 0
    assert by_key_a[(coll_a2, "no-manifest")] == 0

    # Tenant scoping: tenant B's collections never appear under tenant A's GUC.
    collections_seen_a = {r[1] for r in rows_a if r[0] == "collection"}
    assert coll_b not in collections_seen_a
    assert coll_b2 not in collections_seen_a

    # Totals reconcile: each cause's 'total' row is the sum of every
    # per-collection row for that cause (the grid's whole point).
    totals_a = {r[2]: r[3] for r in rows_a if r[0] == "total"}
    for cause in ("live", "tombstoned-owner", "other-collection-only", "no-manifest"):
        expected = sum(v for (_, c), v in by_key_a.items() if c == cause)
        assert totals_a[cause] == expected, f"total for {cause!r} does not reconcile against its own collections"
    assert totals_a["live"] == 2  # coll_a's 1 + coll_a2's 1
    assert totals_a["tombstoned-owner"] == 1
    assert totals_a["other-collection-only"] == 1
    assert totals_a["no-manifest"] == 1

    # Reverse direction: tenant A's collections are invisible under tenant B's GUC.
    rows_b = _run_census(substrate_state, tenant_b)
    by_key_b = {(r[1], r[2]): r[3] for r in rows_b if r[0] == "collection"}
    collections_seen_b = {r[1] for r in rows_b if r[0] == "collection"}
    assert coll_a not in collections_seen_b
    assert coll_a2 not in collections_seen_b
    assert by_key_b[(coll_b, "live")] == 1
    assert by_key_b[(coll_b, "tombstoned-owner")] == 1
    assert by_key_b[(coll_b, "other-collection-only")] == 1
    assert by_key_b[(coll_b, "no-manifest")] == 1


@pytest.mark.integration
def test_livec_census_catches_a_missing_tombstone_join(
    t2_service_env: str, substrate_state: dict, tmp_path: Path,
) -> None:
    """Mutation proof: dropping ``AND d.deleted_at IS NULL`` from the
    'live' CASE branch must misclassify the tombstoned-owner chunk as
    live -- i.e. the assertions in the test above would fail against this
    mutant. Demonstrated directly (not asserted-and-hoped): the mutant's
    own census output is read back and shown to disagree with the correct
    file's classification of the SAME seeded data.
    """
    tenant = t2_service_env
    coll, coll2 = _coll("mut"), _coll("mut2")
    _seed_one_chunk_per_cause(coll, coll2)

    original = _SQL_PATH.read_text()
    needle = (
        "                        AND d.deleted_at IS NULL\n"
        "                 ) THEN 'live'"
    )
    replacement = "                 ) THEN 'live'"
    assert needle in original, (
        "mutation target text not found -- livec_census.sql's 'live' CASE "
        "branch shape changed; update this test's needle to match"
    )
    mutant_path = tmp_path / "livec_census_mutant.sql"
    mutant_path.write_text(original.replace(needle, replacement))

    rows = _run_census(substrate_state, tenant, mutant_path)
    by_key = {(r[1], r[2]): r[3] for r in rows if r[0] == "collection"}

    assert by_key[(coll, "live")] == 2, (
        "mutant expected to inflate 'live' by the tombstoned-owner chunk "
        "once the deleted_at IS NULL join guard is removed"
    )
    assert by_key[(coll, "tombstoned-owner")] == 0, (
        "mutant expected to empty 'tombstoned-owner' -- its one chunk was "
        "misclassified as 'live'"
    )
