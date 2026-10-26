# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 1 (nexus-jypmj.1.4): the named primary source, resolved and read.

Two stages, both bounded and both disclosing what they could not do:

1. :func:`resolve_named_sources` turns a :class:`~nexus.plans.evidence.SourceRef` plus the
   owner qualifier into ``resolved``, ``candidates`` or ``unresolved``. The candidate set
   is one capped full-text ``find`` per source (the RDR number, then the legacy ``prose``
   registrations only if the RDR registrations have none). ``rdr_canonical`` supplies the
   identity and the no-guess rule: basename identity (``rdr_key_of``, ``group_rdr_candidates``)
   and ``resolve_canonical_tumbler`` with the qualified owner as the admitted owner. It never
   calls the unbounded listings that rule's old caller used, nor ``resolve_tumbler``'s first
   exact match. A candidate set that reaches its cap is never resolved: the missing
   candidate could be a second one.
2. :func:`select_primary_evidence` reads a resolved document's ordered manifest, hydrates
   its chunks (capped, shared chashes fetched once), labels each chunk, and selects a
   section-diverse subset under the evidence budget.

Who may label a chunk (research 7 and 18, nexus-tko2w): a manifest row's ``section_title``
counts only where ``labels_written`` is true, and then an empty string means "no label
here". Everywhere else (an engine without the catalog-046 columns, a legacy row, a quarantine
restore) the label is derived from the chunk's own text in manifest order: a Markdown
heading starts a section and the following chunks inherit it. The T3 chunk row's own
``section_title`` is never read: identical text in two documents is one row whose metadata is
the last writer's, so :func:`t3_chunk_fetcher` hands the selector text and nothing else.

Every limit is a field of :class:`PrimaryEvidenceCaps`, the one calibration object
(:data:`DEFAULT_CAPS`). The reserved share (``max_selected_items``) stays below the
operator input cap (``runner._OPERATOR_MAX_INPUTS``, 100) so plan evidence keeps room.
Merging this evidence ahead of the plan's inputs is :mod:`nexus.plans.reserved_stream`
(nexus-jypmj.1.6); this module only produces it.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

import structlog

from nexus.catalog.rdr_canonical import (
    group_rdr_candidates,
    rdr_key_of,
    resolve_canonical_tumbler,
)
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, ManifestRow
from nexus.plans.evidence import EvidenceRequest, OwnerResolution, SourceRef

_log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_CAPS",
    "ChunkFetcher",
    "ContentLabel",
    "DocumentCoverage",
    "NamedSourceCatalog",
    "NamedSourceResolution",
    "PrimaryEvidence",
    "PrimaryEvidenceCaps",
    "ResolvedSource",
    "SelectedChunk",
    "SourceCandidate",
    "derive_content_labels",
    "gather_primary_evidence",
    "resolve_named_sources",
    "select_primary_evidence",
    "t3_chunk_fetcher",
]


# ── Caps: the one calibration object ─────────────────────────────────────────


@dataclass(frozen=True)
class PrimaryEvidenceCaps:
    """Every bound on named-source resolution and primary evidence, in one place.

    Initial values are calibration parameters (RDR-232 section 2), derived from the
    operator and bundle limits, not constants the RDR locks. A document is ~1-3 KB a
    chunk, so ``max_manifest_rows`` x chunk size is what ``max_fetched_chars`` backs up.
    """

    #: Named sources resolved per request; the rest are dropped and disclosed.
    max_named_sources: int = 6
    #: ``limit`` on every candidate ``find``. A reply at the cap is "possibly truncated".
    max_candidates_per_lookup: int = 100
    #: Manifest rows hydrated across ALL sources; a larger manifest is sampled, not cut.
    max_manifest_rows: int = 300
    #: Manifest rows KEPT after the catalog read, across all sources, before sorting and
    #: sampling (nexus-jypmj.1.6). ``get_manifests`` has no row limit, so this bounds what the
    #: rest of the pipeline holds, not the reply; a longer manifest is spread-sampled.
    max_manifest_rows_read: int = 3000
    #: Characters fetched from T3 across all sources (stops at the first batch past it).
    max_fetched_chars: int = 600_000
    #: Unique chunks reserved for the primary stream (< ``runner._OPERATOR_MAX_INPUTS``).
    max_selected_items: int = 24
    #: Characters in the selected evidence.
    max_selected_chars: int = 60_000
    #: Chashes per T3 fetch.
    fetch_batch_ids: int = 100
    #: Seconds named-source resolution may take, and the same again for the primary read. The caller's
    #: own deadline (``nx_answer``'s ``budget_seconds``) can make either shorter; neither runs on the
    #: event loop, and a hit is disclosed (the answer proceeds without the stream).
    max_seconds: float = 20.0

    def to_wire(self) -> dict[str, int]:
        return asdict(self)


DEFAULT_CAPS = PrimaryEvidenceCaps()


# ── Narrow ports (the real catalog and T3 satisfy them) ──────────────────────


