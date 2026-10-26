# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 1 (nexus-jypmj.1.7, assumption A7): the bounded T2 lifecycle stream.

T2 is outside the corpus graph. When a named source resolves to an RDR, the RDR's own T2 records
(its metadata record and its latest gate record) are a third evidence stream beside the document's
chunks. They are read here and handed to :meth:`ReservedStream.from_evidence` as ``extra`` items, so
they lead the reserved share and neither the share cap nor the operator input cap evicts them.

Where the records live. The RDR tooling writes them to T2 project ``<repo>_rdr`` (``nx rdr``,
the rdr-gate skill), ``<repo>`` being the main-checkout basename. The catalog registers a repo owner
under that same name (``indexer``: ``register_owner(name=repo_name, owner_type="repo")``), so the
project is ``<owner name>_rdr`` where the owner is the one holding the RESOLVED DOCUMENT. It is never
taken from the request: an ART RDR-084 reads ``ART_rdr`` and cannot read ``nexus_rdr``. An owner that
is not a repo owner, has no name, or is not in the catalog leaves the project undetermined, and the
stream says so instead of guessing.

Tenancy. The tenant is not checked here. The engine binds it to the bearer of the T2 client and of the
catalog client (the declared tenant header is advisory), so a source in another tenant cannot resolve
(the catalog is tenant-scoped), its owner row is not found, and T2 returns nothing for its project.

Bounds, all in :class:`LifecycleCaps` and all disclosed: the reads are exact ``get(project, title)``
calls (no search, no listing), at most four title shapes for the metadata record and four for the gate
record per source; records admitted, T2 reads, characters per record and characters in total are
capped. A record past a cap is cut or not admitted and the cut is stated.

Time. The reads are sequential and blocking, and a thread cannot be cancelled once it is running, so the
bound has to be inside each call. The run has a monotonic budget (the smaller of ``max_seconds`` and the
caller's own deadline, ``nx_answer``'s ``budget_seconds``); every read is handed what is left of it, at
most ``call_timeout_seconds``, as its timeout, and a read that is not worth starting (under a quarter of a
second left) is not made. The caller's ``t2_get`` is told to give a timed-out read up rather than retry it
at the same clamp.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal

import structlog

from nexus.catalog.tumbler import Tumbler
from nexus.plans.primary_evidence import NamedSourceCatalog, NamedSourceResolution, ResolvedSource
from nexus.plans.reserved_stream import ReservedItem

_log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_LIFECYCLE_CAPS",
    "LifecycleCaps",
    "LifecycleRecord",
    "LifecycleStream",
    "SourceLifecycle",
    "T2Get",
    "rdr_title_shapes",
    "read_lifecycle_stream",
]

#: ``(project, title, timeout_seconds) -> entry | None``: the T2 client's exact ``get``, told how long it
#: may take. The only T2 call this module makes, so an unbounded read (search, listing) is not
#: expressible through it.
T2Get = Callable[[str, str, float], "dict[str, Any] | None"]

#: A read is not started with less than this left of the budget (a shorter timeout is "do not try").
_MIN_CALL_SECONDS: float = 0.25


@dataclass(frozen=True)
class LifecycleCaps:
    """Every bound on the lifecycle stream, in one calibration object."""

    #: Records admitted across all sources (two per RDR source: metadata and latest gate).
    max_records: int = 6
    #: T2 ``get`` calls across all sources (eight is one source with neither record present).
    max_reads: int = 24
    #: Characters kept of one record; a longer record is cut and says so.
    max_record_chars: int = 4_000
    #: Characters of record body kept across all admitted records (a cut marker is not counted).
    max_chars: int = 18_000
    #: Seconds the whole lookup may take (the caller's own deadline can make it shorter).
    max_seconds: float = 20.0
    #: Seconds one T2 read may take (what is left of the budget can make it shorter).
    call_timeout_seconds: float = 5.0

    def to_wire(self) -> dict[str, int | float]:
        return asdict(self)


