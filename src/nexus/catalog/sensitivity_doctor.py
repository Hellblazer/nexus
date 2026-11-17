# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The doctor rows of RDR-243 Technical Design item 10 (nexus-rfvyy.6.17, nexus-rfvyy.8.7), shared by ``nx catalog
doctor`` and ``nx doctor`` so the two surfaces cannot drift apart.

* ``sensitive_in_aspect_eligible`` (WARN): a sensitive document whose collection has an aspect extractor
  (``select_config``) or already holds aspect rows. Nothing has leaked yet; the document is exposed to the
  next index run.
* ``sensitive_with_aspects`` (FAIL): a sensitive document that already has an aspect row or a queued task.
  Extracted aspects are LLM output over the document's text, so this is the leaked-before-flagged case
  (RDR-243 Failure Modes); the remedy line is ``nx enrich delete --tumbler <t>``.

* ``sensitive_in_external_embedder`` (WARN): a sensitive document in a collection whose embedder is an
  external service (a Voyage model token, or a legacy name that carries none). The text of the document
  left the machine to be embedded, or will on its next index run.
* ``sensitivity_mcp_output`` (Day 2 Operations, Verify column): reports the active ``sensitivity.mcp_output``
  value, ``withhold`` (the default) or ``flag``, and WARNs on ``flag`` (sensitive content reaches the session
  model) or on a value it does not recognise. A config row, not a document row: always applicable.

Each check returns one report dict::

    {"name", "status", "reason", "sensitive_documents", "findings": [{"tumbler", "collection", "why"}]}

``status`` is ``not_applicable``, ``pass``, ``warn`` or ``fail``. A tenant with no sensitive document, an
engine that does not advertise ``catalog_sensitivity_supported``, and a catalog that cannot be read are all
``not_applicable`` and never ``pass``: a row that examined nothing proves nothing (the nexus-7zhag and
nexus-moht0 doctrines), and a row that read nothing must not go red on a virgin box. ``fail`` and ``warn``
need a sensitive document actually met. A ``not_applicable`` row whose reason starts "not checked:" could not
look (:func:`is_not_checked`); "no sensitive documents in this catalog" looked and found none.