class NamedSourceCatalog(Protocol):
    """The only catalog calls this module makes."""

    def find(self, query: str, *, content_type: str | None = ..., limit: int = ...) -> list[CatalogEntry]: ...
    def get_owner_by_prefix(self, tumbler_prefix: str) -> dict | None: ...
    def resolve(self, tumbler: Any, *, follow_alias: bool = ...) -> CatalogEntry | None: ...
    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[ManifestRow]]: ...


#: ``(collection, chashes) -> {chash: text}``. Text only: the T3 chunk row's metadata
#: (``section_title`` and the rest) is the last writer's and never crosses this boundary.
ChunkFetcher = Callable[[str, Sequence[str]], Mapping[str, str]]


def t3_chunk_fetcher(t3: Any) -> ChunkFetcher:
    """A :data:`ChunkFetcher` over the T3 client, through ``nx_answer``'s batched fetch."""

    def fetch(collection: str, chashes: Sequence[str]) -> dict[str, str]:
        from nexus.mcp.core import _batched_get_by_ids  # noqa: PLC0415 — deferred: core imports the plans package

        found = _batched_get_by_ids(t3, collection, list(chashes))
        return {cid: str(entry["content"]) for cid, entry in found.items() if entry.get("content")}

    return fetch


# ── Resolution ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SourceCandidate:
    tumbler: str
    title: str
    file_path: str
    owner: str
    content_type: str
    collection: str

    @classmethod
    def of(cls, entry: CatalogEntry) -> SourceCandidate:
        return cls(
            tumbler=str(entry.tumbler), title=entry.title, file_path=entry.file_path,
            owner=str(entry.tumbler.owner_address()), content_type=entry.content_type,
            collection=entry.physical_collection,
        )


@dataclass(frozen=True)
class ResolvedSource:
    """One named source's outcome: ``resolved`` (``tumbler`` set), ``candidates`` (several
    documents fit; nothing picked) or ``unresolved`` (nothing fits, or the lookup could not
    be trusted). ``qualified`` says an owner qualifier narrowed the choice."""

    ref: SourceRef
    status: Literal["resolved", "candidates", "unresolved"]
    tumbler: str | None = None
    title: str = ""
    file_path: str = ""
    collection: str = ""
    chunk_count: int = 0
    qualified: bool = False
    candidates: tuple[SourceCandidate, ...] = ()
    candidates_truncated: bool = False
    detail: str = ""
    #: The RDR number the resolved document's own path carries (``docs/rdr/rdr-084-....md``), or
    #: ``None`` when the document is not an RDR file. The lifecycle stream (nexus-jypmj.1.7) keys on it.
    rdr_number: int | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "source": self.ref.to_wire(),
            "status": self.status,
            "tumbler": self.tumbler,
            "title": self.title,
            "file_path": self.file_path,
            "qualified": self.qualified,
            "candidates": [asdict(c) for c in self.candidates],
            "candidates_truncated": self.candidates_truncated,
            "detail": self.detail,
            "rdr_number": self.rdr_number,
        }


@dataclass(frozen=True)
class NamedSourceResolution:
    sources: tuple[ResolvedSource, ...] = ()
    #: Values of the sources beyond ``max_named_sources``, not resolved.
    dropped: tuple[str, ...] = ()
    catalog_calls: int = 0
    caps: PrimaryEvidenceCaps = DEFAULT_CAPS

    def resolved(self) -> tuple[ResolvedSource, ...]:
        return tuple(s for s in self.sources if s.status == "resolved")

    @classmethod
    def deadline_hit(
        cls, sources: Sequence[SourceRef], *, caps: PrimaryEvidenceCaps = DEFAULT_CAPS,
    ) -> NamedSourceResolution:
        """Every source unresolved at the deadline: a catalog call that did not return in time (a thread
        cannot be cancelled), so nothing is known about any of them."""
        refs = list(sources)
        kept, dropped = refs[: caps.max_named_sources], refs[caps.max_named_sources:]
        return cls(
            sources=tuple(_unresolved(r, _deadline_detail(f"the lookup of {r.value}", caps)) for r in kept),
            dropped=tuple(r.value for r in dropped), catalog_calls=0, caps=caps,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "sources": [s.to_wire() for s in self.sources],
            "dropped": list(self.dropped),
            "catalog_calls": self.catalog_calls,
        }

    def disclosures(self, *, owner_failure_disclosed: bool = False, primary_stream: bool = True) -> list[str]:
        """One-line text notices for what the request could not honour.

        *owner_failure_disclosed*: the request's own notice already states a failed owner
        qualifier, so a source unresolved only for that reason is not repeated (the structured
        envelope still carries it per source). *primary_stream*: the plan has a primary evidence
        stream; on a single-step plan it has none, and the notice says so rather than imply the
        document is read as evidence.
        """
        lines: list[str] = []
        for s in self.sources:
            if s.status == "resolved" and not s.qualified:
                tail = (
                    "" if primary_stream else
                    "; this single-step plan has no primary evidence stream, so the document is not read as evidence"
                )
                lines.append(
                    f"[evidence request: named source {s.ref.value} resolved to {s.tumbler} but was not "
                    f"owner-qualified (pass owner or project to pin it){tail}]"
                )
            elif s.status == "candidates":
                names = ", ".join(c.tumbler for c in s.candidates)
                lines.append(
                    f"[evidence request: named source {s.ref.value} is ambiguous: "
                    f"{len(s.candidates)} candidates ({names}); {s.detail}]"
                )
            elif s.status == "unresolved":
                if owner_failure_disclosed and s.detail.startswith(OWNER_FAILURE_PREFIX):
                    continue
                lines.append(f"[evidence request: named source {s.ref.value} unresolved: {s.detail}]")
        if self.dropped:
            lines.append(
                f"[evidence request: {len(self.dropped)} named source(s) beyond the cap of "
                f"{self.caps.max_named_sources} were dropped: {', '.join(self.dropped)}]"
            )
        return lines


