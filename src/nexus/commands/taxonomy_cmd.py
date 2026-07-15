# SPDX-License-Identifier: AGPL-3.0-or-later
"""CLI command group for topic taxonomy (RDR-061 P3-2, RDR-070 nexus-2dq)."""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import click
import numpy as np
import structlog

from nexus.commands._helpers import default_db_path as _default_db_path


def _T2Database(path):
    """Lazy T2Database constructor (avoids module-level import poisoning by test mocks).

    RDR-128 P3 (nexus-sbxbe.3): this factory backs ~17 ``nx taxonomy``
    subcommands and is the single construction site the lint sees for all
    of them. It is irreducible: the read-only subcommands (``status`` /
    ``list`` / ``show`` / ``hubs`` / ``audit`` / ``links``) issue raw
    ``db.taxonomy.conn.execute(...)`` SELECTs, and a raw SQLite cursor
    cannot cross the daemon RPC boundary; while ``discover`` / ``rebuild``
    / ``split`` / ``project`` interleave a ChromaDB centroid write with the
    T2-generated ``topic_id`` inside one lock, which likewise cannot route.
    The reads do not contend on the WAL writer lock; the writes are
    infrequent operator commands, not the automated hot path.
    """
    from nexus.db.t2 import T2Database  # noqa: PLC0415 - deferred to avoid circular import at module load
    return T2Database(path)  # epsilon-allow: taxonomy CLI factory — read-only subcommands need raw-cursor SELECTs (no WAL writer contention) and discover/rebuild/split interleave chroma-centroid writes keyed on T2-generated topic_ids; neither can cross the daemon RPC (RDR-128 P3 documented-irreducible)

def _has_raw_access(taxonomy: Any) -> bool:
    """Return True when taxonomy is a SQLite CatalogTaxonomy (raw .conn / ._lock available).

    Returns False for HttpTaxonomyStore (service mode), where raw cursor
    access is unavailable.  CLI commands that need aggregate queries not
    exposed by the public API must guard with this check and either use
    the public API or skip that display section in service mode.
    """
    return hasattr(taxonomy, "_lock") and hasattr(taxonomy, "conn")


if TYPE_CHECKING:
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy

_log = structlog.get_logger(__name__)


def _require_supported_taxonomy_backend(t3: Any, taxonomy: Any) -> None:
    """Block only the unsupported split: service T3 + raw-SQLite taxonomy.

    nexus-7ydks. Supported: both service-backed (6.0 default) or both raw
    (legacy / ``NX_STORAGE_BACKEND=sqlite``). The split case has no raw Chroma
    client for the legacy centroid path, so refuse cleanly.
    """
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 - deferred to avoid circular import at module load

    if is_service_backed(t3) and _has_raw_access(taxonomy):
        raise click.ClickException(
            "taxonomy discovery is not supported with a service-backed T3 vector "
            "store but a raw-SQLite taxonomy store (NX_STORAGE_BACKEND_TAXONOMY="
            "sqlite). Use a uniform backend: either let both default to the "
            "service, or set NX_STORAGE_BACKEND=sqlite for both."
        )


def _enumerate_discoverable_collections(t3: Any, exclude: list[str]) -> list[str]:
    """Return collection names with >=5 chunks, skipping excludes + taxonomy__.

    Backend-uniform: raw T3 exposes ``_client.list_collections()`` (Chroma
    Collection objects); service T3 exposes ``list_collections()`` (list of
    dicts with ``name``/``count``). nexus-7ydks.
    """
    from fnmatch import fnmatch  # noqa: PLC0415 - branch-local; deferred to call time

    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 - deferred to avoid circular import at module load

    names: list[tuple[str, int]] = []
    if is_service_backed(t3):
        for c in t3.list_collections():
            names.append((c.get("name", ""), int(c.get("count", 0) or 0)))
    else:
        for c in t3._client.list_collections():
            names.append((c.name, c.count()))
    return [
        name for name, count in names
        if count >= 5
        and name
        and not name.startswith("taxonomy__")
        and not any(fnmatch(name, pat) for pat in exclude)
    ]


