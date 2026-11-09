# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Which row survives a merge of stale note URIs (nexus-bivz3.24; Sam's ruling "Chunks decide", ask
bivz324-merge-winner).

A merge candidate is a stale note S (``nx-note://X/t`` on a row living in ``Y``) whose target ``nx-note://Y/t`` is
held by a live twin W, or a stale row that would rewrite onto the same target as another stale row (a sibling).
``POST /v1/catalog/merge`` moves neither chunks nor aspects nor highlights, so the survivor must be the row that
holds the content. The rule, as a total order over a group of rows:

1. a row that holds chunks beats one that holds none;
2. among rows that hold chunks, the strictly newer ``indexed_at`` wins (a missing or unreadable one is the oldest);
3. a tie goes to the live twin;
4. a tie among stale rows goes to the lowest tumbler.

For one stale row and its twin that is: the twin wins unless the stale row holds chunks and the twin holds none,
or both hold chunks and the stale row is newer. "Holds chunks" is ``catalog_documents.chunk_count``, the column
``nx catalog merge`` reads for its refusal (nexus-bivz3.27), not a manifest recount. The rule, run on the census, never
picks a chunkless winner over a chunk holder, but the census can be a round old: the verb reads both rows' count
again just before each merge and refuses a pair that would fold a chunk holder into a chunkless winner.
When both rows hold chunks, the loser's chunks and meta (tags, ttl) stay behind the alias.

A group is the twin plus every stale row that points at it, so two stale rows sharing one twin do not chain-merge.
The functions here are pure: they read the census's merge entries and decide; the verb does the calling.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Candidate:
    """One row in a merge group, as the census describes it."""

    tumbler: str
    chunk_count: int
    indexed_at: str | None
    is_twin: bool
    uri: str  # the row's actual source_uri now (the twin's may be the chroma:// spelling)


@dataclass(frozen=True)
class Decision:
    """The winner of one group, the rows that merge into it, and why."""

    winner: Candidate
    losers: tuple[Candidate, ...]
    rule: str
    target_uri: str = ""
    members: tuple[Candidate, ...] = field(default_factory=tuple)

    @property
    def rewrite_needed(self) -> bool:
        """A stale winner keeps its stale URI through the merge, so it is rewritten after."""
        return not self.winner.is_twin

    @property
    def merge_order(self) -> tuple[Candidate, ...]:
        """The order the losers are merged in. Under a stale winner the twin holds the target URI, so it goes LAST
        (and the winner is rewritten at once): the window in which nothing holds the URI is then one request, and a
        refused or failed earlier merge leaves the twin in place. Otherwise tumbler order."""
        if not self.rewrite_needed:
            return self.losers
        return tuple(sorted(self.losers, key=lambda c: c.is_twin))


@dataclass(frozen=True)
class Plan:
    decisions: list[Decision]
    skipped: list[dict]


def _instant(value: str | None) -> datetime.datetime:
    """``indexed_at`` as an aware UTC instant; missing or unreadable is the oldest possible."""
    oldest = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
    if not isinstance(value, str) or not value:
        return oldest
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return oldest
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def _tumbler_key(tumbler: str) -> tuple:
    """Tumblers compare as numbers (1.1.2 before 1.1.10); anything else falls back to text after the numeric ones."""
    try:
        return (0, tuple(int(p) for p in tumbler.split(".")))
    except ValueError:
        return (1, tumbler)


def _rank(c: Candidate) -> tuple:
    """Higher is better. Rank 1-3 of the rule; the lowest-tumbler tie-break is applied by the caller."""
    holds = c.chunk_count > 0
    return (holds, _instant(c.indexed_at) if holds else _instant(None), c.is_twin)


def decide_group(members: list[Candidate]) -> Decision:
    """Pick the winner of ``members`` (two or more rows) by the rule above."""
    if len(members) < 2:
        raise ValueError("a merge group needs at least two rows")
    best = max(_rank(c) for c in members)
    top = [c for c in members if _rank(c) == best]
    winner = min(top, key=lambda c: _tumbler_key(c.tumbler))
    losers = tuple(
        sorted(
            (c for c in members if c is not winner),
            key=lambda c: _tumbler_key(c.tumbler),
        )
    )
    twin = next((c for c in members if c.is_twin), None)
    holders = [c for c in members if c.chunk_count > 0]
    if twin is not None:
        if winner.is_twin:
            if not holders:
                rule = "no row holds chunks: the twin wins by default"
            elif len(holders) == 1:
                rule = "the twin holds the only chunks"
            elif any(
                _instant(c.indexed_at) == _instant(winner.indexed_at)
                for c in holders
                if c is not winner
            ):
                rule = "both hold chunks at the same time: ties go to the twin"
            else:
                rule = "both hold chunks and the twin is newer"
        elif twin.chunk_count == 0:
            if len([c for c in holders if not c.is_twin]) == 1:
                rule = "the stale row holds the only chunks"
            else:
                rule = (
                    "several stale rows hold chunks and the twin none: the newest wins "
                    "(the lowest tumbler on a tie)"
                )
        else:
            rule = "both hold chunks and the stale row is newer"
    elif not holders:
        rule = "no row holds chunks: the lowest tumbler wins"
    elif len(holders) == 1:
        rule = "the row holds the only chunks"
    elif len({_instant(c.indexed_at) for c in holders}) == 1:
        rule = "the rows that hold chunks were indexed at the same time: the lowest tumbler wins"
    else:
        rule = "the newest of the rows that hold chunks"
    return Decision(winner=winner, losers=losers, rule=rule, members=tuple(members))


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def plan(entries: list[dict]) -> Plan:
    """Group the census's merge entries and decide each group. An entry that lacks what the rule reads is skipped
    with its reason, never guessed at; so is a stale sibling whose group-mates are not in the report (they show
    up in a later round, once this round's merges have moved the window)."""
    skipped: list[dict] = []
    twin_groups: dict[str, list[Candidate]] = {}
    twin_info: dict[str, Candidate] = {}
    sibling_groups: dict[str, list[tuple[dict, Candidate]]] = {}

    def skip(e: dict, why: str) -> None:
        skipped.append({"tumbler": e.get("tumbler"), "why": why})

    for e in entries:
        tumbler, chunks = e.get("tumbler"), _int(e.get("chunk_count"))
        if not isinstance(tumbler, str) or chunks is None:
            skip(e, "the entry has no usable tumbler or chunk_count")
            continue
        stale = Candidate(
            tumbler, chunks, e.get("indexed_at"), False, str(e.get("source_uri") or "")
        )
        reason = e.get("reason")
        if reason == "twin":
            twin, twin_chunks = e.get("twin"), _int(e.get("twin_chunk_count"))
            if not isinstance(twin, str) or not twin or twin_chunks is None:
                skip(e, "the entry has no usable twin or twin_chunk_count")
                continue
            if twin not in twin_info:
                twin_info[twin] = Candidate(
                    twin,
                    twin_chunks,
                    e.get("twin_indexed_at"),
                    True,
                    str(e.get("twin_source_uri") or e.get("new_uri") or ""),
                )
            twin_groups.setdefault(twin, []).append(stale)
        elif reason == "sibling":
            target = e.get("new_uri")
            if not isinstance(target, str) or not target:
                skip(e, "the sibling entry has no new_uri to group by")
                continue
            sibling_groups.setdefault(target, []).append((e, stale))
        else:
            skip(e, f"unknown merge reason {reason!r}")

    decisions: list[Decision] = []
    for twin, stales in twin_groups.items():
        d = decide_group([*stales, twin_info[twin]])
        target = next(
            (
                e.get("new_uri")
                for e in entries
                if e.get("twin") == twin and e.get("new_uri")
            ),
            "",
        )
        decisions.append(Decision(d.winner, d.losers, d.rule, str(target), d.members))
    for target, pairs in sibling_groups.items():
        if len(pairs) < 2:
            skip(
                pairs[0][0],
                "alone in the report: its sibling is not in the report yet, left for the next round",
            )
            continue
        d = decide_group([c for _, c in pairs])
        decisions.append(Decision(d.winner, d.losers, d.rule, target, d.members))
    return Plan(decisions, skipped)