_RDR_KEY_NUMBER = re.compile(r"^rdr-0*(\d+)(?:-|$)")


def _key_number(entry: CatalogEntry) -> int | None:
    key = rdr_key_of(entry)
    m = _RDR_KEY_NUMBER.match(key) if key else None
    return int(m.group(1)) if m else None


def _comparable(a: Tumbler, b: Tumbler) -> bool:
    """One is a segment prefix of the other."""
    return a.is_prefix_of(b) or b.is_prefix_of(a)


@dataclass(frozen=True)
class _Qualifier:
    owner: Tumbler
    document: Tumbler | None  # set when the qualifier is document depth (scope 1.2.4686)

    @property
    def text(self) -> str:
        return str(self.document or self.owner)


class LookupDeadline(Exception):  # noqa: N818 — a control-flow signal, caught where a catalog failure is
    """Raised before a catalog call that the deadline no longer leaves time for."""


def _deadline_detail(what: str, caps: PrimaryEvidenceCaps) -> str:
    return (
        f"{what} stopped at the deadline (at most {caps.max_seconds:g} s, or the answer's own budget if that "
        "was shorter); nothing was resolved"
    )


class _Calls:
    """Counts the catalog calls one resolution makes, and starts none past its deadline."""

    def __init__(
        self, cat: NamedSourceCatalog | None, *, end: float | None = None,
        clock: Callable[[], float] = time.monotonic, caps: PrimaryEvidenceCaps = DEFAULT_CAPS,
    ) -> None:
        self.cat = cat
        self.n = 0
        self.end, self.clock, self.caps = end, clock, caps

    def _gate(self) -> None:
        if self.end is not None and self.clock() >= self.end:
            raise LookupDeadline

    def failed(self, what: str, exc: Exception) -> str:
        """The ``detail`` for a catalog call that raised *exc*."""
        if isinstance(exc, LookupDeadline):
            return _deadline_detail(f"the lookup of {what}", self.caps)
        return f"catalog lookup of {what} failed ({type(exc).__name__})"

    def find(self, query: str, content_type: str, limit: int) -> list[CatalogEntry]:
        assert self.cat is not None
        self._gate()
        self.n += 1
        return list(self.cat.find(query, content_type=content_type, limit=limit))

    def owner(self, prefix: str) -> dict | None:
        assert self.cat is not None
        self._gate()
        self.n += 1
        return self.cat.get_owner_by_prefix(prefix)

    def resolve(self, tumbler: str) -> CatalogEntry | None:
        assert self.cat is not None
        self._gate()
        self.n += 1
        return self.cat.resolve(tumbler)


#: Start of a per-source ``detail`` that repeats the request's own owner-qualifier failure.
OWNER_FAILURE_PREFIX = "owner qualifier "


def _qualifier(owner: OwnerResolution, calls: _Calls) -> tuple[_Qualifier | None, str]:
    """``(qualifier, "")`` or ``(None, "")`` when none was given, or ``(None, why)`` when the
    qualifier cannot be honoured. A qualifier that cannot be honoured is never loosened."""
    if owner.status == "none":
        return None, ""
    if owner.status != "resolved" or not owner.tumbler:
        return None, f"{OWNER_FAILURE_PREFIX}{owner.status}: {owner.detail}".rstrip(": ")
    if calls.cat is None:
        return None, "the catalog is unavailable, so the owner qualifier cannot be checked"
    tumbler = Tumbler.parse(owner.tumbler)
    owner_addr = tumbler.owner_address()
    try:
        row = calls.owner(str(owner_addr))
    except Exception as exc:  # noqa: BLE001 — the disclosure stage never raises; a failed probe is reported
        _log.warning("named_source_owner_probe_failed", owner=str(owner_addr), error=str(exc))
        return None, calls.failed(f"owner {owner_addr}", exc)
    if row is None:
        return None, (
            f"owner {owner_addr} is not in the catalog (the qualifier {owner.tumbler} names no "
            "registered owner); nothing was searched"
        )
    return _Qualifier(owner_addr, tumbler if tumbler.depth >= 3 else None), ""


