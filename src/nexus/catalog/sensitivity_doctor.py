# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The two doctor rows of RDR-243 Technical Design item 10 (nexus-rfvyy.6.17), shared by ``nx catalog
doctor`` and ``nx doctor`` so the two surfaces cannot drift apart.

* ``sensitive_in_aspect_eligible`` (WARN): a sensitive document whose collection has an aspect extractor
  (``select_config``) or already holds aspect rows. Nothing has leaked yet; the document is exposed to the
  next index run.
* ``sensitive_with_aspects`` (FAIL): a sensitive document that already has an aspect row or a queued task.
  Extracted aspects are LLM output over the document's text, so this is the leaked-before-flagged case
  (RDR-243 Failure Modes); the remedy line is ``nx enrich delete --tumbler <t>``.

Each check returns one report dict::

    {"name", "status", "reason", "sensitive_documents", "findings": [{"tumbler", "collection", "why"}]}

``status`` is ``not_applicable``, ``pass``, ``warn`` or ``fail``. A tenant with no sensitive document, an
engine that does not advertise ``catalog_sensitivity_supported``, and a catalog that cannot be read are all
``not_applicable`` and never ``pass``: a row that examined nothing proves nothing (the nexus-7zhag and
nexus-moht0 doctrines), and a row that read nothing must not go red on a virgin box. ``fail`` and ``warn``
need a sensitive document actually met.

The collaborators are injectable (``reader``, ``aspects``, ``queue``, ``select``) for tests; the defaults
are the production handles. Reading the sensitive set walks the catalog's documents once (the engine has
no sensitive filter on ``/list``), which a person running a doctor verb has asked for.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

NOT_APPLICABLE = "not_applicable"
PASS = "pass"
WARN = "warn"
FAIL = "fail"

IN_ELIGIBLE = "sensitive_in_aspect_eligible"
WITH_ASPECTS = "sensitive_with_aspects"


def _report(name: str, status: str, reason: str = "", total: int = 0, findings: list[dict] | None = None) -> dict:
    return {"name": name, "status": status, "reason": reason, "sensitive_documents": total,
            "findings": findings or []}


def _sensitive_documents(name: str, reader: Any) -> tuple[list[Any], dict | None]:
    """``(documents, None)`` or ``([], a not-applicable report)``."""
    try:
        if reader is None:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred, factory has a heavy import graph

            reader = make_catalog_reader()
        if reader is None:
            return [], _report(name, NOT_APPLICABLE, "no catalog is available")
        probe = getattr(reader, "catalog_sensitivity_probe", None)
        if callable(probe):
            supported, version = probe()
            if supported is not True:
                what = f"engine {version} does not advertise catalog_sensitivity_supported" if version else \
                    "the engine's capabilities could not be read"
                return [], _report(name, NOT_APPLICABLE, what)
        documents = [e for e in reader.all_documents(limit=0) if getattr(e, "sensitive", False) is True]
    except Exception as exc:  # noqa: BLE001 — an unreadable catalog is not a finding; the row is not applicable, never red
        _log.warning("sensitivity_doctor_catalog_unreadable", check=name, error=str(exc))
        return [], _report(name, NOT_APPLICABLE, f"the catalog could not be read ({type(exc).__name__}: {exc})")
    if not documents:
        return [], _report(name, NOT_APPLICABLE, "no sensitive documents in this catalog")
    return documents, None


def check_sensitive_in_aspect_eligible(
    *, reader: Any = None, aspects: Any = None, select: Callable[[str], Any] | None = None,
) -> dict:
    """The WARN row: sensitive documents in a collection that has an extractor or aspect rows."""
    documents, na = _sensitive_documents(IN_ELIGIBLE, reader)
    if na is not None:
        return na
    try:
        if select is None:
            from nexus.aspect_extractor import select_config as select  # noqa: PLC0415 — deferred, the extractor module has a heavy import graph
        if aspects is None:
            from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore  # noqa: PLC0415 — deferred, CLI startup cost

            aspects = HttpDocumentAspectsStore()
        why_by_collection: dict[str, str] = {}
        for collection in sorted({d.physical_collection for d in documents}):
            reasons = []
            if select(collection) is not None:
                reasons.append("the collection has an aspect extractor")
            if aspects.list_by_collection(collection, limit=1):
                reasons.append("the collection already holds aspect rows")
            if reasons:
                why_by_collection[collection] = " and ".join(reasons)
    except Exception as exc:  # noqa: BLE001 — cannot tell: not applicable with the cause, never a false red or a false pass
        _log.warning("sensitivity_doctor_eligible_unreadable", error=str(exc))
        return _report(IN_ELIGIBLE, NOT_APPLICABLE, f"the aspect state could not be read ({type(exc).__name__}: {exc})",
                       len(documents))
    findings = [
        {"tumbler": str(d.tumbler), "collection": d.physical_collection, "why": why_by_collection[d.physical_collection]}
        for d in documents if d.physical_collection in why_by_collection
    ]
    return _report(IN_ELIGIBLE, WARN if findings else PASS, "", len(documents), findings)


