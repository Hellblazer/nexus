# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection-rename data-plane operation.

nexus-8g79.10 (V5): hosted at the package root (peer to ``indexer``,
``health``, ``pdf_extractor``) so library-layer callers — the indexer's
RDR-103 Phase 5 conformant-shape migrator at ``indexer.py:435`` — can
invoke without reaching up into ``nexus.commands.collection``.

The function raises ``ClickException`` at T2-cascade-failure as part
of its documented contract (CG-1, nexus-nhyh). That keeps the CLI
surface usable; non-CLI callers can catch ``click.ClickException``
or any other exception type as they prefer.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click


def rename_collection_data_plane(
    old: str,
    new: str,
    *,
    t3_db: Any,
    catalog: Any | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Rename a collection across T2 + T3 + catalog cascades.

    Ordering (SIG-8 / nexus-nhyh): T2 cascade runs FIRST, T3 rename
    runs LAST. Rationale: T2 SQL UPDATEs are reversible (operator can
    run the inverse UPDATE or re-run rename); ChromaDB collection rename
    is irrevocable in a single call. Running T2 first minimises
    irrecoverable drift: if T3 fails after T2 succeeds the operator can
    reverse T2 manually; if T2 fails no T3 rename was attempted so the
    system is still consistent.

    T2 cascade failure (nexus-nhyh / CG-1): raises ``ClickException``
    with a non-zero exit code and an actionable error message. T2
    cascade failures are NOT fail-open — an incomplete T2 cascade
    leaves collection references orphaned across six indexed columns.
    The operator must see the failure.

    Catalog cascade failure: fail-open (warn + continue). The catalog is
    a derived view; a failed catalog cascade can be repaired via
    ``nx catalog rebuild`` without touching T3 or T2.

    Returns a dict with counts per cascade table so callers can render
    their own summaries.

    ``on_warn`` is invoked with a string message when the catalog
    cascade fails open. The CLI default routes it to
    ``click.echo(..., err=True)``; callers in non-CLI contexts can
    pass their own.

    nexus-8g79.10 (V5): ``t3_db`` is now required (no ``_t3()`` default).
    CLI callers pass ``_t3()`` explicitly; library callers (the
    indexer's RDR-103 P5 migrator) construct their own T3 handle.
    """
    if on_warn is None:
        on_warn = lambda msg: click.echo(msg, err=True)  # noqa: E731

    # Tombstone caveat (RDR-156 P3, Decision 6): on the service path
    # collection_exists() reads the tombstone-filtered stats view, so a
    # collection whose every chunk belongs to trashed documents reads as
    # ABSENT here — "not found" may mean "trashed; restore it first".
    # Distinguishing the two needs a raw existence probe (bead nexus-9n485;
    # materializes only once trash verbs ship).
    if not t3_db.collection_exists(old):
        raise click.ClickException(f"collection not found: {old!r}")
    if t3_db.collection_exists(new):
        raise click.ClickException(f"collection already exists: {new!r}")

    counts = {
        "tax_topics": 0,
        "tax_assignments": 0,
        "tax_meta": 0,
        "tax_centroids": 0,
        "chash": 0,
        "aspects": 0,
        "aspect_queue": 0,
        "highlights": 0,
        "relevance_log": 0,
        "search_telemetry": 0,
        "hook_failures": 0,
        "catalog_docs": 0,
    }

    # ── Service mode: ONE atomic server-side re-home (RDR-164 P3) ─────────────
    # In service mode the entire in-Postgres cascade — T3 pgvector chunks, chash
    # index, taxonomy topics/assignments/meta/centroids, the aspect family,
    # telemetry, AND the catalog documents + registry row — is a single
    # transactional CatalogRepository.renameCollection on the Java service. Fold
    # it into one call instead of the SQLite-era fan-out (T2 cascade + separate
    # T3 Chroma rename + catalog cascade). The atomic txn means a failure leaves
    # the collection fully unchanged, so it raises (not fail-open) just like the
    # T2-cascade-failure contract below. No separate t3_db.rename_collection:
    # the pgvector chunks were re-homed inside the same transaction.
    from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — circular-dep avoidance (nexus.db.storage_mode)

    if storage_backend_for("catalog") == StorageBackend.SERVICE:
        client = catalog
        if client is None or not hasattr(client, "rename_collection_cascade"):
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.factory)
            client = make_catalog_reader()
        if client is None:  # service mode always returns a client; guard for a clear error
            raise click.ClickException("catalog service client unavailable")
        try:
            renamed = client.rename_collection_cascade(old, new)
        except Exception as exc:
            raise click.ClickException(
                f"service rename failed -- collection {old!r} is unchanged "
                f"(the re-home is atomic). Fix the error and retry:\n  {exc}"
            ) from exc
        counts["tax_topics"] = renamed.get("topics", 0)
        counts["tax_assignments"] = renamed.get("topic_assignments", 0)
        counts["tax_meta"] = renamed.get("taxonomy_meta", 0)
        counts["tax_centroids"] = (
            renamed.get("taxonomy_centroids_384", 0)
            + renamed.get("taxonomy_centroids_768", 0)
            + renamed.get("taxonomy_centroids_1024", 0)
        )
        counts["chash"] = renamed.get("chash_index", 0)
        counts["aspects"] = renamed.get("document_aspects", 0)
        counts["aspect_queue"] = renamed.get("aspect_extraction_queue", 0)
        counts["highlights"] = renamed.get("document_highlights", 0)
        counts["relevance_log"] = renamed.get("relevance_log", 0)
        counts["search_telemetry"] = renamed.get("search_telemetry", 0)
        counts["hook_failures"] = renamed.get("hook_failures", 0)
        counts["catalog_docs"] = renamed.get("catalog_documents", 0)
        return counts

    # ── Local (sqlite/Chroma) mode: client-side fan-out ──────────────────────
    # ── T2 cascade FIRST (reversible) ────────────────────────────────────────
    # Failure here raises ClickException -- T3 rename has not yet run so the
    # system remains fully consistent. Operator can diagnose and retry.
    # RDR-128 P3 (nexus-sbxbe.3): route the multi-store cascade through the
    # daemon so this command does not open memory.db directly. The op is on
    # the daemon's database-pseudo-store allowlist and its dict return
    # round-trips framed JSON; T2Client's facade passthrough makes the
    # write_fn body work whether routed or degraded to a direct T2Database.
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)

    try:
        cascade = t2_index_write(
            lambda t2db: t2db.rename_collection_cascade(old=old, new=new)
        )
        counts["tax_topics"] = cascade.get("tax_topics", 0)
        counts["tax_assignments"] = cascade.get("tax_assignments", 0)
        counts["tax_meta"] = cascade.get("tax_meta", 0)
        counts["chash"] = cascade.get("chash", 0)
        counts["aspects"] = cascade.get("aspects", 0)
        counts["aspect_queue"] = cascade.get("aspect_queue", 0)
        counts["highlights"] = cascade.get("highlights", 0)
        counts["search_telemetry"] = cascade.get("search_telemetry", 0)
        counts["hook_failures"] = cascade.get("hook_failures", 0)
        # tax_centroids stays 0 in local mode: the sqlite taxonomy cascade
        # re-homes centroids internally without a separate per-table count.
        # Service mode surfaces them from the server response above.
    except Exception as exc:
        raise click.ClickException(
            f"T2 cascade failed before T3 rename -- collection {old!r} is "
            f"unchanged. Fix the error and retry:\n  {exc}"
        ) from exc

    # ── T3 rename LAST (irrevocable) ─────────────────────────────────────────
    # T2 is already committed. If T3 fails here the operator can reverse T2
    # by running the inverse rename (``nx collection rename new old``).
    t3_db.rename_collection(old, new)

    # ── Catalog cascade (fail-open) ───────────────────────────────────────────
    try:
        if catalog is None:
            # RDR-146 P1.2: rename_collection is a write; route through the
            # write-only daemon proxy. A caller-supplied ``catalog`` is used
            # as-is (it is expected to be write-capable).
            from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.factory)
            catalog = make_catalog_writer()
        counts["catalog_docs"] = catalog.rename_collection(old, new)
    except Exception as exc:  # noqa: BLE001 — catalog cascade is best-effort after T2+T3 succeeded; surfaced via on_warn
        on_warn(f"warn: T2+T3 rename succeeded but catalog cascade failed: {exc}")

    return counts


def remap_collection_references(
    source: str,
    target: str,
    *,
    catalog: Any | None = None,
    on_warn: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Re-point every T2 + catalog collection reference ``source -> target``.

    RDR-162 P2 cross-model migrate: after the stored-text re-embed has copied a
    legacy collection's chunks into a model-remapped TARGET (e.g. a minilm-384
    source re-embedded into its ``bge-base-en-v15-768`` target via
    :func:`nexus.migration.vector_etl.cross_model_target_name`) AND the target is
    verified-populated, the catalog/topic ``source_collection`` /
    ``physical_collection`` references still name the dead source. This re-points
    them to the live target.

    Distinct from :func:`rename_collection_data_plane`: that is a MOVE (it also
    renames the T3 collection and refuses a pre-existing target). The cross-model
    migrate is COPY-not-move (RDR-155 RF-5): the source Chroma collection is
    never mutated (so a failed migrate is re-runnable from the untouched source)
    and the TARGET legitimately already exists (the ETL just populated it). So
    this runs ONLY the two reference cascades — the T2 atomic cascade and the
    fail-open catalog cascade — and never touches T3, and never guards on target
    existence.

    Ordering invariant (the CALLER must honour): this is the one mutation of the
    cross-model migrate; invoke it only AFTER the target leg verifies populated
    (mirror RDR-144 reindex-first / delete-after-verify), so a mid-migrate
    failure never leaves dangling references.

    T2 cascade failure raises ``ClickException`` (orphaned references are not
    fail-open). Catalog cascade failure warns and continues (the catalog is a
    derived view, repairable via ``nx catalog rebuild``). Returns a per-table
    counts dict.
    """
    if on_warn is None:
        on_warn = lambda msg: click.echo(msg, err=True)  # noqa: E731

    counts = {
        "tax_topics": 0,
        "tax_assignments": 0,
        "tax_meta": 0,
        "chash": 0,
        "aspects": 0,
        "aspect_queue": 0,
        "highlights": 0,
        "search_telemetry": 0,
        "hook_failures": 0,
        "catalog_docs": 0,
    }

    # ── T2 reference cascade (raises on failure) ─────────────────────────────
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — circular-dep avoidance (nexus.mcp_infra)

    try:
        cascade = t2_index_write(
            lambda t2db: t2db.rename_collection_cascade(old=source, new=target)
        )
        for key in (
            "tax_topics", "tax_assignments", "tax_meta", "chash",
            "aspects", "aspect_queue", "highlights", "search_telemetry",
            "hook_failures",
        ):
            counts[key] = cascade.get(key, 0)
    except Exception as exc:
        raise click.ClickException(
            f"T2 reference cascade failed remapping {source!r} -> {target!r}; "
            f"references are unchanged. Fix the error and retry:\n  {exc}"
        ) from exc

    # ── Catalog reference cascade (fail-open) ────────────────────────────────
    try:
        if catalog is None:
            from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — circular-dep avoidance (nexus.catalog.factory)
            catalog = make_catalog_writer()
        # nexus-gaou3: this IS the legitimate cross-model repoint (target already
        # populated by the ETL). In service mode the Java endpoint 409s a rename onto
        # an existing target UNLESS cross_model=True, so signal it. Only pass the kwarg
        # in service mode — the local catalog writer's rename_collection has no such
        # parameter (and local mode has no 409 to bypass).
        from nexus.db.storage_mode import StorageBackend, storage_backend_for  # noqa: PLC0415 — circular-dep avoidance (nexus.db.storage_mode)

        if storage_backend_for("catalog") == StorageBackend.SERVICE:
            counts["catalog_docs"] = catalog.rename_collection(
                source, target, cross_model=True
            )
        else:
            counts["catalog_docs"] = catalog.rename_collection(source, target)
    except Exception as exc:  # noqa: BLE001 — catalog cascade is best-effort after T2 remap; surfaced via on_warn
        on_warn(
            f"warn: T2 reference remap succeeded but catalog cascade failed: {exc}"
        )

    return counts