def _unresolved(ref: SourceRef, detail: str, **kw: Any) -> ResolvedSource:
    return ResolvedSource(ref=ref, status="unresolved", detail=detail, **kw)


def _resolved(ref: SourceRef, entry: CatalogEntry, qualified: bool,
              candidates: tuple[SourceCandidate, ...] = ()) -> ResolvedSource:
    return ResolvedSource(
        ref=ref, status="resolved", tumbler=str(entry.tumbler), title=entry.title,
        file_path=entry.file_path, collection=entry.physical_collection,
        chunk_count=entry.chunk_count, qualified=qualified, candidates=candidates,
        rdr_number=_key_number(entry),
    )


def _resolve_tumbler(ref: SourceRef, qualifier: _Qualifier | None, calls: _Calls) -> ResolvedSource:
    try:
        entry = calls.resolve(ref.value)
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        _log.warning("named_source_resolve_failed", source=ref.value, error=str(exc))
        return _unresolved(ref, calls.failed(ref.value, exc))
    if entry is None:
        return _unresolved(ref, f"document {ref.value} is not in the catalog")
    if qualifier is not None:
        inside = entry.tumbler.owner_address() == qualifier.owner and (
            qualifier.document is None or _comparable(entry.tumbler, qualifier.document)
        )
        if not inside:
            return _unresolved(
                ref, f"document {entry.tumbler} lies outside the qualifier {qualifier.text}; nothing was substituted",
            )
    return _resolved(ref, entry, qualifier is not None)


def _resolve_rdr(ref: SourceRef, qualifier: _Qualifier | None, calls: _Calls, caps: PrimaryEvidenceCaps) -> ResolvedSource:
    number = ref.number or 0
    query = f"RDR-{number:03d}"
    found: list[CatalogEntry] = []
    truncated = False
    try:
        for content_type in ("rdr", "prose"):  # legacy prose registrations only if there are no rdr ones
            rows = calls.find(query, content_type, caps.max_candidates_per_lookup)
            truncated = truncated or len(rows) >= caps.max_candidates_per_lookup
            found = [e for e in rows if not e.alias_of and _key_number(e) == number]
            if found:
                break
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        _log.warning("named_source_find_failed", source=ref.value, error=str(exc))
        return _unresolved(ref, calls.failed(ref.value, exc))

    scoped = found
    foreign = 0
    if qualifier is not None:
        scoped = [
            e for e in found
            if e.tumbler.owner_address() == qualifier.owner
            and (qualifier.document is None or _comparable(e.tumbler, qualifier.document))
        ]
        foreign = len(found) - len(scoped)
    candidates = tuple(SourceCandidate.of(e) for e in scoped)
    where = f" under {qualifier.text}" if qualifier is not None else ""
    note = f" ({foreign} in other projects ignored)" if foreign else ""

    if truncated:
        return _unresolved(
            ref,
            f"the candidate lookup for {ref.value} reached its cap of {caps.max_candidates_per_lookup} "
            "and may be hiding a second candidate; name the document by its tumbler",
            candidates=candidates, candidates_truncated=True,
        )
    groups = group_rdr_candidates(scoped)
    if not groups:
        return _unresolved(ref, f"no registered document for {ref.value}{where}{note}")
    if len(groups) > 1:
        return ResolvedSource(
            ref=ref, status="candidates", candidates=candidates, qualified=qualifier is not None,
            detail=(f"{len(groups)} documents carry the number {ref.value}{where}; "
                    "name the document by its tumbler as the source"),
        )
    (key, group), = groups.items()
    owners = {e.tumbler.owner_address() for e in group}
    if len(owners) > 1:
        return ResolvedSource(
            ref=ref, status="candidates", candidates=candidates, qualified=False,
            detail=(f"{ref.value} exists in {len(owners)} projects ({', '.join(sorted(str(o) for o in owners))}); "
                    "pass owner or project to choose"),
        )
    # One basename identity under one owner: the canonical rule decides among the registrations
    # (an rdr registration beats a legacy prose one; two of a kind are not guessed between).
    owner = qualifier.owner if qualifier is not None else next(iter(owners))
    winner = resolve_canonical_tumbler(group, owner, repo_source_prefix="", rdr_key=key)
    if winner is None:
        return ResolvedSource(
            ref=ref, status="candidates", candidates=candidates, qualified=qualifier is not None,
            detail=f"{ref.value} is registered more than once{where}; name the document by its tumbler as the source",
        )
    entry = next(e for e in group if e.tumbler == winner)
    return _resolved(ref, entry, qualifier is not None, candidates)