def _queued_doc_ids(queue: Any) -> set[str]:
    """Every doc id with a task in either side of the aspect queue: the old table's waiting and failed
    rows, and the ``aspects`` tuple subspace (every claim state, a dead task included)."""
    from nexus.commands.aspects import _task_identity  # noqa: PLC0415 — deferred, the verb module has a heavy import graph
    from nexus.db.t2.http_aspect_queue import (  # noqa: PLC0415 — deferred, CLI startup cost
        ASPECTS_KEYS,
        ASPECTS_SUBSPACE,
        AspectQueueListWaitingUnsupportedError,
        aspects_census,
    )

    ids: set[str] = set()
    try:
        waiting = queue.list_waiting()
    except AspectQueueListWaitingUnsupportedError:
        waiting = queue.list_pending()
    ids.update(r.doc_id for r in waiting if r.doc_id)
    ids.update(r.doc_id for r in queue.list_failed() if r.doc_id)

    tuples = queue._tuples()
    census = aspects_census(tuples)
    if census is not None and (census.available + census.claimed + census.dead) > 0:
        since = None
        while True:
            page = tuples.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=300, since=since)
            if not page:
                break
            for row in page:
                ident = _task_identity(row)
                if ident is not None:
                    ids.add(str(ident["doc_id"]))
            cursor = (page[-1].created_at, page[-1].id)
            if cursor == since:  # an engine that ignored `since` would loop forever
                break
            since = cursor
    return ids


def check_sensitive_with_aspects(
    *, reader: Any = None, aspects: Any = None, queue: Any = None,
    queued_doc_ids: Callable[[Any], set[str]] = _queued_doc_ids,
) -> dict:
    """The FAIL row: sensitive documents that already have an aspect row or a queued task."""
    documents, na = _sensitive_documents(WITH_ASPECTS, reader)
    if na is not None:
        return na
    try:
        if aspects is None:
            from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore  # noqa: PLC0415 — deferred, CLI startup cost

            aspects = HttpDocumentAspectsStore()
        if queue is None:
            from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 — deferred, CLI startup cost

            queue = HttpAspectQueue()
        queued = queued_doc_ids(queue)
        findings = []
        for d in documents:
            tumbler = str(d.tumbler)
            whys = []
            if aspects.get_by_doc_id(tumbler) is not None:
                whys.append("has an aspect row")
            if tumbler in queued:
                whys.append("has a queued aspect task")
            if whys:
                findings.append({"tumbler": tumbler, "collection": d.physical_collection, "why": " and ".join(whys)})
    except Exception as exc:  # noqa: BLE001 — cannot tell: not applicable with the cause, never a false red or a false pass
        _log.warning("sensitivity_doctor_with_aspects_unreadable", error=str(exc))
        return _report(WITH_ASPECTS, NOT_APPLICABLE, f"the aspect state could not be read ({type(exc).__name__}: {exc})",
                       len(documents))
    return _report(WITH_ASPECTS, FAIL if findings else PASS, "", len(documents), findings)


def remedy_lines(report: dict) -> list[str]:
    """The commands that clear a ``sensitive_with_aspects`` finding, one per offending document."""
    if report["name"] != WITH_ASPECTS or report["status"] != FAIL:
        return []
    return [f"nx enrich delete --tumbler {f['tumbler']}" for f in report["findings"]]


def render_text(report: dict, *, limit: int = 50) -> list[str]:
    """The row as printable lines. The label, not-applicable wording and remedy lines are the same on both
    doctor surfaces because both print this."""
    name, status = report["name"], report["status"]
    if status == NOT_APPLICABLE:
        return [f"[ ] {name}: not applicable - {report['reason']}"]
    total = report["sensitive_documents"]
    if status == PASS:
        return [f"[✓] {name}: none of {total} sensitive document(s)"]
    mark = "[!]" if status == WARN else "[✗]"
    word = "WARN" if status == WARN else "FAIL"
    findings = report["findings"]
    lines = [f"{mark} {name}: {word} - {len(findings)} of {total} sensitive document(s)"]
    for f in findings[:limit]:
        lines.append(f"    {f['tumbler']}  {f['collection']}  {f['why']}")
    if len(findings) > limit:
        lines.append(f"    ... and {len(findings) - limit} more")
    if status == FAIL:
        lines.append("  Extracted aspects are model output over a sensitive document. Remove them:")
        remedies = remedy_lines(report)
        lines.extend(f"    {c}" for c in remedies[:limit])
        if len(remedies) > limit:
            lines.append(f"    ... and {len(remedies) - limit} more")
    else:
        lines.append("  The next index run would extract aspects from these. Opt the collection out of extraction "
                     "or accept the exposure knowingly.")
    return lines