The collaborators are injectable (``reader``, ``aspects``, ``queue``, ``select``) for tests; the defaults
are the production handles. The sensitive set comes from the engine's bounded list (``GET /v1/catalog/sensitive``,
engine-service 0.1.188 adds it) and only from it (RDR-241): when the list is unavailable (the engine does not
advertise it, the route answers 404 or 503, a reader without it) or capped, each row is ``not_applicable`` with
a reason that starts "not checked:", names the cause and says what to do. No row ever walks the catalog. A
"not checked" row never gates ``nx doctor``; ``nx catalog doctor --sensitive-with-aspects`` exits
``EXIT_COULD_NOT_CHECK`` for it.
"""
from __future__ import annotations

import contextlib
import contextvars
from types import SimpleNamespace
from collections.abc import Callable, Iterator
from typing import Any

import structlog

from nexus._log_safe_url import scrub_transport_error

_log = structlog.get_logger(__name__)

NOT_APPLICABLE = "not_applicable"
PASS = "pass"
WARN = "warn"
FAIL = "fail"

IN_ELIGIBLE = "sensitive_in_aspect_eligible"
WITH_ASPECTS = "sensitive_with_aspects"
IN_EXTERNAL_EMBEDDER = "sensitive_in_external_embedder"
MCP_OUTPUT = "sensitivity_mcp_output"

MCP_OUTPUT_VALUES = frozenset({"withhold", "flag"})


def _report(name: str, status: str, reason: str = "", total: int = 0, findings: list[dict] | None = None) -> dict:
    return {"name": name, "status": status, "reason": reason, "sensitive_documents": total,
            "findings": findings or []}


class SensitiveListUnavailable(Exception):
    """``HttpCatalogClient.sensitive_documents(strict=True)`` got no answer; :attr:`reason` says why (nexus-uwggq.1.7).

    A caller that must not scan the catalog instead (``nx catalog list --sensitive``) tells the causes apart: an
    engine without the route is a message about the engine, a timeout or a failed probe is a retry.
    """

    #: The engine answered ``GET /version`` without ``catalog_sensitive_list_supported``, or the route answers 404.
    UNSUPPORTED = "unsupported"
    #: ``GET /version`` itself failed, so no capability could be read; the engine said nothing about this route.
    PROBE_FAILED = "probe_failed"
    #: The route answered 503: the engine's own statement timeout, or the service unavailable.
    TIMEOUT = "timeout"
    #: Any other status, or a transport error.
    FAILED = "failed"
    #: The body is not the route's shape.
    MALFORMED = "malformed"

    def __init__(self, reason: str) -> None:
        super().__init__(f"the engine's sensitive list is unavailable ({reason})")
        self.reason = reason


def sensitive_answer(result: object) -> dict | None:
    """The route's body as ``{"count", "capped", "documents": [{"tumbler", "physical_collection"}]}``, or ``None`` when
    it is malformed or inconsistent (an uncapped list whose length is not ``count``)."""
    if not isinstance(result, dict):
        return None
    count, capped, documents = result.get("count"), result.get("capped"), result.get("documents")
    well_formed = (
        not isinstance(count, bool) and isinstance(count, int) and count >= 0 and isinstance(capped, bool)
        and isinstance(documents, list)
        # A document flagged before it was indexed has no collection: the engine sends null, read as "",
        # as _to_entry reads it for the walk.
        and all(isinstance(d, dict) and isinstance(d.get("tumbler"), str) and "physical_collection" in d
                and isinstance(d["physical_collection"], (str, type(None))) for d in documents)
        and (capped or len(documents) == count)
    )
    if not well_formed:
        return None
    return {
        "count": count, "capped": capped,
        "documents": [{"tumbler": d["tumbler"], "physical_collection": d.get("physical_collection") or ""}
                      for d in documents],
    }


#: The most sensitive documents the bounded route is asked for: the engine's own maximum (CatalogSensitiveReads.
#: MAX_LIMIT). The route reads the whole sensitive set from its index whatever the limit, so asking for fewer
#: saves nothing. A tenant with more reads "not checked": the rows do not walk the catalog to cover the rest.
_SENSITIVE_LIST_LIMIT = 10_000

#: Every reason that says a row could not look starts with this; :func:`is_not_checked` reads it back.
NOT_CHECKED_PREFIX = "not checked:"

#: The "not checked" reason for each way the engine's list fails to answer (nexus-uwggq.1.8). ``UNSUPPORTED`` is
#: the one cause about the engine. ``PROBE_FAILED`` and ``TIMEOUT`` can pass on a second run. ``FAILED`` and
#: ``MALFORMED`` also cover deterministic causes (a 401 or 403, a persistent 5xx, a body that is not the route's
#: shape), so their text sends the reader to the token and the engine's logs when a second run does not help.
_NOT_CHECKED_BY_CAUSE: dict[str, str] = {
    SensitiveListUnavailable.UNSUPPORTED: (
        f"{NOT_CHECKED_PREFIX} the engine does not offer the sensitive list (a local engine needs engine-service "
        "0.1.188 or later; on a managed engine, ask the engine's operator to allow it at the public edge)"
    ),
    SensitiveListUnavailable.PROBE_FAILED: (
        f"{NOT_CHECKED_PREFIX} the engine's capabilities could not be read; run nx doctor again"
    ),
    SensitiveListUnavailable.TIMEOUT: (
        f"{NOT_CHECKED_PREFIX} the engine did not answer in time or was unavailable; run nx doctor again"
    ),
    SensitiveListUnavailable.FAILED: (
        f"{NOT_CHECKED_PREFIX} the engine could not be reached or failed; run nx doctor again; "
        "if it persists, check the engine's logs or the token"
    ),
    SensitiveListUnavailable.MALFORMED: (
        f"{NOT_CHECKED_PREFIX} the engine's answer could not be read; run nx doctor again; "
        "if it persists, check the engine's logs or the token"
    ),
}
NOT_CHECKED_UNSUPPORTED = _NOT_CHECKED_BY_CAUSE[SensitiveListUnavailable.UNSUPPORTED]
NOT_CHECKED_CAPPED = (
    f"{NOT_CHECKED_PREFIX} more than {_SENSITIVE_LIST_LIMIT:,} sensitive documents; "
    "list them with nx catalog list --sensitive --owner <owner>"
)


def is_not_checked(report: dict) -> bool:
    """Did the row fail to look (as opposed to looking and finding no sensitive document)?"""
    return report["status"] == NOT_APPLICABLE and str(report["reason"]).startswith(NOT_CHECKED_PREFIX)


def _scrubbed(exc: BaseException, reader: Any) -> str:
    """*exc*'s text through the transport scrubber (it can quote a URL's credentials or a bearer token); the
    reader's own base URL, when it has one, is what the scrubber checks the text against."""
    # Called from inside except handlers: the production reader is a lazy handle whose attribute read builds the
    # service client and re-raises a build failure that is not an AttributeError, so getattr's default never
    # applies. Nothing read here may raise out of the handler it serves.
    try:
        url = getattr(reader, "_base_url", "")
    except Exception:  # noqa: BLE001 — a reader that cannot even name its URL scrubs against none
        url = ""
    return scrub_transport_error(exc, url if isinstance(url, str) else "")


