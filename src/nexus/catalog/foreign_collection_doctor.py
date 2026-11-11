# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``nx doctor`` row for documents whose manifest names chunks outside their own collection (GH #1611).

A repo index (``nx index repo``) used to resolve a file onto a catalog document another owner held (a curator's
``nx index pdf <file> --collection X``) and write the file's chunks into the repo's collection under that document's
tumbler. The document kept ``physical_collection`` = X, but its manifest now named chunks in the repo's collection:
a search scoped to X misses them and X's live count falls short of its stored count. The write path no longer does
this; this row finds the documents already damaged, through one bounded engine read
(``GET /v1/catalog/manifest/foreign_collection``).

Kept out of :mod:`nexus.health` (over its file-size cap); the sweep calls :func:`check_manifest_foreign_collection`.
"""
from __future__ import annotations

from typing import Any

import structlog

_log = structlog.get_logger(__name__)

LABEL = "manifest chunks outside the document's collection"

#: Documents named in the row; the total is always given.
MAX_NAMED_DOCUMENTS = 10

#: The newest engine tag that does NOT carry the route (engine-service-v0.1.188). While ``REQUIRED_ENGINE_VERSION`` is
#: at or below it, a read that cannot say is the expected state, not a defect.
_ROUTE_ABSENT_AT_OR_BELOW: tuple[int, int, int] = (0, 1, 188)

#: How many documents one read lists (the engine's default; the total comes back whatever this is).
_READ_LIMIT = 1000


def _document_line(doc: dict) -> str:
    others = ", ".join(doc["collections"])
    return f"{doc['tumbler']} (in {doc['physical_collection']}) has {doc['chunks']} chunk(s) in {others}"


def check_manifest_foreign_collection() -> list[Any]:
    """One row: documents whose manifest chunks are not in their ``physical_collection``.

    Best-effort and never raises: a catalog that cannot be opened is a skip, and a read that cannot say (an engine
    without the route, a statement timeout, a malformed answer) is never rendered as "none". Its severity follows the
    engine floor, as ``health._check_manifest_null_collection``'s does: informational while ``REQUIRED_ENGINE_VERSION``
    is at or below the newest engine without the route, a WARN once it is above.

    READ-ONLY. The fix text says what each command does and what it leaves (see :func:`_fixes`).
    """
    from nexus.engine_version import REQUIRED_ENGINE_VERSION  # noqa: PLC0415 — stdlib-only leaf
    from nexus.health import HealthResult  # noqa: PLC0415 — deferred: health imports this module's caller

    try:
        from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

        cat = make_catalog_reader()
        if cat is None:
            return [HealthResult(label=LABEL, ok=True, detail="skipped (no catalog)")]
    except Exception as exc:  # noqa: BLE001 — best-effort: must not crash `nx doctor`
        _log.debug("doctor_foreign_collection_catalog_unavailable", error=str(exc))
        return [HealthResult(label=LABEL, ok=True, detail="skipped (catalog unavailable)")]

    try:
        report = cat.manifest_foreign_collection(_READ_LIMIT)
    except Exception as exc:  # noqa: BLE001 — a reader without the method (older client, test double) cannot say
        _log.debug("doctor_foreign_collection_read_failed", error=str(exc))
        report = None

    if report is None:
        if REQUIRED_ENGINE_VERSION <= _ROUTE_ABSENT_AT_OR_BELOW:
            return [HealthResult(
                label=LABEL, ok=True,
                detail=(
                    "informational: could not be read. The engine predates GET /v1/catalog/manifest/foreign_collection "
                    f"(REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} is at or below the last engine without it), "
                    "so this is expected, not a defect; re-run after the next engine tag."
                ),
            )]
        return [HealthResult(
            label=LABEL, ok=False, warn=True,
            detail=(
                "UNKNOWN: could not read which documents have manifest chunks outside their collection, though "
                f"REQUIRED_ENGINE_VERSION={REQUIRED_ENGINE_VERSION} should carry GET "
                "/v1/catalog/manifest/foreign_collection. The read failed or timed out on this catalog."
            ),
        )]

    documents = report["documents"]
    total = report["count"]
    if total == 0:
        return [HealthResult(label=LABEL, ok=True, detail="none")]

    shown = documents[:MAX_NAMED_DOCUMENTS]
    more = total - len(shown)
    detail = (
        f"{total} document(s) have manifest chunks outside their physical_collection, so a search scoped to that "
        "collection misses those chunks and its live count falls short of its stored count (GH #1611): "
        + "; ".join(_document_line(d) for d in shown)
        + (f" (+{more} more)" if more > 0 else "")
    )
    return [HealthResult(label=LABEL, ok=False, warn=True, detail=detail, fix_suggestions=_fixes(shown[0]))]


def _fixes(doc: dict) -> list[str]:
    """The repair, step by step, for the first document named; each line says what the command does and leaves.

    ``nx index pdf --force`` re-chunks the file, re-sends every chunk to the collection named, and writes the
    document's manifest as a REPLACE of every row it had, whichever collection those rows named (the engine's
    ``writeManifest`` deletes all rows of the document, then inserts), so the manifest names the right collection
    afterwards. It does not touch the chunks in the other collection (its orphan clean-up reads the target collection
    only). Those chunks then have no manifest owner; the engine moves such a chunk to quarantine once it has been
    ownerless 30 days (``nx t3 gc -c``, or the prune at the end of ``nx index repo`` of that collection), and it
    expires later. Nothing deletes them sooner.
    """
    home, other = doc["physical_collection"], doc["collections"][0]
    return [
        f"nx catalog show {doc['tumbler']}   (names the document's source file; repeat for each document listed)",
        f"nx index pdf <that file> --collection {home} --force   (for a Markdown document, nx index md): re-sends "
        f"the document's chunks to {home} and replaces ALL of its manifest rows with rows naming them. It does not "
        f"remove the copy in {other}",
        f"nx t3 gc -c {other} --dry-run   (lists the chunks in {other} that have had no manifest owner for 30 days, "
        "so right after the re-index it lists none of the stray copy; the stray copy stays until it passes that "
        "grace, then gc moves it to quarantine, and it expires later)",
    ]


def parse_foreign_collection_body(result: Any) -> dict | None:
    """The checked body of ``GET /v1/catalog/manifest/foreign_collection`` for
    :meth:`~nexus.catalog.http_catalog_client.HttpCatalogClient.manifest_foreign_collection`, or ``None`` ("cannot say")
    when it is malformed or inconsistent: an uncapped list whose length is not ``count``, a nonzero count listing
    nothing, or a document with no other collection."""
    if not isinstance(result, dict):
        return None
    count, capped, documents = result.get("count"), result.get("capped"), result.get("documents")

    def _is_count(v: Any) -> bool:
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0

    well_formed = (
        _is_count(count) and isinstance(capped, bool) and isinstance(documents, list)
        and all(
            isinstance(d, dict) and isinstance(d.get("tumbler"), str) and isinstance(d.get("physical_collection"), str)
            and isinstance(d.get("collections"), list) and bool(d["collections"])
            and all(isinstance(c, str) for c in d["collections"])
            and _is_count(d.get("chunks"))
            for d in documents
        )
        and (capped or len(documents) == count)
        and (bool(documents) or count == 0)  # a nonzero count with nothing listed cannot be shown
    )
    if not well_formed:
        _log.warning("http_catalog_client.manifest_foreign_collection_malformed", keys=sorted(result))
        return None
    return {
        "count": count, "capped": capped,
        "documents": [
            {"tumbler": d["tumbler"], "physical_collection": d["physical_collection"],
             "collections": list(d["collections"]), "chunks": d["chunks"]}
            for d in documents
        ],
    }
