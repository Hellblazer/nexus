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
   candidate could be a second one. Where one number has sibling files (``rdr-200-*.md``), the file whose
   frontmatter ``id`` is the number resolves (nexus-xho6c): the id comes from an injected
   :data:`FrontmatterIdLookup`, never from the catalog document or the chunks, which do not record it.
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

import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

import structlog

from nexus.catalog.rdr_canonical import (
    group_rdr_candidates,
    is_in_repo,
    rdr_frontmatter_id,
    rdr_key_of,
    rdr_source_prefix,
    resolve_canonical_tumbler,
)
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import RDR_FRONTMATTER_ID_META_KEY, CatalogEntry, ManifestRow
from nexus.plans.evidence import EvidenceRequest, OwnerResolution, SourceRange, SourceRef, SourceRefError

_log = structlog.get_logger(__name__)

#: Whether the named-source failure logs must leave the source id out (nexus-jypmj.2 follow-up, code review S5).
#: An id the caller BOUND is a parameter of the call; an id read from the question text is the caller's wording,
#: and under ``trace=False`` that wording is not logged. ``nx_answer`` sets this around the resolution stage; a
#: ContextVar so that the worker thread the stage runs in (``asyncio.to_thread`` copies the context) sees it.
_REDACT_SOURCE_LOGS: ContextVar[bool] = ContextVar("nx_redact_source_logs", default=False)

#: What a redacted log carries in place of the id.
REDACTED_SOURCE = "[from the question]"


@contextmanager
def redacted_source_logs(on: bool) -> Iterator[None]:
    """Within the block, the named-source failure logs carry :data:`REDACTED_SOURCE` and the exception TYPE
    instead of the source id and the exception text (the text can quote the id)."""
    token = _REDACT_SOURCE_LOGS.set(bool(on))
    try:
        yield
    finally:
        _REDACT_SOURCE_LOGS.reset(token)


def _source_log_fields(ref: SourceRef, exc: BaseException, *, text: bool = True) -> dict[str, str]:
    """The ``source`` and ``error`` fields of a named-source failure log. *text* False keeps the exception
    type only even when the id is shown."""
    if _REDACT_SOURCE_LOGS.get():
        return {"source": REDACTED_SOURCE, "error": type(exc).__name__}
    return {"source": ref.value, "error": str(exc) if text else type(exc).__name__}

