# SPDX-License-Identifier: AGPL-3.0-or-later
"""Collection-rename data-plane operation.

nexus-8g79.10 (V5): hosted at the package root (peer to ``indexer``,
``health``, ``pdf_extractor``) so library-layer callers — the indexer's
RDR-103 Phase 5 conformant-shape migrator at ``indexer.py:435`` — can
invoke without reaching up into ``nexus.commands.collection``.

The function raises ``ClickException`` when the server-side rename fails
as part of its documented contract (CG-1, nexus-nhyh). That keeps the CLI
surface usable; non-CLI callers can catch ``click.ClickException``
or any other exception type as they prefer.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import click

from nexus.db.collection_state import CollectionState, probe_collection_state


def rename_summary_parts(counts: dict[str, int]) -> list[str]:
    """The aspect-family clauses of a rename summary (nexus-l0ai3).

    ``aspect_tasks`` is the engine's ``aspects_tasks`` count: the unconsumed
    ``aspects`` task bodies the rename rewrites to name the new collection
    (RDR-235). The ``aspect_extraction_queue`` count is gone with the table
    (nexus-hbnml). Zero or absent counts (an engine predating a key) print
    nothing. Shared by ``nx collection
    rename`` and ``nx catalog rename-collection`` so they cannot drift.
    """
    parts: list[str] = []
    if counts.get("aspect_tasks"):
        parts.append(f"{counts['aspect_tasks']} aspect tasks")
    return parts


def _engine_model_mismatch_detail(exc: Exception) -> str | None:
    """The engine's text when *exc* is its 409 ``collection_model_mismatch``
    (nexus-ucagi), else ``None``. The bare ``HTTPStatusError`` string is only
    the status line; the body names both models and the name that would be
    accepted."""
    import json  # noqa: PLC0415 — stdlib, branch-local

    from nexus.db.engine_reasons import COLLECTION_MODEL_MISMATCH_REASON  # noqa: PLC0415 — deferred: error path only

    response = getattr(exc, "response", None)
    if response is None or getattr(response, "status_code", None) != 409:
        return None
    try:
        body = json.loads(response.text)
    except Exception:  # noqa: BLE001 — an unparseable body is simply not ours
        return None
    if not isinstance(body, dict) or body.get("reason") != COLLECTION_MODEL_MISMATCH_REASON:
        return None
    return str(body.get("error") or COLLECTION_MODEL_MISMATCH_REASON)


def _engine_refusal_detail(exc: Exception) -> str | None:
    """The status line and the engine's reason when *exc* is a 4xx whose JSON body carries ``error`` (nexus-02454),
    else ``None``. ``str(HTTPStatusError)`` is only httpx's status line, so a label the engine refused read as
    ``400 Bad Request`` with no reason. A 5xx body is the engine's own failure (the server log has it), not a reason
    to show."""
    import json  # noqa: PLC0415 — stdlib, branch-local

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if not isinstance(status, int) or not 400 <= status < 500:
        return None
    try:
        body = json.loads(response.text)
    except Exception:  # noqa: BLE001 — an unparseable body carries no reason
        return None
    reason = body.get("error") if isinstance(body, dict) else None
    if not isinstance(reason, str) or not reason:
        return None
    return f"{exc}: {reason}"


def rename_collection_data_plane(
    old: str,
    new: str,
    *,
    t3_db: Any,
    catalog: Any | None = None,
    on_warn: Callable[[str], None] | None = None,
    check_model_token: bool = True,
) -> dict[str, int]:
    """Rename a collection in one atomic server-side re-home.

    The engine's ``rename_collection_cascade`` is a single transaction
    (``CatalogRepository.renameCollection``) over the T3 chunks, the chash
    index, taxonomy, the aspect family, telemetry, and the catalog documents
    and registry row. A failure leaves the collection fully unchanged and
    raises ``ClickException`` with a non-zero exit code and an actionable
    message: it is not fail-open. (nexus-i711w removed the client-side
    fan-out, T2 cascade first and T3 last, that this function used to run.)
    The engine decides the rename's path inside that transaction from its
    gate 3 latch row (RDR-229 P3.5a), and may answer a typed 409
    (``rename_refused``, ``rename_path_mismatch``); the client reads it as a
    failed rename.

    Returns a dict with counts per cascade table so callers can render
    their own summaries.

    ``on_warn`` is the warning sink (the CLI default routes it to
    ``click.echo(..., err=True)``); the server-side rename has no fail-open
    leg today, so nothing calls it.

    nexus-8g79.10 (V5): ``t3_db`` is now required (no ``_t3()`` default).
    CLI callers pass ``_t3()`` explicitly; library callers (the
    indexer's RDR-103 P5 migrator) construct their own T3 handle.

    nexus-ucagi: a conformant *new* name's model token is sent to the engine,
    which refuses (409 ``collection_model_mismatch``, nothing written) a token
    that disagrees with the source row's ``embedding_model``. That makes every
    caller of this function guarded, not only the CLI verbs that check first.
    ``check_model_token=False`` withholds the token: the operator override
    ``nx collection rename --force-prefix-change`` documents, for a row that is
    the wrong one.
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
    #     ("nx catalog restore the trashed document(s) first"); say so instead
    #     (nexus-dkymw: the verb nexus-xavu7 found missing now exists).
    #   - new PRESENT or TOMBSTONED -> refuse. This rename path has no
    #     escape valve, so
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
            f"trashed document) — run `nx catalog trash` to see it, then "
            f"`nx catalog restore <tumbler>` to bring the document(s) back "
            f"before renaming."
        )
    new_state = probe_collection_state(t3_db, new)
    if new_state is not CollectionState.ABSENT:
        raise click.ClickException(f"collection already exists: {new!r}")
    # RDR-229 P3.6: the target is a label this call creates (an existing one was refused just above), so the label
    # rule judges it here, ahead of the engine, which enforces the same rule and stays the authority.
    from nexus.corpus import validate_collection_name  # noqa: PLC0415 — deferred: nexus.corpus imports back into this package
    try:
        validate_collection_name(new)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    counts = {
        "tax_topics": 0,
        "tax_assignments": 0,
        "tax_meta": 0,
        "tax_centroids": 0,
        "chash": 0,
        "aspects": 0,
        "aspect_tasks": 0,
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
        if check_model_token:
            renamed = client.rename_collection_cascade(old, new)
        else:
            renamed = client.rename_collection_cascade(
                old, new, check_model_token=False,
            )
    except Exception as exc:
        detail = _engine_model_mismatch_detail(exc) or _engine_refusal_detail(exc) or str(exc)
        raise click.ClickException(
            f"service rename failed -- collection {old!r} is unchanged "
            f"(the re-home is atomic). Fix the error and retry:\n  {detail}"
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
    # nexus-l0ai3: the unconsumed aspects task bodies the rename rewrote.
    counts["aspect_tasks"] = renamed.get("aspects_tasks", 0)
    counts["highlights"] = renamed.get("document_highlights", 0)
    counts["relevance_log"] = renamed.get("relevance_log", 0)
    counts["search_telemetry"] = renamed.get("search_telemetry", 0)
    counts["hook_failures"] = renamed.get("hook_failures", 0)
    counts["catalog_docs"] = renamed.get("catalog_documents", 0)
    return counts