def _fetch_service_vectors(
    collection_name: str, t3: Any,
) -> tuple[list[str], list[str], "np.ndarray"] | None:
    """Fetch (doc_ids, texts, embeddings) for a collection via the nexus-service.

    nexus-7ydks. Enumerates ids + documents through the service collection
    stub's paginated ``get`` and pulls the stored embeddings server-side via
    ``t3.get_embeddings`` (no re-embed). Returns ``None`` on a fatal fetch
    problem (collection missing, or embeddings the service could not align to
    the requested ids — which would silently corrupt clustering).
    """
    try:
        n = t3.count(collection_name)
    except Exception:  # noqa: BLE001 - best-effort count; failure logged via log.warning, returns None
        _log.warning("taxonomy_service_count_failed", collection=collection_name)
        return None
    if n < 5:
        return [], [], np.empty((0, 0), dtype=np.float32)

    stub = t3.get_or_create_collection(collection_name)
    ids: list[str] = []
    texts: list[str] = []
    offset = 0
    page_size = 250  # service quota: Get limit 300
    _milestone = max(n // 4, 1)
    _next = _milestone
    while offset < n:
        if offset >= _next and _next < n:
            _progress(f"    fetching {offset:,}/{n:,} chunks ({100 * offset // n}%)")
            _next += _milestone
        page = stub.get(include=["documents"], limit=page_size, offset=offset)
        page_ids = page.get("ids") or []
        page_docs = page.get("documents") or []
        if not page_ids:
            break
        for i, pid in enumerate(page_ids):
            doc = page_docs[i] if i < len(page_docs) else None
            if doc is not None:
                ids.append(pid)
                texts.append(doc)
        offset += len(page_ids)
        if len(page_ids) < page_size:
            break

    _progress(f"    fetched {len(ids):,} chunks")
    # Positional-alignment assumption (nexus-7ydks S2): get_embeddings returns
    # rows in request order (documented contract, http_vector_client.py), so
    # embeddings[i] pairs with ids[i]/texts[i]. The count-equality check below
    # is the tripwire; if the service ever returned an id->vector MAP instead,
    # this would need a by-id realign rather than the positional zip.
    embeddings = t3.get_embeddings(collection_name, ids)
    if embeddings is None or len(embeddings) != len(ids):
        # get_embeddings drops ids the service cannot resolve (N < len(ids)),
        # which would desync ids/texts/embeddings. Refuse rather than cluster
        # misaligned rows (feedback_no_silent_fallbacks_for_correctness).
        _log.warning(
            "taxonomy_service_embedding_misalign",
            collection=collection_name,
            ids=len(ids),
            embeddings=0 if embeddings is None else len(embeddings),
        )
        return None
    return ids, texts, np.asarray(embeddings, dtype=np.float32)


def _discover_via_service(
    collection_name: str, taxonomy: Any, t3: Any, *, force: bool,
) -> int:
    """Service-backed discovery: fetch vectors from the service, persist through
    the HttpTaxonomyStore drop-in (centroids via the service's
    ``/v1/taxonomy/centroids`` HTTP route).

    nexus-7ydks. The store's ``discover_topics`` / ``rebuild_taxonomy`` are
    complete CatalogTaxonomy mirrors and persist through the Java service (the
    single writer), so no daemon-routed split is needed here.
    """
    fetched = _fetch_service_vectors(collection_name, t3)
    if fetched is None:
        return 0
    doc_ids, texts, embeddings = fetched
    if len(doc_ids) < 5:
        _log.info("too_few_docs", collection=collection_name, n=len(doc_ids))
        return 0
    _progress(f"    clustering {len(doc_ids):,} x {embeddings.shape[1]}d (service)...")
    if force:
        return taxonomy.rebuild_taxonomy(collection_name, doc_ids, embeddings, texts)
    return taxonomy.discover_topics(collection_name, doc_ids, embeddings, texts)


def _progress(msg: str) -> None:
    """Print a progress message and flush immediately (works in pipes/redirects)."""
    import sys  # noqa: PLC0415 - branch-local; deferred to call time

    click.echo(msg)
    try:
        sys.stdout.buffer.flush()
    except Exception as exc:  # noqa: BLE001 - best-effort cleanup; non-fatal
        _log.debug("taxonomy_stdout_flush_failed", error=str(exc))


# ── Shared function (M5 — callable from CLI and index_repo_cmd) ──────────────


def discover_for_collection(
    collection_name: str,
    taxonomy: "CatalogTaxonomy",
    t3: Any,
    *,
    force: bool = False,
) -> int:
    """Fetch texts + embeddings from a T3 collection, run HDBSCAN discovery.

    Uses the existing T3 embeddings (Voyage on cloud, MiniLM on local)
    rather than re-embedding. This preserves the quality of the original
    embedding model — Voyage-code-3 for code, Voyage-context-3 for docs.
    Falls back to local MiniLM re-embedding when T3 embeddings are not
    available (e.g., collection stored without embeddings).

    Shared entry point for the CLI ``nx taxonomy discover`` and
    programmatic callers (``index_repo_cmd``, ``post_store_hook``).

    Parameters
    ----------
    collection_name:
        ChromaDB collection to discover topics for.
    taxonomy:
        :class:`CatalogTaxonomy` (raw) or ``HttpTaxonomyStore`` (service).
    t3:
        The T3 handle (``T3Database`` raw, or ``HttpVectorClient`` service).
        nexus-7ydks: service-backed handles route through
        :func:`_discover_via_service`; raw handles use ``t3._client``.
    force:
        If True, delete existing topics for this collection before
        re-discovering (calls ``rebuild_taxonomy``).

    Returns
    -------
    int
        Number of topics created.
    """
    # nexus-7ydks: service-backed taxonomy store → fetch from the service and
    # persist through the HttpTaxonomyStore drop-in (centroids via the service's
    # /v1/taxonomy/centroids HTTP route).
    if not _has_raw_access(taxonomy):
        return _discover_via_service(collection_name, taxonomy, t3, force=force)

    # Raw path (unchanged): daemon-routed persist (RDR-128/151) over a raw
    # Chroma client. Accept either a ``T3Database`` handle (use ``._client``)
    # or a raw chroma client passed directly (programmatic / test callers).
    chroma_client = getattr(t3, "_client", t3)
    try:
        coll = chroma_client.get_collection(
            collection_name, embedding_function=None,
        )
    except Exception:  # noqa: BLE001 - collection-missing tolerated; logged via log.warning, returns 0
        _log.warning("collection_not_found", collection=collection_name)
        return 0

    n = coll.count()
    if n < 5:
        _log.info("too_few_docs", collection=collection_name, n=n)
        return 0

    # Fetch doc_ids, documents, and existing embeddings in pages.
    # Uses T3 embeddings (Voyage on cloud) when available.
    all_ids: list[str] = []
    all_texts: list[str] = []
    all_embs: list[list[float]] = []
    has_t3_embeddings = True
    offset = 0
    page_size = 250  # Cloud quota: Get limit 300
    _milestone_step = max(n // 4, 1)
    _next_milestone = _milestone_step
    while offset < n:
        if offset >= _next_milestone and _next_milestone < n:
            _progress(f"    fetching {offset:,}/{n:,} chunks ({100 * offset // n}%)")
            _next_milestone += _milestone_step
        page = coll.get(
            include=["documents", "embeddings"],
            limit=page_size,
            offset=offset,
        )
        page_ids = page["ids"]
        page_docs = page.get("documents") or []
        page_embs = page.get("embeddings")
        if page_embs is None:
            page_embs = [None] * len(page_ids)
            has_t3_embeddings = False

        for i, pid in enumerate(page_ids):
            doc = page_docs[i] if i < len(page_docs) else None
            emb = page_embs[i] if i < len(page_embs) else None
            if doc is not None:
                all_ids.append(pid)
                all_texts.append(doc)
                if emb is not None and len(emb) > 0:
                    all_embs.append(list(emb))
                else:
                    has_t3_embeddings = False

        offset += len(page_ids)
        if len(page_ids) < page_size:
            break

    import time  # noqa: PLC0415 - branch-local; deferred to call time

    _progress(f"    fetched {len(all_ids):,} chunks")

    # Use T3 embeddings if all docs have them; else fall back to MiniLM
    if has_t3_embeddings and len(all_embs) == len(all_ids):
        _progress(f"    embedding: using T3 native ({len(all_embs[0])}d)")
        embeddings = np.array(all_embs, dtype=np.float32)
    else:
        from nexus.db.local_ef import LocalEmbeddingFunction  # noqa: PLC0415 - deferred to avoid circular import at module load

        _progress(f"    embedding: re-encoding with MiniLM (384d)")
        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        embeddings = np.array(ef(all_texts), dtype=np.float32)

    _progress(f"    clustering {len(all_ids):,} x {embeddings.shape[1]}d...")
    t0 = time.monotonic()

    # RDR-151 Phase 3 (nexus-uzay8): compute the topic clusters/centroids
    # client-side (chroma + numpy), route the pure-T2 PERSIST through the
    # daemon (t2_index_write), then write the chroma centroids locally from
    # the daemon-returned topic_ids. ``taxonomy`` is used only for the
    # force-path READ of old state (read-only; no WAL writer contention) and
    # the static centroid helpers — no direct T2 write happens here.
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load

    centroid_coll = CatalogTaxonomy._create_centroid_collection(chroma_client)
    if force:
        old = taxonomy.read_rebuild_old_state(collection_name, centroid_coll)
        for i in range(0, len(old["old_centroid_ids"]), 300):
            centroid_coll.delete(ids=old["old_centroid_ids"][i:i + 300])
        plan = CatalogTaxonomy.compute_rebuild_plan(
            collection_name, all_ids, embeddings, all_texts,
            old_centroids=old["old_centroids"],
            old_labels=old["old_labels"],
            old_review_statuses=old["old_review_statuses"],
            old_centroid_topic_ids=old["old_centroid_topic_ids"],
            manual_assignments=old["manual_assignments"],
        )
        specs = plan["specs"]
        topic_ids = t2_index_write(
            lambda db: db.taxonomy.persist_rebuild_topics(collection_name, plan)
        )
    else:
        specs = CatalogTaxonomy.compute_discovered_topics(
            collection_name, all_ids, embeddings, all_texts,
        )
        topic_ids = (
            t2_index_write(
                lambda db: db.taxonomy.persist_discovered_topics(collection_name, specs)
            )
            if specs else []
        )

    result = len(topic_ids)
    if topic_ids:
        c_ids, c_embs, c_metas = CatalogTaxonomy._centroid_records_for(
            collection_name, specs, topic_ids,
        )
        if c_ids:
            CatalogTaxonomy._batched_upsert(centroid_coll, c_ids, c_embs, c_metas)
        # Cross-collection links: compute (chroma) local, persist routed.
        try:
            pairs = CatalogTaxonomy.compute_cross_links(
                collection_name, c_embs, c_metas, centroid_coll,
            )
            if pairs:
                t2_index_write(lambda db: db.taxonomy.persist_cross_links(pairs))
        except Exception:  # noqa: BLE001 - best-effort cross-link discovery; logged via log.debug
            _log.debug("discover_cross_links_failed", exc_info=True)

    # Record doc count for rebalance tracking (routed).
    t2_index_write(
        lambda db: db.taxonomy.record_discover_count(collection_name, len(all_ids))
    )

    elapsed = time.monotonic() - t0
    _progress(f"    clustered in {elapsed:.1f}s")
    return result


# ── CLI commands ─────────────────────────────────────────────────────────────


@click.group()
def taxonomy() -> None:
    """Topic taxonomy — browsable knowledge hierarchy."""


@taxonomy.command("status")
@click.option("--collection", "-c", default="", help="Show only this collection")
@click.option("--limit", "-n", default=0, type=int, help="Show only top N collections by doc count (0 = all)")
@click.option("--summary", is_flag=True, help="Show only the totals line")
@click.option("--needs-review", is_flag=True, help="Show only collections with pending topics")
def status_cmd(collection: str, limit: int, summary: bool, needs_review: bool) -> None:
    """Show taxonomy health: collections, coverage, review state.

    \b
    Examples:
      nx taxonomy status                              # all collections
      nx taxonomy status --summary                    # totals line only
      nx taxonomy status -c docs__nexus               # one collection
      nx taxonomy status -n 10                        # top 10 by docs
      nx taxonomy status --needs-review               # pending review only
    """
    with _T2Database(_default_db_path()) as db:
        if _has_raw_access(db.taxonomy):
            # Storage review I-1: every .conn access goes through the
            # domain-store lock. These are read-only queries but the lock
            # protects against a concurrent writer on the same connection.
            with db.taxonomy._lock:  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                all_topics = db.taxonomy.conn.execute(  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                    "SELECT collection, COUNT(*), SUM(doc_count), "
                    "SUM(CASE WHEN review_status = 'pending' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN review_status = 'accepted' THEN 1 ELSE 0 END) "
                    "FROM topics GROUP BY collection ORDER BY SUM(doc_count) DESC"
                ).fetchall()
        else:
            # Service mode: derive per-collection aggregate from public API
            raw_topics = db.taxonomy.get_all_topics()
            from collections import defaultdict  # noqa: PLC0415 - branch-local; deferred to call time
            _agg: dict[str, list[int, int, int, int]] = defaultdict(lambda: [0, 0, 0, 0])
            for t in raw_topics:
                c = t.get("collection", "")
                _agg[c][0] += 1  # n_topics
                _agg[c][1] += int(t.get("doc_count") or 0)  # n_docs
                if t.get("review_status") == "pending":
                    _agg[c][2] += 1
                if t.get("review_status") == "accepted":
                    _agg[c][3] += 1
            all_topics = [
                (c, v[0], v[1], v[2], v[3])
                for c, v in sorted(_agg.items(), key=lambda x: -x[1][1])
            ]

        if not all_topics:
            click.echo("No taxonomy data. Run `nx index repo` or `nx taxonomy discover`.")
            return

        # Compute totals across ALL topics (independent of filters)
        total_topics = sum(r[1] for r in all_topics)
        total_assigned = sum(r[2] for r in all_topics)
        total_pending = sum(r[3] for r in all_topics)

        # GitHub #239 + bead nexus-gwhy: per-collection projection
        # assignment counts so status can flag collections with topics
        # but no cross-collection projection data. Computed over the
        # full universe; the Action hint must not under-count when the
        # user passes ``-n`` to truncate the display (code-review
        # finding C-2).
        projection_counts = db.taxonomy.get_projection_counts_by_collection()
        missing_projection: list[str] = [
            coll for coll, n_topics, *_ in all_topics
            if n_topics > 0 and projection_counts.get(coll, 0) == 0
        ]

        if _has_raw_access(db.taxonomy):
            with db.taxonomy._lock:  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                link_count = db.taxonomy.conn.execute(  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                    "SELECT COUNT(*) FROM topic_links"
                ).fetchone()[0]
        else:
            # Service mode: count via the public link-pairs API (nexus-ntkr5 —
            # the previous hardcoded 0 hid 7,520 live links after the first
            # `links --refresh` run). Works on both stores: SQLite returns a
            # dict, HttpTaxonomyStore a list; len() is shape-agnostic.
            # raw_topics was fetched above in this same service-mode branch —
            # reuse it rather than paying a second HTTP round-trip.
            _ids = [t["id"] for t in raw_topics]
            link_count = len(db.taxonomy.get_topic_link_pairs(_ids)) if _ids else 0

        # Apply filters
        rows = all_topics
        if collection:
            rows = [r for r in rows if r[0] == collection]
            if not rows:
                click.echo(f"No taxonomy data for collection '{collection}'.")
                return
        if needs_review:
            rows = [r for r in rows if r[3] > 0]
        if limit > 0:
            rows = rows[:limit]

        if not summary:
            click.echo("Taxonomy Status\n")
            for coll, n_topics, n_docs, n_pending, n_accepted in rows:
                if _has_raw_access(db.taxonomy):
                    with db.taxonomy._lock:  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                        meta = db.taxonomy.conn.execute(  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                            "SELECT last_discover_doc_count, last_discover_at "
                            "FROM taxonomy_meta WHERE collection = ?",
                            (coll,),
                        ).fetchone()
                else:
                    meta = None  # service mode: per-collection meta not yet exposed

                rebal = ""
                if meta:
                    _, last_at = meta
                    if last_at:
                        rebal = f"  discovered {last_at[:10]}"

                status_parts = []
                if n_accepted:
                    status_parts.append(f"{n_accepted} accepted")
                if n_pending:
                    status_parts.append(f"{n_pending} pending")
                status_str = ", ".join(status_parts) if status_parts else "all pending"

                proj_count = projection_counts.get(coll, 0)
                proj_note = "  [no projection]" if (proj_count == 0 and n_topics > 0) else ""

                click.echo(f"  {coll}{proj_note}")
                click.echo(f"    {n_topics} topics, {n_docs} docs assigned ({status_str}){rebal}")

            click.echo("")

        click.echo(
            f"Total: {len(all_topics)} collections, {total_topics} topics, "
            f"{total_assigned} docs assigned, {link_count} topic links"
        )
        if total_pending:
            click.echo(f"Action: {total_pending} topics need review. Run `nx taxonomy review`.")
        if missing_projection:
            example = missing_projection[0]
            click.echo(
                f"Action: {len(missing_projection)} collection(s) have no cross-collection "
                f"projection. Run `nx taxonomy project {example} --persist` "
                f"(or `nx taxonomy project --backfill --persist` for all)."
            )

        # GH #251 + RDR-095: surface recent post-store hook failures.
        # SQLite-only: hook_failures table not accessible in service mode.
        rows: list[tuple[str, int, int, str | None]] = []
        try:
            if _has_raw_access(db.taxonomy):
                with db.taxonomy._lock:  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                    try:
                        rows = db.taxonomy.conn.execute(  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                            "SELECT hook_name, is_batch, "
                            "       COALESCE(batch_doc_ids, '') "
                            "FROM hook_failures "
                            "WHERE occurred_at >= datetime('now', '-1 day')"
                        ).fetchall()
                        rows = [(r[0], 1, r[1], r[2]) for r in rows]
                    except Exception:  # noqa: BLE001 - legacy-schema fallback for pre-4.14.1 batch columns
                        # Pre-4.14.1 schema: batch columns absent. Read with
                        # legacy shape and treat every row as scalar.
                        legacy = db.taxonomy.conn.execute(  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                            "SELECT hook_name FROM hook_failures "
                            "WHERE occurred_at >= datetime('now', '-1 day')"
                        ).fetchall()
                        rows = [(r[0], 1, 0, None) for r in legacy]
        except Exception:  # noqa: BLE001 - best-effort row read; degrades to empty list
            rows = []

        if rows:
            import json as _json  # noqa: PLC0415 - branch-local; deferred to call time
            from collections import Counter  # noqa: PLC0415 - branch-local; deferred to call time

            total_recent = sum(n for _, n, _, _ in rows)
            per_hook: Counter[str] = Counter()
            docs_affected = 0
            for name, _count, is_batch, batch_payload in rows:
                per_hook[name] += 1
                if is_batch and batch_payload:
                    try:
                        docs_affected += len(_json.loads(batch_payload))
                    except (ValueError, TypeError):
                        docs_affected += 1
                else:
                    docs_affected += 1

            hook_summary = ", ".join(
                f"{name}={n}" for name, n in sorted(per_hook.items())
            )
            if docs_affected > total_recent:
                affected_note = (
                    f" affecting {docs_affected} document(s)"
                )
            else:
                affected_note = ""
            click.echo(
                f"Action: {total_recent} post-store hook failure(s)"
                f"{affected_note} in the last 24h "
                f"({hook_summary}). Run `nx doctor --check-schema` and check "
                "structlog output; tail `~/.config/nexus/logs/` for details."
            )


@taxonomy.command("list")
@click.option("--collection", "-c", default="", help="Filter by collection/project")
@click.option("--depth", "-d", default=2, type=int, help="Tree depth", show_default=True)
def list_cmd(collection: str, depth: int) -> None:
    """Show topic tree."""
    from nexus.taxonomy import get_topic_tree  # noqa: PLC0415 - deferred to avoid circular import at module load

    depth = min(depth, 4)
    with _T2Database(_default_db_path()) as db:
        tree = get_topic_tree(db, collection, max_depth=depth)
        # Count docs with no topic assignment (noise / uncategorized).
        # Lock taken per storage review I-1 (SQLite only).
        if _has_raw_access(db.taxonomy):
            with db.taxonomy._lock:  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                total_assigned = db.taxonomy.conn.execute(  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                    "SELECT COUNT(DISTINCT doc_id) FROM topic_assignments"
                    + (" WHERE topic_id IN (SELECT id FROM topics WHERE collection = ?)" if collection else ""),
                    (collection,) if collection else (),
                ).fetchone()[0]
        else:
            total_assigned = 0  # service mode: assignment count not exposed via public API
    if not tree:
        click.echo("No topics found. Run `nx taxonomy discover --collection <name>` first.")
        return
    for node in tree:
        _print_tree(node, indent=0)
    total_docs = sum(_tree_doc_count(n) for n in tree)
    if total_docs > total_assigned:
        click.echo(f"\nUncategorized: {total_docs - total_assigned} docs")


def _tree_doc_count(node: dict) -> int:
    """Recursively sum doc_count across a tree node and all its children."""
    return node["doc_count"] + sum(
        _tree_doc_count(c) for c in node.get("children", [])
    )


def _print_tree(node: dict, indent: int = 0) -> None:
    # GitHub #241 Item 1: root nodes carry their collection name as a
    # ``[coll]`` prefix column so ``nx taxonomy list`` without -c is
    # immediately readable on multi-collection taxonomies. Children
    # inherit their root's collection and don't need the tag.
    prefix = "  " * indent + ("├── " if indent > 0 else "")
    if indent == 0 and node.get("collection"):
        click.echo(
            f"[{node['collection']}]  {node['label']} ({node['doc_count']} docs)"
        )
    else:
        click.echo(f"{prefix}{node['label']} ({node['doc_count']} docs)")
    for child in node.get("children", []):
        _print_tree(child, indent + 1)


@taxonomy.command("show")
@click.argument("topic_id", type=int)
@click.option("--limit", "-n", default=20, help="Max docs to show", show_default=True)
def show_cmd(topic_id: int, limit: int) -> None:
    """Show documents assigned to a topic."""
    from nexus.taxonomy import get_topic_docs  # noqa: PLC0415 - deferred to avoid circular import at module load

    with _T2Database(_default_db_path()) as db:
        docs = get_topic_docs(db, topic_id, limit=limit)
    if not docs:
        click.echo(f"No documents in topic {topic_id}.")
        return
    click.echo(f"Topic {topic_id}: {len(docs)} documents")
    click.echo("-" * 60)
    for doc in docs:
        click.echo(f"  {doc['doc_id']}")


@taxonomy.command("discover")
@click.option("--collection", "-c", default="", help="T3 collection (omit for --all)")
@click.option("--all", "discover_all", is_flag=True, help="Discover all eligible T3 collections")
@click.option("--force", is_flag=True, help="Delete existing topics before re-discovering")
def discover_cmd(collection: str, discover_all: bool, force: bool) -> None:
    """Discover topics from T3 collections using HDBSCAN clustering.

    Use --collection for a single collection, or --all to discover
    topics for every T3 collection (respects local_exclude_collections).
    """
    from fnmatch import fnmatch  # noqa: PLC0415 - branch-local; deferred to call time

    from nexus.config import is_local_mode, load_config  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.db import make_t3  # noqa: PLC0415 - deferred to avoid circular import at module load

    if not collection and not discover_all:
        click.echo("Specify --collection <name> or --all.")
        return

    cfg = load_config()
    exclude = (
        cfg.get("taxonomy", {}).get("local_exclude_collections", [])
        if is_local_mode() else []
    )
    t3 = make_t3()

    if discover_all:
        # Backend-support is checked per-collection inside the loop below
        # (authoritative); enumeration itself needs no taxonomy handle.
        targets = _enumerate_discoverable_collections(t3, exclude)
        if not targets:
            click.echo("No eligible collections found.")
            return
        click.echo(f"Discovering topics for {len(targets)} collections...")
    else:
        if is_local_mode() and any(fnmatch(collection, pat) for pat in exclude):
            click.echo(
                f"Warning: {collection!r} matches taxonomy.local_exclude_collections "
                f"({exclude}). Local MiniLM clusters poorly on code. Proceeding anyway."
            )
        targets = [collection]

    auto_label = cfg.get("taxonomy", {}).get("auto_label", True)
    can_label = auto_label and _claude_available()

    total_topics = 0
    total_labeled = 0
    with _T2Database(_default_db_path()) as db:
        for i, col_name in enumerate(targets, 1):
            if len(targets) > 1:
                click.echo(f"[{i}/{len(targets)}] {col_name}")
            _require_supported_taxonomy_backend(t3, db.taxonomy)
            count = discover_for_collection(
                col_name, db.taxonomy, t3, force=force,
            )
            if count:
                click.echo(f"  {col_name}: {count} topics")
                total_topics += count
                # Label immediately after each collection (incremental, crash-safe)
                if can_label:
                    labeled = relabel_topics(
                        db.taxonomy, collection=col_name, only_pending=True,
                    )
                    if labeled:
                        click.echo(f"  {col_name}: labeled {labeled} topics")
                        total_labeled += labeled
            else:
                click.echo(f"  {col_name}: skipped")

        # Cross-collection projection pass (RDR-075 SC-7). nexus-9pqoj:
        # project_against handles both backends; pass the raw chroma client for
        # a raw T3Database (has ._client) or the service handle itself for an
        # HttpVectorClient (no ._client).
        _proj_handle = getattr(t3, "_client", t3)
        if total_topics and len(targets) > 1:
            try:
                proj_count = 0
                for col_name in targets:
                    others = [c for c in targets if c != col_name]
                    if others:
                        result = db.taxonomy.project_against(
                            col_name, others, _proj_handle, threshold=0.85,
                        )
                        if result.get("incomplete_fetch"):
                            click.echo(
                                f"  Projection: skipped {col_name} (incomplete "
                                "service embedding read; collection may be mid-index)"
                            )
                            continue
                        assignments = result.get("chunk_assignments", [])
                        if assignments:
                            _persist_assignments(
                                assignments, col_name, quiet=True,
                            )
                            proj_count += len(assignments)
                if proj_count:
                    click.echo(f"  Projection: {proj_count} cross-collection assignments")
                    # Co-occurrence topic links (SC-5, SC-7)
                    # RDR-151 Phase 3: route via daemon.
                    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load
                    cooc = t2_index_write(lambda db: db.taxonomy.generate_cooccurrence_links())
                    if cooc:
                        click.echo(f"  Links:      {cooc} co-occurrence topic links")
            except Exception:  # noqa: BLE001 - best-effort projection; logged via log.warning
                _log.warning("discover_projection_failed", exc_info=True)

        # Refresh L1 context cache after discovery
        if total_topics:
            try:
                from pathlib import Path as _Path  # noqa: PLC0415 - branch-local; deferred to call time
                from nexus.context import generate_context_l1  # noqa: PLC0415 - deferred to avoid circular import at module load
                generate_context_l1(db.taxonomy, repo_path=_Path.cwd())
            except Exception as exc:  # noqa: BLE001 - non-fatal best-effort step
                _log.debug("taxonomy_context_l1_generation_failed", error=str(exc))

    click.echo(f"\nTotal: {total_topics} topics, {total_labeled} labeled.")


@taxonomy.command("rebuild")
@click.option("--collection", "-c", default="", help="T3 collection to rebuild taxonomy for")
@click.option("--project", "-p", default="", hidden=True, help="Deprecated: use --collection instead")
@click.option("-k", default=None, type=int, hidden=True, help="Deprecated: cluster count is automatic")
def rebuild_cmd(collection: str, project: str, k: int | None) -> None:
    """Rebuild topic taxonomy from scratch (alias for discover --force)."""
    from nexus.db import make_t3  # noqa: PLC0415 - deferred to avoid circular import at module load

    # Backward compat: old --project flag maps to --collection
    if project and not collection:
        click.echo(
            f"Note: --project is deprecated. Use --collection instead.\n"
            f"  Hint: nx taxonomy rebuild --collection {project}\n"
        )
        collection = project

    if not collection:
        click.echo("Specify --collection <name>. Use `nx taxonomy discover --all` for all collections.")
        return

    if k is not None:
        click.echo("Note: -k is deprecated. Cluster count is now automatic (HDBSCAN).")

    with _T2Database(_default_db_path()) as db:
        t3 = make_t3()
        _require_supported_taxonomy_backend(t3, db.taxonomy)
        count = discover_for_collection(
            collection, db.taxonomy, t3, force=True,
        )
    click.echo(f"Rebuilt {count} topics for collection {collection!r}.")


# ── Review command (RDR-070, nexus-lbu) ─────────────────────────────────────


def _resolve_doc_titles(doc_ids: list[str]) -> list[str]:
    """Resolve doc_ids to human-readable titles via catalog, fallback to raw ID."""
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 - deferred to avoid circular import at module load

        cat = make_catalog_reader()
        if cat is None:
            return doc_ids
        titles: list[str] = []
        for doc_id in doc_ids:
            results = cat.search(doc_id)
            if results:
                titles.append(results[0].get("title", doc_id))
            else:
                titles.append(doc_id)
        return titles
    except Exception:  # noqa: BLE001 - best-effort; degrades to input doc_ids
        return doc_ids


def _display_topic(
    topic: dict[str, Any],
    index: int,
    total: int,
    taxonomy: "CatalogTaxonomy",
) -> None:
    """Display a single topic for review."""
    import json  # noqa: PLC0415 - branch-local; deferred to call time

    click.echo(f"\n{'─' * 60}")
    click.echo(f"  [{index}/{total}]  {topic['label']}  ({topic['doc_count']} docs)")

    # c-TF-IDF terms
    if topic.get("terms"):
        try:
            terms = json.loads(topic["terms"])
            click.echo(f"  Terms: {', '.join(terms)}")
        except (json.JSONDecodeError, TypeError):
            pass

    # Representative docs
    doc_ids = taxonomy.get_topic_doc_ids(topic["id"], limit=3)
    if doc_ids:
        titles = _resolve_doc_titles(doc_ids)
        click.echo("  Docs:")
        for title in titles:
            click.echo(f"    - {title}")

    click.echo(f"{'─' * 60}")


def _show_merge_targets(
    current_id: int,
    collection: str,
    taxonomy: "CatalogTaxonomy",
) -> None:
    """Show all other topics in the same collection as merge targets."""
    targets = taxonomy.get_topics_for_collection(collection, exclude_id=current_id)
    if not targets:
        click.echo("  No other topics to merge into.")
        return
    click.echo("  Available merge targets:")
    for t in targets:
        click.echo(f"    [{t['id']}] {t['label']} ({t['doc_count']} docs)")


@taxonomy.command("review")
@click.option("--collection", "-c", default="", help="Filter by collection")
@click.option(
    "--limit", "-n", default=None, type=int,
    help="Topics per session (default: 15 interactive, 5000 with --auto)",
)
@click.option(
    "--auto", is_flag=True,
    help="Batched claude_dispatch verdicts instead of interactive prompts",
)
@click.option(
    "--yes", "-y", is_flag=True,
    help="Skip the destructive-action confirmation prompt (--auto only)",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Print verdicts without applying any mutations (--auto only)",
)
@click.option(
    "--batch-size", default=40, type=int, show_default=True,
    help="Topics per claude_dispatch call (--auto only)",
)
def review_cmd(
    collection: str,
    limit: int | None,
    auto: bool,
    yes: bool,
    dry_run: bool,
    batch_size: int,
) -> None:
    """Interactive topic review — accept, rename, merge, delete, or skip."""
    resolved_limit = limit if limit is not None else (5000 if auto else 15)

    if auto:
        with _T2Database(_default_db_path()) as db:
            _review_auto(db, collection, resolved_limit, yes, dry_run, batch_size)
        return

    with _T2Database(_default_db_path()) as db:
        topics = db.taxonomy.get_unreviewed_topics(collection=collection, limit=resolved_limit)
        if not topics:
            click.echo("No unreviewed topics. All done!")
            return

        click.echo(f"Reviewing {len(topics)} topic(s)")
        click.echo("Actions: [a]ccept  [r]ename  [m]erge  [d]elete  [S]kip")

        # RDR-151 Phase 3 (nexus-uzay8): all T2 writes routed via daemon.
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load

        for i, topic in enumerate(topics, 1):
            _display_topic(topic, i, len(topics), db.taxonomy)

            try:
                action = click.prompt(
                    "Action",
                    type=click.Choice(["a", "r", "m", "d", "S"], case_sensitive=True),
                    default="S",
                )
            except (click.Abort, EOFError):
                click.echo("\n  Aborted.")
                break

            if action == "a":
                _tid = topic["id"]
                t2_index_write(lambda db, _t=_tid: db.taxonomy.mark_topic_reviewed(_t, "accepted"))
                click.echo(f"  Accepted: {topic['label']}")

            elif action == "r":
                new_label = click.prompt("  New label")
                _tid = topic["id"]
                _lbl = new_label
                t2_index_write(lambda db, _t=_tid, _l=_lbl: db.taxonomy.rename_topic(_t, _l))
                click.echo(f"  Renamed: {topic['label']} -> {new_label}")

            elif action == "m":
                _show_merge_targets(topic["id"], topic["collection"], db.taxonomy)
                target_id = click.prompt("  Merge into topic ID", type=int)
                target = db.taxonomy.get_topic_by_id(target_id)
                if target is None:
                    click.echo(f"  Topic {target_id} not found, skipping.")
                    continue
                _src = topic["id"]
                _tgt = target_id
                t2_index_write(lambda db, _s=_src, _t=_tgt: db.taxonomy.merge_topics(_s, _t))
                click.echo(f"  Merged into: {target['label']}")

            elif action == "d":
                _tid = topic["id"]
                t2_index_write(lambda db, _t=_tid: db.taxonomy.delete_topic(_t))
                click.echo(f"  Deleted: {topic['label']}")

            elif action == "S":
                click.echo("  Skipped.")

    click.echo("\nReview session complete.")


# ── Manual operations (RDR-070, nexus-c3w) ──────────────────────────────────


@taxonomy.command("assign")
@click.argument("doc_id")
@click.argument("topic_label")
@click.option("--collection", "-c", default="", help="Collection scope for label lookup")
def assign_cmd(doc_id: str, topic_label: str, collection: str) -> None:
    """Assign a document to a topic by label."""
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load
    with _T2Database(_default_db_path()) as db:
        topic_id = db.taxonomy.resolve_label(topic_label, collection=collection)
        if topic_id is None:
            click.echo(f"Topic '{topic_label}' not found.")
            return
        # RDR-151 Phase 3 (nexus-uzay8): route via daemon.
        _did = doc_id
        _tid = topic_id
        t2_index_write(lambda db, _d=_did, _t=_tid: db.taxonomy.assign_topic(_d, _t, assigned_by="manual"))
        click.echo(f"Assigned '{doc_id}' to topic '{topic_label}' (id={topic_id}).")


@taxonomy.command("rename")
@click.argument("topic_label")
@click.argument("new_label")
@click.option("--collection", "-c", default="", help="Collection scope for label lookup")
@click.option(
    "--no-accept", is_flag=True,
    help=(
        "Rename without transitioning review_status to 'accepted'. "
        "Default behaviour accepts the topic, consistent with the "
        "interactive `review` rename path (typing the new label is "
        "an acknowledgement). Use --no-accept to correct a typo on "
        "a still-pending topic without advancing it through review."
    ),
)
def rename_cmd(
    topic_label: str, new_label: str, collection: str, no_accept: bool,
) -> None:
    """Rename a topic. By default, also transitions to 'accepted'.

    Code-review finding M-1 + bead nexus-gwhy: the standalone rename
    command previously always transitioned review_status to 'accepted'
    as a side effect of ``rename_topic``. Default behaviour preserved
    (the user typing a new label is an acknowledgement); ``--no-accept``
    lets you fix a typo without forcing the topic through review.
    """
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load
    with _T2Database(_default_db_path()) as db:
        topic_id = db.taxonomy.resolve_label(topic_label, collection=collection)
        if topic_id is None:
            click.echo(f"Topic '{topic_label}' not found.")
            return
        # RDR-151 Phase 3 (nexus-uzay8): route via daemon.
        _tid = topic_id
        _lbl = new_label
        if no_accept:
            t2_index_write(lambda db, _t=_tid, _l=_lbl: db.taxonomy.update_topic_label(_t, _l))
            click.echo(
                f"Renamed '{topic_label}' -> '{new_label}' (review_status preserved)."
            )
        else:
            t2_index_write(lambda db, _t=_tid, _l=_lbl: db.taxonomy.rename_topic(_t, _l))
            click.echo(f"Renamed '{topic_label}' -> '{new_label}'.")


@taxonomy.command("merge")
@click.argument("source_label")
@click.argument("target_label")
@click.option("--collection", "-c", default="", help="Collection scope for label lookup")
def merge_cmd(source_label: str, target_label: str, collection: str) -> None:
    """Merge source topic into target topic."""
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load
    with _T2Database(_default_db_path()) as db:
        source_id = db.taxonomy.resolve_label(source_label, collection=collection)
        if source_id is None:
            click.echo(f"Source topic '{source_label}' not found.")
            return
        target_id = db.taxonomy.resolve_label(target_label, collection=collection)
        if target_id is None:
            click.echo(f"Target topic '{target_label}' not found.")
            return
        # RDR-151 Phase 3 (nexus-uzay8): route via daemon.
        _src = source_id
        _tgt = target_id
        t2_index_write(lambda db, _s=_src, _t=_tgt: db.taxonomy.merge_topics(_s, _t))
        click.echo(f"Merged '{source_label}' into '{target_label}'.")


@taxonomy.command("split")
@click.argument("topic_label")
@click.option("--k", "-k", default=2, type=int, help="Number of sub-topics", show_default=True)
@click.option("--collection", "-c", default="", help="Collection scope for label lookup")
def split_cmd(topic_label: str, k: int, collection: str) -> None:
    """Split a topic into k sub-topics via KMeans clustering.

    RDR-151 Phase 3 (nexus-uzay8): the T3 fetch + MiniLM reembed + KMeans
    clustering happens locally (compute phase), then the pure-T2 persist
    (DELETE parent assignments + INSERT children) is routed through the
    daemon via t2_index_write.  Chroma centroid operations happen locally
    before and after the routed persist using the returned child IDs.
    """
    import numpy as _np  # noqa: PLC0415 - heavy dep deferred to call time
    from nexus.db import make_t3  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.db.local_ef import LocalEmbeddingFunction  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.db.t2.catalog_taxonomy import CatalogTaxonomy  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load

    with _T2Database(_default_db_path()) as db:
        topic_id = db.taxonomy.resolve_label(topic_label, collection=collection)
        if topic_id is None:
            click.echo(f"Topic '{topic_label}' not found.")
            return

        topic = db.taxonomy.get_topic_by_id(topic_id)
        if topic is None:
            click.echo(f"Topic '{topic_label}' not found.")
            return

        doc_ids = db.taxonomy.get_all_topic_doc_ids(topic_id)
        if len(doc_ids) < k:
            click.echo(f"Split '{topic_label}' into 0 sub-topics.")
            return

        collection_name = topic["collection"]
        t3 = make_t3()

        # nexus-9pqoj: service-backed split. The store's split_topic does the
        # full fetch -> compute -> persist -> centroid round-trip via the service.
        if not _has_raw_access(db.taxonomy):
            # service-backed split_topic persists through the Java service (the
            # single writer for HttpTaxonomyStore), not the SQLite WAL writer the
            # boundary lint guards, so t2_index_write routing does not apply.
            child_count = db.taxonomy.split_topic(topic_id, k, t3)  # epsilon-allow: service single-writer persist
            click.echo(f"Split '{topic_label}' into {child_count} sub-topics.")
            if child_count:
                coll_scope = collection_name or collection
                scope = f" -c {coll_scope}" if coll_scope else ""
                click.echo(
                    f"Action: {child_count} new sub-topics have n-gram labels. "
                    f"Run `nx taxonomy label{scope}` to get human-readable labels."
                )
            return

        # Raw path (unchanged): refuse the split-backend config, then inline.
        _require_supported_taxonomy_backend(t3, db.taxonomy)
        chroma_client = t3._client

        # Fetch texts from T3 collection
        try:
            coll = chroma_client.get_collection(collection_name, embedding_function=None)
        except Exception:  # noqa: BLE001 - collection-missing surfaced to user via click.echo, returns
            click.echo(f"Collection '{collection_name}' not found in T3.")
            return

        _PAGE = 250
        fetched_ids: list[str] = []
        texts: list[str] = []
        for i in range(0, len(doc_ids), _PAGE):
            batch = doc_ids[i : i + _PAGE]
            result = coll.get(ids=batch, include=["documents"])
            for fid, fdoc in zip(result.get("ids") or [], result.get("documents") or []):
                if fdoc:
                    fetched_ids.append(fid)
                    texts.append(fdoc)

        if len(texts) < k:
            click.echo(f"Split '{topic_label}' into 0 sub-topics.")
            return

        ef = LocalEmbeddingFunction(model_name="all-MiniLM-L6-v2")
        embeddings = _np.array(ef(texts), dtype=_np.float32)

        # COMPUTE: KMeans + c-TF-IDF (pure CPU, no T2 writes)
        split_result = CatalogTaxonomy.compute_split(
            topic_id=topic_id,
            doc_ids=doc_ids,
            texts=texts,
            fetched_ids=fetched_ids,
            embeddings=embeddings,
            collection_name=collection_name,
            k=k,
        )
        child_specs = split_result["child_specs"]
        if not child_specs:
            click.echo(f"Split '{topic_label}' into 0 sub-topics.")
            return

        # PERSIST: route T2 writes through daemon
        child_ids = t2_index_write(lambda db: db.taxonomy.persist_split(split_result))
        child_count = len(child_ids)

        # LOCAL: chroma centroid cleanup (remove parent, add children)
        if child_count:
            centroid_coll = CatalogTaxonomy._create_centroid_collection(chroma_client)
            parent_centroid_id = f"{collection_name}:{topic_id}"
            try:
                centroid_coll.delete(ids=[parent_centroid_id])
            except Exception as exc:  # noqa: BLE001 - best-effort; non-fatal
                _log.debug("taxonomy_centroid_delete_failed", error=str(exc))
            c_ids = [f"{collection_name}:{cid}" for cid in child_ids]
            c_embs = [spec["centroid"] for spec in child_specs]
            c_metas = [
                {
                    "topic_id": cid,
                    "label": spec["label"],
                    "collection": collection_name,
                    "doc_count": spec["doc_count"],
                }
                for cid, spec in zip(child_ids, child_specs)
            ]
            CatalogTaxonomy._batched_upsert(centroid_coll, c_ids, c_embs, c_metas)

        click.echo(f"Split '{topic_label}' into {child_count} sub-topics.")
        if child_count:
            coll_scope = collection_name or collection
            scope = f" -c {coll_scope}" if coll_scope else ""
            click.echo(
                f"Action: {child_count} new sub-topics have n-gram labels. "
                f"Run `nx taxonomy label{scope}` to get human-readable labels."
            )


# ── Topic-aware links (RDR-070, nexus-40f) ──────────────────────────────────


def _try_load_catalog() -> Any:
    """Load the catalog if initialized, else return None."""
    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 - deferred to avoid circular import at module load

        return make_catalog_reader()
    except Exception as exc:  # noqa: BLE001 - best-effort lookup; degrades to None
        _log.debug("taxonomy_catalog_reader_unavailable", error=str(exc))
    return None


def compute_topic_links(
    taxonomy: "CatalogTaxonomy",
    catalog: Any,
    *,
    collection: str = "",
    persist: bool = False,
) -> list[dict[str, Any]]:
    """Derive inter-topic relationships from catalog link graph.

    Joins catalog links (tumbler→tumbler) with topic assignments
    (doc_id→topic) via file_path matching. Returns aggregated
    topic-pair counts with link types.

    When ``persist=True``, also writes to the ``topic_links`` T2 table
    for use by ``apply_topic_boost`` at search time.
    """
    from collections import Counter, defaultdict  # noqa: PLC0415 - branch-local; deferred to call time

    # Build doc_id → (topic_label, topic_id) index from T2
    topics = taxonomy.get_topics()
    if collection:
        topics = [t for t in topics if t.get("collection") == collection]

    topic_label_map: dict[int, str] = {t["id"]: t["label"] for t in topics}
    doc_to_topic_label: dict[str, str] = {}
    doc_to_topic_id: dict[str, int] = {}
    for topic in topics:
        doc_ids = taxonomy.get_all_topic_doc_ids(topic["id"])
        for did in doc_ids:
            doc_to_topic_label[did] = topic_label_map[topic["id"]]
            doc_to_topic_id[did] = topic["id"]

    if not doc_to_topic_label:
        return []

    # Build prefix index: file_path → first matching doc_id (O(N) build, O(1) lookup)
    # Sorted doc_ids enable prefix matching via bisect
    from bisect import bisect_left  # noqa: PLC0415 - branch-local; deferred to call time

    sorted_doc_ids = sorted(doc_to_topic_label.keys())

    def _find_by_prefix(prefix: str) -> str | None:
        """Find first doc_id that starts with prefix via binary search."""
        idx = bisect_left(sorted_doc_ids, prefix)
        if idx < len(sorted_doc_ids) and sorted_doc_ids[idx].startswith(prefix):
            return sorted_doc_ids[idx]
        return None

    # Build tumbler → topic via catalog entry resolution
    links = catalog.link_query(limit=0)
    if not links:
        return []

    tumbler_cache: dict[str, tuple[str, int] | None] = {}

    def _resolve_topic(tumbler: Any) -> tuple[str, int] | None:
        key = str(tumbler)
        if key in tumbler_cache:
            return tumbler_cache[key]
        entry = catalog.resolve(tumbler)
        result = None
        if entry and entry.file_path:
            fp = entry.file_path
            if fp in doc_to_topic_label:
                result = (doc_to_topic_label[fp], doc_to_topic_id[fp])
            else:
                match = _find_by_prefix(fp)
                if match:
                    result = (doc_to_topic_label[match], doc_to_topic_id[match])
        tumbler_cache[key] = result
        return result

    # Aggregate links between topics
    pair_counts: Counter[tuple[str, str]] = Counter()
    pair_types: dict[tuple[str, str], set[str]] = defaultdict(set)
    # Also track by topic_id for persistence
    id_pair_counts: Counter[tuple[int, int]] = Counter()
    id_pair_types: dict[tuple[int, int], set[str]] = defaultdict(set)

    for link in links:
        from_info = _resolve_topic(link.from_tumbler)
        to_info = _resolve_topic(link.to_tumbler)
        if from_info and to_info and from_info[1] != to_info[1]:
            from_label, from_id = from_info
            to_label, to_id = to_info
            # Canonical ordering
            label_key = (from_label, to_label) if from_label < to_label else (to_label, from_label)
            pair_counts[label_key] += 1
            pair_types[label_key].add(link.link_type)

            id_key = (from_id, to_id) if from_id < to_id else (to_id, from_id)
            id_pair_counts[id_key] += 1
            id_pair_types[id_key].add(link.link_type)

    result = [
        {
            "from_topic": k[0],
            "to_topic": k[1],
            "link_count": v,
            "link_types": sorted(pair_types[k]),
        }
        for k, v in pair_counts.most_common()
    ]

    # Persist to T2 for search-time topic boost (routed via daemon)
    if persist and id_pair_counts:
        persist_data = [
            {
                "from_topic_id": k[0],
                "to_topic_id": k[1],
                "link_count": v,
                "link_types": sorted(id_pair_types[k]),
            }
            for k, v in id_pair_counts.most_common()
        ]
        from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load
        t2_index_write(lambda db: db.taxonomy.upsert_topic_links(persist_data))

    return result


@taxonomy.command("links")
@click.option("--collection", "-c", default="", help="Filter by collection")
@click.option(
    "--refresh", is_flag=True,
    help="Recompute catalog-derived links before displaying (requires catalog).",
)
def links_cmd(collection: str, refresh: bool) -> None:
    """Show all inter-topic relationships in topic_links.

    Includes cross-collection projection links from RDR-075 (link_types
    contains 'projection' or 'cooccurrence') AND catalog-derived links
    from compute_topic_links (link_types contains 'cites', 'implements',
    etc.).  Use --refresh to recompute catalog-derived links first.
    """
    with _T2Database(_default_db_path()) as db:
        if refresh:
            catalog = _try_load_catalog()
            if catalog is None:
                click.echo("No catalog initialized — skipping --refresh.")
            else:
                compute_topic_links(
                    db.taxonomy, catalog, collection=collection, persist=True,
                )

        # Display all rows in topic_links, joined with topic labels.
        # Lock taken per storage review I-1 (SQLite only).
        if _has_raw_access(db.taxonomy):
            with db.taxonomy._lock:  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                if collection:
                    rows = db.taxonomy.conn.execute(  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                        "SELECT t1.label, t1.collection, t2.label, t2.collection, "
                        "       tl.link_count, tl.link_types "
                        "FROM topic_links tl "
                        "JOIN topics t1 ON tl.from_topic_id = t1.id "
                        "JOIN topics t2 ON tl.to_topic_id = t2.id "
                        "WHERE t1.collection = ? OR t2.collection = ? "
                        "ORDER BY tl.link_count DESC",
                        (collection, collection),
                    ).fetchall()
                else:
                    rows = db.taxonomy.conn.execute(  # epsilon-allow: guarded by _has_raw_access (service-mode skip); raw-cursor aggregate not in public API
                        "SELECT t1.label, t1.collection, t2.label, t2.collection, "
                        "       tl.link_count, tl.link_types "
                        "FROM topic_links tl "
                        "JOIN topics t1 ON tl.from_topic_id = t1.id "
                        "JOIN topics t2 ON tl.to_topic_id = t2.id "
                        "ORDER BY tl.link_count DESC"
                    ).fetchall()
        else:
            # Service mode: get link pairs via public API and resolve labels
            _all_topics = {t["id"]: t for t in db.taxonomy.get_all_topics()}
            _topic_ids = list(_all_topics.keys())
            _pairs = db.taxonomy.get_topic_link_pairs(_topic_ids) if _topic_ids else []
            rows = []
            for from_id, to_id, count in _pairs:
                from_t = _all_topics.get(from_id, {})
                to_t   = _all_topics.get(to_id, {})
                if collection and (from_t.get("collection") != collection and
                                   to_t.get("collection") != collection):
                    continue
                rows.append((
                    from_t.get("label", str(from_id)),
                    from_t.get("collection", ""),
                    to_t.get("label", str(to_id)),
                    to_t.get("collection", ""),
                    count,
                    "[]",
                ))

        if not rows:
            click.echo("No topic links found.")
            return

        click.echo(f"Topic relationships ({len(rows)} pairs):\n")
        for from_label, from_coll, to_label, to_coll, count, types_json in rows:
            try:
                import json as _json  # noqa: PLC0415 - branch-local; deferred to call time
                types_str = ", ".join(_json.loads(types_json))
            except Exception:  # noqa: BLE001 - best-effort label parse; falls back to raw json string
                types_str = types_json
            click.echo(
                f"  [{from_coll}] {from_label} <-> [{to_coll}] {to_label}"
                f"  ({count} links: {types_str})"
            )


# ── LLM-powered labeling (RDR-070) ──────────────────────────────────────────


def _claude_available() -> bool:
    """Check if claude CLI is on PATH."""
    import shutil  # noqa: PLC0415 - branch-local; deferred to call time

    return shutil.which("claude") is not None


_LABEL_SCHEMA: dict = {
    "type": "object",
    "required": ["labels"],
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["idx", "label"],
                "properties": {
                    "idx": {"type": "integer", "minimum": 1},
                    "label": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 60,
                    },
                },
            },
        },
    },
}