DEFAULT_LIFECYCLE_CAPS = LifecycleCaps()

_GATE_SUFFIX = "-gate-latest"


def rdr_title_shapes(number: int) -> tuple[str, ...]:
    """The title shapes an RDR's T2 records are found under, in lookup order: bare, zero-padded,
    and ``RDR-``-prefixed in both widths. Mirrors ``nexus.commands.rdr._t2_rdr_titles`` (that module
    is the CLI; a test pins the two equal)."""
    return tuple(dict.fromkeys((str(number), f"{number:03d}", f"RDR-{number}", f"RDR-{number:03d}")))


@dataclass(frozen=True)
class LifecycleRecord:
    source: str
    project: str
    title: str
    kind: Literal["metadata", "gate-latest"]
    text: str
    chars_full: int
    truncated: bool
    #: Characters of the body kept (``text`` also holds the cut marker when ``truncated``).
    kept: int = 0

    def to_wire(self) -> dict[str, Any]:
        """Identity and size, not the body (the body is the evidence, not the disclosure)."""
        return {
            "source": self.source, "project": self.project, "title": self.title, "kind": self.kind,
            "chars": len(self.text), "chars_full": self.chars_full, "kept": self.kept, "truncated": self.truncated,
        }

    def reserved_item(self, rdr_number: int) -> ReservedItem:
        return ReservedItem.record(
            f"t2:{self.project}/{self.title}", self.text,
            label=f"RDR-{rdr_number:03d} {self.kind} record, T2 {self.project}/{self.title}",
        )


SourceStatus = Literal[
    "read", "no_records", "not_rdr", "not_resolved", "project_undetermined", "read_failed", "over_cap",
    "read_capped", "duplicate",
]


@dataclass(frozen=True)
class SourceLifecycle:
    """What happened to one named source: ``read`` (at least one record admitted; ``detail`` then names
    a record that is absent), or why not. ``read_capped`` is a lookup a cap cut before it found a record
    (the record may exist), never to be read as ``no_records``; ``duplicate`` is a source naming an RDR
    an earlier source already read."""

    source: str
    status: SourceStatus
    project: str = ""
    rdr_number: int | None = None
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "source": self.source, "status": self.status, "project": self.project,
            "rdr_number": self.rdr_number, "detail": self.detail,
        }


def _skipped(s: ResolvedSource) -> SourceLifecycle:
    if s.status != "resolved":
        return SourceLifecycle(
            s.ref.value, "not_resolved",
            detail=f"the source is {s.status}, so it has no lifecycle stream" + (f": {s.detail}" if s.detail else ""),
        )
    return SourceLifecycle(s.ref.value, "not_rdr", detail="the resolved document is not an RDR")


