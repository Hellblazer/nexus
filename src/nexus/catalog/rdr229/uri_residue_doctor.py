# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The ``nx doctor`` row for ``chroma://`` residue (RDR-229 P1.2, nexus-p2m1x.2): the pre-flight that says how many
source-URI rows P2.10's repair move will meet, on this install, before it registers.

A ``chroma://<collection>/...`` source URI is migration residue under a file-routed collection (RDR-231). The RDR's
premise was that its segment always carries a ``rdr__``/``docs__``/``code__`` prefix; ``nx catalog rename-collection
--allow-legacy`` and the moves ``NoteUri.moveTo`` made disprove it (T2 ``nexus_rdr/229-research-57``). This row counts,
for ``catalog_documents``, ``document_aspects`` and ``document_highlights``:

* the ``chroma://`` rows whose segment has no file-routed prefix, split by whether the row's collection has content type
  code, docs or rdr (``file_typed``) or not (``other``: a knowledge note spelled the legacy way, or a collection with no
  registry row); and among each, the rows whose segment is not their own collection (no safe repair rule covers those);
* the ``nx-note://`` rows of a file-typed collection whose segment has no file-routed prefix, which no string rule
  tells from a note (``note-uri-001`` rewrote such residue to this spelling); and among them the rows whose segment is
  not their own collection;
* informationally, the ``chroma://`` rows whose segment IS file-routed: the residue ``nx catalog backfill-source-uri``
  already repairs.

READ ONLY, and no catalog walk (RDR-241): one engine read, ``GET /v1/catalog/uri_segment_census``, which groups the
rows server-side (:mod:`nexus.catalog.engine_reads.uri_segments`). This module classifies the groups with
``aspect_readers.FILE_ROUTED_PREFIXES`` and its segment test, so the prefix list is not copied.

GATING. The row WARNs and never fails, and is not in ``doctor._GATING_SUPPLEMENTARY_CHECKS``. RDR-229 calls it a
pre-flight, but it is information, and this row is where the residue is COUNTED: P2.10's repair moves
(``rdr229-residue-*``, and from P3.1 their replacements ``rdr229-p31-residue-*``) restore a file-routed segment where they can and skip, silently, a row whose new value a live
document already holds, so what a move leaves behind is read here, from the census, and not from the move. A FAIL would
make ``nx doctor`` exit non-zero on every install that carries any, for a state the moves may already be clearing.