async def _generate_labels_batch(
    items: list[tuple[list[str], list[str]]],
    glossary_text: str = "",
) -> list[str | None]:
    """Generate labels for a batch of topics via ``claude_dispatch``.

    RDR-085: Each item is ``(terms, sample_doc_ids)``. Returns a list of
    labels — same length as ``items``, ``None`` at indices where the
    schema-enforced response either omitted an entry or returned one
    outside the 3–60 char window.

    When *glossary_text* is supplied (via :func:`nexus.glossary.format_for_prompt`),
    it is prepended to the prompt so the LLM has project vocabulary
    context before the numbered topic list — eliminates the
    ``SSMF → "Single Mode Fiber"`` class of training-prior hallucination
    documented in ``docs/field-reports/2026-04-15-architecture-as-code-from-art.md``.
    """
    if not items:
        return []

    lines = []
    for i, (terms, doc_ids) in enumerate(items, 1):
        doc_names = [d.split("/")[-1].split(":")[0][:25] for d in doc_ids[:3]]
        lines.append(
            f"{i}. terms=[{', '.join(terms[:5])}] docs=[{', '.join(doc_names)}]"
        )

    prompt_parts: list[str] = []
    if glossary_text:
        prompt_parts.append(glossary_text)
    prompt_parts.append(
        "You are a topic labeler. Label each numbered topic in 3-5 words.\n"
        'Return {"labels": [{"idx": <1-based>, "label": "<3-60 chars>"}, ...]} — '
        "one entry per numbered topic, idx matches the number you were given.\n"
    )
    prompt_parts.append("\n".join(lines))
    prompt = "\n\n".join(prompt_parts)

    results: list[str | None] = [None] * len(items)
    try:
        from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 - deferred to avoid circular import at module load

        payload = await claude_dispatch(prompt, _LABEL_SCHEMA, timeout=120.0)
    except Exception:  # noqa: BLE001 - best-effort payload parse; degrades to current results
        return results

    labels = payload.get("labels") if isinstance(payload, dict) else None
    if not isinstance(labels, list):
        return results

    for entry in labels:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("idx")
        label = entry.get("label", "")
        if not isinstance(idx, int) or not isinstance(label, str):
            continue
        slot = idx - 1
        if 0 <= slot < len(items) and 3 <= len(label) <= 60:
            results[slot] = label.strip().strip('"').strip("'")
    return results


