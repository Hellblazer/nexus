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

    if not t3_db.collection_exists(old):
        raise click.ClickException(f"collection not found: {old!r}")
    if t3_db.collection_exists(new):
        raise click.ClickException(f"collection already exists: {new!r}")

    counts = {
        "tax_topics": 0,
        "tax_assignments": 0,
        "tax_meta": 0,
        "chash": 0,
        "aspects": 0,
        "aspect_queue": 0,
        "search_telemetry": 0,
        "hook_failures": 0,
        "catalog_docs": 0,
    }

    # ── T2 cascade FIRST (reversible) ────────────────────────────────────────
    # Failure here raises ClickException -- T3 rename has not yet run so the
    # system remains fully consistent. Operator can diagnose and retry.
    from nexus.config import default_db_path  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415

    try:
        with T2Database(default_db_path()) as t2db:
            cascade = t2db.rename_collection_cascade(old=old, new=new)
            counts["tax_topics"] = cascade.get("tax_topics", 0)
            counts["tax_assignments"] = cascade.get("tax_assignments", 0)
            counts["tax_meta"] = cascade.get("tax_meta", 0)
            counts["chash"] = cascade.get("chash", 0)
            counts["aspects"] = cascade.get("aspects", 0)
            counts["aspect_queue"] = cascade.get("aspect_queue", 0)
            counts["search_telemetry"] = cascade.get("search_telemetry", 0)
            counts["hook_failures"] = cascade.get("hook_failures", 0)
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
            from nexus.catalog.catalog import Catalog  # noqa: PLC0415
            from nexus.config import catalog_path  # noqa: PLC0415
            cat_path = catalog_path()
            catalog = Catalog(cat_path, cat_path / ".catalog.db")
        counts["catalog_docs"] = catalog.rename_collection(old, new)
    except Exception as exc:
        on_warn(f"warn: T2+T3 rename succeeded but catalog cascade failed: {exc}")

    return counts