@dataclass(frozen=True)
class LifecycleStream:
    records: tuple[LifecycleRecord, ...] = ()
    sources: tuple[SourceLifecycle, ...] = ()
    reads: int = 0
    caps_hit: tuple[str, ...] = ()
    caps: LifecycleCaps = DEFAULT_LIFECYCLE_CAPS
    #: RDR number of each record, parallel to ``records`` (the header names it).
    numbers: tuple[int, ...] = ()

    @property
    def chars(self) -> int:
        return sum(r.kept for r in self.records)

    def reserved_items(self) -> tuple[ReservedItem, ...]:
        return tuple(r.reserved_item(n) for r, n in zip(self.records, self.numbers, strict=True))

    @classmethod
    def unavailable(cls, resolution: NamedSourceResolution, detail: str) -> LifecycleStream:
        """T2 could not be opened at all: every resolved RDR source reports it."""
        return cls(sources=tuple(
            SourceLifecycle(s.ref.value, "read_failed", rdr_number=s.rdr_number, detail=detail)
            if s.status == "resolved" and s.rdr_number is not None else _skipped(s)
            for s in resolution.sources
        ))

    def to_wire(self) -> dict[str, Any]:
        return {
            "sources": [s.to_wire() for s in self.sources],
            "records": [r.to_wire() for r in self.records],
            "reads": self.reads,
            "chars": self.chars,
            "caps_hit": list(self.caps_hit),
            "caps": self.caps.to_wire(),
        }

    def disclosures(self) -> list[str]:
        """Text notices for what the stream could not deliver or delivered cut. A source that did not
        resolve costs no line here (the named-source notices say why it did not resolve)."""
        lines: list[str] = []
        for s in self.sources:
            if s.status == "project_undetermined":
                lines.append(
                    f"[evidence request: {s.source}: the T2 lifecycle records were not read because the "
                    f"project could not be determined: {s.detail}]"
                )
            elif s.status == "read_failed":
                lines.append(f"[evidence request: {s.source}: the T2 lifecycle records could not be read: {s.detail}]")
            elif s.status == "no_records":
                lines.append(f"[evidence request: {s.source}: no T2 lifecycle records: {s.detail}]")
            elif s.status == "over_cap":
                lines.append(f"[evidence request: {s.source}: T2 lifecycle records not read: {s.detail}]")
            elif s.status == "read_capped":
                lines.append(
                    f"[evidence request: {s.source}: the T2 lifecycle lookup was cut short, so a record may "
                    f"exist that was not found: {s.detail}]"
                )
            elif s.status == "not_rdr":
                lines.append(
                    f"[evidence request: {s.source}: the resolved document is not an RDR, so it has no T2 "
                    "lifecycle records]"
                )
            elif s.status == "read" and s.detail:
                lines.append(f"[evidence request: {s.source}: {s.detail}]")
        for r in self.records:
            if r.truncated:
                lines.append(
                    f"[evidence request: T2 record {r.project}/{r.title} has {r.chars_full} characters; "
                    f"truncated to the first {r.kept} (max_record_chars {self.caps.max_record_chars}, "
                    f"max_chars {self.caps.max_chars})]"
                )
        for name in self.caps_hit:
            lines.append(_CAP_NOTICES[name].format(caps=self.caps))
        return lines


#: One notice per cap that cut something, whatever the source's own status says.
_CAP_NOTICES: dict[str, str] = {
    "max_records": "[evidence request: T2 lifecycle records reached the max_records limit of {caps.max_records}; "
                   "records past it were not admitted]",
    "max_reads": "[evidence request: T2 lifecycle lookups reached the max_reads limit of {caps.max_reads} reads; "
                 "titles past it were not tried]",
    "max_chars": "[evidence request: T2 lifecycle records reached the max_chars limit of {caps.max_chars}; "
                 "text past it was not kept]",
    "deadline": "[evidence request: T2 lifecycle lookups stopped at the deadline (at most {caps.max_seconds} s, "
                "or the answer's own budget if that was shorter); titles past it were not tried]",
}