__all__ = [
    "DEFAULT_CAPS",
    "ChunkFetcher",
    "ContentLabel",
    "DocumentCoverage",
    "NamedSourceCatalog",
    "NamedSourceResolution",
    "PrimaryEvidence",
    "PrimaryEvidenceCaps",
    "QUESTION_SOURCE_CAP",
    "TITLE_MATCH_SHARE",
    "RDR_FRONTMATTER_EXTRACTOR",
    "FrontmatterId",
    "FrontmatterIdLookup",
    "ResolvedSource",
    "SelectedChunk",
    "SourceCandidate",
    "derive_content_labels",
    "gather_primary_evidence",
    "question_source_caps",
    "recorded_frontmatter_id",
    "resolve_named_sources",
    "title_hint_match",
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
    #: Frontmatter-id reads one RDR number may cost when several files share it (nexus-xho6c); the
    #: largest group in docs/rdr is nine files. A group past it is candidates, with the cap named.
    max_frontmatter_lookups: int = 24
    #: Seconds one frontmatter-id read may take (what is left of the time budget can make it shorter).
    frontmatter_call_seconds: float = 5.0
    #: Chashes per T3 fetch.
    fetch_batch_ids: int = 100
    #: Seconds named-source resolution may take, and the same again for the primary read. The caller's
    #: own deadline (``nx_answer``'s ``budget_seconds``) can make either shorter; neither runs on the
    #: event loop, and a hit is disclosed (the answer proceeds without the stream).
    max_seconds: float = 20.0

    def to_wire(self) -> dict[str, int | float]:
        return asdict(self)


DEFAULT_CAPS = PrimaryEvidenceCaps()

#: Named sources resolved when they were READ FROM THE QUESTION rather than passed as a ``source`` binding
#: (nexus-jypmj.2.4). A binding is a deliberate request and keeps ``max_named_sources``; a question that
#: mentions many RDR ids (a range, a survey) is not, and each id costs an owner probe, up to two capped
#: finds and a share of the primary stream. The ids past the cap are named in ``NamedSourceResolution.dropped``
#: and in a text notice, never silently dropped.
#:
#: A CALIBRATION PARAMETER, not a derived value: the developer chose 3 (the resolver's own cap is 6, and
#: RDR-232 is silent). Nothing measured it; the held-out set of nexus-jypmj.2.8 is where to calibrate it.
QUESTION_SOURCE_CAP = 3

#: The most dropped source ids the resolution lists (wire and text notice); ``dropped_omitted`` counts the
#: rest. The drop itself is unchanged: every id past the cap is dropped and disclosed as a count at least.
LISTED_DROPPED = 20


def question_source_caps(caps: PrimaryEvidenceCaps = DEFAULT_CAPS) -> PrimaryEvidenceCaps:
    """*caps* with ``max_named_sources`` held to :data:`QUESTION_SOURCE_CAP` (or lower, if *caps* is)."""
    return replace(caps, max_named_sources=min(caps.max_named_sources, QUESTION_SOURCE_CAP))


# ── Narrow ports (the real catalog and T3 satisfy them) ──────────────────────


class NamedSourceCatalog(Protocol):
    """The only catalog calls this module makes."""

    def find(self, query: str, *, content_type: str | None = ..., limit: int = ...) -> list[CatalogEntry]: ...
    def get_owner_by_prefix(self, tumbler_prefix: str) -> dict | None: ...
    def resolve(self, tumbler: Any, *, follow_alias: bool = ...) -> CatalogEntry | None: ...
    def get_manifests(self, doc_ids: list[str]) -> dict[str, list[ManifestRow]]: ...


#: ``(doc_ids, max_rows_per_doc) -> (manifests, true row counts)``, or ``None`` when the engine did not honour the
#: limit (nexus-jypmj.6; ``HttpCatalogClient.get_manifests_sampled``). A document longer than the limit comes back
#: as an even sample of that many rows; the counts hold every returned document's TRUE live row count.
ManifestSampler = Callable[[list[str], int], "tuple[dict[str, list[ManifestRow]], dict[str, int]] | None"]

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
    #: The qualifier that narrowed the choice was the active project, inferred (nexus-jypmj.2.4), not a binding.
    inferred: bool = False
    #: How many registered documents carrying this number sit in OTHER projects than the qualifier's (the ones
    #: the qualifier left out). An inferred choice with ``foreign`` above 0 is the case the inference mattered in.
    foreign: int = 0
    #: The part of ``detail`` that says an inferred qualifier was set aside because the active project holds
    #: no such number (the result is the unqualified one, another project's document). Every origin prints it.
    fallback: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "source": self.ref.to_wire(),
            "status": self.status,
            "tumbler": self.tumbler,
            "title": self.title,
            "file_path": self.file_path,
            "qualified": self.qualified,
            "inferred": self.inferred,
            "foreign": self.foreign,
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
    #: ``explicit`` (a ``source`` binding) or ``question`` (RDR ids read from the question's text,
    #: nexus-jypmj.2.4). Shown per source on the wire and in the notices; it does not change resolution.
    origin: Literal["explicit", "question"] = "explicit"
    #: The spans of RDR numbers the question named by their ends (``RDR-078 through RDR-095``). Only the ends
    #: are sources; the numbers between are never expanded, and the disclosure says so (nexus-jypmj.2.4).
    ranges: tuple[SourceRange, ...] = ()

    def resolved(self) -> tuple[ResolvedSource, ...]:
        return tuple(s for s in self.sources if s.status == "resolved")

    @classmethod
    def failed(
        cls, sources: Sequence[SourceRef], reason: str, *, caps: PrimaryEvidenceCaps = DEFAULT_CAPS,
    ) -> NamedSourceResolution:
        """Every source unresolved because the stage itself failed (never raises past the caller): nothing is
        known about any of them. *reason* is free of paths and secrets (an exception's type, not its text)."""
        refs = list(sources)
        kept, dropped = refs[: caps.max_named_sources], refs[caps.max_named_sources:]
        return cls(
            sources=tuple(_unresolved(r, f"the lookup of {r.value} failed: {reason}") for r in kept),
            dropped=tuple(r.value for r in dropped), catalog_calls=0, caps=caps,
        )

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
            "sources": [{**s.to_wire(), "origin": self.origin} for s in self.sources],
            "dropped": list(self.dropped[:LISTED_DROPPED]),
            **({"dropped_omitted": len(self.dropped) - LISTED_DROPPED} if len(self.dropped) > LISTED_DROPPED else {}),
            "catalog_calls": self.catalog_calls,
            "origin": self.origin,
            "ranges": [r.to_wire() for r in self.ranges],
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
        from_question = self.origin == "question"
        for s in self.sources:
            name = f"{s.ref.value} (read from the question)" if from_question else s.ref.value
            if s.status == "resolved":
                tail = (
                    "" if primary_stream else
                    "; this single-step plan has no primary evidence stream, so the document is not read as evidence"
                )
                # A source read from the question says nothing about a plain resolution (one project holds the
                # number; the envelope has it): the line is for a doubt, i.e. an inference that chose among
                # projects holding the number. A source the caller bound keeps its line (nexus-jypmj.2.4).
                if s.inferred:
                    if not from_question or s.foreign:
                        others = f"; {s.foreign} document(s) in other projects carry the number" if s.foreign else ""
                        lines.append(
                            f"[evidence request: named source {name} resolved to {s.tumbler} by the active "
                            f"project, inferred from the session{others}; no owner or project binding confirmed it "
                            f"(pass owner or project to pin it){tail}]"
                        )
                elif not s.qualified and not from_question:
                    lines.append(
                        f"[evidence request: named source {name} resolved to {s.tumbler} but was not "
                        f"owner-qualified (pass owner or project to pin it){tail}]"
                    )
                # A fallback is never silent, from the question or not: the session's project was reliable and held no
                # such number, so the document read is another project's (nexus-jypmj.2.4, confirmation N1). The
                # plain unique match with no reliable project has no fallback and stays silent.
                caveat = tail if from_question and s.fallback else ""
                if s.detail:  # the fallback, a choice among sibling files, and the files passed over (nexus-xho6c)
                    lines.append(f"[evidence request: named source {name} resolved to {s.tumbler}: {s.detail}{caveat}]")
            elif s.status == "candidates":
                names = ", ".join(c.tumbler for c in s.candidates)
                lines.append(
                    f"[evidence request: named source {name} is ambiguous: "
                    f"{len(s.candidates)} candidates ({names}); {s.detail}]"
                )
            elif s.status == "unresolved":
                if owner_failure_disclosed and s.detail.startswith(OWNER_FAILURE_PREFIX):
                    continue
                lines.append(f"[evidence request: named source {name} unresolved: {s.detail}]")
        if self.dropped:
            from_q, fate = (" read from the question", "dropped, not resolved") if self.origin == "question" else ("", "dropped")
            lines.append(
                f"[evidence request: {len(self.dropped)} named source(s){from_q} beyond the cap of "
                f"{self.caps.max_named_sources} were {fate}: {', '.join(self.dropped[:LISTED_DROPPED])}"
                + (f" and {len(self.dropped) - LISTED_DROPPED} more" if len(self.dropped) > LISTED_DROPPED else "")
                + "]"
            )
        for rng in self.ranges:
            if text := rng.disclosure():
                lines.append(f"[evidence request: {text}]")
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
    #: The ``file://`` prefix of the owner's own ``docs/rdr/`` tree (``rdr_source_prefix`` of the owner's
    #: ``repo_root``), or ``""`` for an owner with no repo root. It lets ``rdr_canonical`` admit a stale-owner
    #: registration of the project's own file; empty degrades to owner-only matching.
    source_prefix: str = ""
    #: The owner is the active project, inferred (nexus-jypmj.2.4): it narrows an RDR number among projects, but a
    #: document tumbler is never held to it, and it yields to the unqualified lookup when the project has no match.
    inferred: bool = False

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

    def call_timeout(self, limit: float) -> float:
        """Seconds a read that is not a catalog call may take: *limit*, or what is left of the budget if
        that is less. Raises :class:`LookupDeadline` when too little is left to start one."""
        self._gate()
        left = limit if self.end is None else min(limit, self.end - self.clock())
        if left < _MIN_CALL_SECONDS:
            raise LookupDeadline
        return left

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
    inferred = owner.status == "inferred"
    if not (owner.status == "resolved" or inferred) or not owner.tumbler:
        return None, f"{OWNER_FAILURE_PREFIX}{owner.status}: {owner.detail}".rstrip(": ")
    if calls.cat is None:
        return None, "the catalog is unavailable, so the owner qualifier cannot be checked"
    tumbler = Tumbler.parse(owner.tumbler)
    owner_addr = tumbler.owner_address()
    try:
        row = calls.owner(str(owner_addr))
    except Exception as exc:  # noqa: BLE001 — the disclosure stage never raises; a failed probe is reported
        _log.warning("named_source_owner_probe_failed", owner=str(owner_addr), error=str(exc))
        if inferred and not isinstance(exc, LookupDeadline):
            return None, ""  # an inference that cannot be checked is dropped, not turned into a failure
        return None, calls.failed(f"owner {owner_addr}", exc)
    if row is None and inferred:
        return None, ""  # the inferred project is not a registered owner: resolve as if no tier had applied
    if row is None:
        return None, (
            f"owner {owner_addr} is not in the catalog (the qualifier {owner.tumbler} names no "
            "registered owner); nothing was searched"
        )
    return _Qualifier(
        owner_addr, tumbler if tumbler.depth >= 3 and not inferred else None, _owner_source_prefix(row),
        inferred=inferred,
    ), ""


def _owner_source_prefix(row: Mapping[str, Any]) -> str:
    """The owner's ``docs/rdr/`` source prefix, derived as the other ``rdr_canonical`` callers do
    (:func:`~nexus.catalog.rdr_canonical.rdr_source_prefix` of the repo root); ``""`` without one."""
    root = row.get("repo_root")
    if not root or not isinstance(root, str) or not os.path.isabs(root):
        return ""  # a relative root names no place: owner-only matching, as for an owner with no root
    return rdr_source_prefix(root)


def _unresolved(ref: SourceRef, detail: str, **kw: Any) -> ResolvedSource:
    return ResolvedSource(ref=ref, status="unresolved", detail=detail, **kw)


def _resolved(ref: SourceRef, entry: CatalogEntry, qualified: bool,
              candidates: tuple[SourceCandidate, ...] = (), inferred: bool = False, foreign: int = 0) -> ResolvedSource:
    return ResolvedSource(
        ref=ref, status="resolved", tumbler=str(entry.tumbler), title=entry.title,
        file_path=entry.file_path, collection=entry.physical_collection,
        chunk_count=entry.chunk_count, qualified=qualified, candidates=candidates,
        rdr_number=_key_number(entry), inferred=inferred, foreign=foreign,
    )


def _resolve_tumbler(ref: SourceRef, qualifier: _Qualifier | None, calls: _Calls) -> ResolvedSource:
    if qualifier is not None and qualifier.inferred:
        qualifier = None  # a tumbler names one document outright; the session's project cannot reject it
    try:
        entry = calls.resolve(ref.value)
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        _log.warning("named_source_resolve_failed", **_source_log_fields(ref, exc))
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


# ── Sibling files: which one is the RDR (nexus-xho6c) ────────────────────────────────────

#: The ``extractor_name`` of the deterministic RDR parser (``aspect_extractor._REGISTRY["rdr__"]``); a pin
#: in the tests holds the two together. Only a row it wrote says what the file's frontmatter holds.
RDR_FRONTMATTER_EXTRACTOR = "rdr-frontmatter-v1"

# Why a file's frontmatter id is unknown. Counted per file and named in the candidates ``detail``.
NO_ASPECT_ROW = "no_aspect_row"
OTHER_EXTRACTOR = "other_extractor"
FRONTMATTER_NOT_PARSED = "frontmatter_not_parsed"
ASPECT_STORE_ERROR = "aspect_store_error"
NO_FILE_SOURCE = "no_file_source"
FILE_ABSENT = "file_absent"
FILE_NOT_ALLOWED = "file_not_allowed"
FILE_PARSE_FAILED = "file_parse_failed"
ID_UNREADABLE = "id_unreadable"

_REASON_TEXT: dict[str, str] = {
    NO_ASPECT_ROW: "no aspect row yet",
    OTHER_EXTRACTOR: "an aspect row from another extractor",
    FRONTMATTER_NOT_PARSED: "an aspect row whose frontmatter was not parsed",
    ASPECT_STORE_ERROR: "the aspect store could not be read",
    NO_FILE_SOURCE: "no file:// source on the registration",
    FILE_ABSENT: "the file is not readable on this host",
    FILE_NOT_ALLOWED: "the file lies outside the allowed docs/rdr path",
    FILE_PARSE_FAILED: "the file's frontmatter did not parse",
    ID_UNREADABLE: "an id value that is not a readable RDR id",
}

#: A read is not started with less than this left of the time budget.
_MIN_CALL_SECONDS: float = 0.25


@dataclass(frozen=True)
class FrontmatterId:
    """What is known of one registration's frontmatter ``id``.

    ``known`` with ``value == ""``: the frontmatter was parsed and has no ``id``. ``known`` with another
    value: the raw ``id`` (``"RDR-200"``, ``200``, ``"companion-note"``), compared through :func:`_read_id`,
    never as text. Not ``known``: ``reasons`` says why (the codes above), and nothing is assumed.
    """

    known: bool
    value: Any = None
    reasons: tuple[str, ...] = ()

    @classmethod
    def of(cls, value: Any) -> FrontmatterId:
        return cls(True, "" if value is None else value)

    @classmethod
    def unknown(cls, *reasons: str) -> FrontmatterId:
        return cls(False, None, tuple(reasons))


#: ``(entry, timeout seconds, the owner's docs/rdr source prefix or "") -> what is known of its frontmatter
#: id``. The timeout is what is left of the budget, at most ``PrimaryEvidenceCaps.frontmatter_call_seconds``.
FrontmatterIdLookup = Callable[[CatalogEntry, float, str], FrontmatterId]

#: A file larger than this is not read as a fallback; an RDR is tens of KB.
_MAX_FRONTMATTER_FILE_BYTES = 2_000_000

#: An id that opens with a digit or has ``rdr`` before a digit is meant as an RDR id even when it is not
#: readable as one (``RDR-200 (superseded)``); ``companion-note`` is not.
_RDR_ISH = re.compile(r"rdr\W*\d|^\W*\d", re.IGNORECASE)
_MAX_ID_TEXT = 64


def _read_id(raw: Any) -> tuple[Literal["number", "none", "unreadable"], int | None]:
    """Classify a frontmatter ``id``: ``("number", n)`` for an RDR id (normalized as a ``source`` binding
    is: ``RDR-200``, ``rdr-200``, ``200`` and the integer 200 give 200); ``("none", None)`` for no id or a
    value that is plainly not an RDR id (``companion-note``, a tumbler, a boolean); ``("unreadable", None)``
    for a value that is present and meant as an RDR id but cannot be read as one (``RDR-200 (superseded)``,
    a list, a float, a number too large to read). Never raises."""
    if raw is None or isinstance(raw, bool):
        return "none", None
    if isinstance(raw, (list, tuple, dict, set, float)):
        return "unreadable", None
    try:
        text = str(raw).strip()
        if not text:
            return "none", None
        if len(text) > _MAX_ID_TEXT:
            return ("unreadable", None) if _RDR_ISH.search(text[:_MAX_ID_TEXT]) else ("none", None)
        try:
            ref = SourceRef.parse(text)
        except SourceRefError:  # not a readable id; whether it was meant as one decides below
            return ("unreadable", None) if _RDR_ISH.search(text) else ("none", None)
        return ("number", ref.number) if ref.kind == "rdr" and ref.number is not None else ("none", None)
    except Exception:  # noqa: BLE001 — a value that cannot even be rendered (a huge int) is unreadable
        return "unreadable", None


def _file_frontmatter_id(entry: CatalogEntry, source_prefix: str = "") -> FrontmatterId:
    """The ``id`` in the frontmatter of the file the registration points at, when this host has that file.

    A fallback only: the server may be remote, and then the file is not there. The path is the
    registration's own ``file://`` source (no host part), resolved; it must be a ``.md`` file under a
    ``docs/rdr`` directory and, when the owner has a source prefix, under that prefix. The header is split
    by the extractor's own parser (``_split_rdr_frontmatter``), so a value reads as the aspect row would
    read it (``id: 040`` is 40, never octal). Nothing of the file's content is logged.
    """
    uri = entry.source_uri
    if not uri.startswith("file://"):
        return FrontmatterId.unknown(NO_FILE_SOURCE)
    try:
        from nexus.aspect_extractor import RdrSourceNotAllowed, contained_rdr_md_from_uri  # noqa: PLC0415 — deferred: the extractor module is heavy; its one containment rule is shared with the header read

        try:
            path = contained_rdr_md_from_uri(uri, source_prefix)
        except RdrSourceNotAllowed:
            return FrontmatterId.unknown(FILE_NOT_ALLOWED)
        if not path.is_file() or path.stat().st_size > _MAX_FRONTMATTER_FILE_BYTES:
            return FrontmatterId.unknown(FILE_ABSENT)
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeDecodeError):
        return FrontmatterId.unknown(FILE_ABSENT)
    try:
        parsed, raw = rdr_frontmatter_id(text)  # the one rule the aspect row and the registration stamp use
    except Exception as exc:  # noqa: BLE001 — an unparseable header is unknown, never a failure
        _log.warning("named_source_frontmatter_file_parse_failed", error=type(exc).__name__)
        return FrontmatterId.unknown(FILE_PARSE_FAILED)
    if not parsed:
        return FrontmatterId.unknown(FILE_PARSE_FAILED)  # an opening fence with no closing one
    return FrontmatterId.of(raw)