#: The read shared inside :func:`one_walk`: ``(documents, not-applicable reason)``, or unset outside one.
_SHARED: contextvars.ContextVar[dict[str, tuple[list[Any], str | None]] | None] = contextvars.ContextVar(
    "sensitivity_doctor_shared_walk", default=None,
)


@contextlib.contextmanager
def one_walk() -> Iterator[None]:
    """Inside this scope the rows that find their own catalog share one read of the sensitive set (F30,
    nexus-f3yqd.6): the engine's bounded list. There is no walk to fall back on (RDR-241): where the list is
    unavailable or capped the shared answer is the "not checked" reason. A row given its own *reader* still
    reads it alone. The result lives only for the scope, so a later run sees a fresh catalog."""
    token = _SHARED.set({})
    try:
        yield
    finally:
        _SHARED.reset(token)


def _sensitive_documents(name: str, reader: Any) -> tuple[list[Any], dict | None]:
    """``(documents, None)`` or ``([], a not-applicable report)``."""
    shared = _SHARED.get() if reader is None else None
    if shared is not None and "read" in shared:
        documents, reason = shared["read"]
        return (documents, None) if reason is None else ([], _report(name, NOT_APPLICABLE, reason))
    documents, reason = _read_sensitive(name, reader)
    if shared is not None:
        shared["read"] = (documents, reason)
    return (documents, None) if reason is None else ([], _report(name, NOT_APPLICABLE, reason))


def _bounded_sensitive(reader: Any) -> tuple[dict | None, str]:
    """``(answer, "")``, or ``(None, the "not checked" reason)`` if the reader has no such read, it fails, or it
    cannot say. The read is strict: the client names why it has no answer (:class:`SensitiveListUnavailable`) and
    the reason says that cause, not a guess. There is no walk behind it."""
    bounded = getattr(reader, "sensitive_documents", None)
    if not callable(bounded):
        return None, NOT_CHECKED_UNSUPPORTED
    try:
        answer = bounded(limit=_SENSITIVE_LIST_LIMIT, strict=True)
    except SensitiveListUnavailable as exc:
        _log.info("sensitivity_doctor_sensitive_list_unavailable", cause=exc.reason)
        return None, _NOT_CHECKED_BY_CAUSE.get(exc.reason, _NOT_CHECKED_BY_CAUSE[SensitiveListUnavailable.FAILED])
    except Exception as exc:  # noqa: BLE001 — best-effort read; a failure is "not checked", never "no sensitive documents"
        _log.info("sensitivity_doctor_bounded_read_failed", error=_scrubbed(exc, reader))
        return None, _NOT_CHECKED_BY_CAUSE[SensitiveListUnavailable.FAILED]
    if isinstance(answer, dict):
        return answer, ""
    return None, _NOT_CHECKED_BY_CAUSE[SensitiveListUnavailable.MALFORMED]