# ── nx taxonomy review --auto (nexus-6i01g, nexus-vfs67) ───────────────────

_REVIEW_VERDICT_SCHEMA: dict = {
    "type": "object",
    "required": ["verdicts"],
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "action"],
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "action": {
                        "type": "string",
                        "enum": ["accept", "rename", "delete", "merge"],
                    },
                    "label": {"type": "string", "minLength": 3, "maxLength": 60},
                    "target_id": {"type": "integer"},
                    "reason": {"type": "string", "maxLength": 200},
                },
            },
        },
    },
}


async def _generate_review_verdicts_batch(
    items: list[tuple[int, str, list[str], list[str], str]],
) -> list[dict | None]:
    """Generate review verdicts for a batch of topics via ``claude_dispatch``.

    Each item is ``(topic_id, label, terms, sample_doc_ids, collection)``.
    Returns one verdict dict (or ``None``, fail-open) per item, in item
    order. Verdict shapes:

    - ``{"action": "accept"}``
    - ``{"action": "rename", "label": <3-60 chars, stripped>}``
    - ``{"action": "delete", "reason": <str>}``
    - ``{"action": "merge", "target_id": <int>, "reason": <str>}``

    Verdicts are matched back to items by the REAL topic id (schema
    property ``"id"``), not a positional index — a stacked-review fix
    (nexus-6i01g finding 3): a prior version keyed by 1-based ``idx``
    alongside a displayed ``id=`` in the prompt, two near-identical
    numbering schemes the model could conflate, producing a
    wrong-topic-verdict. An entry whose ``id`` is not in this batch is
    ignored (fail-open), not misapplied to some other slot by position.

    Fail-open (mirrors :func:`_generate_labels_batch`): dispatch raising
    ANY exception, a malformed/missing ``verdicts`` key, or a per-entry
    validation failure (bad id, unknown action, missing ``label``/
    ``target_id`` where required) all degrade to ``None`` at that slot —
    the caller leaves the corresponding topic pending. ``target_id``
    guard-validation (self-merge, merge-chain same-run source/target
    collision, missing/cross-collection target, target-deleted-this-run)
    happens in the CLI orchestration layer, not here — this function only
    enforces schema-shape validity.
    """
    if not items:
        return []

    id_to_slot = {topic_id: slot for slot, (topic_id, *_rest) in enumerate(items)}

    lines = []
    for topic_id, label, terms, doc_ids, collection in items:
        doc_names = [d.split("/")[-1].split(":")[0][:25] for d in doc_ids[:3]]
        lines.append(
            f'- id={topic_id} label="{label}" terms=[{", ".join(terms[:5])}] '
            f'docs=[{", ".join(doc_names)}] collection={collection}'
        )

    prompt = (
        "You are reviewing topic-cluster candidates from an automated taxonomy "
        "discovery pass. For each topic below, decide exactly one action:\n"
        "  accept - the label is specific and coherent for the terms/docs shown.\n"
        "  rename - the underlying cluster is coherent but the label is bad; "
        'supply a new label (3-60 chars) as "label".\n'
        "  delete - the topic is syntax pattern-pollution, not a real subject: "
        "pytest/monkeypatch scaffolding, Java test boilerplate, license/import "
        'headers, CSS blobs, home-directory path fragments. Give a one-line "reason".\n'
        "  merge - the topic is a near-duplicate of another topic in the same "
        "collection (usually one listed below); supply the REAL topic id (the "
        '"id=" value) as "target_id" plus a one-line "reason".\n'
        'Return {"verdicts": [{"id": <the id= value shown above>, '
        '"action": "<accept|rename|delete|merge>", "label": "<if rename>", '
        '"target_id": <if merge>, "reason": "<if delete/merge>"}, ...]} — '
        "one entry per topic, keyed by its id (not its position in this list).\n\n"
        + "\n".join(lines)
    )

    results: list[dict | None] = [None] * len(items)
    try:
        from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 - deferred to avoid circular import at module load

        payload = await claude_dispatch(prompt, _REVIEW_VERDICT_SCHEMA, timeout=180.0)
    except Exception:  # noqa: BLE001 - best-effort payload parse; degrades to current results
        return results

    verdicts = payload.get("verdicts") if isinstance(payload, dict) else None
    if not isinstance(verdicts, list):
        return results

    for entry in verdicts:
        if not isinstance(entry, dict):
            continue
        topic_id = entry.get("id")
        action = entry.get("action")
        if not isinstance(topic_id, int) or not isinstance(action, str):
            continue
        if topic_id not in id_to_slot:
            continue
        slot = id_to_slot[topic_id]
        if action == "accept":
            results[slot] = {"action": "accept"}
        elif action == "rename":
            new_label = entry.get("label")
            if isinstance(new_label, str):
                stripped = new_label.strip()
                if 3 <= len(stripped) <= 60:
                    results[slot] = {"action": "rename", "label": stripped}
        elif action == "delete":
            reason = entry.get("reason", "")
            results[slot] = {"action": "delete", "reason": reason if isinstance(reason, str) else ""}
        elif action == "merge":
            target_id = entry.get("target_id")
            if isinstance(target_id, int):
                reason = entry.get("reason", "")
                results[slot] = {
                    "action": "merge",
                    "target_id": target_id,
                    "reason": reason if isinstance(reason, str) else "",
                }
        # unknown action strings are schema-rejected upstream, but guard
        # defensively in case a future model deviates from the enum.
    return results