One report dict, like :mod:`nexus.catalog.sensitivity_doctor`'s: ``{"name", "status", "reason", "rows_examined",
"counts", "totals"}``. ``status`` is ``not_applicable`` (a ``reason`` starting "not checked:" could not look: the
engine lacks the route, timed out, answered a capped or malformed body; any other ``not_applicable`` looked and found no
source-URI row to classify), ``pass`` or ``warn``. A row that examined nothing is never a pass.
"""
from __future__ import annotations

from typing import Any

import structlog

from nexus.aspect_readers import FILE_ROUTED_PREFIXES, _collection_is_file_routed
from nexus.catalog.engine_reads import uri_segments
from nexus.catalog.engine_reads.route_unavailable import EngineRouteUnavailable

_log = structlog.get_logger(__name__)

NAME = "uri_residue"
LABEL = "uri-residue"
FLAG = uri_segments.FLAG
NOT_APPLICABLE = "not_applicable"
PASS = "pass"
WARN = "warn"
NOT_CHECKED_PREFIX = "not checked:"

#: The engine release that carries ``GET /v1/catalog/uri_segment_census``.
ROUTE_ENGINE = "0.1.200"

#: A collection is file-typed when its content type's name prefix (``<content_type>__``) is file-routed: the types
#: of :data:`FILE_ROUTED_PREFIXES`, never a second list.
FILE_TYPED_CONTENT_TYPES: frozenset[str] = frozenset(p.removesuffix("__") for p in FILE_ROUTED_PREFIXES)

#: The key of the ``other_by_content_type`` bucket for a collection whose registry row is missing or has no content type.
NO_CONTENT_TYPE = "(no content type)"

_COUNT_PAIR = {"rows": 0, "segment_not_collection": 0}


def _pair() -> dict[str, int]:
    return dict(_COUNT_PAIR)


def _empty_table() -> dict[str, Any]:
    return {
        "chroma_prefix_free": {"file_typed": _pair(), "other": _pair(), "other_by_content_type": {}},
        "note_prefix_free_in_file_typed": _pair(),
        "chroma_file_routed_segment": 0,
    }


def classify(groups: list[dict]) -> tuple[dict[str, dict], dict[str, Any], int]:
    """``(per-table counts, totals, rows examined)`` for the engine's groups. Pure: no reader, no I/O."""
    counts = {t: _empty_table() for t in uri_segments.TABLES}
    examined = 0
    for grp in groups:
        rows, segment = grp["rows"], grp["segment"]
        examined += rows
        table = counts[grp["table"]]
        file_routed = _collection_is_file_routed(segment)
        file_typed = grp["content_type"] in FILE_TYPED_CONTENT_TYPES
        if grp["scheme"] == "chroma":
            if file_routed:
                table["chroma_file_routed_segment"] += rows
                continue
            bucket = table["chroma_prefix_free"]["file_typed" if file_typed else "other"]
            if not file_typed:
                # keyed by the content type the engine reports, so a row with no registry row ('') is visible
                by_type = table["chroma_prefix_free"]["other_by_content_type"].setdefault(
                    grp["content_type"] or NO_CONTENT_TYPE, _pair())
                by_type["rows"] += rows
                if segment != grp["collection"]:
                    by_type["segment_not_collection"] += rows
        else:
            if file_routed or not file_typed:
                continue
            bucket = table["note_prefix_free_in_file_typed"]
        bucket["rows"] += rows
        if segment != grp["collection"]:
            bucket["segment_not_collection"] += rows
    totals = {
        "chroma_prefix_free_file_typed": _pair(),
        "chroma_prefix_free_other": _pair(),
        "chroma_prefix_free_other_by_content_type": {},
        "note_prefix_free_in_file_typed": _pair(),
        "chroma_file_routed_segment": 0,
    }
    for table in counts.values():
        totals["chroma_file_routed_segment"] += table["chroma_file_routed_segment"]
        for key, src in (("chroma_prefix_free_file_typed", table["chroma_prefix_free"]["file_typed"]),
                         ("chroma_prefix_free_other", table["chroma_prefix_free"]["other"]),
                         ("note_prefix_free_in_file_typed", table["note_prefix_free_in_file_typed"])):
            for k in _COUNT_PAIR:
                totals[key][k] += src[k]
        for ct, pair in table["chroma_prefix_free"]["other_by_content_type"].items():
            agg = totals["chroma_prefix_free_other_by_content_type"].setdefault(ct, _pair())
            for k in _COUNT_PAIR:
                agg[k] += pair[k]
    return counts, totals, examined


def _report(status: str, reason: str, *, examined: int = 0, counts: dict | None = None, totals: dict | None = None) -> dict:
    return {"name": NAME, "status": status, "reason": reason, "rows_examined": examined,
            "counts": counts or {}, "totals": totals or {}}


def _not_checked(why: str) -> dict:
    return _report(NOT_APPLICABLE, f"{NOT_CHECKED_PREFIX} {why}")


def is_not_checked(report: dict) -> bool:
    """True when the row could not look (as opposed to looking and finding no source-URI row)."""
    return report["status"] == NOT_APPLICABLE and report["reason"].startswith(NOT_CHECKED_PREFIX)


def _unavailable(exc: EngineRouteUnavailable) -> str:
    if exc.reason == EngineRouteUnavailable.UNSUPPORTED:
        return (f"this engine does not answer GET /v1/catalog{uri_segments.ROUTE}; engine-service {ROUTE_ENGINE} or later "
                f"does (it advertises {FLAG} on GET /version), or, for a cloud client, the conexus edge does not yet list "
                f"{FLAG}. Re-run after the engine upgrade; the catalog is not walked instead.")
    return exc.describe() + "; re-run to retry."


