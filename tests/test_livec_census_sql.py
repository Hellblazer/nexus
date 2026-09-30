# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/sql/livec_census.sql`` (RDR-192, bead nexus-wbfpw.32): the
v0.1.136 deploy gate for live(c). Runs the standalone census file, via
psql, exactly as conexus will run it against production -- as ``nexus_svc``
(NOSUPERUSER NOBYPASSRLS), with FORCE RLS live, the tenant set only through
``set_config('nexus.tenant', ...)`` inside the file's own transaction.

Real engine substrate (``t2_service_env``; see ``tests/_engine_substrate.py``
and ``tests/test_wbfpw31_nxexp_owner.py``): catalog rows are created through
the real catalog writer (``register``/``write_manifest``/``delete_document``).
The chunks are INSERTed with substrate SQL (``tests/_chunk_seed.py``): the
engine refuses an ownerless ``/v1/vectors/upsert-chunks`` write from RDR-223
Phase 3 on, and the ``no-manifest`` / ``other-collection-only`` shapes this
census exists to classify are ownerless chunks by definition.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

import nexus.db.http_vector_client as hvc
from nexus.catalog.chunk_quarantine import now_stamp, quarantine_collection_name
from tests._catalog_fixture_ops import ActiveCatalog
from tests._chunk_seed import seed_chunks_direct
from tests._engine_substrate import ensure_engine, mint_test_tenant