def recorded_frontmatter_id(get_aspect: Callable[[str, float], Any] | None) -> FrontmatterIdLookup:
    """The id source for :func:`resolve_named_sources`: the document's registration ``meta``, then its
    ``document_aspects`` row, then its file.

    The RDR indexer writes the frontmatter ``id`` onto the catalog document when it registers (or updates)
    the document (``meta[RDR_FRONTMATTER_ID_META_KEY]``, nexus-jypmj.7), so the id is there for every
    registration made since, in local and cloud mode, with no wait on the aspect worker and no extra call: the
    entry is already in hand. A registration that predates the stamp (or whose header did not parse) has no
    key and falls through. Next is the deterministic ``rdr-frontmatter-v1`` aspect row (``extras["rdr_id"]``),
    in the engine's T2, filled asynchronously by the aspect worker; a re-index or ``nx enrich aspects`` fills
    either. *get_aspect* is ``(doc_id, timeout) -> AspectRecord | None`` (the store's ``get_by_doc_id``), keyed
    by the catalog tumbler string.

    An aspect row is authoritative only when it shows the frontmatter was parsed: written by the RDR extractor, with
    a confidence and non-empty ``extras``. A row without them (an extractor that failed leaves null fields;
    rows written before nexus-xho6c have empty ``extras`` on every document) says nothing about the header,
    and neither does a row from another extractor or no row: the lookup then reads the file under the
    registration's ``file://`` source where this host has it. A store that raises is not asked again for the
    rest of the request, and the file is tried all the same.
    """
    store_failed = False

    def lookup(entry: CatalogEntry, timeout: float, source_prefix: str = "") -> FrontmatterId:
        nonlocal store_failed
        meta = entry.meta
        if isinstance(meta, dict) and RDR_FRONTMATTER_ID_META_KEY in meta:
            return FrontmatterId.of(meta[RDR_FRONTMATTER_ID_META_KEY])
        reasons: list[str] = []
        if get_aspect is not None and not store_failed:
            try:
                row = get_aspect(str(entry.tumbler), timeout)
            except Exception as exc:  # noqa: BLE001 — a failing store falls through to the file
                store_failed = True
                _log.warning("named_source_aspect_store_failed", error=type(exc).__name__)
                reasons.append(ASPECT_STORE_ERROR)
            else:
                extras = getattr(row, "extras", None)
                if row is None:
                    reasons.append(NO_ASPECT_ROW)
                elif row.extractor_name != RDR_FRONTMATTER_EXTRACTOR:
                    reasons.append(OTHER_EXTRACTOR)
                elif row.confidence is None or not isinstance(extras, dict) or not extras:
                    reasons.append(FRONTMATTER_NOT_PARSED)
                else:
                    return FrontmatterId.of(extras.get("rdr_id"))
        elif get_aspect is not None:
            reasons.append(ASPECT_STORE_ERROR)
        from_file = _file_frontmatter_id(entry, source_prefix)
        if from_file.known:
            return from_file
        return FrontmatterId.unknown(*reasons, *from_file.reasons)

    return lookup