def _read_sensitive(name: str, reader: Any) -> tuple[list[Any], str | None]:
    """The sensitive documents, or the reason the rows are not applicable. Makes no catalog walk."""
    try:
        if reader is None:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred, factory has a heavy import graph

            reader = make_catalog_reader()
        if reader is None:
            return [], f"{NOT_CHECKED_PREFIX} no catalog is available; run nx doctor again once the service is running"
        probe = getattr(reader, "catalog_sensitivity_probe", None)
        if callable(probe):
            supported, version = probe()
            if supported is not True:
                # Two inputs reach here and the text for each must be true for every case of it. A version with
                # no flag: the engine predates the column, or a managed edge trims the flag. No version: the
                # probe got no /version answer, or the answer names no version (both read ``(False, "")``).
                what = (
                    f"engine {version} does not advertise catalog_sensitivity_supported; a local engine needs a "
                    "newer engine-service, and on a managed engine the public edge may trim the flag (ask the "
                    "engine's operator)" if version else
                    "the engine's /version answer could not be read or names no version, so "
                    "catalog_sensitivity_supported could not be confirmed; run nx doctor again; "
                    "if it persists, check the engine's logs or the token"
                )
                return [], f"{NOT_CHECKED_PREFIX} {what}"
        # F30 (nexus-f3yqd.6): an engine with the bounded route answers in one indexed read. Where it does not
        # answer, or the list is capped, the rows are "not checked": RDR-241 removed the catalog walk that stood here.
        answer, cause = _bounded_sensitive(reader)
        if answer is None:
            return [], cause
        if answer.get("capped") is not False:
            return [], NOT_CHECKED_CAPPED
        documents = [
            SimpleNamespace(tumbler=d["tumbler"], physical_collection=d["physical_collection"], sensitive=True)
            for d in answer.get("documents", [])
        ]
    except Exception as exc:  # noqa: BLE001 — an unreadable catalog is not a finding; the row is not applicable, never red
        detail = _scrubbed(exc, reader)
        _log.warning("sensitivity_doctor_catalog_unreadable", check=name, error=detail)
        return [], f"{NOT_CHECKED_PREFIX} the catalog could not be read ({type(exc).__name__}: {detail}); run nx doctor again"
    if not documents:
        return [], "no sensitive documents in this catalog"
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
        detail = _scrubbed(exc, aspects)
        _log.warning("sensitivity_doctor_eligible_unreadable", error=detail)
        return _report(IN_ELIGIBLE, NOT_APPLICABLE,
                       f"{NOT_CHECKED_PREFIX} the aspect state could not be read ({type(exc).__name__}: {detail}); "
                       "run nx doctor again", len(documents))
    findings = [
        {"tumbler": str(d.tumbler), "collection": d.physical_collection, "why": why_by_collection[d.physical_collection]}
        for d in documents if d.physical_collection in why_by_collection
    ]
    return _report(IN_ELIGIBLE, WARN if findings else PASS, "", len(documents), findings)


def _queued_doc_ids(queue: Any) -> set[str]:
    """Every doc id with a live task (available or claimed) in the ``aspects`` tuple subspace. A dead
    task is left out: ``nx aspects requeue-failed`` never revives the task of a flagged document, it
    carries identity and no text, and retention clears it, so counting it would hold the row at FAIL
    with no remedy. The old queue table and its read routes are retired (nexus-hbnml); rows an engine
    still holds in a kept table are drained into the subspace at engine start, so they show here after."""
    from nexus.commands.aspects import _task_identity  # noqa: PLC0415 — deferred, the verb module has a heavy import graph
    from nexus.db.t2.http_aspect_queue import (  # noqa: PLC0415 — deferred, CLI startup cost
        ASPECTS_KEYS,
        ASPECTS_SUBSPACE,
        aspects_census,
    )

    ids: set[str] = set()
    tuples = queue._tuples()
    census = aspects_census(tuples)
    if census is not None and (census.available + census.claimed) > 0:
        since = None
        while True:
            page = tuples.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=300, since=since)
            if not page:
                break
            for row in page:
                if row.claim_state == "dead":
                    continue
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
        detail = _scrubbed(exc, aspects)
        _log.warning("sensitivity_doctor_with_aspects_unreadable", error=detail)
        return _report(WITH_ASPECTS, NOT_APPLICABLE,
                       f"{NOT_CHECKED_PREFIX} the aspect state could not be read ({type(exc).__name__}: {detail}); "
                       "run nx doctor again", len(documents))
    return _report(WITH_ASPECTS, FAIL if findings else PASS, "", len(documents), findings)


