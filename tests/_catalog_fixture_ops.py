# SPDX-License-Identifier: AGPL-3.0-or-later
"""Substrate-agnostic catalog fixture access for the unit suite (nexus-aqbrk).

RDR-158/155 substrate port. A large family of catalog tests seeds state with
``Catalog.init(dir)`` and then drives a CLI verb or hook that reads the
catalog through ``make_catalog_reader`` / ``make_catalog_writer``. Under the
engine substrate those two disagree: the test writes the LOCAL ``.catalog.db``
while the code under test reads the SERVICE catalog, so every count comes back
zero and every field empty — the bucket-2 "opaque assert" profile
(``assert 0 == 2024``, ``assert '' == 'Vaswani et al.'``).

:class:`ActiveCatalog` closes that gap by routing the test's own seeding
through the SAME factories the code under test uses. The test then exercises
whichever catalog is real, which is strictly more coverage than the local-only
form it replaces.

HISTORY. The local-only verb family this module used to carve out (``nx
catalog synthesize-log``, the doctor replay/consistency verbs, their
``local_catalog_backend`` pin) died with the local SQLite catalog in the
nexus-i711w terminal deletion; the fixture was removed in the Stage 5
sweep. Everything routes through the factories now.
"""
from __future__ import annotations

from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS

__all__ = [
    "ActiveCatalog",
    "active_reader",
    "bypass_fk_seed_chunk",
    "count_documents",
    "documents_by_file_path",
    "documents_by_title",
    "fk_dropped_for_dangling_seed",
    "only_document",
    "seed_manifest_chunks",
    "unroutable_write_target",
]


def active_reader() -> Any:
    """The catalog reader the CLI itself would use, resolved fresh.

    Deliberately not memoised: a cached reader can be a snapshot taken before
    the test's writes (the SQLite reader opens ``mode=ro`` at construction),
    so every read resolves a current one.
    """
    from nexus.commands import catalog as _cat_cmd

    return _cat_cmd._get_catalog()


def count_documents(collection: str | None = None) -> int:
    """Document count via the active reader.

    Replaces ``cat._db.execute("SELECT count(*) FROM documents")``, which has
    no service-mode equivalent.
    """
    reader = active_reader()
    if collection is not None:
        return len(reader.list_by_collection(collection))
    return sum(1 for _ in reader.all_documents())


def documents_by_file_path(file_path: str) -> list[Any]:
    """Every document whose ``file_path`` equals *file_path*, in catalog order.

    Replaces ``cat._db.execute("SELECT <col> FROM documents WHERE file_path =
    ?").fetchall()``. ``find_by_file_path`` is the nearest single-shot public
    equivalent, but it is ``LIMIT 1`` on BOTH substrates
    (``catalog.py::find_by_file_path``, ``HttpCatalogClient.find_by_file_path``),
    so it cannot express the "exactly one row" cardinality that the
    double-registration guards assert — the whole point of those tests is that
    a second row would mean pre-flight and the post-hook both registered. A
    scan states it.

    Scanning is affordable here because the catalogs these run against hold a
    handful of rows: the SQLite arm gets a per-test tmp catalog, and the engine
    arm gets a per-test tenant (``tests/conftest.py::t2_service_env`` mints one
    tenant-bound token per test).
    """
    return [d for d in active_reader().all_documents() if d.file_path == file_path]


def documents_by_title(title: str) -> list[Any]:
    """Every document whose ``title`` equals *title* EXACTLY, via the reader.

    Replaces ``cat._db.execute("SELECT ... FROM documents WHERE title = ?")``.

    Deliberately NOT ``find(title)``: ``find`` is a SUBSTRING search on both
    substrates with no ordering contract, so a title that is a prefix of
    another matches both and the caller's ``[0]`` silently picks whichever
    the backend happened to return first. That exact trap cost a
    misdiagnosis in tests/test_enrich_command.py, where "Enriched Paper"
    also matched "Non-Enriched Paper" and the engine arm reversed the two
    after an update. Exact comparison over ``all_documents()`` has neither
    failure mode.

    nexus-fgxmk later promoted the FTS-then-exact-filter idiom into
    ``HttpCatalogClient.find_by_title_exact``; this scan stays for callers
    that must not depend on the ``/search`` result cap.
    """
    return [d for d in active_reader().all_documents() if d.title == title]


def only_document() -> Any:
    """The single document in the catalog, asserting there is exactly one.

    Replaces ``cat._db.execute("SELECT <col> FROM documents").fetchone()`` in
    tests that register one entry and read a column back. ``fetchone()``
    silently took the first of N; this states the "exactly one" the tests
    already meant.
    """
    docs = list(active_reader().all_documents())
    assert len(docs) == 1, f"expected exactly one document, got {len(docs)}"
    return docs[0]