def _file_name(entries: Sequence[CatalogEntry], key: str) -> str:
    return PurePosixPath(entries[0].file_path).name if entries and entries[0].file_path else key


def _names(names: Sequence[str], limit: int = 12) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f", and {len(names) - limit} more" if len(names) > limit else "")


def _why_unknown(unknown: Mapping[str, tuple[str, list[str], str]], capped: bool, caps: PrimaryEvidenceCaps) -> str:
    """The aggregated reason (and remedy) for files whose frontmatter id could not be read."""
    counts: dict[str, int] = {}
    collections: dict[str, set[str]] = {}
    for _name, reasons, collection in unknown.values():
        for code in dict.fromkeys(reasons):
            counts[code] = counts.get(code, 0) + 1
            collections.setdefault(code, set()).add(collection)
    parts = [f"{_REASON_TEXT[c]} for {counts[c]}" for c in _REASON_TEXT if c in counts]
    if capped:
        parts.append(f"the lookup reached its cap of {caps.max_frontmatter_lookups} frontmatter reads")
    why = "; ".join(parts) or "their frontmatter id could not be read"
    hints: list[str] = []
    for code, flag in ((NO_ASPECT_ROW, ""), (OTHER_EXTRACTOR, " --all"), (FRONTMATTER_NOT_PARSED, " --all")):
        for coll in sorted(collections.get(code, ())):
            cmd = f"`nx enrich aspects {coll}{flag} --validate-sample 0`"
            if cmd not in hints:
                hints.append(cmd)
    remedy = (
        f"the aspect rows are filled asynchronously; run {' and '.join(hints)} to fill them "
        "(the parse is deterministic; `--validate-sample 0` skips the model-based sample check), or "
    ) if hints else ""
    return f"{why}; {remedy}"


