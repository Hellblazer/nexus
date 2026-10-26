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
reduction route sees the same evidence. Later operators are only observed: the stream counts how
many reserved items are missing from their input, so a drop by an intermediate operator (an
unbundled rank) is reported rather than silent (critique observation 13).

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

from nexus.plans.primary_evidence import PrimaryEvidence, SelectedChunk

__all__ = [
    "RESERVED_SHARE_MAX",
    "ReservedItem",
    "ReservedStream",
    "active_reserved_stream",
    "reserved_scope",
    "set_reserved_step",
]

#: Reserved items one run may merge. Calibration constant; must stay below
#: ``runner._OPERATOR_MAX_INPUTS`` (100) so plan evidence keeps room (a test pins this).
RESERVED_SHARE_MAX: int = 50

#: Operator args that carry the evidence a later operator reduces.
_EVIDENCE_KEYS: tuple[str, ...] = ("items", "inputs", "content", "context", "groups")

_SOURCE_MARKER = "[source: "


@dataclass(frozen=True)
class ReservedItem:
    token: str
    chash: str
    raw: str
    text: str

    @classmethod
    def record(cls, ident: str, text: str, *, label: str = "") -> ReservedItem:
        """A reserved item that is not a T3 chunk (the T2 lifecycle records, nexus-jypmj.1.7):
        no chash, so only the text dedupe applies. *ident* names it in the header and the token."""
        token = f"primary:{ident}"
        head = f"{label}; " if label else ""
        return cls(token=token, chash="", raw=text, text=f"[primary record {head}{token}]\n{text}")


def _item(chunk: SelectedChunk) -> ReservedItem:
    token = f"primary:{chunk.chash[:12]}"
    where = f"{chunk.source} ({chunk.doc_id}) chunk {chunk.position}"
    if chunk.label:
        where += f", section {chunk.label}"
    return ReservedItem(
        token=token, chash=chunk.chash, raw=chunk.text,
        text=f"[primary source {where}; {token}]\n{chunk.text}",
    )


def _strip_source_note(text: str) -> str:
    if text.startswith(_SOURCE_MARKER):
        _, _, rest = text.partition("\n")
        return rest
    return text


@dataclass
class ReservedStream:
    """One run's reserved primary evidence and what the runner did with it."""

    items: tuple[ReservedItem, ...]
    over_share_dropped: int = 0
    merged: bool = False
    merged_step: int | None = None
    current_step: int | None = None
    deduped_secondary: int = 0
    secondary_cut: int = 0
    capped: bool = False
    #: ``{"step", "missing", "of"}`` for the most recent later operator (the terminal one at
    #: the end of the run); ``None`` until a later operator is observed.
    last_observed: dict[str, int] | None = None
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

    # ── observe a later operator ─────────────────────────────────────────────

    def observe(self, args: dict[str, Any]) -> None:
        blob = " ".join(str(args[k]) for k in _EVIDENCE_KEYS if k in args)
        missing = sum(1 for i in self.items if i.token not in blob)
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


def set_reserved_step(index: int | None) -> None:
    """Tell the active stream which plan step is about to hydrate (no-op without a stream)."""
    stream = _ACTIVE.get()
    if stream is not None:
        stream.current_step = index