def _print_review_destructive_plan(
    deletes: list[dict[str, Any]], merges: list[dict[str, Any]],
) -> None:
    """Print a grouped, human-readable plan for pending delete/merge verdicts."""
    click.echo("\nDestructive actions pending:")
    if deletes:
        click.echo(f"  Deletes ({len(deletes)}):")
        for d in deletes:
            reason = d.get("_reason", "")
            click.echo(f"    [{d['id']}] {d['label']} ({d['doc_count']} docs) - {reason}")
    if merges:
        click.echo(f"  Merges ({len(merges)}):")
        for m in merges:
            reason = m.get("_reason", "")
            click.echo(
                f"    [{m['id']}] {m['label']} ({m['doc_count']} docs) -> "
                f"[{m['_target_id']}] {m['_target_label']} ({m['_target_doc_count']} docs) - {reason}"
            )


def _review_auto(
    db: Any,
    collection: str,
    limit: int,
    yes: bool,
    dry_run: bool,
    batch_size: int,
) -> None:
    """Batched, unattended review: swaps the human judge for ``claude_dispatch``.

    accept/rename apply immediately (unless ``dry_run``); delete/merge are
    held as a destructive plan requiring ``click.confirm`` or ``--yes``
    (``dry_run`` suppresses ALL mutations, including accept/rename).
    Dispatch is sequential per batch — no parallelism (V1 non-goal,
    nexus-6i01g).

    Merge validation runs in a second pass once the whole batch's verdicts
    are known (guard order documented inline below), including a CRITICAL
    same-run merge-chain guard: a merge target that is itself a merge
    source this run is dropped rather than applied, which would otherwise
    risk silently orphaning assignments (``merge_topics`` has no
    target-existence check; T2 SQLite runs with foreign keys off). Apply
    loops additionally recheck target existence immediately before each
    merge and wrap every ``t2_index_write`` call so one bad delete/merge
    does not abort the remaining batch — failures are counted separately
    from guard-dropped skips and reported in the summary line.
    """
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load

    topics = db.taxonomy.get_unreviewed_topics(collection=collection, limit=limit)
    if not topics:
        click.echo("No unreviewed topics. All done!")
        return

    click.echo(f"Auto-reviewing {len(topics)} topic(s) in batches of {batch_size}...")

    accepted = 0
    renamed = 0
    skipped = 0
    candidate_deletes: list[dict[str, Any]] = []
    candidate_merges: list[dict[str, Any]] = []

    for start in range(0, len(topics), batch_size):
        batch = topics[start : start + batch_size]
        items: list[tuple[int, str, list[str], list[str], str]] = []
        for t in batch:
            try:
                terms = json.loads(t["terms"]) if t.get("terms") else []
            except (json.JSONDecodeError, TypeError):
                terms = []
            doc_ids = db.taxonomy.get_topic_doc_ids(t["id"], limit=3)
            items.append((t["id"], t["label"], terms, doc_ids, t["collection"]))

        verdicts = asyncio.run(_generate_review_verdicts_batch(items))

        for topic, verdict in zip(batch, verdicts):
            if verdict is None:
                skipped += 1
                continue
            action = verdict["action"]
            if action == "accept":
                if not dry_run:
                    _tid = topic["id"]
                    t2_index_write(lambda db, _t=_tid: db.taxonomy.mark_topic_reviewed(_t, "accepted"))
                accepted += 1
            elif action == "rename":
                if not dry_run:
                    _tid = topic["id"]
                    _lbl = verdict["label"]
                    t2_index_write(lambda db, _t=_tid, _l=_lbl: db.taxonomy.rename_topic(_t, _l))
                renamed += 1
            elif action == "delete":
                candidate_deletes.append({**topic, "_reason": verdict.get("reason", "")})
            elif action == "merge":
                candidate_merges.append(
                    {
                        **topic,
                        "_target_id": verdict.get("target_id"),
                        "_reason": verdict.get("reason", ""),
                    }
                )

    # Second pass: validate merges only once the full delete- and
    # merge-source sets are known. Guard order (any violation: skipped += 1,
    # topic stays pending):
    #   1. self-merge (target_id == own id).
    #   2. target is itself a merge SOURCE in this run. CRITICAL
    #      (nexus-6i01g stacked-review finding 1): without this guard, a
    #      same-run merge chain A->B, B->C can silently orphan data — if
    #      B->C applies before A->B, merge_topics(A, B) runs against an
    #      already-deleted B. CatalogTaxonomy.merge_topics has no
    #      target-existence check and T2 SQLite has foreign keys OFF (no
    #      ``PRAGMA foreign_keys=ON``), so A's assignments would silently
    #      become orphaned rows pointing at a deleted topic_id. Dropping
    #      any merge whose target is also a source is deterministic
    #      regardless of apply order: A->B always drops, B->C always
    #      proceeds (subject to its own guards).
    #   3. target does not exist.
    #   4. target is in a different collection.
    #   5. target was itself verdict-deleted this run.
    delete_ids = {d["id"] for d in candidate_deletes}
    merge_source_ids = {m["id"] for m in candidate_merges}
    pending_merges: list[dict[str, Any]] = []
    for m in candidate_merges:
        target_id = m["_target_id"]
        if target_id == m["id"]:
            skipped += 1
            continue
        if target_id in merge_source_ids:
            skipped += 1
            continue
        target = db.taxonomy.get_topic_by_id(target_id)
        if target is None:
            skipped += 1
            continue
        if target["collection"] != m["collection"]:
            skipped += 1
            continue
        if target_id in delete_ids:
            skipped += 1
            continue
        pending_merges.append(
            {
                **m,
                "_target_label": target["label"],
                "_target_doc_count": target["doc_count"],
            }
        )

    if dry_run:
        if candidate_deletes or pending_merges:
            _print_review_destructive_plan(candidate_deletes, pending_merges)
        click.echo(
            f"\nDry run: {accepted} would be accepted, {renamed} would be renamed, "
            f"{len(candidate_deletes)} would be deleted, {len(pending_merges)} would be merged, "
            f"{skipped} skipped."
        )
        return

    deleted = 0
    merged = 0
    failed = 0
    if candidate_deletes or pending_merges:
        _print_review_destructive_plan(candidate_deletes, pending_merges)
        try:
            proceed = yes or click.confirm("Apply the above destructive actions?")
        except (click.Abort, EOFError):
            proceed = False

        if proceed:
            for d in candidate_deletes:
                _tid = d["id"]
                try:
                    t2_index_write(lambda db, _t=_tid: db.taxonomy.delete_topic(_t))
                except Exception as exc:  # noqa: BLE001 - per-item apply resilience: one bad delete must not abort the batch
                    _log.warning(
                        "taxonomy_review_auto_apply_failed",
                        topic_id=_tid,
                        action="delete",
                        error=str(exc),
                    )
                    click.echo(f"  Failed to delete topic {_tid}: {exc}")
                    failed += 1
                    continue
                deleted += 1
            for m in pending_merges:
                _src = m["id"]
                _tgt = m["_target_id"]
                # Defensive apply-time recheck (nexus-6i01g stacked-review
                # finding 1): the target may have been removed between
                # second-pass validation and this apply loop (e.g. an
                # earlier delete in THIS loop, or an external actor) —
                # merge_topics has no target-existence check of its own.
                if db.taxonomy.get_topic_by_id(_tgt) is None:
                    _log.warning(
                        "taxonomy_review_auto_apply_failed",
                        topic_id=_src,
                        action="merge",
                        error=f"target {_tgt} no longer exists",
                    )
                    click.echo(f"  Failed to merge topic {_src} into {_tgt}: target no longer exists")
                    failed += 1
                    continue
                try:
                    t2_index_write(lambda db, _s=_src, _t=_tgt: db.taxonomy.merge_topics(_s, _t))
                except Exception as exc:  # noqa: BLE001 - per-item apply resilience: one bad merge must not abort the batch
                    _log.warning(
                        "taxonomy_review_auto_apply_failed",
                        topic_id=_src,
                        action="merge",
                        error=str(exc),
                    )
                    click.echo(f"  Failed to merge topic {_src} into {_tgt}: {exc}")
                    failed += 1
                    continue
                merged += 1
        else:
            # Finding 6: declined destructive items must still land in the
            # skipped bucket so the summary tally accounts for every
            # proposed action, not just the applied ones.
            skipped += len(candidate_deletes) + len(pending_merges)
            click.echo("Declined; topics remain pending.")

    click.echo(
        f"\nAuto-review complete: {accepted} accepted, {renamed} renamed, "
        f"{deleted} deleted, {merged} merged, {skipped} skipped, {failed} failed."
    )