def _carrier_group(
    ref: SourceRef, groups: dict[str, list[CatalogEntry]], lookup: FrontmatterIdLookup,
    calls: _Calls, caps: PrimaryEvidenceCaps, source_prefix: str = "",
) -> tuple[str | None, str]:
    """``(basename key, note)`` of the one file whose frontmatter carries *ref*'s id, or ``(None, why not)``.

    A file counts once however many registrations it has. The file is picked only when every file of the
    number has a known id and exactly one of them is *ref*'s: a file whose id could not be read might be a
    second carrier, so it keeps the candidates, with the reason and the remedy. At most
    ``caps.max_frontmatter_lookups`` reads, each given what is left of the time budget.
    """
    carriers: dict[str, str] = {}  # basename key -> file name, for each state
    clear: dict[str, str] = {}
    unknown: dict[str, tuple[str, list[str], str]] = {}  # key -> (file name, reasons, collection)
    asked, capped, failure = 0, False, ""
    for key, entries in groups.items():
        if len(carriers) > 1:
            break  # two carriers settle it: candidates
        state: str | None = None  # carrier | clear | None (unknown)
        reasons: list[str] = []
        for entry in sorted(entries, key=lambda e: e.content_type != "rdr"):
            if failure:
                break
            if asked >= caps.max_frontmatter_lookups:
                capped = True
                break
            try:
                got = lookup(entry, calls.call_timeout(caps.frontmatter_call_seconds), source_prefix)
            except Exception as exc:  # noqa: BLE001 — reported, never raised
                _log.warning("named_source_frontmatter_id_failed", **_source_log_fields(ref, exc, text=False))
                failure = (
                    _deadline_detail("the frontmatter id lookup", caps) if isinstance(exc, LookupDeadline)
                    else f"the frontmatter id lookup failed ({type(exc).__name__})"
                )
                break
            asked += 1
            if not got.known:
                reasons.extend(got.reasons)
                continue  # nothing readable for this registration: try the file's other one
            kind, number = _read_id(got.value)
            if kind == "unreadable":
                reasons.append(ID_UNREADABLE)
                break
            state = "carrier" if kind == "number" and number == ref.number else "clear"
            break
        name = _file_name(entries, key)
        if state == "carrier":
            carriers[key] = name
        elif state == "clear":
            clear[key] = name
        else:
            unknown[key] = (name, reasons, entries[0].physical_collection)
    total = len(groups)
    hint = "name the document by its tumbler as the source"
    if len(carriers) > 1:
        return None, f"{len(carriers)} files carry the frontmatter id {ref.value} ({_names(list(carriers.values()))}); {hint}"
    if failure:
        return None, f"{failure}, so {total} files sharing the number {ref.value} stay candidates; {hint}"
    if unknown:
        have = (
            f"one file carries it ({next(iter(carriers.values()))}), but " if carriers
            else f"no file is known to carry {ref.value}, and "
        )
        names = _names([n for n, _r, _c in unknown.values()])
        return None, f"{have}{len(unknown)} could not be checked ({names}): {_why_unknown(unknown, capped, caps)}{hint}"
    if not carriers:
        return None, f"no file among the {total} sharing the number carries the frontmatter id {ref.value}; {hint}"
    (key, name), = carriers.items()
    return key, (
        f"{total} files share the number {ref.value}; its frontmatter id is in {name}; "
        f"passed over: {_names(list(clear.values()))}"
    )