def seed_manifest_chunks(collection: str, chashes: Iterable[str]) -> None:
    """Write a stub ``nexus.chunks`` row for each of *chashes* in *collection*.

    RDR-191 Phase 5 (bead nexus-o8dil.29 / nexus-dbzxb): ``fk_catalog_chunks_chunk``
    (catalog-029) now rejects a manifest row naming a ``(tenant, collection, chash)``
    tuple with no matching ``nexus.chunks`` row. A large family of fixtures across
    this suite historically wrote manifest rows with synthetic bookkeeping chashes
    that were never real T3 chunks. This seeds a minimal matching chunk per chash —
    idiom 1 of the collateral-damage sweep (reorder: write the chunk before the
    manifest), mirroring the Java-side sweep's ``vecRepo.upsertChunks(...)`` calls
    ahead of ``catalogRepo.writeManifest(...)`` (see ``ManifestChunkFkTest.java``) —
    so ``write_manifest`` / ``append_manifest_chunks`` / ``atomic_manifest_replace``
    succeed without weakening what the test actually asserts. Chunk TEXT content is
    irrelevant to the FK (it only keys on chash); a deterministic stub is used.

    Idempotent — the server's ``upsert_chunks`` is an UPSERT — so callers may
    seed the same chash more than once (e.g. re-writing a manifest for an
    idempotency test) without consequence.

    Uses the same-model PASSTHROUGH form (explicit zero ``embeddings``,
    ``nexus.db.http_vector_client.HttpVectorClient.upsert_chunks``'s
    ``embeddings=`` kwarg) rather than letting the server embed — two
    reasons: (1) it works uniformly for BOTH locally-embeddable collection
    names (``bge-base-en-v15-768`` / ``minilm-l6-v2-384``) and cloud-model
    names (``voyage-*``), which the test substrate's ``onnx-local``
    embedding mode otherwise refuses outright (``HTTP 422: ... has no
    embedder for model 'voyage-context-3'``) — several fixtures (e.g.
    ``test_mcp_server.py``'s ``cloud_mode``-forced collection naming) need
    a real backing chunk in a voyage-named collection with no API key
    available; (2) it is a single deterministic zero-vector, no ONNX
    inference per stub. The embedding VALUES are never asserted on by any
    caller of this helper — only the chash's presence/absence matters to
    the FK.
    """
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.db.reconcile import dim_for_model_token

    ids = sorted({c for c in chashes if c})
    if not ids:
        return
    segments = collection.split("__")
    dim = dim_for_model_token(segments[2]) if len(segments) >= 3 else None
    if dim is None:
        dim = 768
    HttpVectorClient().upsert_chunks(
        collection, ids, [f"fk-stub chunk for {c}" for c in ids],
        embeddings=[[0.0] * dim for _ in ids],
    )