def check_uri_residue(*, reader: Any = None) -> dict:
    """The row. Best-effort and never raises: a catalog that cannot be opened, an engine without the route, a timeout, a
    capped or malformed answer are each ``not_applicable`` with a "not checked:" reason, never a pass."""
    if reader is None:
        try:
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred to avoid circular import

            reader = make_catalog_reader()
        except Exception as exc:  # noqa: BLE001 — best-effort: must not crash `nx doctor`
            _log.debug("doctor_uri_residue_catalog_unavailable", error=str(exc))
            return _not_checked("the catalog could not be opened.")
        if reader is None:
            return _not_checked("there is no catalog.")
    try:
        read = reader.uri_segment_census
    except AttributeError:
        return _not_checked("this client's catalog reader cannot ask for the census.")
    except Exception as exc:  # noqa: BLE001 — the production handle builds its client on first attribute read and re-raises a failed build
        _log.debug("doctor_uri_residue_reader_unavailable", error=str(exc))
        return _not_checked("the catalog could not be opened.")
    try:
        body = read(uri_segments.CENSUS_MAX)
    except EngineRouteUnavailable as exc:
        return _not_checked(_unavailable(exc))
    except Exception as exc:  # noqa: BLE001 — a failed read is "cannot say", never "none"
        _log.debug("doctor_uri_residue_read_failed", error=str(exc))
        return _not_checked("the census read failed; re-run to retry.")
    if body.get("capped"):
        return _not_checked(f"the engine's answer holds more than {uri_segments.CENSUS_MAX} groups, so its counts would "
                            "be lower bounds.")
    counts, totals, examined = classify(body["groups"])
    if examined == 0:
        return _report(NOT_APPLICABLE, "no catalog row carries a chroma:// or nx-note:// source_uri", counts=counts,
                       totals=totals)
    repairable = totals["chroma_prefix_free_file_typed"]["rows"] + totals["note_prefix_free_in_file_typed"]["rows"]
    if repairable == 0:
        return _report(PASS, f"no chroma:// or nx-note:// residue under a file-typed collection; examined {examined} "
                             "source-URI row(s)", examined=examined, counts=counts, totals=totals)
    ft, nt = totals["chroma_prefix_free_file_typed"], totals["note_prefix_free_in_file_typed"]
    return _report(
        WARN,
        f"{ft['rows']} chroma:// row(s) with a prefix-free segment under a code/docs/rdr collection "
        f"({ft['segment_not_collection']} whose segment is not its own collection) and {nt['rows']} nx-note:// row(s) "
        f"of such a collection with a prefix-free segment ({nt['segment_not_collection']} whose segment is not its own "
        f"collection); examined {examined} source-URI row(s)",
        examined=examined, counts=counts, totals=totals)


def _pair_text(p: dict) -> str:
    return f"{p['rows']} (segment is not its collection: {p['segment_not_collection']})"


def _by_type_text(by_type: dict) -> str:
    return ", ".join(f"{ct}: {_pair_text(p)}" for ct, p in sorted(by_type.items())) or "none"


def render_text(report: dict, *, limit: int = 50) -> list[str]:  # noqa: ARG001 — same signature as the sibling rows
    """The row as text lines: one verdict line, then, for a finding or a pass, the per-table counts; a finding also
    says what clears them."""
    status = report["status"]
    if status == NOT_APPLICABLE:
        head = "not checked" if is_not_checked(report) else "N/A"
        return [f"{LABEL}: {head} ({report['reason'].removeprefix(NOT_CHECKED_PREFIX).strip()})"]
    lines = [f"{LABEL}: {'PASS' if status == PASS else 'WARN'} ({report['reason']})"]
    for table, c in report["counts"].items():
        pf = c["chroma_prefix_free"]
        lines.append(f"  {table}:")
        lines.append(f"    chroma:// with a prefix-free segment, code/docs/rdr collection: {_pair_text(pf['file_typed'])}")
        lines.append(f"    chroma:// with a prefix-free segment, any other collection: {_pair_text(pf['other'])}"
                     f" [by content type: {_by_type_text(pf['other_by_content_type'])}]")
        lines.append(f"    nx-note:// with a prefix-free segment, code/docs/rdr collection: "
                     f"{_pair_text(c['note_prefix_free_in_file_typed'])}")
        lines.append(f"    chroma:// with a file-routed segment (nx catalog backfill-source-uri repairs these): "
                     f"{c['chroma_file_routed_segment']}")
    if status == WARN:
        lines.append(
            "  RDR-229 P2.10's repair moves (data moves rdr229-residue-*, run by the engine in the background) restore a "
            "file-routed segment to the rows whose segment is their own collection; a row whose new value a live document "
            "holds is skipped, and the rest stay in this count. From P3.1 an open one of those moves is superseded by a "
            "replacement that also sets the collection id (rdr229-p31-residue-catalog-documents, "
            "rdr229-p31-residue-document-aspects, rdr229-p31-residue-document-highlights), so on an upgraded install the "
            "move that runs carries the rdr229-p31-residue-* label. Re-run this row after the moves complete: what it still "
            "counts is what they did not clear. This row never fails the sweep.")
    return lines