def relabel_topics(
    taxonomy: "CatalogTaxonomy",
    *,
    collection: str = "",
    only_pending: bool = True,
    batch_size: int = 20,
    workers: int = 4,
    project_root: Path | None = None,
) -> int:
    """Relabel topics using batched ``claude_dispatch`` calls.

    RDR-085 migrates the labeler off its bespoke subprocess shell-out
    onto the shipped ``claude_dispatch`` substrate. Glossary resolution
    runs once per command invocation and is reused across every batch —
    subsequent ThreadPoolExecutor workers each call ``asyncio.run()`` to
    drive the async dispatcher in isolation.

    Args:
        project_root: Repo root for glossary resolution. Defaults to
            ``Path.cwd()`` when unset.
    """
    import asyncio  # noqa: PLC0415 - branch-local; deferred to call time
    import json  # noqa: PLC0415 - branch-local; deferred to call time
    from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: PLC0415 - branch-local; deferred to call time

    if only_pending:
        topics = taxonomy.get_unreviewed_topics(collection=collection, limit=5000)
    else:
        # GitHub #243: include split children on the relabel_all path.
        # ``get_topics()`` returns only roots, which hid split sub-topics.
        topics = taxonomy.get_all_topics(collection=collection)

    if not topics:
        return 0

    # Prepare work items: (topic_id, terms, doc_ids)
    work: list[tuple[int, str, list[str], list[str]]] = []
    for topic in topics:
        terms = json.loads(topic["terms"]) if topic.get("terms") else []
        if not terms:
            continue
        doc_ids = taxonomy.get_topic_doc_ids(topic["id"], limit=5)
        work.append((topic["id"], topic["label"], terms, doc_ids))

    if not work:
        return 0

    # Resolve glossary once per command invocation (RDR-085).
    glossary_text = ""
    try:
        from nexus.glossary import format_for_prompt, load_glossary  # noqa: PLC0415 - deferred to avoid circular import at module load

        root = project_root or Path.cwd()
        glossary = load_glossary(root, collection=collection or None)
        glossary_text = format_for_prompt(glossary) if glossary else ""
    except Exception:  # noqa: BLE001 - best-effort glossary load; logged via log.debug
        _log.debug("glossary_load_failed", exc_info=True)

    # Split into batches
    batches: list[list[tuple[int, str, list[str], list[str]]]] = []
    for i in range(0, len(work), batch_size):
        batches.append(work[i : i + batch_size])

    _progress(f"    labeling {len(work)} topics ({len(batches)} batches, {workers} workers)")

    count = 0
    batches_done = 0

    def _label_batch(batch: list) -> list[tuple[int, str | None]]:
        items = [(w[2], w[3]) for w in batch]  # (terms, doc_ids)
        # Each worker thread has no event loop; asyncio.run() is safe.
        # nexus-8g79.33: assert the invariant defensively — if a future
        # caller invokes _label_batch from an already-async context,
        # asyncio.run() would raise the opaque
        # "asyncio.run() cannot be called from a running event loop".
        # This assert surfaces the bug at the call site.
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # expected: no running loop, asyncio.run() is safe.
        else:
            raise RuntimeError(
                "_label_batch called from an async context — "
                "use loop.run_until_complete or refactor caller."
            )
        labels = asyncio.run(_generate_labels_batch(items, glossary_text=glossary_text))
        return [(w[0], lbl) for w, lbl in zip(batch, labels)]

    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_label_batch, b): b for b in batches}
        for future in as_completed(futures):
            batches_done += 1
            for tid, label in future.result():
                if label:
                    # GitHub #241 Item 3: use update_topic_label rather than
                    # rename_topic so review_status stays 'pending'. The
                    # human-driven ``nx taxonomy review`` is where topics
                    # transition pending → accepted; batch LLM labeling
                    # should not short-circuit that step.
                    # RDR-151 Phase 3 (nexus-uzay8): route via daemon.
                    _tid = tid  # capture for lambda
                    _lbl = label
                    t2_index_write(lambda db, _t=_tid, _l=_lbl: db.taxonomy.update_topic_label(_t, _l))
                    count += 1
            _progress(f"    batch {batches_done}/{len(batches)} done ({count} renamed)")

    return count