def _run_psql(sql: str) -> None:
    """Execute *sql* against the test substrate's own PG cluster as its
    superuser (trust auth — ``tests/_engine_substrate.py``'s ``_boot()``
    ``initdb``s with ``--auth=trust`` and no separate credential surface).
    Bypasses RLS (a real superuser connection, not merely FORCE-RLS-exempt).
    Shared by :func:`bypass_fk_seed_chunk` and
    :func:`fk_dropped_for_dangling_seed`.
    """
    import os
    import subprocess

    from tests._engine_substrate import _DBNAME, ensure_engine

    state = ensure_engine()
    proc = subprocess.run(
        [str(state["pg_bin"] / "psql"), "-h", "127.0.0.1", "-p", str(state["pg_port"]),
         "-U", os.environ["USER"], "-d", _DBNAME, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"_run_psql: psql failed: {proc.stderr}\nSQL: {sql}")


def bypass_fk_seed_chunk(
    tenant: str, collection: str, chash: str, *, dim: int = 768,
) -> None:
    """Insert a stub ``nexus.chunks`` row directly via ``psql`` — a
    deliberate FK-adjacent bypass for the ONE class of fixture that cannot
    reach ``fk_catalog_chunks_chunk`` satisfaction through any real write
    path (nexus-dbzxb, RDR-191 Phase 5 collateral sweep, idiom 3; Python
    equivalent of the Java sweep's DROP/INSERT/re-ADD CONSTRAINT local
    bypass — see ``ManifestChunkFkTest.java``).

    ``fk_catalog_chunks_chunk`` itself does not care about collection-name
    conformance, only chash PRESENCE — but every real ``/v1/vectors/*`` T3
    write endpoint (``upsert_chunks``, ``put``/``store-put``, WITH or
    WITHOUT the same-model passthrough) enforces RDR-103 four-segment
    naming server-side, unconditionally. A genuinely non-conformant LEGACY
    2-segment collection (the nexus-hmxi grandfathering scenario:
    ``store_put`` resolving into a pre-existing pre-Phase-5 collection
    name) can therefore never get a real backing chunk through ANY
    documented write path — this is the state idiom 1/2 cannot reach.

    Connects as the test substrate's own PG superuser (trust auth, no
    separate credential surface — see ``tests/_engine_substrate.py``'s
    ``_boot()``), which bypasses RLS, so no ``nexus.tenant`` GUC stamp is
    needed. ``ON CONFLICT DO NOTHING`` makes repeat seeding of the same
    chash idempotent, matching :func:`seed_manifest_chunks`'s contract.

    NOTE this INSERTs a real, present ``nexus.chunks`` row — appropriate
    when the test's OWN presence check reads through a separately mocked
    T3 client (e.g. ``test_catalog_cli.py::TestVerifyCommand``, where the
    "damaged" signal comes from a patched ``existing_ids``, never a real
    query against this table). For a test whose SUBJECT is the real
    ``nexus.chunks`` presence itself — a genuinely DANGLING manifest row,
    e.g. ``test_c2_manifest_null_collection_engine.py``'s ghost-document
    fixtures (a real INSERT here would flip "missing" to "present" and
    invert the fixture's own premise; RDR-191 Phase 6, nexus-o8dil.33,
    2026-08-15 retired the engine-side ``manifest_verify_all()``/
    ``manifest_orphans(dim)`` functions this docstring originally named as
    the motivating example, but the seeding idiom itself is unchanged and
    still exercised) — use :func:`fk_dropped_for_dangling_seed` instead.

    nexus-dbzxb round 2 (RDR-191 Phase 5, sibling bead nexus-o8dil.49):
    ``nexus.chunks`` now ALSO carries ``chunks_collection_fk`` (fk-004,
    landed AFTER this function's first version) — ``FOREIGN KEY
    (tenant_id, collection) REFERENCES nexus.catalog_collections (tenant_id,
    name)``. The real ``/v1/vectors/upsert-chunks`` write path used by
    :func:`seed_manifest_chunks` auto-registers the collection row
    server-side (production collection-creation is part of that endpoint's
    contract), so idiom-1/2 callers never see this — but a raw bypass
    INSERT must register the parent stub itself first, mirroring
    ``fk-004-1-reconcile``'s own additive ``INSERT ... ON CONFLICT DO
    NOTHING`` shape (the Java sweep's precedent for this exact FK
    direction). All other ``catalog_collections`` columns default (see
    ``catalog-001-baseline.xml`` changeset 7), so ``(tenant_id, name)`` is
    sufficient.
    """
    vec_col = {384: "embedding_384", 768: "embedding_768", 1024: "embedding_1024"}[dim]
    vec_literal = "[" + ",".join(["0"] * dim) + "]"
    sql = (
        "INSERT INTO nexus.catalog_collections (tenant_id, name) "
        f"VALUES ({tenant!r}, {collection!r}) "
        "ON CONFLICT (tenant_id, name) DO NOTHING;\n"
        "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, "
        f"{vec_col}) VALUES ({tenant!r}, {collection!r}, "
        f"decode({chash!r}, 'hex'), 'fk-bypass-stub', {vec_literal!r}::vector) "
        "ON CONFLICT (tenant_id, collection, chash) DO NOTHING;"
    )
    _run_psql(sql)


@contextmanager
def fk_dropped_for_dangling_seed():
    """Temporarily DROP ``fk_catalog_chunks_chunk`` so a manifest write
    inside the block may reference a chash with NO ``nexus.chunks`` row
    AT ALL — a genuinely, durably dangling row, indistinguishable from
    pre-catalog-029 production data — then restore the constraint
    (re-ADD ... NOT VALID) on exit, even on error.

    nexus-dbzxb (RDR-191 Phase 5 Python collateral sweep, idiom 3): Python
    equivalent of the Java sweep's local bypass (DROP CONSTRAINT IF EXISTS
    / insert / re-ADD NOT VALID — see the RDR-191 Phase 5 T2 implementation
    note). This is the ONLY idiom that produces a row that SURVIVES commit
    while dangling: neither a plain INSERT (the FK is validated, immediate,
    non-deferrable-away, on every write_manifest call) nor
    ``SET CONSTRAINTS ... DEFERRED`` (the two class-B production sites'
    trick — ``nexus.purge_trash`` / ``CatalogRepository.deleteCollectionTxn``)
    can produce one: DEFERRED only postpones the check to COMMIT, and a
    manifest row whose chunk was deleted in the SAME transaction still
    fails that deferred check unless the referencing manifest row is ALSO
    removed before commit — which is exactly what both class-B sites do
    (chunk-then-manifest, both gone by commit), never leaving a dangling
    survivor. A truly dangling row can only be inserted while the
    constraint does not exist at all.

    SAFE only because pytest-xdist workers run tests SEQUENTIALLY within
    one worker process against that worker's OWN engine substrate (each
    worker boots and owns its own PG+JAR — ``tests/_engine_substrate.py``'s
    ``ensure_engine()`` is process-memoized, not machine-shared): no other
    test on this worker can observe the transient constraint-less window.
    Re-ADD is NOT VALID (not re-validated), so the seeded dangling row is
    grandfathered in permanently, but every subsequent write inside this
    worker's session is enforced exactly as before — this is scoped to the
    handful of call sites whose actual subject is a dangling row, never
    applied to a shared helper used by correctly-matched fixtures.
    """
    _run_psql(
        "ALTER TABLE nexus.catalog_document_chunks "
        "DROP CONSTRAINT IF EXISTS fk_catalog_chunks_chunk;"
    )
    try:
        yield
    finally:
        _run_psql(
            "ALTER TABLE nexus.catalog_document_chunks "
            "ADD CONSTRAINT fk_catalog_chunks_chunk "
            "FOREIGN KEY (tenant_id, collection, chash) "
            "REFERENCES nexus.chunks (tenant_id, collection, chash) "
            "ON UPDATE CASCADE DEFERRABLE INITIALLY IMMEDIATE NOT VALID;"
        )


def unroutable_write_target() -> Any:
    """A target for a catalog WRITE that is not on ``CATALOG_WRITE_OPS``.

    ``set_alias`` is the known case (nexus-iltyk): it mutates, but it is not
    whitelisted, so ``CatalogWriter.__getattr__`` will not forward it on the
    SQLite arm — while in service mode ``_SharedServiceCatalogHandle`` proxies
    every attribute and therefore performs the write through what the factory
    calls a READER.

    So there is no single object that can do it on both substrates:
      - service: the shared handle, which proxies it (and is what production
        would end up using today, whether or not that is intended)
      - sqlite:  a directly-constructed WRITABLE ``Catalog``, since the
                 factory reader is ``mode=ro``

    This exists so a test can perform such a write on either substrate without
    the facade pretending the typed factories support it. It is deliberately
    ugly and deliberately named: if nexus-iltyk is resolved by whitelisting
    the op, every caller of this should collapse back to ``ActiveCatalog``.
    """
    # The SQLite arm died with the local catalog (nexus-i711w terminal
    # deletion) — the service reader is the only substrate left, so this
    # helper degenerates to active_reader() until nexus-iltyk whitelists
    # the op and callers collapse back to ActiveCatalog.
    return active_reader()


class ActiveCatalog:
    """Read/write facade over whichever catalog backend is live.

    Writes go through ``make_catalog_writer()`` (``CatalogWriter`` on SQLite,
    ``_ServiceCatalogWriter`` on the engine); everything else resolves against
    ``make_catalog_reader()``. The split is keyed on
    :data:`CATALOG_WRITE_OPS`, the same whitelist the daemon write-shim and
    the service writer both enforce, so an op that is not routable as a write
    fails here the way it would in production rather than silently reading.

    A writer is opened and closed per call. That is heavier than holding one,
    and it is what the CLI actually does — every ``nx catalog`` write command
    opens a writer for the duration of the command — so it also keeps the
    test honest about writer lifetime.
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(
                f"ActiveCatalog has no private attribute {name!r}. Raw handles "
                f"(_db, _conn) have no service-mode equivalent — read through "
                f"the public API (see tests._catalog_fixture_ops.count_documents)."
            )
        if name in CATALOG_WRITE_OPS:
            return _WriteOp(name)
        return getattr(active_reader(), name)


class _WriteOp:
    """One whitelisted write, applied through a freshly-opened writer."""

    def __init__(self, op: str) -> None:
        self._op = op

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        from nexus.catalog.factory import make_catalog_writer

        writer = make_catalog_writer()
        try:
            return getattr(writer, self._op)(*args, **kwargs)
        finally:
            writer.close()