_TITLE_WORD = re.compile(r"[a-z0-9]+")
#: Words that carry no title: a hint's own filler, never evidence that a title matches.
_TITLE_STOP = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "its", "are", "was", "rdr",
})
#: The share of a title hint's words that a document's title or file name must hold for the hint to match it.
TITLE_MATCH_SHARE = 0.6


def _title_words(text: str) -> frozenset[str]:
    return frozenset(w for w in _TITLE_WORD.findall(text.lower()) if len(w) >= 3 and w not in _TITLE_STOP)


#: A title hint vetoes an inference only if it has at least this many distinctive words. The hint is whatever
#: follows the id in the topic sentence ("in detail", "and its gate record"), so a one-word hint is as likely a
#: filler as a title (nexus-jypmj.2.4, confirmation N3).
TITLE_VETO_MIN_WORDS = 2


def distinctive_title_words(hint: str, number: int | None = None) -> frozenset[str]:
    """The words of a title *hint* that can tell one document from another: not a stopword, not shorter than
    three characters, and not the RDR id (``rdr`` and, given *number*, the number itself)."""
    return frozenset(
        w for w in _title_words(hint) if not (number is not None and w.isdigit() and int(w) == number)
    )


def title_hint_match(entry: CatalogEntry, hints: Sequence[str]) -> float:
    """The best share (0 to 1) of any one title *hint*'s words found in the document's title or file name.

    Used to VETO an inference, never to choose a document: a score says "the question's words fit this
    document", and the caller acts on it only to refuse a guess (nexus-jypmj.2.4, RDR-232 section 1).
    """
    have = _title_words(f"{entry.title} {PurePosixPath(entry.file_path).stem}")
    best = 0.0
    for hint in hints:
        want = _title_words(hint)
        if want:
            best = max(best, len(want & have) / len(want))
    return best


def _resolve_rdr(
    ref: SourceRef, qualifier: _Qualifier | None, calls: _Calls, caps: PrimaryEvidenceCaps,
    frontmatter_id: FrontmatterIdLookup | None = None, title_hints: Sequence[str] = (),
) -> ResolvedSource:
    number = ref.number or 0
    query = f"RDR-{number:03d}"
    def admitted(e: CatalogEntry) -> bool:
        """The project's own: registered under the qualified owner, or (a stale owner) a file under that
        owner's docs/rdr/. rdr_canonical's one admission gate answers it; the document-depth qualifier
        still has to agree. Without a qualifier every candidate is admitted."""
        return qualifier is None or (
            is_in_repo(e, qualifier.owner, qualifier.source_prefix)
            and (qualifier.document is None or _comparable(e.tumbler, qualifier.document))
        )

    found: list[CatalogEntry] = []
    truncated = False
    try:
        # Legacy prose registrations are read only when the rdr ones hold no candidate the qualifier admits:
        # another project's rdr-typed RDR-084 must not hide this project's stale prose registration of it.
        for content_type in ("rdr", "prose"):
            rows = calls.find(query, content_type, caps.max_candidates_per_lookup)
            truncated = truncated or len(rows) >= caps.max_candidates_per_lookup
            found += [e for e in rows if not e.alias_of and _key_number(e) == number]
            if any(admitted(e) for e in found):
                break
    except Exception as exc:  # noqa: BLE001 — reported, never raised
        _log.warning("named_source_find_failed", **_source_log_fields(ref, exc))
        return _unresolved(ref, calls.failed(ref.value, exc))

    scoped = [e for e in found if admitted(e)]
    fallback = ""  # why an inferred qualifier was set aside (the active project holds no such RDR)
    if not scoped and found and qualifier is not None and qualifier.inferred:
        # The session's project has no such number. The inference was a tiebreaker for a number that more than
        # one project holds, not a restriction: resolve as if no tier had applied, and say so.
        fallback = f"the active project (owner {qualifier.owner}) holds no {ref.value}, so it was not used"
        qualifier = None
        scoped = list(found)
    title_hints = [h for h in title_hints if len(distinctive_title_words(h, number)) >= TITLE_VETO_MIN_WORDS]
    if qualifier is not None and qualifier.inferred and title_hints and scoped and len(found) > len(scoped):
        # The question carries a title for this number. If the session's project's document does not fit it and
        # another project's does, the session's project is the wrong guess: the title VETOES the inference (it
        # never picks a document). Candidates of every project, and nothing is read as evidence.
        mine = max(title_hint_match(e, title_hints) for e in scoped)
        mine_keys = {str(e.tumbler) for e in scoped}
        others = [e for e in found if str(e.tumbler) not in mine_keys and title_hint_match(e, title_hints) >= TITLE_MATCH_SHARE]
        if mine < TITLE_MATCH_SHARE and others:
            return ResolvedSource(
                ref=ref, status="candidates", candidates=tuple(SourceCandidate.of(e) for e in found), qualified=False,
                detail=(
                    f"the session's project (owner {qualifier.owner}) holds a {ref.value} but the question's title "
                    f"{title_hints[0]!r} does not fit it and does fit {', '.join(str(e.tumbler) for e in others)}; "
                    "the project was not inferred. Pass owner or project to choose, or name the document by its "
                    "tumbler as the source"
                ),
            )
    inferred = qualifier is not None and qualifier.inferred
    foreign = len(found) - len(scoped)
    candidates = tuple(SourceCandidate.of(e) for e in scoped)
    where = (
        f" under {qualifier.text}{' (the active project, inferred)' if inferred else ''}"
        if qualifier is not None else ""
    )
    note = f" ({foreign} in other projects ignored)" if foreign else ""

    def noted(detail: str) -> str:
        return "; ".join(part for part in (fallback, detail) if part)

    if truncated:
        return _unresolved(
            ref,
            noted(f"the candidate lookup for {ref.value} reached its cap of {caps.max_candidates_per_lookup} "
                  "and may be hiding a second candidate; name the document by its tumbler"),
            candidates=candidates, candidates_truncated=True,
        )
    groups = group_rdr_candidates(scoped)
    if not groups:
        return _unresolved(ref, noted(f"no registered document for {ref.value}{where}{note}"))
    chosen = ""  # the note of a choice among sibling files by their frontmatter id
    if len(groups) > 1:
        if frontmatter_id is None:
            return ResolvedSource(
                ref=ref, status="candidates", candidates=candidates, qualified=qualifier is not None, inferred=inferred,
                detail=noted(f"{len(groups)} documents carry the number {ref.value}{where}; "
                             "name the document by its tumbler as the source"),
            )
        # Sibling files (rdr-200-*.md, phase notes beside the RDR): the one whose frontmatter id is this
        # number is the RDR (Sam, 2026-10-25). Zero or several carriers is still candidates, with the reason.
        won, chosen = _carrier_group(
            ref, groups, frontmatter_id, calls, caps, qualifier.source_prefix if qualifier is not None else "",
        )
        if won is None:
            return ResolvedSource(
                ref=ref, status="candidates", candidates=candidates, qualified=qualifier is not None, inferred=inferred,
                detail=noted(f"{len(groups)} documents carry the number {ref.value}{where}: {chosen}"),
            )
        groups = {won: groups[won]}
    (key, group), = groups.items()
    owners = {e.tumbler.owner_address() for e in group}
    if qualifier is None and len(owners) > 1:  # a qualifier already chose the project; its stale owners are its own
        return ResolvedSource(
            ref=ref, status="candidates", candidates=candidates, qualified=False,
            detail=noted(f"{ref.value} exists in {len(owners)} projects ({', '.join(sorted(str(o) for o in owners))}); "
                         "pass owner or project to choose"),
        )
    # One basename identity under one owner: the canonical rule decides among the registrations
    # (an rdr registration beats a legacy prose one; two of a kind are not guessed between).
    owner = qualifier.owner if qualifier is not None else next(iter(owners))
    winner = resolve_canonical_tumbler(
        group, owner, repo_source_prefix=qualifier.source_prefix if qualifier is not None else "", rdr_key=key,
    )
    if winner is None:
        return ResolvedSource(
            ref=ref, status="candidates", candidates=candidates, qualified=qualifier is not None, inferred=inferred,
            detail=noted(f"{ref.value} is registered more than once{where}; name the document by its tumbler as the source"),
        )
    entry = next(e for e in group if e.tumbler == winner)
    out = _resolved(ref, entry, qualifier is not None, candidates, inferred, foreign)
    detail = noted(chosen)
    return replace(out, detail=detail, fallback=fallback) if detail else out