class _Run:
    """The budget of one call."""

    def __init__(self, caps: LifecycleCaps, t2_get: T2Get, *, deadline: float | None,
                 clock: Callable[[], float]) -> None:
        self.caps, self.t2_get, self.clock = caps, t2_get, clock
        end = clock() + caps.max_seconds
        self.end = end if deadline is None else min(end, deadline)
        self.reads = 0
        self.chars = 0
        self.records: list[LifecycleRecord] = []
        self.numbers: list[int] = []
        self.caps_hit: list[str] = []
        self.t2_failure: str | None = None
        #: The cap that cut the current source's lookup, or ``None``; reset per source.
        self.cut: str | None = None
        #: ``(project, rdr number) -> the source value that read it``, so a second naming reads nothing.
        self.seen: dict[tuple[str, int], str] = {}
        self.admitted: set[tuple[str, str]] = set()

    def hit(self, name: str) -> None:
        if name not in self.caps_hit:
            self.caps_hit.append(name)

    def out_of_time(self) -> bool:
        """True (and the cap recorded) when too little of the time budget is left to start a read."""
        if self.end - self.clock() < _MIN_CALL_SECONDS:
            self.hit("deadline")
            return True
        return False

    def read(self, project: str, title: str) -> dict[str, Any] | None:
        """One exact ``get``, given what is left of the budget (at most the per-call ceiling)."""
        timeout = min(self.end - self.clock(), self.caps.call_timeout_seconds)
        self.reads += 1
        entry = self.t2_get(project, title, timeout)
        return entry if isinstance(entry, dict) else None

    def admit(self, source: str, project: str, title: str, kind: Literal["metadata", "gate-latest"],
              content: str, number: int) -> int:
        """1 when the record was admitted (possibly cut), else 0."""
        if (project, title) in self.admitted:
            return 0
        if len(self.records) >= self.caps.max_records:
            self.hit("max_records")
            self.cut = "max_records"
            return 0
        room = self.caps.max_chars - self.chars
        if room <= 0:
            self.hit("max_chars")
            self.cut = "max_chars"
            return 0
        limit = min(self.caps.max_record_chars, room)
        body = content.strip()
        cut = len(body) > limit
        text = f"{body[:limit]}\n[truncated: first {limit} of {len(body)} characters]" if cut else body
        if cut and limit < self.caps.max_record_chars:
            self.hit("max_chars")
        self.chars += min(len(body), limit)
        self.admitted.add((project, title))
        self.records.append(LifecycleRecord(source, project, title, kind, text, len(body), cut, min(len(body), limit)))
        self.numbers.append(number)
        return 1

    def first_present(self, project: str, titles: tuple[str, ...]) -> tuple[str, str] | None:
        """``(title, content)`` of the first title with a non-blank record, within the read cap and the
        time budget. A cap that stops the search before every title was tried sets ``self.cut``."""
        for title in titles:
            if self.reads >= self.caps.max_reads:
                self.hit("max_reads")
                self.cut = "max_reads"
                return None
            if self.out_of_time():
                self.cut = "deadline"
                return None
            entry = self.read(project, title)
            if entry and str(entry.get("content") or "").strip():
                return title, str(entry["content"])
        return None

    def spent(self) -> str | None:
        """The cap that is already used up before a source starts (recorded), else ``None``."""
        if len(self.records) >= self.caps.max_records:
            self.hit("max_records")
            return "max_records"
        if self.reads >= self.caps.max_reads:
            self.hit("max_reads")
            return "max_reads"
        if self.out_of_time():
            return "deadline"
        return None


def _project_for(source: ResolvedSource, cat: NamedSourceCatalog | None) -> tuple[str, str]:
    """``(project, "")`` or ``("", why)``. The project is ``<owner name>_rdr`` for the repo owner that
    holds the resolved document; nothing else names it."""
    if cat is None:
        return "", "the catalog is unavailable, so the document's owner cannot be read"
    try:
        owner_addr = str(Tumbler.parse(source.tumbler or "").owner_address())
        row = cat.get_owner_by_prefix(owner_addr)
    except Exception as exc:  # noqa: BLE001 — the stream never raises; the failure is disclosed
        _log.warning("lifecycle_stream_owner_probe_failed", source=source.ref.value, error=str(exc))
        return "", f"the owner of {source.tumbler} could not be read ({type(exc).__name__})"
    if not isinstance(row, dict):
        return "", f"owner {owner_addr} is not in the catalog (for this tenant)"
    if row.get("owner_type") != "repo":
        return "", f"owner {owner_addr} is a {row.get('owner_type') or 'typeless'} owner, not a repo, so it has no RDR project"
    name = str(row.get("name") or "").strip()
    if not name:
        return "", f"owner {owner_addr} has no name, so its RDR project cannot be named"
    return f"{name}_rdr", ""


