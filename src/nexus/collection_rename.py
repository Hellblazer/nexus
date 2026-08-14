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

from nexus.db.collection_state import CollectionState, probe_collection_state


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

    # Tombstone-vs-absent guard (RDR-156 P3, Decision 6; nexus-9n485). On the
    # service path collection_exists() reads the tombstone-filtered stats
    # view, so a collection whose every chunk belongs to a trashed document
    # reads exactly like one that never existed. probe_collection_state
    # additionally consults the RAW /v1/vectors/collections listing (never
    # tombstone-filtered — trashing sets catalog_documents.deleted_at, it
    # never touches the physical chunks_<dim> rows) to tell the two apart:
    #   - old ABSENT -> "not found" (accurate).
    #   - old TOMBSTONED -> "not found" would hide the real remedy
    #     ("restore the trashed documents first"); say so instead.
    #   - new PRESENT or TOMBSTONED -> refuse. This rename path has no
    #     cross_model escape valve (unlike remap_collection_references), so
    #     claiming a tombstoned name would silently land live/fresh data on
    #     top of dead rows sharing the same collection name.
    # No-op for any t3_db that is not literally an HttpVectorClient (T3Database
    # and the MagicMock fakes throughout the existing rename test suite) —
    # those backends have no tombstone concept and keep the prior two-state
    # collection_exists() behaviour unchanged.
    old_state = probe_collection_state(t3_db, old)
    if old_state is CollectionState.ABSENT:
        raise click.ClickException(f"collection not found: {old!r}")
    if old_state is CollectionState.TOMBSTONED:
        raise click.ClickException(
            f"collection {old!r} is tombstoned (every chunk belongs to a "
            f"trashed document) — restore the trashed document(s) before renaming."
        )
    new_state = probe_collection_state(t3_db, new)
    if new_state is not CollectionState.ABSENT:
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

    # ── ONE atomic server-side re-home (RDR-164 P3, unconditional since
    # nexus-i711w) ────────────────────────────────────────────────────────────
    # The entire in-Postgres cascade — T3 pgvector chunks, chash index,
    # taxonomy topics/assignments/meta/centroids, the aspect family,
    # telemetry, AND the catalog documents + registry row — is a single
    # transactional CatalogRepository.renameCollection on the Java service.
    # The atomic txn means a failure leaves the collection fully unchanged,
    # so it raises (not fail-open).
    #
    # nexus-i711w: the SQLite-era client-side fan-out (T2 cascade + separate
    # T3 rename + catalog cascade) that lived here as the else-arm died with
    # the local catalog. KNOWN KNOCK-ON (bead nexus-i711w comment 20): a
    # catalog=service + vectors=chroma split-mode rename lost its local leg
    # with this excision — that mode combination no longer exists post
    # RDR-155 P4b.
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
    # RDR-191 Phase 4 (nexus-o8dil.19/.47 "one era" ruling): the three
    # per-dim centroid tables unify into nexus.taxonomy_centroids, and the
    # engine's cascade response is expected to carry ONE "taxonomy_centroids"
    # count post-unification. Fall back to summing the three legacy per-dim
    # keys when the unified key is absent, so this client tolerates either
    # response shape (relevant only transiently, if this Python repoint and
    # the Java-side CatalogRepository repoint land in different commits of
    # the same batch); once the engine always emits the unified key the
    # fallback branch is simply never reached (unified key is never None).
    _unified_centroids = renamed.get("taxonomy_centroids")
    if _unified_centroids is None:
        _unified_centroids = (
            renamed.get("taxonomy_centroids_384", 0)
            + renamed.get("taxonomy_centroids_768", 0)
            + renamed.get("taxonomy_centroids_1024", 0)
        )
    counts["tax_centroids"] = _unified_centroids
    counts["chash"] = renamed.get("chash_index", 0)
    counts["aspects"] = renamed.get("document_aspects", 0)
    counts["aspect_queue"] = renamed.get("aspect_extraction_queue", 0)
    counts["highlights"] = renamed.get("document_highlights", 0)
    counts["relevance_log"] = renamed.get("relevance_log", 0)
    counts["search_telemetry"] = renamed.get("search_telemetry", 0)
    counts["hook_failures"] = renamed.get("hook_failures", 0)
    counts["catalog_docs"] = renamed.get("catalog_documents", 0)
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
        # populated by the ETL). The Java endpoint 409s a rename onto an existing
        # target UNLESS cross_model=True, so signal it. Unconditional since
        # nexus-i711w — the local catalog writer (which lacked the kwarg) is gone.
        counts["catalog_docs"] = catalog.rename_collection(
            source, target, cross_model=True
        )
    except Exception as exc:  # noqa: BLE001 — catalog cascade is best-effort after T2 remap; surfaced via on_warn
        # The catalog is a derived view (see docstring): a cascade miss here —
        # e.g. a 404 when the legacy source was never registered as a catalog
        # collection — is non-fatal and repairable. State the remedy so this
        # does not read as a data-loss failure (nexus-i6p73).
        on_warn(
            f"warn: T2 reference remap succeeded but catalog cascade failed: {exc}\n"
            f"       (non-fatal: the catalog is a derived view; run "
            f"`nx catalog rebuild` to reconcile.)"
        )

    return counts