def _external_embedder(collection: str) -> str | None:
    """The external model token a collection embeds with, or ``None`` when it embeds locally. A conformant
    name carries its model token; a token outside :data:`nexus.corpus.LOCAL_EMBEDDING_MODELS` is Voyage. A
    legacy name carries none and counts as Voyage (``voyage_model_for_collection`` is its only dispatch)."""
    from nexus.catalog.collection_name import CollectionName  # noqa: PLC0415 — deferred: leaf value object
    from nexus.corpus import (  # noqa: PLC0415 — deferred, corpus has a heavy import graph
        LOCAL_EMBEDDING_MODELS,
        voyage_model_for_collection,
    )

    # The value object is the one sanctioned parse of a candidate name (the same rule the write verbs use in
    # nexus.sensitive_write); a name it cannot parse is a legacy one and reads as Voyage.
    try:
        token = CollectionName.parse(collection).embedding_model
    except ValueError:
        return voyage_model_for_collection(collection)
    return None if token in LOCAL_EMBEDDING_MODELS else token


def check_sensitive_in_external_embedder(*, reader: Any = None) -> dict:
    """The WARN row: sensitive documents in a collection embedded by an external service."""
    documents, na = _sensitive_documents(IN_EXTERNAL_EMBEDDER, reader)
    if na is not None:
        return na
    findings = []
    for d in documents:
        model = _external_embedder(d.physical_collection)
        if model is not None:
            findings.append({"tumbler": str(d.tumbler), "collection": d.physical_collection,
                             "why": f"embedded by an external service ({model})"})
    return _report(IN_EXTERNAL_EMBEDDER, WARN if findings else PASS, "", len(documents), findings)


def check_sensitivity_mcp_output(*, value: str | None = None) -> dict:
    """The ``sensitivity.mcp_output`` row: the posture the MCP tools apply. *value* is injectable for tests; the
    default goes through :func:`nexus.sensitive_output.mcp_output_setting`, the tools' own reader (global config
    only; an absent key is ``withhold``, the RDR's default; an unknown or unreadable value is ``withhold``)."""
    from nexus.sensitive_output import McpOutputSetting, mcp_output_setting, posture_of  # noqa: PLC0415 — deferred, CLI startup cost

    setting = McpOutputSetting(posture_of(value), value) if value is not None else mcp_output_setting()
    if setting.error:
        _log.warning("sensitivity_doctor_config_unreadable", error=setting.error)
        return {**_report(MCP_OUTPUT, WARN, f"the config could not be read ({setting.error}); the tools withhold"),
                "value": setting.posture}
    raw = setting.raw
    if raw is not None and (not isinstance(raw, str) or raw.strip().lower() not in MCP_OUTPUT_VALUES):
        return {**_report(MCP_OUTPUT, WARN, f"unrecognised value {raw!r}; expected withhold or flag; the tools withhold"),
                "value": setting.posture}
    return {**_report(MCP_OUTPUT, WARN if setting.posture == "flag" else PASS), "value": setting.posture}


def remedy_lines(report: dict) -> list[str]:
    """The commands that clear a ``sensitive_with_aspects`` finding, one per offending document."""
    if report["name"] != WITH_ASPECTS or report["status"] != FAIL:
        return []
    return [f"nx enrich delete --tumbler {f['tumbler']}" for f in report["findings"]]


def render_text(report: dict, *, limit: int = 50) -> list[str]:
    """The row as printable lines. The label, not-applicable wording and remedy lines are the same on both
    doctor surfaces because both print this."""
    name, status = report["name"], report["status"]
    if name == MCP_OUTPUT:
        mark = "[✓]" if status == PASS else "[!]"
        lines = [f"{mark} {name}: {report['value']}" + (" - WARN" if status == WARN else "")]
        if report["reason"]:
            lines.append(f"    {report['reason']}")
        if report["value"] == "flag":
            lines.append("  Sensitive content is returned to the session model with a [sensitive] badge. "
                         "Set sensitivity.mcp_output to withhold to stop that.")
        return lines
    if is_not_checked(report):
        return [f"[ ] {name}: {report['reason']}"]
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
    elif name == IN_EXTERNAL_EMBEDDER:
        lines.append("  These documents' text is sent to an external embedder. Re-index them into a local-model "
                     "collection of the same subject, or accept the exposure knowingly.")
    else:
        lines.append("  The next index run would extract aspects from these. Opt the collection out of extraction "
                     "or accept the exposure knowingly.")
    return lines