@taxonomy.command("label")
@click.option("--collection", "-c", default="", help="Filter by collection")
@click.option("--all", "relabel_all", is_flag=True, help="Relabel all topics, not just pending")
def label_cmd(collection: str, relabel_all: bool) -> None:
    """Generate human-readable topic labels using Claude."""
    if not _claude_available():
        click.echo("claude CLI not found. Install Claude Code to use LLM labeling.")
        return

    with _T2Database(_default_db_path()) as db:
        # GitHub #243: the pre-check must see split sub-topics (children
        # with parent_id set); ``get_topics()`` only returns roots, so
        # a post-split pending child would be silently skipped here.
        # ``get_unreviewed_topics`` and ``get_all_topics`` both include
        # children, which matches what ``relabel_topics`` actually
        # iterates over.
        if relabel_all:
            target = db.taxonomy.get_all_topics(collection=collection)
        else:
            target = db.taxonomy.get_unreviewed_topics(
                collection=collection, limit=5000,
            )

        if not target:
            click.echo("No topics to label.")
            return

        click.echo(f"Labeling {len(target)} topics via Claude haiku...")
        count = relabel_topics(
            db.taxonomy,
            collection=collection,
            only_pending=not relabel_all,
        )
        click.echo(f"Relabeled {count}/{len(target)} topics.")


@taxonomy.command("project")
@click.argument("source_collection", default="")
@click.option(
    "--against", "-a", default="",
    help="Comma-separated target collections (omit for all other collections with topics)",
)
@click.option(
    "--threshold", "-t", default=None, type=float,
    help=(
        "Cosine similarity threshold. When omitted, per-corpus-type "
        "defaults apply: code__* → 0.70, knowledge__* → 0.50, "
        "docs__*/rdr__* → 0.55. See docs/exploration/taxonomy-projection-tuning.md."
    ),
)
@click.option("--top-k", default=3, type=int, show_default=True, help="Top-k centroids per chunk")
@click.option("--persist", is_flag=True, help="Write projection assignments (assigned_by='projection')")
@click.option("--backfill", is_flag=True, help="Project all collections against each other")
@click.option(
    "--use-icf", "use_icf", is_flag=True,
    help=(
        "Apply ICF (Inverse Collection Frequency) weighting — suppresses "
        "ubiquitous hub topics before threshold + top-k ranking. Stored "
        "similarity remains raw cosine (RDR-077 RF-8)."
    ),
)
def project_cmd(
    source_collection: str,
    against: str,
    threshold: float | None,
    top_k: int,
    persist: bool,
    backfill: bool,
    use_icf: bool,
) -> None:
    """Project source collection chunks against target collection centroids.

    Reports matched topics with chunk counts and average similarity,
    plus novel chunks below the threshold.  Use --persist to write
    projection assignments to topic_assignments.

    \b
    Threshold resolution (RDR-077 Phase 4a):
      explicit --threshold overrides; otherwise the per-prefix default
      applies (see --threshold option for the full table).
    \b
    Examples:
      nx taxonomy project docs__art-architecture --against knowledge__art
      nx taxonomy project code__nexus --threshold 0.80 --persist
      nx taxonomy project code__nexus --use-icf --persist
      nx taxonomy project --backfill --persist
    """
    from nexus.corpus import default_projection_threshold  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.db import make_t3  # noqa: PLC0415 - deferred to avoid circular import at module load

    db = _T2Database(_default_db_path())
    t3 = make_t3()
    # nexus-9pqoj: refuse the split-backend config; project_against handles both
    # backends, so pass the chroma client (raw T3Database) or the service handle
    # (HttpVectorClient has no ._client).
    _require_supported_taxonomy_backend(t3, db.taxonomy)
    _proj_handle = getattr(t3, "_client", t3)

    # Resolve threshold: explicit flag wins; otherwise per-corpus default
    # (defaults applied at the per-source level inside _run_backfill).
    resolved_threshold = threshold
    if resolved_threshold is None and source_collection:
        resolved_threshold = default_projection_threshold(source_collection)

    try:
        if backfill:
            _run_backfill(
                db.taxonomy, _proj_handle,
                threshold=threshold, top_k=top_k, persist=persist,
                use_icf=use_icf,
            )
            return

        if not source_collection:
            click.echo("Specify a source collection or use --backfill.")
            return

        # Determine target collections.
        #
        # GitHub #238 + bead nexus-gwhy: single-source defaults to
        # every collection with topics minus the source, matching the
        # backfill target set. Previously the single-source path tried
        # ``list_sibling_collections`` first (same-repo, different-prefix
        # collections) and fell back only if empty; users with multiple
        # collections under one prefix family (e.g. several ``docs__*``
        # from distinct repos) saw "against 1 collection(s)" while
        # ``project --backfill`` on the same source targeted the full set.
        # Use ``--against`` to scope explicitly when the default is too
        # wide.
        if against:
            targets = [c.strip() for c in against.split(",") if c.strip()]
        else:
            targets = [
                c for c in db.taxonomy.get_distinct_collections()
                if c != source_collection
            ]
            if not targets:
                click.echo("No other collections have topics. Run 'nx taxonomy discover' first.")
                return

        _progress(
            f"Projecting {source_collection} against {len(targets)} "
            f"collection(s) at threshold {resolved_threshold}"
            + (" with ICF weighting" if use_icf else "")
            + "..."
        )

        icf_map = (
            db.taxonomy.compute_icf_map(use_cache=True) if use_icf else None
        )
        result = db.taxonomy.project_against(
            source_collection, targets, _proj_handle,
            threshold=resolved_threshold, top_k=top_k,
            icf_map=icf_map,
            progress=True,
        )
        # nexus-9pqoj S1: a service fetch that could not align embeddings to ids
        # returns empty-with-flag; surface it loudly instead of looking like
        # "no matches".
        if result.get("incomplete_fetch"):
            raise click.ClickException(
                f"Could not read all source embeddings for {source_collection} "
                "from the service (count mismatch). The collection may be mid-"
                "index; retry once indexing settles, or re-index it."
            )

        # Fall through: display logic uses `threshold` local — rebind
        # to the resolved value so messages reflect what was applied.
        threshold = resolved_threshold

        # Display results
        matched = result["matched_topics"]
        novel = result["novel_chunks"]
        total = result["total_chunks"]

        if matched:
            click.echo(f"\nMatched topics (threshold {threshold}):")
            for m in matched:
                click.echo(
                    f"  [{m['topic_id']}] {m['label']} ({m['collection']}) "
                    f"— {m['chunk_count']} chunks, avg sim {m['avg_similarity']:.2f}"
                )
        else:
            click.echo("\nNo matched topics above threshold.")

        click.echo(f"\nNovel chunks: {len(novel)} (no centroid match >= {threshold})")
        covered = total - len(novel)
        click.echo(f"Total: {len(matched)} matched topics, {covered}/{total} chunks covered")

        if persist and result.get("chunk_assignments"):
            _persist_assignments(
                result["chunk_assignments"], source_collection,
            )
        elif matched and not persist:
            click.echo("\nRun with --persist to write assignments to topic_assignments.")

    except ValueError as e:
        click.echo(f"Error: {e}")
    finally:
        db.close()


def _persist_assignments(
    chunk_assignments: list[tuple[str, int, float]],
    source_collection: str,
    *,
    quiet: bool = False,
) -> int:
    """Write per-chunk projection assignments from ``project_against`` results.

    Each tuple is ``(doc_id, topic_id, raw_cosine_similarity)`` per RDR-077
    RF-3. *source_collection* identifies the origin of these chunks (used
    later for ICF hub detection).

    Returns the number of assignments written. Set *quiet* to suppress CLI
    output (used when called from pipeline context).

    RDR-151 Phase 3 (nexus-uzay8): all T2 writes routed via t2_index_write.
    The assign_topic calls are batched as a single persist_assignments call
    so the daemon takes one write lock, not N individual locks.
    """
    from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 - deferred to avoid circular import at module load
    # Build the serializable assignment dicts for the daemon-routable
    # persist_assignments method (avoids N individual daemon RPCs).
    assignment_dicts = [
        {
            "doc_id": doc_id,
            "topic_id": topic_id,
            "assigned_by": "projection",
            "similarity": similarity,
            "source_collection": source_collection,
        }
        for doc_id, topic_id, similarity in chunk_assignments
    ]
    if assignment_dicts:
        t2_index_write(lambda db: db.taxonomy.persist_assignments(assignment_dicts))
    # GitHub #240 + bead nexus-gwhy: rebuild projection entries in
    # topic_links so ``nx taxonomy links`` reflects the new assignments.
    # Without this refresh, ``links`` stayed at the centroid-similarity
    # pairs written at discover time while ``hubs`` (live query) moved
    # ahead. Cost is one aggregate + upsert per persist call.
    t2_index_write(lambda db: db.taxonomy.refresh_projection_links())
    if not quiet:
        click.echo(f"Persisted {len(chunk_assignments)} projection assignment(s).")
    return len(chunk_assignments)


def _run_backfill(
    taxonomy: "CatalogTaxonomy",
    chroma_client: Any,
    *,
    threshold: float | None = None,
    top_k: int = 3,
    persist: bool = False,
    use_icf: bool = False,
) -> None:
    """Project all collections against each other.

    When *threshold* is None, applies the RDR-077 per-corpus-type default
    for each source collection (``default_projection_threshold``). An
    explicit *threshold* short-circuits that and applies uniformly.
    """
    from nexus.corpus import default_projection_threshold  # noqa: PLC0415 - deferred to avoid circular import at module load

    collections = taxonomy.get_distinct_collections()

    if not collections:
        click.echo("No collections with topics found. Run 'nx taxonomy discover' first.")
        return

    click.echo(f"Backfilling {len(collections)} collection(s)...")

    # ICF map computed once per backfill invocation (per RDR-077 caching).
    icf_map = taxonomy.compute_icf_map(use_cache=True) if use_icf else None

    total_assigned = 0
    total_novel = 0
    for i, src in enumerate(collections, 1):
        targets = [c for c in collections if c != src]
        if not targets:
            continue
        per_src_threshold = (
            threshold if threshold is not None
            else default_projection_threshold(src)
        )
        _progress(
            f"  [{i}/{len(collections)}] {src} → {len(targets)} target(s) "
            f"@ threshold {per_src_threshold}..."
        )
        try:
            result = taxonomy.project_against(
                src, targets, chroma_client,
                threshold=per_src_threshold, top_k=top_k,
                icf_map=icf_map,
                progress=True,
            )
            if result.get("incomplete_fetch"):
                click.echo(
                    f"    Skipped: incomplete service embedding read for {src} "
                    "(collection may be mid-index; retry later)"
                )
                continue
            matched = len(result["matched_topics"])
            novel = len(result["novel_chunks"])
            chunks = result["total_chunks"]
            click.echo(
                f"    {matched} matched topics, {novel} novel, "
                f"{chunks} chunks, {len(result.get('chunk_assignments', []))} assignments"
            )
            total_novel += novel

            if persist and result.get("chunk_assignments"):
                _persist_assignments(result["chunk_assignments"], src)
                total_assigned += len(result["chunk_assignments"])
        except Exception as e:  # noqa: BLE001 - per-item skip surfaced to user via click.echo
            click.echo(f"    Skipped: {e}")

    click.echo(
        f"Backfill complete: {total_assigned} assignments, {total_novel} novel chunks "
        f"across {len(collections)} collections."
    )