# Not integration-marked (nexus-wbfpw.38): the substrate provisions itself,
# and CI's default selection must run this RDR-192 pin.

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
    owner = cat.register_owner(f"{coll}-owner", "curator")

    chash_live = _chash(f"{coll}:live")
    chash_tomb = _chash(f"{coll}:tomb")
    chash_other = _chash(f"{coll}:other")
    chash_none = _chash(f"{coll}:none")

    seed_chunks_direct(
        coll,
        ids=[chash_live, chash_tomb, chash_other, chash_none],
        documents=[
            "livec census live chunk", "livec census tombstoned-owner chunk",
            "livec census other-collection-only chunk", "livec census no-manifest chunk",
        ],
        metadatas=[
            {"chunk_text_hash": chash_live, "title": "live.txt:1-1"},
            {"chunk_text_hash": chash_tomb, "title": "tomb.txt:1-1"},
            {"chunk_text_hash": chash_other, "title": "other.txt:1-1"},
            {"chunk_text_hash": chash_none, "title": "none.txt:1-1"},
        ],
    )
    # FK-satisfying copy: same chash, physically stored under coll2 too.
    seed_chunks_direct(
        coll2,
        ids=[chash_other],
        documents=["livec census other-collection-only chunk"],
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


def _psql(state: dict, tenant: str | None, *, sql_path: Path | None = None, command: str | None = None):
    """Run psql as ``nexus_svc`` (NOSUPERUSER NOBYPASSRLS) with ON_ERROR_STOP,
    either a file (*sql_path*) or a single *command*."""
    args = [
        str(Path(state["pg_bin"]) / "psql"),
        "-h", "127.0.0.1", "-p", str(state["pg_port"]),
        "-U", "nexus_svc", "-d", state["pg_dbname"],
        "-v", "ON_ERROR_STOP=1", "-A", "-F,", "-t",
    ]
    if tenant is not None:
        args += ["-v", f"tenant={tenant}"]
    args += ["-f", str(sql_path)] if sql_path is not None else ["-c", command or ""]
    return subprocess.run(args, capture_output=True, text=True, timeout=130)


def _run_census(state: dict, tenant: str, sql_path: Path = _SQL_PATH) -> list[tuple[str, str | None, str, int]]:
    """Run *sql_path* via ``psql -f``, as ``nexus_svc``, scoped to *tenant*.

    Returns ``(row_kind, collection, cause, chunk_count)`` tuples. The
    ``SELECT set_config(...)`` preamble's own one-field output line, and
    every non-SELECT command's status line (``BEGIN``, ``SET``, ``COMMIT``),
    are filtered out by field count -- neither carries three commas.
    """
    proc = _psql(state, tenant, sql_path=sql_path)
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
    for cause in ("live", "quarantine", "tombstoned-owner", "other-collection-only", "no-manifest"):
        expected = sum(v for (_, c), v in by_key_a.items() if c == cause)
        assert totals_a[cause] == expected, f"total for {cause!r} does not reconcile against its own collections"
    assert totals_a["live"] == 2  # coll_a's 1 + coll_a2's 1
    assert totals_a["tombstoned-owner"] == 1
    assert totals_a["other-collection-only"] == 1
    assert totals_a["no-manifest"] == 1
    assert totals_a["quarantine"] == 0

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


def test_livec_census_reports_quarantine_siblings_as_their_own_cause(
    t2_service_env: str, substrate_state: dict,
) -> None:
    """A chunk GC moved into a quarantine-* sibling keeps whatever manifest
    rows it had under the ORIGIN collection name, so the own-collection
    probes would misread it as other-collection-only or no-manifest, a
    false regression under the deploy condition. It must read 'quarantine'.
    Seeded through the real GC route, not a hand-named collection."""
    coll = _coll("quar")
    cat = ActiveCatalog()
    db = hvc.HttpVectorClient(tenant="unused-client-side-tag")  # gc route only
    owner = cat.register_owner(f"{coll}-owner", "curator")
    live, orphan = _chash(f"{coll}:live"), _chash(f"{coll}:orphan")
    seed_chunks_direct(
        coll, ids=[live, orphan],
        documents=["livec census quarantine live chunk", "livec census quarantine orphan chunk"],
        metadatas=[
            {"chunk_text_hash": live, "title": "qlive.txt:1-1"},
            {"chunk_text_hash": orphan, "title": "qorphan.txt:1-1"},
        ],
    )
    doc = str(cat.register(
        owner, f"{coll}-doc", content_type="knowledge",
        physical_collection=coll, file_path=f"/tmp/{coll}/qlive.txt",
    ))
    cat.write_manifest(doc, [{"chash": live, "position": 0}], collection=coll)

    sibling = quarantine_collection_name(coll)
    moved = db.gc_quarantine_orphans(coll, sibling, now_stamp(), 20)
    assert int(moved.get("moved", 0)) == 1, moved

    by_key = {(r[1], r[2]): r[3] for r in _run_census(substrate_state, t2_service_env) if r[0] == "collection"}
    assert by_key[(sibling, "quarantine")] == 1
    assert by_key[(sibling, "no-manifest")] == 0
    assert by_key[(sibling, "other-collection-only")] == 0
    assert by_key[(coll, "live")] == 1
    assert by_key[(coll, "no-manifest")] == 0


def test_livec_census_refuses_an_unset_or_empty_tenant(
    t2_service_env: str, substrate_state: dict,
) -> None:
    """For this gate an all-zero grid reads as a pass, so a missing or
    misspelled tenant must fail loud instead."""
    unset = _psql(substrate_state, "", sql_path=_SQL_PATH)
    assert unset.returncode != 0
    assert "nexus.tenant is unset" in unset.stderr

    wrong = _psql(substrate_state, "wbfpw32-no-such-tenant", sql_path=_SQL_PATH)
    assert wrong.returncode != 0
    assert "holds no chunks" in wrong.stderr


def test_livec_census_manifest_probe_is_an_index_condition_under_rls(
    t2_service_env: str, substrate_state: dict,
) -> None:
    """The file writes no tenant_id predicate and relies on the RLS policy.
    The chash equality reaches idx_catalog_chunks_chash as an Index Cond
    only because byteaeq is leakproof (texteq likewise for collection); if
    either flag flips, the equality becomes a post-scan filter and ~317k
    probes blow the 120 s timeout. Pin both flags, and pin that the chash
    equality CAN be an Index Cond for nexus_svc. Which index the planner
    then picks is a costing choice that only production statistics settle;
    the 2026-09-27 manifest-less census ran the same join shape on
    production at p50 23 ms and at most 4.4 s for 47,778 chunks."""
    flags = _psql(
        substrate_state, None,
        command="SELECT proname, proleakproof FROM pg_proc "
                "WHERE proname IN ('byteaeq', 'texteq') ORDER BY proname",
    )
    assert flags.returncode == 0, flags.stderr
    assert flags.stdout.split() == ["byteaeq,t", "texteq,t"], flags.stdout

    # On a near-empty table the planner may cost any tenant_id-leading index
    # the same as (tenant, chash) and pick it, which says nothing about
    # pushdown. Dropping only idx_catalog_chunks_collection was not enough:
    # CI then picked idx_catalog_chunks_doc_id with chash as a Filter
    # (nexus-wbfpw.38, the first time a routine gate ran this test). So, as
    # the substrate superuser, drop EVERY other index and the primary key
    # inside a transaction that is rolled back, switch to nexus_svc, and
    # require the chash equality in the Index Cond of the one index left.
    # If byteaeq stops being leakproof the chash test becomes a Filter and
    # the cond assertion below still fails.
    q = (
        "BEGIN; DO $$ DECLARE r record; BEGIN "
        "FOR r IN SELECT conname FROM pg_constraint "
        "WHERE conrelid = 'nexus.catalog_document_chunks'::regclass "
        "AND contype IN ('p', 'u') LOOP "
        "EXECUTE format('ALTER TABLE nexus.catalog_document_chunks "
        "DROP CONSTRAINT %I CASCADE', r.conname); END LOOP; "
        "FOR r IN SELECT i.relname FROM pg_index x "
        "JOIN pg_class i ON i.oid = x.indexrelid "
        "WHERE x.indrelid = 'nexus.catalog_document_chunks'::regclass "
        "AND i.relname <> 'idx_catalog_chunks_chash' LOOP "
        "EXECUTE format('DROP INDEX nexus.%I', r.relname); END LOOP; END $$; "
        "SET LOCAL ROLE nexus_svc; SET LOCAL enable_seqscan = off; "
        f"SELECT set_config('nexus.tenant', '{t2_service_env}', true); "
        "EXPLAIN SELECT 1 FROM nexus.catalog_document_chunks m "
        "WHERE m.chash = '\\x00'::bytea; ROLLBACK;"
    )
    plan = subprocess.run(
        [
            str(Path(substrate_state["pg_bin"]) / "psql"),
            "-h", "127.0.0.1", "-p", str(substrate_state["pg_port"]),
            "-U", substrate_state["pg_user"], "-d", substrate_state["pg_dbname"],
            "-v", "ON_ERROR_STOP=1", "-A", "-t", "-c", q,
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert plan.returncode == 0, plan.stderr
    text = plan.stdout
    assert "idx_catalog_chunks_chash" in text, text
    cond = [line for line in text.splitlines() if "Index Cond" in line]
    assert cond and "chash" in cond[0] and "tenant_id" in cond[0], text