def resolve_named_sources(
    sources: Sequence[SourceRef],
    owner: OwnerResolution,
    *,
    cat: NamedSourceCatalog | None,
    caps: PrimaryEvidenceCaps = DEFAULT_CAPS,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    frontmatter_id: FrontmatterIdLookup | None = None,
    title_hints: Mapping[str, Sequence[str]] | None = None,
) -> NamedSourceResolution:
    """Resolve each named source inside the owner qualifier. Never raises.

    *title_hints* maps a source value (``RDR-084``) to the titles the question's text gives it. They act only on
    an INFERRED qualifier, as a veto (:func:`title_hint_match`); they never choose a document and never move an
    explicit qualifier.

    *frontmatter_id* (:func:`recorded_frontmatter_id`): where one RDR number has sibling files, the file
    whose frontmatter carries the id resolves; without it, siblings are candidates as before.

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
            out.append(_resolve_rdr(ref, qualifier, calls, caps, frontmatter_id, (title_hints or {}).get(ref.value, ())))
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
    status: Literal["ok", "no_manifest", "manifest_error", "manifest_not_read"]
    #: Why a ``manifest_not_read`` document was never requested: ``max_manifest_rows_read``, ``deadline``,
    #: or ``earlier_request_failed: <ExceptionType>``. ``""`` for every other status.
    not_read_reason: str = ""
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
            if d.status == "manifest_not_read":
                if d.not_read_reason == "deadline":
                    why = "the read reached its deadline before it was requested"
                elif d.not_read_reason.startswith("earlier_request_failed"):
                    failure = d.not_read_reason.partition(": ")[2]
                    why = "an earlier manifest request failed" + (f" ({failure})" if failure else "") + ", so it was never requested"
                else:
                    why = "the manifests already read reached the cap on rows read"
                lines.append(
                    f"[evidence request: the manifest of {who} was not read: {why}; no primary evidence was read from it]"
                )
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


def _fair_row_limit(wants: Sequence[int], budget: int) -> int:
    """The water-fill level over *wants* (recorded chunk counts) for a row *budget*: the largest per-document
    limit ``L >= 1`` with ``sum(min(want, L)) <= budget``. Documents under the level are read whole and the
    larger ones share what is left, the split ``_water_fill`` makes on rows already read. Over budget even at
    one row a document, the level is 1."""
    ordered = sorted(max(w, 1) for w in wants)
    left = budget
    for k, w in enumerate(ordered):
        share = left // (len(ordered) - k)
        if w > share:
            return max(1, share)
        left -= w
    return ordered[-1] if ordered else 1


def _read_manifests(
    cat: NamedSourceCatalog, docs: Sequence[ResolvedSource], caps: PrimaryEvidenceCaps, *,
    end: float, clock: Callable[[], float], caps_hit: list[str], sampler: ManifestSampler | None = None,
) -> tuple[dict[str, list[ManifestRow]], set[str], dict[str, str], dict[str, int]]:
    """``(manifests, failed, unread, row_counts)``: the manifests of *docs* pulled under the read cap (nexus-jypmj.1.17).

    *sampler* (nexus-jypmj.6) is the engine's per-document row limit. When the recorded chunk counts together
    exceed ``caps.max_manifest_rows_read`` it replaces the smallest-first read below: ONE request for every
    document, at the water-fill level of the recorded counts, so a large source is sampled instead of dropped
    and each document answers with its true row count (``row_counts``, the second sampled result). Within the
    cap, with no sampler, with one the engine did not honour (``None``), or with one that raised, the read is
    the smallest-first one below, exactly as before (a request that failed there fails again per document,
    through the handling below). ``row_counts`` is empty on the unsampled path.

    ``get_manifests`` has no row limit, so the cap acts on what is requested. The documents go in
    ascending order of the chunk count the catalog RECORDS for them, grouped into calls whose recorded
    counts fit what is left of ``caps.max_manifest_rows_read`` (a document that does not fit goes alone).
    The order and the grouping rely on the recorded count; the stop uses the rows actually returned.
    Once the rows read reach the cap, or the deadline passes, the remaining documents are not requested:
    ``unread`` maps each to the reason (``max_manifest_rows_read`` or ``deadline``) and ``caps_hit`` names
    it. The cap is soft by one document: a call that fits when planned, or a document larger than what is
    left, can take the rows read past the cap, so the rows read can reach the cap plus one document (a
    single named source larger than the cap must still be read). A call that raises leaves its documents
    ``failed``; every later document was never requested and is ``unread`` with the failure as its reason.
    When the recorded counts fit the cap this is one call, as before. One document's own reply is still
    whole: only the service could limit that.
    """
    tumblers = {d.tumbler: d.chunk_count for d in docs if d.tumbler}
    order = sorted(tumblers, key=lambda t: tumblers[t])  # stable: ties keep request order
    manifests: dict[str, list[ManifestRow]] = {}
    failed: set[str] = set()
    unread: dict[str, str] = {}
    if sampler is not None and order and sum(max(tumblers[t], 0) for t in order) > caps.max_manifest_rows_read:
        limit = _fair_row_limit([tumblers[t] for t in order], caps.max_manifest_rows_read)
        try:
            sampled = sampler(list(order), limit)
        except Exception as exc:  # noqa: BLE001 — the limit is an optimisation; the smallest-first read below answers (and reports its own failure per document)
            _log.warning("primary_evidence_manifest_sampler_failed", error=str(exc))
            sampled = None
        if sampled is not None:
            got, counts = sampled
            return dict(got), failed, unread, dict(counts)
        # the engine did not honour the limit, or the request failed: fall through to the smallest-first read
    pulled = i = 0
    while i < len(order):
        if i and (pulled >= caps.max_manifest_rows_read or clock() >= end):
            reason = "max_manifest_rows_read" if pulled >= caps.max_manifest_rows_read else "deadline"
            caps_hit.append(reason)
            unread.update(dict.fromkeys(order[i:], reason))
            break
        room = caps.max_manifest_rows_read - pulled
        batch = [order[i]]
        planned = max(tumblers[order[i]], 0)
        i += 1
        while i < len(order) and planned + max(tumblers[order[i]], 0) <= room:
            planned += max(tumblers[order[i]], 0)
            batch.append(order[i])
            i += 1
        try:
            got = cat.get_manifests(batch)
        except Exception as exc:  # noqa: BLE001 — reported per document, never raised
            _log.warning("primary_evidence_manifest_failed", error=str(exc))
            failed.update(batch)
            unread.update(dict.fromkeys(order[i:], f"earlier_request_failed: {type(exc).__name__}"))
            break
        manifests.update(got)
        pulled += sum(len(rows) for rows in got.values())
    return manifests, failed, unread, {}


def select_primary_evidence(
    resolution: NamedSourceResolution,
    *,
    cat: NamedSourceCatalog,
    fetch_chunks: ChunkFetcher,
    caps: PrimaryEvidenceCaps | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    manifest_sampler: ManifestSampler | None = None,
) -> PrimaryEvidence:
    """Read each resolved source's manifest, hydrate its chunks and select the evidence.

    Never raises. Everything cut by a cap, sampled, missing or unlabelled is on the result.

    *manifest_sampler* is the engine's per-document row limit (:data:`ManifestSampler`, nexus-jypmj.6); the
    caller passes one only for an engine that advertises it. ``None`` is the smallest-first read of
    nexus-jypmj.1.17.

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
    caps_hit: list[str] = []
    manifests, manifest_failed, manifest_unread, engine_row_counts = _read_manifests(
        cat, docs, caps, end=end, clock=clock, caps_hit=caps_hit, sampler=manifest_sampler,
    )

    # 1. rows per document, then a row allowance per document (small documents whole,
    #    the rest share what is left, sampled evenly)
    all_rows: list[list[ManifestRow]] = []
    for d in docs:
        rows = sorted((r for r in manifests.get(d.tumbler or "", []) if r.chash), key=lambda r: r.position)
        all_rows.append(rows)
    true_counts = [len(r) for r in all_rows]
    # A document the engine sampled (nexus-jypmj.6) arrives short: its true row count is the engine's, and the
    # sample is a cap hit like any other cut by the read cap.
    engine_total = [max(engine_row_counts.get(d.tumbler or "", 0), true_counts[k]) for k, d in enumerate(docs)]
    if any(engine_total[k] > true_counts[k] for k in range(len(docs))):
        caps_hit.append("max_manifest_rows_read")
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
        n_rows = engine_total[k]
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
            "status": (
                "manifest_error" if d.tumbler in manifest_failed
                else "manifest_not_read" if d.tumbler in manifest_unread
                else "no_manifest" if no_manifest else "ok"
            ),
            "not_read_reason": manifest_unread.get(d.tumbler or "", ""),
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
    frontmatter_id: FrontmatterIdLookup | None = None,
) -> tuple[NamedSourceResolution, PrimaryEvidence]:
    """Resolve the request's named sources and read their primary evidence.

    The entry point nexus-jypmj.1.6 calls. No named source gives an empty resolution and no
    evidence; an unresolved or ambiguous one gives no evidence for it, and its reason is on
    the resolution.
    """
    resolution = resolve_named_sources(
        request.sources, request.owner, cat=cat, caps=caps, frontmatter_id=frontmatter_id,
    )
    if cat is None:
        return resolution, PrimaryEvidence(caps=caps)
    return resolution, select_primary_evidence(resolution, cat=cat, fetch_chunks=fetch_chunks, caps=caps)
