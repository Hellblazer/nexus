# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 1 (nexus-jypmj.1.6): the reserved primary stream, merged at the runner.

The named source's evidence (:class:`~nexus.plans.primary_evidence.PrimaryEvidence`) is not a plan
step. ``nx_answer`` builds a :class:`ReservedStream` from it and hands it to ``plan_run``; the
runner's operator hydration (``_hydrate_operator_args``) merges it AHEAD of the plan's own inputs
at the first operator that consumes retrieved evidence, in both shapes an operator receives them:

* ``ids`` auto-hydration: the ``_OPERATOR_MAX_INPUTS`` positional cap runs after the merge, so it
  cuts only secondary items.
* explicit ``inputs`` (an earlier ``store_get_many`` step; plan 473's ``rank``): the same cap
  runs, but ONLY when a stream was merged. A run with no reserved stream keeps its uncapped
  behaviour (the S4 resolution of the round-2 critique).

The merge happens once per run, at one plan step (``merged_step``). A re-hydration of that same
step (the oversized-bundle per-step fallback, the continuation envelope) merges again, so every
reduction route sees the same evidence. Later operators are only observed: where an operator's
input is the materialised output of a rank or filter, the stream counts the reserved items missing
from it, so a drop by an unbundled rank is reported rather than silent (critique observation 13).
No claim is made inside a bundle (the later step holds a deferred ``$stepN`` ref, not the items) or
after a summarize/generate/extract (a reduction is not a drop).

The reserved share stays below ``runner._OPERATOR_MAX_INPUTS`` (:data:`RESERVED_SHARE_MAX`), and
every merged item counts against the bundle prompt limit through the runner's existing
post-hydration size check. Items are deduplicated against vector hits by chash (``ids`` shape) or
by text (``inputs`` shape); the primary copy and its attribution header are kept.

State lives in a ``ContextVar`` so the dispatcher, the bundle path and the continuation envelope
(all callers of ``_hydrate_operator_args``) share it without a signature change.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from nexus.plans.bundle import DEFERRED_REF_KEY
from nexus.plans.primary_evidence import PrimaryEvidence, SelectedChunk

__all__ = [
    "EVIDENCE_KEYS",
    "RESERVED_SHARE_MAX",
    "ReservedItem",
    "ReservedStream",
    "active_reserved_stream",
    "reserved_scope",
    "reserved_stream_is_evidence",
    "set_reserved_step",
    "strip_source_note",
]

#: Reserved items one run may merge. Calibration constant; must stay below
#: ``runner._OPERATOR_MAX_INPUTS`` (100) so plan evidence keeps room (a test pins this).
RESERVED_SHARE_MAX: int = 50

#: Operator args that carry the evidence a later operator reduces.
EVIDENCE_KEYS: tuple[str, ...] = ("items", "inputs", "content", "context", "groups")

#: Operators that only keep or reorder the items they are given. A reserved item missing from the
#: input of the step after one of these was dropped by it; after any other operator (summarize,
#: generate, extract, compare: they reduce or rewrite) its absence is the reduction itself.
_DROPPING_OPERATORS: frozenset[str] = frozenset({"rank", "filter"})

_SOURCE_MARKER = "[source: "


@dataclass(frozen=True)
class ReservedItem:
    """One merged reserved item. ``stream`` is ``primary`` (a chunk of a named source) or ``t2`` (a
    lifecycle record); ``collection``, ``doc_id`` and ``source`` say where it came from, so the
    reducer-input diagnostics (nexus-jypmj.1.8) count the input by stream and collection without
    reading the text."""

    token: str
    chash: str
    raw: str
    text: str
    stream: str = "primary"
    collection: str = ""
    doc_id: str = ""
    source: str = ""

    @classmethod
    def record(cls, ident: str, text: str, *, label: str = "", source: str = "") -> ReservedItem:
        """A reserved item that is not a T3 chunk (the T2 lifecycle records, nexus-jypmj.1.7):
        no chash, so only the text dedupe applies. *ident* names it in the header and the token.

        The token ends in ``#`` (and a ``#`` inside *ident* is escaped), so no record token is a prefix
        of another: ``primary:t2:P/084#`` is not found inside ``primary:t2:P/084-gate-latest#``, which
        the substring checks in :meth:`ReservedStream.observe` and ``is_primary_text`` need to count one
        dropped record as one."""
        token = f"primary:{ident.replace('#', '%23')}#"
        head = f"{label}; " if label else ""
        return cls(
            token=token, chash="", raw=text, text=f"[primary record {head}{token}]\n{text}",
            stream="t2", source=source,
        )


def _item(chunk: SelectedChunk) -> ReservedItem:
    token = f"primary:{chunk.chash[:12]}"
    where = f"{chunk.source} ({chunk.doc_id}) chunk {chunk.position}"
    if chunk.label:
        where += f", section {chunk.label}"
    return ReservedItem(
        token=token, chash=chunk.chash, raw=chunk.text,
        text=f"[primary source {where}; {token}]\n{chunk.text}",
        stream="primary", collection=chunk.collection, doc_id=chunk.doc_id, source=chunk.source,
    )


def strip_source_note(text: str) -> str:
    """*text* without the ``[source: ...]`` line the hydration prefixes to a chunk."""
    if text.startswith(_SOURCE_MARKER):
        _, _, rest = text.partition("\n")
        return rest
    return text


_strip_source_note = strip_source_note


@dataclass
class ReservedStream:
    """One run's reserved primary evidence and what the runner did with it."""

    items: tuple[ReservedItem, ...]
    over_share_dropped: int = 0
    merged: bool = False
    merged_step: int | None = None
    current_step: int | None = None
    current_producers: tuple[str, ...] = ()
    deduped_secondary: int = 0
    secondary_cut: int = 0
    capped: bool = False
    #: ``{"step", "missing", "of"}`` for the most recent later operator (the terminal one at
    #: the end of the run); ``None`` until a later operator is observed.
    last_observed: dict[str, int] | None = None
    #: Tokens of the reserved items the most recent observed operator's input no longer held (the
    #: per-item detail behind ``last_observed["missing"]``; the diagnostics attribute them to sources).
    last_missing_tokens: tuple[str, ...] = ()
    _raw: frozenset[str] = field(default_factory=frozenset, repr=False)
    _chashes: frozenset[str] = field(default_factory=frozenset, repr=False)

    @classmethod
    def from_evidence(
        cls, evidence: PrimaryEvidence | None, *, share_max: int = RESERVED_SHARE_MAX,
        extra: Sequence[ReservedItem] = (),
    ) -> ReservedStream | None:
        """``None`` when there is nothing to reserve, so the run stays byte-identical.

        *extra* are non-chunk items (T2 records) that lead the share; the chunks fill the rest,
        so a cap on the share never evicts them.
        """
        chunks = evidence.items if evidence is not None else ()
        if not chunks and not extra:
            return None
        room = max(share_max - len(extra), 0)
        items = (*extra, *(_item(c) for c in chunks[:room]))
        return cls(
            items=items, over_share_dropped=len(chunks) - min(len(chunks), room),
            _raw=frozenset(i.raw for i in items), _chashes=frozenset(i.chash for i in items if i.chash),
        )

    # ── merge ────────────────────────────────────────────────────────────────

    def should_merge(self) -> bool:
        """First evidence-consuming operator, or a re-hydration of that same step."""
        return not self.merged or self.merged_step == self.current_step

    def mark_merged(self) -> None:
        self.merged = True
        self.merged_step = self.current_step

    def is_primary_chash(self, chash: str) -> bool:
        return chash in self._chashes

    def is_primary_text(self, text: str) -> bool:
        if not isinstance(text, str):
            return False
        return text in self._raw or _strip_source_note(text) in self._raw or any(
            i.token in text and i.raw in text for i in self.items
        )

    def merge(self, secondary: Sequence[Any], *, pre_deduped: int = 0) -> list[Any]:
        """Reserved texts ahead of *secondary*, which loses any item that is a reserved chunk.

        *pre_deduped* counts hits the caller already removed by chash. The counters describe the
        latest hydration of the merge step (a re-hydration replaces them, never adds).
        """
        kept = [s for s in secondary if not self.is_primary_text(s)]
        self.deduped_secondary = pre_deduped + len(secondary) - len(kept)
        self.secondary_cut, self.capped = 0, False
        self.mark_merged()
        return [i.text for i in self.items] + kept

    def note_cap(self, merged_count: int, kept: int) -> None:
        """The runner's positional cap fired on the merged list; only secondary items were cut."""
        self.capped = True
        self.secondary_cut = merged_count - kept

    def reset_run(self) -> None:
        """A whole-plan re-run (the continuation fallback) starts from a clean state."""
        self.merged, self.merged_step, self.current_step = False, None, None
        self.deduped_secondary, self.secondary_cut, self.capped = 0, 0, False
        self.last_observed = None
        self.last_missing_tokens = ()

    # ── observe a later operator ─────────────────────────────────────────────

    def observe(self, args: dict[str, Any]) -> None:
        """Count the reserved items a later operator's input no longer holds.

        The claim is made only where it is true. The input must be materialised: inside a bundle
        (and in the continuation's Shape A) a later step's evidence arg is a deferred ``$stepN``
        sentinel, and the earlier step has not run yet. Every producer of that input must be a
        rank or filter: an input condensed by a summarize or generate holds no item tokens by
        design, and an input with no ``$stepN`` producer is not an operator's output at all.
        """
        feeders = {p.removeprefix("operator_") for p in self.current_producers}
        if not feeders or not feeders <= _DROPPING_OPERATORS:
            return
        if any(_holds_deferred_ref(args[k]) for k in EVIDENCE_KEYS if k in args):
            return
        blob = " ".join(str(args[k]) for k in EVIDENCE_KEYS if k in args)
        gone = tuple(i.token for i in self.items if i.token not in blob)
        missing = len(gone)
        self.last_missing_tokens = gone
        self.last_observed = {"step": -1 if self.current_step is None else self.current_step,
                              "missing": missing, "of": len(self.items)}

    # ── wire ─────────────────────────────────────────────────────────────────

    def report(self) -> dict[str, Any]:
        return {
            "reserved_items": len(self.items),
            "reserved_chars": sum(len(i.text) for i in self.items),
            "merged": self.merged,
            "merged_at_step": self.merged_step if self.merged else None,
            "over_share_dropped": self.over_share_dropped,
            "deduped_secondary": self.deduped_secondary,
            "secondary_cut": self.secondary_cut,
            "capped": self.capped,
            "dropped_before_terminal": (
                dict(self.last_observed) if self.last_observed and self.last_observed["missing"] else None
            ),
        }

    def disclosures(self) -> list[str]:
        lines: list[str] = []
        if not self.merged:
            lines.append(
                "[evidence request: primary evidence was read but the plan has no operator step that "
                "consumes retrieved evidence, so it was not merged into a reduction]"
            )
        if self.over_share_dropped:
            lines.append(
                f"[evidence request: {self.over_share_dropped} primary item(s) exceeded the reserved "
                f"share of {RESERVED_SHARE_MAX} and were not merged]"
            )
        obs = self.last_observed
        if obs and obs["missing"]:
            lines.append(
                f"[evidence request: {obs['missing']} of {obs['of']} reserved primary items did not "
                "reach the final reduction; an intermediate operator dropped them]"
            )
        return lines


def _holds_deferred_ref(value: Any) -> bool:
    if isinstance(value, str):  # a list of refs is JSON-serialised before the operator sees it
        return DEFERRED_REF_KEY in value
    if isinstance(value, dict):
        return bool(value.get(DEFERRED_REF_KEY)) or any(_holds_deferred_ref(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_holds_deferred_ref(v) for v in value)
    return False


def reserved_stream_is_evidence(stream: ReservedStream | None) -> bool:
    """Whether the runner merged a reserved stream into a reduction and it holds items.

    The one predicate behind both zero-evidence guards (``nx_answer``'s empty-retrieval guard and the
    continuation handoff's ``_prefix_produced_zero_evidence``): a stream no operator consumed
    (``merged`` false) reached no reducer and is not evidence the reducer saw.
    """
    return stream is not None and stream.merged and bool(stream.items)


_ACTIVE: ContextVar[ReservedStream | None] = ContextVar("nexus_reserved_stream", default=None)


def active_reserved_stream() -> ReservedStream | None:
    return _ACTIVE.get()


@contextlib.contextmanager
def reserved_scope(stream: ReservedStream | None) -> Iterator[None]:
    """Make *stream* the one operator hydration merges for the duration of the block."""
    token = _ACTIVE.set(stream)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def set_reserved_step(index: int | None, *, feeds_from: Sequence[str] = ()) -> None:
    """Tell the active stream which plan step is about to hydrate (no-op without a stream).

    *feeds_from* names the tools that produced the step's evidence args (the ``$stepN`` refs in
    its raw args). Only a producer that keeps or reorders items (rank, filter) lets the stream
    call a missing reserved item "dropped".
    """
    stream = _ACTIVE.get()
    if stream is not None:
        stream.current_step = index
        stream.current_producers = tuple(feeds_from)