def resolve_named_sources(
    sources: Sequence[SourceRef],
    owner: OwnerResolution,
    *,
    cat: NamedSourceCatalog | None,
    caps: PrimaryEvidenceCaps = DEFAULT_CAPS,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> NamedSourceResolution:
    """Resolve each named source inside the owner qualifier. Never raises.

    Catalog calls: one owner probe (when a qualifier resolved) plus, per source, one
    ``resolve`` (a tumbler) or at most two capped ``find`` calls (an RDR id).

    *deadline* is an absolute time on *clock* (the caller's own budget); the run also stops at
    ``caps.max_seconds`` from its start, whichever is sooner. No catalog call is started past it, so a
    thread the caller abandoned at the deadline ends after the call it is in; each source it did not reach
    is ``unresolved`` with the deadline as the reason.
    """
    refs = list(sources)
    kept, dropped = refs[: caps.max_named_sources], refs[caps.max_named_sources:]
    end = clock() + caps.max_seconds
    calls = _Calls(cat, end=end if deadline is None else min(end, deadline), clock=clock, caps=caps)
    qualifier, failure = _qualifier(owner, calls)
    out: list[ResolvedSource] = []
    for ref in kept:
        if failure:
            out.append(_unresolved(ref, failure))
        elif cat is None:
            out.append(_unresolved(ref, "the catalog is unavailable, so the source cannot be resolved"))
        elif ref.kind == "tumbler":
            out.append(_resolve_tumbler(ref, qualifier, calls))
        else:
            out.append(_resolve_rdr(ref, qualifier, calls, caps))
    return NamedSourceResolution(
        sources=tuple(out), dropped=tuple(r.value for r in dropped), catalog_calls=calls.n, caps=caps,
    )


# ── Section labels ───────────────────────────────────────────────────────────

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(\S.*?)[ \t]*#*[ \t]*$")


@dataclass(frozen=True)
class ContentLabel:
    """What one chunk's own text says about its section.

    ``label``: the section the chunk begins in (its first heading, else the one inherited);
    ``headings``: every heading in the chunk; ``carry``: the section the chunk ends in, which
    the next chunk inherits.
    """

    label: str | None
    headings: tuple[str, ...]
    carry: str | None


def derive_content_labels(texts: Sequence[str]) -> list[ContentLabel]:
    """Section labels from chunk text in manifest order. A Markdown heading starts a section;
    following chunks inherit it; content before the first heading is unlabelled. Headings
    inside a code fence do not count, and a fence left open at a chunk boundary stays open
    into the next chunk."""
    out: list[ContentLabel] = []
    carry: str | None = None
    fence: str | None = None
    for text in texts:
        headings: list[str] = []
        for line in text.splitlines():
            stripped = line.lstrip()
            if fence is None:
                if stripped.startswith(("```", "~~~")):
                    fence = stripped[:3]
                    continue
            else:
                if stripped.startswith(fence):
                    fence = None
                continue
            m = _HEADING.match(line)
            if m:
                headings.append(m.group(1).strip())
        label = headings[0] if headings else carry
        if headings:
            carry = headings[-1]
        out.append(ContentLabel(label=label, headings=tuple(headings), carry=carry))
    return out


# ── Primary evidence ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SelectedChunk:
    """One selected chunk, attributed to the document and position it was read from.
    ``also_at`` lists the other ``(doc_id, position)`` pairs holding the same text."""

    source: str
    doc_id: str
    position: int
    chash: str
    collection: str
    label: str | None
    label_source: Literal["manifest", "content", "none"]
    text: str
    also_at: tuple[tuple[str, int], ...] = ()

    def to_wire(self) -> dict[str, Any]:
        """Positions and labels, not the body (the body is the evidence, not the disclosure)."""
        return {
            "source": self.source, "doc_id": self.doc_id, "position": self.position,
            "chash": self.chash, "collection": self.collection, "label": self.label,
            "label_source": self.label_source, "chars": len(self.text),
            "also_at": [[d, p] for d, p in self.also_at],
        }


@dataclass(frozen=True)
class DocumentCoverage:
    source: str
    doc_id: str
    title: str
    status: Literal["ok", "no_manifest", "manifest_error"]
    manifest_rows: int = 0
    manifest_rows_expected: int = 0
    rows_hydrated: int = 0
    rows_sampled: bool = False
    missing_content: int = 0
    selected_rows: int = 0
    labelled_rows: int = 0
    labels_from_manifest: int = 0
    labels_from_content: int = 0
    positional_fallback: bool = False
    metadata_partial: bool = False
    partial: bool = True

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PrimaryEvidence:
    items: tuple[SelectedChunk, ...] = ()
    documents: tuple[DocumentCoverage, ...] = ()
    caps_hit: tuple[str, ...] = ()
    manifest_rows_hydrated: int = 0
    fetched_chars: int = 0
    selected_chars: int = 0
    fetch_errors: int = 0
    caps: PrimaryEvidenceCaps = DEFAULT_CAPS
    #: Exception type name when the whole read failed (nothing was read and the reason is disclosed); ``""``
    #: for a read that ran.
    error: str = ""

    @property
    def selected_items(self) -> int:
        return len(self.items)

    @property
    def partial(self) -> bool:
        return any(d.partial for d in self.documents)

    def to_wire(self) -> dict[str, Any]:
        return {
            "selected_items": self.selected_items,
            "selected_chars": self.selected_chars,
            "manifest_rows_hydrated": self.manifest_rows_hydrated,
            "fetched_chars": self.fetched_chars,
            "fetch_errors": self.fetch_errors,
            "error": self.error,
            "partial": self.partial,
            "caps_hit": list(self.caps_hit),
            "caps": self.caps.to_wire(),
            "documents": [d.to_wire() for d in self.documents],
            "items": [i.to_wire() for i in self.items],
        }

    def disclosures(self) -> list[str]:
        lines: list[str] = []
        if self.error:
            lines.append(
                f"[evidence request: the named source(s) resolved but the primary evidence could not be read "
                f"({self.error}); no primary evidence was read]"
            )
        for d in self.documents:
            who = f"{d.source} ({d.doc_id})"
            if d.status == "no_manifest":
                lines.append(f"[evidence request: {who} has no manifest in the catalog; no primary evidence was read]")
                continue
            if d.status == "manifest_error":
                lines.append(f"[evidence request: the manifest of {who} could not be read; no primary evidence was read]")
                continue
            if d.manifest_rows_expected and d.manifest_rows_expected != d.manifest_rows:
                lines.append(
                    f"[evidence request: the manifest of {who} lists {d.manifest_rows} chunks but the catalog "
                    f"records {d.manifest_rows_expected}; primary evidence is partial]"
                )
            if d.rows_sampled:
                lines.append(
                    f"[evidence request: {who} has {d.manifest_rows} chunks; {d.rows_hydrated} were sampled "
                    "across the document (not its prefix), so section labels are approximate]"
                )
            if d.positional_fallback:
                lines.append(
                    f"[evidence request: {who} has no headings in its chunks; chunks were chosen by "
                    "position, not section, and no section is claimed]"
                )
            if d.partial and d.selected_rows < d.manifest_rows:
                lines.append(
                    f"[evidence request: primary evidence for {who} is partial: {d.selected_rows} of "
                    f"{d.manifest_rows} chunks selected]"
                )
        if "deadline" in self.caps_hit:
            lines.append(
                f"[evidence request: the primary evidence read stopped at the deadline (at most "
                f"{self.caps.max_seconds:g} s, or the answer's own budget if that was shorter); chunks not "
                "fetched by then are missing]"
            )
        other = [c for c in self.caps_hit if c != "deadline"]
        if other:
            lines.append(f"[evidence request: primary evidence caps reached: {', '.join(other)}]")
        if self.fetch_errors:
            lines.append(f"[evidence request: {self.fetch_errors} chunk fetch(es) failed; those chunks are missing]")
        return lines


# ── Section kinds the synthesis wants first (never assumed present) ──────────

_KINDS: tuple[re.Pattern[str], ...] = (
    re.compile(r"decision|proposed solution|locked|approach|architecture|design", re.I),
    re.compile(r"evidence|research|finding|spike|measure|benchmark|result", re.I),
    re.compile(r"implementation|status|progress|phase|plan|outcome", re.I),
    re.compile(r"open|unresolved|question|risk|deferred|unknown|future", re.I),
)


@dataclass
class _Row:
    doc: int
    position: int
    chash: str
    collection: str
    text: str
    label: str | None
    label_source: Literal["manifest", "content", "none"]
    headings: tuple[str, ...]


def _spread(items: Sequence[int], n: int) -> list[int]:
    """*n* of *items* evenly spaced, both ends included."""
    m = len(items)
    if n <= 0:
        return []
    if n >= m:
        return list(items)
    if n == 1:
        return [items[0]]
    return list(dict.fromkeys(items[round(i * (m - 1) / (n - 1))] for i in range(n)))


def _water_fill(total: int, wants: Sequence[int]) -> list[int]:
    """Split *total* across demands: the small ones are met, the large ones share the rest."""
    alloc = [0] * len(wants)
    order = sorted(range(len(wants)), key=lambda i: wants[i])
    left = total
    for k, i in enumerate(order):
        share = left // (len(order) - k)
        alloc[i] = min(wants[i], share)
        left -= alloc[i]
    return alloc


def _bit_reversed(n: int) -> list[int]:
    """0..n-1 in an order that is spread at every prefix, so a fetch stopped early by a cap
    holds a sample of the whole document, not its start."""
    bits = max(1, (n - 1).bit_length())
    return sorted(range(n), key=lambda i: int(format(i, f"0{bits}b")[::-1], 2))


def _picks(rows: list[_Row], quota: int) -> list[_Row]:
    """The rows of one document in the order they should be admitted, at most *quota*.

    The decision, evidence/research, implementation-status and open-question sections that
    actually exist come first (one chunk each); the rest of the quota spreads evenly over the
    section starts, then over the remaining rows. With no headings it is position only.
    """
    if quota <= 0 or not rows:
        return []
    chosen: list[int] = []
    # sections = runs of one label; the first row of a run represents it
    starts = [i for i, r in enumerate(rows) if i == 0 or r.label != rows[i - 1].label]
    for kind in _KINDS:
        hit = next(
            (i for i in starts
             if i not in chosen and any(kind.search(h) for h in ((rows[i].label or ""), *rows[i].headings))),
            None,
        )
        if hit is not None and len(chosen) < quota:
            chosen.append(hit)
    labelled = any(r.label for r in rows)
    reps = [i for i in (starts if labelled else range(len(rows))) if i not in chosen]
    chosen += _spread(reps, quota - len(chosen))
    if len(chosen) < quota:
        rest = [i for i in range(len(rows)) if i not in chosen]
        chosen += _spread(rest, quota - len(chosen))
    return [rows[i] for i in chosen]


def select_primary_evidence(
    resolution: NamedSourceResolution,
    *,
    cat: NamedSourceCatalog,
    fetch_chunks: ChunkFetcher,
    caps: PrimaryEvidenceCaps | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PrimaryEvidence:
    """Read each resolved source's manifest, hydrate its chunks and select the evidence.

    Never raises. Everything cut by a cap, sampled, missing or unlabelled is on the result.

    *deadline* is an absolute time on *clock* (the caller's own budget); the read also stops at
    ``caps.max_seconds`` from its start. No manifest read or chunk fetch is started past it (``deadline``
    on ``caps_hit``): the chunks fetched by then are selected, the rest are missing and said so.
    """
    caps = caps or resolution.caps
    docs = resolution.resolved()
    if not docs:
        return PrimaryEvidence(caps=caps)
    end = clock() + caps.max_seconds
    if deadline is not None:
        end = min(end, deadline)

    if clock() >= end:
        return PrimaryEvidence(caps_hit=("deadline",), caps=caps)
    try:
        manifests = cat.get_manifests([d.tumbler for d in docs if d.tumbler])
        manifest_failed = False
    except Exception as exc:  # noqa: BLE001 — reported per document, never raised
        _log.warning("primary_evidence_manifest_failed", error=str(exc))
        manifests, manifest_failed = {}, True

    # 1. rows per document, then a row allowance per document (small documents whole,
    #    the rest share what is left, sampled evenly)
    caps_hit: list[str] = []
    all_rows: list[list[ManifestRow]] = []
    for d in docs:
        rows = sorted((r for r in manifests.get(d.tumbler or "", []) if r.chash), key=lambda r: r.position)
        all_rows.append(rows)
    true_counts = [len(r) for r in all_rows]
    for k, read_allow in enumerate(_water_fill(caps.max_manifest_rows_read, true_counts)):
        if true_counts[k] > read_allow:
            all_rows[k] = [all_rows[k][i] for i in _spread(range(true_counts[k]), read_allow)]
            caps_hit.append("max_manifest_rows_read")
    allowance = _water_fill(caps.max_manifest_rows, [len(r) for r in all_rows])
    sampled: list[list[ManifestRow]] = []
    for rows, allow in zip(all_rows, allowance, strict=True):
        sampled.append([rows[i] for i in _spread(range(len(rows)), allow)] if len(rows) > allow else rows)
    if any(len(s) < len(r) for s, r in zip(sampled, all_rows, strict=True)):
        caps_hit.append("max_manifest_rows")

    # 2. fetch: unique chashes per collection, in a spread order, stopping past the char cap
    pending: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    per_doc_order = [
        [sampled[k][i] for i in _bit_reversed(len(sampled[k]))] for k in range(len(docs))
    ]
    for rank in range(max((len(o) for o in per_doc_order), default=0)):
        for k, order in enumerate(per_doc_order):
            if rank < len(order):
                row = order[rank]
                key = (row.collection or docs[k].collection, row.chash)
                if key[0] and key not in seen_keys:
                    seen_keys.add(key)
                    pending.append(key)
    texts: dict[tuple[str, str], str] = {}
    requested: set[tuple[str, str]] = set()
    fetched_chars = 0
    fetch_errors = 0
    for start in range(0, len(pending), caps.fetch_batch_ids):
        if fetched_chars >= caps.max_fetched_chars:
            caps_hit.append("max_fetched_chars")
            break
        if clock() >= end:
            caps_hit.append("deadline")
            break
        by_collection: dict[str, list[str]] = defaultdict(list)
        for collection, chash in pending[start:start + caps.fetch_batch_ids]:
            by_collection[collection].append(chash)
        for collection, chashes in by_collection.items():
            requested.update((collection, c) for c in chashes)
            try:
                got = fetch_chunks(collection, chashes)
            except Exception as exc:  # noqa: BLE001 — those chunks are missing and said so
                _log.warning("primary_evidence_fetch_failed", collection=collection, count=len(chashes), error=str(exc))
                fetch_errors += 1
                continue
            for chash, text in got.items():
                if text:
                    texts[(collection, chash)] = text
                    fetched_chars += len(text)

    # 3. label each document's hydrated rows in manifest order
    per_doc_rows: list[list[_Row]] = []
    coverage_base: list[dict[str, Any]] = []
    for k, d in enumerate(docs):
        rows = all_rows[k]
        n_rows = true_counts[k]
        hydrated = [
            r for r in sampled[k]
            if (r.collection or d.collection or "", r.chash) in texts
        ]
        content = derive_content_labels([texts[(r.collection or d.collection or "", r.chash)] for r in hydrated])
        built: list[_Row] = []
        n_manifest = n_content = 0
        for r, cl in zip(hydrated, content, strict=True):
            collection = r.collection or d.collection or ""
            if r.labels_written:
                label = r.section_title or None
                source: Literal["manifest", "content", "none"] = "manifest"
                headings: tuple[str, ...] = (label,) if label else ()
                n_manifest += 1
            else:
                label, headings = cl.label, cl.headings
                source = "content" if label else "none"
                n_content += 1 if label else 0
            built.append(_Row(k, r.position, r.chash, collection, texts[(collection, r.chash)], label, source, headings))
        per_doc_rows.append(built)
        missing = sum(
            1 for r in sampled[k]
            if (r.collection or d.collection or "", r.chash) in requested
            and (r.collection or d.collection or "", r.chash) not in texts
        )
        no_manifest = not rows
        coverage_base.append({
            "status": "manifest_error" if manifest_failed else ("no_manifest" if no_manifest else "ok"),
            "manifest_rows": n_rows,
            "manifest_rows_expected": d.chunk_count if rows and d.chunk_count != n_rows else 0,
            "rows_hydrated": len(built),
            "rows_sampled": len(sampled[k]) < n_rows,
            "missing_content": missing,
            "labelled_rows": sum(1 for b in built if b.label),
            "labels_from_manifest": n_manifest,
            "labels_from_content": n_content,
        })

    # 4. select: fair share of the item budget, each document's priority list, admitted
    #    round-robin so no document takes the aggregate, shared chunks counted once
    quotas = _water_fill(caps.max_selected_items, [len(b) for b in per_doc_rows])
    queues = [deque(_picks(rows, q)) for rows, q in zip(per_doc_rows, quotas, strict=True)]
    admitted: dict[tuple[str, str], _Row] = {}
    holders: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    held_by_doc: list[set[int]] = [set() for _ in docs]
    chars = 0
    char_capped = False
    while any(queues) and len(admitted) < caps.max_selected_items:
        for k, queue in enumerate(queues):
            if not queue or len(admitted) >= caps.max_selected_items:
                continue
            row = queue.popleft()
            key = (row.collection, row.chash)
            if key in admitted:
                holders[key].append((docs[k].tumbler or "", row.position))
                held_by_doc[k].add(row.position)
                continue
            if chars + len(row.text) > caps.max_selected_chars:
                char_capped = True
                continue
            admitted[key] = row
            held_by_doc[k].add(row.position)
            chars += len(row.text)
    if char_capped:
        caps_hit.append("max_selected_chars")
    if len(admitted) >= caps.max_selected_items and any(queues):
        caps_hit.append("max_selected_items")

    items = sorted(
        (
            SelectedChunk(
                source=docs[row.doc].ref.value, doc_id=docs[row.doc].tumbler or "", position=row.position,
                chash=row.chash, collection=row.collection, label=row.label,
                label_source=row.label_source, text=row.text, also_at=tuple(holders.get(key, ())),
            )
            for key, row in admitted.items()
        ),
        key=lambda i: (next(k for k, d in enumerate(docs) if d.tumbler == i.doc_id), i.position),
    )
    coverage = []
    for k, d in enumerate(docs):
        base = coverage_base[k]
        selected = len(held_by_doc[k])
        positional = base["status"] == "ok" and base["rows_hydrated"] > 0 and base["labelled_rows"] == 0
        coverage.append(DocumentCoverage(
            source=d.ref.value, doc_id=d.tumbler or "", title=d.title, selected_rows=selected,
            positional_fallback=positional,
            metadata_partial=positional or base["labelled_rows"] < base["rows_hydrated"],
            partial=base["status"] != "ok" or selected < base["manifest_rows"] or bool(base["manifest_rows_expected"]),
            **base,
        ))
    return PrimaryEvidence(
        items=tuple(items), documents=tuple(coverage), caps_hit=tuple(dict.fromkeys(caps_hit)),
        manifest_rows_hydrated=sum(len(b) for b in per_doc_rows), fetched_chars=fetched_chars,
        selected_chars=chars, fetch_errors=fetch_errors, caps=caps,
    )


def gather_primary_evidence(
    request: EvidenceRequest,
    *,
    cat: NamedSourceCatalog | None,
    fetch_chunks: ChunkFetcher,
    caps: PrimaryEvidenceCaps = DEFAULT_CAPS,
) -> tuple[NamedSourceResolution, PrimaryEvidence]:
    """Resolve the request's named sources and read their primary evidence.

    The entry point nexus-jypmj.1.6 calls. No named source gives an empty resolution and no
    evidence; an unresolved or ambiguous one gives no evidence for it, and its reason is on
    the resolution.
    """
    resolution = resolve_named_sources(request.sources, request.owner, cat=cat, caps=caps)
    if cat is None:
        return resolution, PrimaryEvidence(caps=caps)
    return resolution, select_primary_evidence(resolution, cat=cat, fetch_chunks=fetch_chunks, caps=caps)