def _read_source(source: ResolvedSource, cat: NamedSourceCatalog | None, run: _Run) -> SourceLifecycle:
    value, number = source.ref.value, source.rdr_number
    assert number is not None  # the caller routes non-RDR sources elsewhere
    if run.t2_failure is not None:
        return SourceLifecycle(value, "read_failed", rdr_number=number,
                               detail=f"{run.t2_failure} (not retried in this call)")
    project, why = _project_for(source, cat)
    if not project:
        return SourceLifecycle(value, "project_undetermined", rdr_number=number, detail=why)
    first = run.seen.get((project, number))
    if first is not None:
        return SourceLifecycle(
            value, "duplicate", project=project, rdr_number=number,
            detail=f"the same RDR and project as source {first}; its records are read and counted once",
        )
    run.seen[(project, number)] = value
    if (spent := run.spent()) is not None:
        limit = {"max_records": f"{run.caps.max_records} records", "max_reads": f"{run.caps.max_reads} reads",
                 "deadline": "time budget"}[spent]
        return SourceLifecycle(
            value, "over_cap", project=project, rdr_number=number,
            detail=f"the lifecycle {spent} limit ({limit}) was already reached",
        )

    shapes = rdr_title_shapes(number)
    admitted = 0
    run.cut = None
    meta = gate = None
    try:
        meta = run.first_present(project, shapes)
        if meta is not None:
            admitted += run.admit(value, project, meta[0], "metadata", meta[1], number)
        if run.cut is None:
            # the gate record is keyed by the same shape as the metadata record when both were written together
            first_shape = meta[0] if meta is not None else None
            gate_shapes = ((first_shape,) if first_shape else ()) + tuple(x for x in shapes if x != first_shape)
            gate = run.first_present(project, tuple(f"{x}{_GATE_SUFFIX}" for x in gate_shapes))
            if gate is not None:
                admitted += run.admit(value, project, gate[0], "gate-latest", gate[1], number)
    except Exception as exc:  # noqa: BLE001 — a failed T2 read is disclosed, never a failed answer
        _log.warning("lifecycle_stream_t2_read_failed", source=value, project=project, error=str(exc))
        run.t2_failure = f"{type(exc).__name__} reading T2 project {project}"
        return SourceLifecycle(value, "read_failed", project=project, rdr_number=number, detail=run.t2_failure)
    label = f"RDR-{number:03d} in T2 project {project}"
    if admitted:
        detail = ""
        if run.cut is None:
            if meta is None:
                detail = f"no metadata record for {label}"
            elif gate is None:
                detail = f"no latest gate record for {label}"
        return SourceLifecycle(value, "read", project=project, rdr_number=number, detail=detail)
    if run.cut is not None:
        return SourceLifecycle(
            value, "read_capped", project=project, rdr_number=number,
            detail=f"the {run.cut} limit stopped the lookup for {label}",
        )
    return SourceLifecycle(
        value, "no_records", project=project, rdr_number=number,
        detail=f"no usable metadata or gate-latest record for {label}",
    )


def read_lifecycle_stream(
    resolution: NamedSourceResolution | None,
    *,
    cat: NamedSourceCatalog | None,
    t2_get: T2Get,
    caps: LifecycleCaps = DEFAULT_LIFECYCLE_CAPS,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> LifecycleStream | None:
    """The T2 lifecycle stream for the resolved RDR sources. ``None`` when no source was named.

    Only a ``resolved`` source whose document is an RDR (``rdr_number``) is read; an ambiguous,
    unresolved or non-RDR source gets a per-source outcome and no records. Never raises.

    *deadline* is an absolute time on *clock* (``time.monotonic`` by default): the caller's own budget.
    The run also stops at ``caps.max_seconds`` from its start, whichever is sooner.
    """
    if resolution is None or not resolution.sources:
        return None
    run = _Run(caps, t2_get, deadline=deadline, clock=clock)
    outcomes: list[SourceLifecycle] = []
    for source in resolution.sources:
        if source.status != "resolved" or source.rdr_number is None or not source.tumbler:
            outcomes.append(_skipped(source))
        else:
            outcomes.append(_read_source(source, cat, run))
    return LifecycleStream(
        records=tuple(run.records), sources=tuple(outcomes), reads=run.reads,
        caps_hit=tuple(run.caps_hit), caps=caps, numbers=tuple(run.numbers),
    )