@taxonomy.command("hubs")
@click.option(
    "--min-collections", "-m", default=2, type=int, show_default=True,
    help="Minimum distinct source collections (DF) required to flag a hub.",
)
@click.option(
    "--max-icf", default=None, type=float,
    help=(
        "Only flag topics with ICF at or below this value. Lower ICF "
        "= more ubiquitous = stronger hub signal. Omit to skip ICF filter."
    ),
)
@click.option(
    "--warn-stale", is_flag=True,
    help=(
        "Flag hubs whose latest projection assignment post-dates the newest "
        "`last_discover_at` across contributing source collections (any hub "
        "with a never-discovered source is treated as stale)."
    ),
)
@click.option(
    "--explain", is_flag=True,
    help="Show why each row was flagged: DF, ICF, matched stopword tokens.",
)
def hubs_cmd(
    min_collections: int,
    max_icf: float | None,
    warn_stale: bool,
    explain: bool,
) -> None:
    """List topics that look like cross-collection hubs (RDR-077 Phase 5).

    A hub is a topic whose projection assignments span many source
    collections with low Inverse Collection Frequency — the
    taxonomic analogue of an English stopword. Output sorted by
    `chunks × (1 - ICF)` descending (worst offenders first).

    \b
    Examples:
      nx taxonomy hubs --min-collections 5 --max-icf 1.2
      nx taxonomy hubs --warn-stale --explain

    See docs/exploration/taxonomy-projection-tuning.md for guidance on interpreting
    the output and acting on flagged topics.
    """
    db = _T2Database(_default_db_path())
    try:
        rows = db.taxonomy.detect_hubs(
            min_collections=min_collections,
            max_icf=max_icf,
            warn_stale=warn_stale,
        )
        if not rows:
            click.echo("No hubs above the configured thresholds.")
            return

        click.echo(
            "TOPIC                                       DF   CHUNKS   ICF   SCORE"
        )
        click.echo("-" * 76)
        for row in rows:
            label = (row.label or f"topic-{row.topic_id}")[:38]
            click.echo(
                f"[{row.topic_id:>5}] {label:<38}"
                f"{row.distinct_source_collections:>4} {row.total_chunks:>7} "
                f"{row.icf:>5.2f} {row.score:>7.2f}"
            )
            if explain:
                parts: list[str] = [
                    f"DF={row.distinct_source_collections}",
                    f"ICF={row.icf:.3f}",
                ]
                if row.matched_stopwords:
                    parts.append(
                        "stopwords=" + ",".join(row.matched_stopwords)
                    )
                if row.source_collections:
                    parts.append(
                        "sources=" + ",".join(row.source_collections)
                    )
                click.echo("         " + " | ".join(parts))
            if warn_stale and row.is_stale:
                bits: list[str] = []
                if row.max_last_discover_at and row.last_assigned_at and (
                    row.last_assigned_at > row.max_last_discover_at
                ):
                    bits.append(
                        f"last_assigned_at={row.last_assigned_at} > "
                        f"max_last_discover_at={row.max_last_discover_at}"
                    )
                if row.never_discovered_count:
                    bits.append(
                        f"{row.never_discovered_count} never-discovered source(s)"
                    )
                click.echo(
                    "         STALE: " + "; ".join(bits)
                    if bits
                    else "         STALE"
                )
    finally:
        db.close()


@taxonomy.command("audit")
@click.option(
    "--collection", "-c", required=True,
    help="Source collection to audit (e.g. code__nexus).",
)
@click.option(
    "--threshold", "-t", default=None, type=float,
    help=(
        "Count projections whose raw cosine similarity falls below this "
        "value. Defaults to the per-corpus-type value "
        "(code__* 0.70, knowledge__* 0.50, docs__*/rdr__* 0.55). See "
        "docs/exploration/taxonomy-projection-tuning.md."
    ),
)
@click.option(
    "--top-n", "-n", default=5, type=int, show_default=True,
    help="Number of receiving hub topics to display.",
)
def audit_cmd(collection: str, threshold: float | None, top_n: int) -> None:
    """Report projection-quality diagnostics for one source collection.

    Output:
      * total projection assignments originating from this collection;
      * p10 / p50 / p90 of raw cosine similarity;
      * count of assignments below threshold (candidates for re-projection);
      * top receiving topics (where this collection's chunks land);
      * pattern-pollution: receiving topics whose labels contain generic
        stopword tokens (`assert`, `class`, `exception`, ...).

    See docs/exploration/taxonomy-projection-tuning.md for interpretation guidance.
    """
    db = _T2Database(_default_db_path())
    try:
        report = db.taxonomy.audit_collection(
            collection, threshold=threshold, top_n=top_n,
        )
        click.echo(f"Audit — {report.collection}")
        click.echo("-" * 60)
        if report.total_assignments == 0:
            click.echo("No projection data for this collection yet.")
            click.echo(
                "Run 'nx taxonomy project "
                f"{report.collection} --persist' to populate."
            )
            return

        click.echo(f"Projection assignments: {report.total_assignments}")
        click.echo(
            "Similarity quantiles (raw cosine): "
            f"p10={report.p10:.3f}  p50={report.p50:.3f}  p90={report.p90:.3f}"
        )
        click.echo(
            f"Below threshold {report.threshold}: "
            f"{report.below_threshold_count} assignment(s) — re-projection candidates"
        )

        click.echo("")
        click.echo("Top receiving topics:")
        if not report.top_receiving_hubs:
            click.echo("  (none)")
        for h in report.top_receiving_hubs:
            label = h.label or f"topic-{h.topic_id}"
            click.echo(
                f"  [{h.topic_id}] {label}  "
                f"(chunks={h.chunk_count}, icf={h.icf:.3f})"
            )

        if report.pattern_pollution:
            click.echo("")
            click.echo("Pattern-pollution (hub stopword labels):")
            for h in report.pattern_pollution:
                click.echo(
                    f"  [{h.topic_id}] {h.label} — matched: "
                    + ",".join(h.matched_stopwords)
                )
    finally:
        db.close()


# ── validate-refs (RDR-081) ───────────────────────────────────────────────────


_DEFAULT_PREFIXES = ["docs", "code", "knowledge", "rdr"]


def _resolve_prefixes(cli_override: str) -> list[str]:
    """Resolve prefix whitelist from CLI flag → config → hardcoded default.

    Priority: ``--prefixes`` → ``.nexus.yml#taxonomy.collection_prefixes`` →
    hardcoded ``[docs, code, knowledge, rdr]``.
    """
    if cli_override:
        return [p.strip() for p in cli_override.split(",") if p.strip()]
    try:
        from nexus.config import load_config  # noqa: PLC0415 - deferred to avoid circular import at module load
        cfg = load_config()
        cfg_prefixes = (cfg.get("taxonomy") or {}).get("collection_prefixes")
        if isinstance(cfg_prefixes, list) and cfg_prefixes:
            return [str(p).strip() for p in cfg_prefixes if str(p).strip()]
    except Exception as exc:  # noqa: BLE001 - best-effort prefix discovery; degrades to defaults
        _log.debug("taxonomy_prefix_discovery_failed", error=str(exc))
    return list(_DEFAULT_PREFIXES)


@taxonomy.command("validate-refs")
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=str),
    required=True,
)
@click.option(
    "--tolerance", default=0.10, type=float, show_default=True,
    help="Chunk-count match window (fractional; 0.10 = ±10%).",
)
@click.option(
    "--strict", is_flag=True, default=False,
    help="Exit non-zero on Missing refs (default: Missing is informational only).",
)
@click.option(
    "--prefixes", default="",
    help="Comma-separated prefix whitelist override (default: config or docs,code,knowledge,rdr).",
)
@click.option(
    "--format", "fmt", type=click.Choice(["table", "json"]), default="table",
    show_default=True,
)
def validate_refs_cmd(paths, tolerance, strict, prefixes, fmt):
    """Scan markdown for stale collection references (RDR-081).

    For each file, finds references like ``docs__architecture`` and
    chunk-count claims like "12,900 chunks" in the same paragraph, then
    compares against current T3 state. Reports drift.

    Exit codes:
      0 — every ref OK (or Missing without --strict)
      1 — at least one Drift (or Missing with --strict)
      2 — scanner/T3 failure
    """
    import json  # noqa: PLC0415 - branch-local; deferred to call time
    import sys  # noqa: PLC0415 - branch-local; deferred to call time

    from nexus.db import make_t3  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.doc.ref_scanner import (  # noqa: PLC0415 - deferred to avoid circular import at module load
        VERDICT_DRIFT, VERDICT_MISSING, VERDICT_OK,
        scan_markdown, validate,
    )

    resolved_prefixes = _resolve_prefixes(prefixes)

    try:
        t3 = make_t3()
    except Exception as exc:  # noqa: BLE001 - T3-unavailable surfaced to user via click.echo then clean exit
        click.echo(f"Error: T3 unavailable: {exc}", err=True)
        raise click.exceptions.Exit(2)

    try:
        all_refs = []
        for p in paths:
            from pathlib import Path as _P  # noqa: PLC0415 - branch-local; deferred to call time
            try:
                all_refs.extend(scan_markdown(_P(p), resolved_prefixes))
            except ValueError as exc:
                click.echo(f"Error: {exc}", err=True)
                raise click.exceptions.Exit(2)
            except Exception as exc:  # noqa: BLE001 - per-scanner failure surfaced to user via click.echo, continues
                click.echo(f"Warning: scanner failed on {p}: {exc}", err=True)

        drifts = validate(all_refs, t3, tolerance=tolerance)
    finally:
        # t3 is not a context manager; best-effort close if present
        close = getattr(t3, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup; non-fatal
                _log.debug("taxonomy_t3_close_failed", error=str(exc))

    # ── Render ──
    if fmt == "json":
        out = [
            {
                "path": str(d.ref.path),
                "line": d.ref.line,
                "collection": d.ref.collection,
                "prefix": d.ref.prefix,
                "claimed_count": d.ref.claimed_count,
                "actual_count": d.actual_count,
                "delta": d.delta,
                "verdict": d.verdict,
                "note": d.note,
            }
            for d in drifts
        ]
        click.echo(json.dumps(out, indent=2))
    else:
        # Table view
        if not drifts:
            click.echo("No references found.")
        else:
            click.echo(
                f"{'Verdict':<8}  {'Collection':<36}  "
                f"{'Claimed':>8}  {'Actual':>8}  {'Delta':>6}  Location"
            )
            click.echo("-" * 96)
            for d in drifts:
                claim = "-" if d.ref.claimed_count is None else f"{d.ref.claimed_count:,}"
                actual = "-" if d.actual_count is None else f"{d.actual_count:,}"
                delta = "-" if d.delta is None else f"{d.delta:+,}"
                loc = f"{d.ref.path}:{d.ref.line}"
                click.echo(
                    f"{d.verdict:<8}  {d.ref.collection:<36}  "
                    f"{claim:>8}  {actual:>8}  {delta:>6}  {loc}"
                )
            # Summary
            drift_n = sum(1 for d in drifts if d.verdict == VERDICT_DRIFT)
            missing_n = sum(1 for d in drifts if d.verdict == VERDICT_MISSING)
            ok_n = sum(1 for d in drifts if d.verdict == VERDICT_OK)
            click.echo("")
            click.echo(
                f"Summary: {ok_n} OK, {drift_n} Drift, {missing_n} Missing "
                f"(tolerance ±{tolerance:.0%})"
            )

    # ── Exit code ──
    any_drift = any(d.verdict == VERDICT_DRIFT for d in drifts)
    any_missing = any(d.verdict == VERDICT_MISSING for d in drifts)
    if any_drift or (strict and any_missing):
        raise click.exceptions.Exit(1)
    raise click.exceptions.Exit(0)


@taxonomy.command("backfill-source-collection")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Commit writes (default is dry-run). IRREVERSIBLE — review the "
         "dry-run output first.",
)
def backfill_source_collection_cmd(apply_: bool) -> None:
    """Backfill topic_assignments.source_collection for legacy hdbscan/
    centroid rows.

    RDR-087 Phase 4.1. Fills NULL source_collection by copying from
    topics.collection where the clustering path (hdbscan/centroid)
    guarantees correctness. Projection rows are untouched; auto-matched
    rows stay NULL (ambiguous source).
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.db.t2 import T2Database  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.taxonomy_backfill import backfill_source_collection  # noqa: PLC0415 - deferred to avoid circular import at module load

    db_path = default_db_path()
    if not db_path.exists():
        raise click.ClickException(f"T2 database not found: {db_path}")

    t2 = T2Database(db_path)  # epsilon-allow: backfill passes the taxonomy store across a fn boundary for a read-then-UPDATE under its _lock; not a routable single-store RPC op (RDR-128 P3 documented-irreducible)
    try:
        # Pass the store (not the raw conn) so _lock is held for the
        # read + UPDATE sequence (review gate C-1).
        report = backfill_source_collection(t2.taxonomy, apply=apply_)
    finally:
        t2.close()

    mode = "DRY-RUN" if report.dry_run else "APPLIED"
    click.echo(f"topic_assignments source_collection backfill ({mode})")
    click.echo(f"  total rows:           {report.total_rows:>8}")
    click.echo(
        f"  non-null before:      {report.non_null_before:>8}  "
        f"({report.coverage_before:.1%} coverage)"
    )
    click.echo(
        f"  eligible (NULL):      {report.eligible_rows:>8}"
    )
    for cat, count in sorted(report.eligible_by_category.items()):
        click.echo(f"    by assigned_by={cat:<10}  {count:>8}")
    if report.dry_run:
        click.echo(
            f"  projected after apply: "
            f"{report.non_null_before + report.eligible_rows:>8}  "
            f"({report.coverage_projected:.1%} coverage)"
        )
        click.echo("Run with --apply to commit.")
    else:
        click.echo(
            f"  updated:              {report.updated_rows:>8}  "
            f"({report.coverage_after:.1%} coverage)"
        )
