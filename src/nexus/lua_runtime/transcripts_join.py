# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Join the pages of a ``transcripts`` run (RDR-240 Technical Design 5; nexus-qo7ie.31).

``procedures/transcripts.lua`` returns one page at a time: ``{rows, truncated_records,
until, next_since}``. A caller that wants the whole window runs again with ``since`` set to
``next_since`` and ``to`` set to the returned ``until``, until a page has no ``next_since``,
then hands the pages here. :func:`join_pages` is the one place that knows how each query's
rows combine, so a page that ends mid-window never changes a count.

How rows combine, per query (the Lua script's header says the same; a test pins the field
sets of both against a real run):

- ``memory``, ``tools`` and ``loads`` return one row per session and name. Pages
  **concatenate** their rows; no field is combined.
- ``agents`` (key ``kind``, ``agent_type``, ``model``): ``dispatches``, ``runs``,
  ``turns``, ``errors``, ``cost_estimate_usd`` and ``unpriced`` are ``sum``;
  ``max_context`` is ``max``; ``context`` is ``mean``.
- ``skills`` (key ``scope``, ``kind``, ``name``): ``count``, ``runs_with_skill``,
  ``long_runs`` and ``long_runs_without_skill`` are ``sum``.
- ``violations`` (key ``rule``): ``count`` and ``main_sessions`` are ``sum``. Every page
  carries a row for every rule, zeros included, so each rule's ``main_sessions`` is the
  window's whole denominator once the pages are summed; ``per_100`` is derived here.
- ``procedures`` (key ``procedure``): ``runs`` and ``hand_written`` are ``sum``.

A ``mean`` field is carried as ``<field>_total`` and ``<field>_count`` and summed as two
``sum`` fields; the join derives ``<field>_mean`` (None when the count is zero), so a mean of
means is never taken. ``truncated_records`` sums.

Standard library only.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Shape:
    """How one query's rows combine. No ``keys`` means per-session rows, concatenated."""

    keys: tuple[str, ...] = ()
    sum: tuple[str, ...] = ()
    max: tuple[str, ...] = ()
    mean: tuple[str, ...] = ()

    @property
    def keyed(self) -> bool:
        return bool(self.keys)

    @property
    def fields(self) -> frozenset[str]:
        """Every field a keyed row carries (a mean field as its total and its count)."""
        means = {f"{m}{suffix}" for m in self.mean for suffix in ("_total", "_count")}
        return frozenset(self.keys) | frozenset(self.sum) | frozenset(self.max) | means


#: The keyed queries. The Lua script's tags must agree; a test compares field sets.
SHAPES: Mapping[str, Shape] = {
    "agents": Shape(
        keys=("kind", "agent_type", "model"),
        sum=("dispatches", "runs", "turns", "errors", "cost_estimate_usd", "unpriced"),
        max=("max_context",),
        mean=("context",),
    ),
    "skills": Shape(
        keys=("scope", "kind", "name"),
        sum=("count", "runs_with_skill", "long_runs", "long_runs_without_skill"),
    ),
    "violations": Shape(keys=("rule",), sum=("count", "main_sessions")),
    "procedures": Shape(keys=("procedure",), sum=("runs", "hand_written")),
    "memory": Shape(),
    "tools": Shape(),
    "loads": Shape(),
}

#: The fields a per-session row may carry (a superset; a row leaves out what is nil).
SESSION_FIELDS: Mapping[str, frozenset[str]] = {
    "memory": frozenset(
        {"session_id", "main", "agent_id", "kind", "name", "type", "count", "last_use", "size"}
    ),
    "tools": frozenset(
        {"session_id", "main", "agent_id", "kind", "name", "server", "sub", "deferred", "called",
         "count", "last_use", "size"}
    ),
    "loads": frozenset(
        {"session_id", "main", "agent_id", "kind", "name", "type", "count", "last_use", "size"}
    ),
}


class JoinError(ValueError):
    """The pages cannot be joined as one window."""


def _num(row: Mapping[str, Any], field: str) -> Any:
    value = row.get(field)
    return 0 if value is None else value


def _join_keyed(shape: Shape, pages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: dict[tuple[Any, ...], dict[str, Any]] = {}
    for page in pages:
        for row in page.get("rows") or ():
            key = tuple(row.get(k) for k in shape.keys)
            into = out.get(key)
            if into is None:
                into = {k: row.get(k) for k in shape.keys}
                for f in shape.sum:
                    into[f] = 0
                for f in shape.max:
                    into[f] = 0
                for f in shape.mean:
                    into[f"{f}_total"] = 0
                    into[f"{f}_count"] = 0
                out[key] = into
            for f in shape.sum:
                into[f] += _num(row, f)
            for f in shape.max:
                into[f] = max(into[f], _num(row, f))
            for f in shape.mean:
                into[f"{f}_total"] += _num(row, f"{f}_total")
                into[f"{f}_count"] += _num(row, f"{f}_count")
    rows = [out[k] for k in sorted(out, key=lambda k: tuple(str(p) for p in k))]
    for row in rows:
        for f in shape.mean:
            n = row[f"{f}_count"]
            row[f"{f}_mean"] = row[f"{f}_total"] / n if n else None
    return rows


def join_pages(
    query: str, pages: Sequence[Mapping[str, Any]], *, allow_partial: bool = False
) -> dict[str, Any]:
    """The pages of one ``transcripts`` window as a single result.

    Returns ``{rows, truncated_records, until, pages}``. Raises :class:`JoinError` for an
    unknown query, no pages, pages that name different ``until`` values, a page before the
    last without a ``next_since``, or (unless ``allow_partial``) a last page that still has
    one.
    """
    shape = SHAPES.get(query)
    if shape is None:
        raise JoinError(f"unknown query {query!r}; one of {', '.join(sorted(SHAPES))}")
    if not pages:
        raise JoinError("no pages to join")
    untils = {p.get("until") for p in pages}
    if len(untils) != 1:
        raise JoinError(f"pages cover different windows: until {sorted(map(str, untils))}")
    for page in pages[:-1]:
        if not page.get("next_since"):
            raise JoinError("a page before the last has no next_since")
    if pages[-1].get("next_since") and not allow_partial:
        raise JoinError(f"the window is not done: next_since {pages[-1]['next_since']!r}")
    if shape.keyed:
        rows = _join_keyed(shape, pages)
        if query == "violations":
            for row in rows:
                n = row["main_sessions"]
                row["per_100"] = row["count"] * 100 / n if n else None
    else:
        rows = [dict(r) for p in pages for r in (p.get("rows") or ())]
    return {
        "rows": rows,
        "truncated_records": sum(_num(p, "truncated_records") for p in pages),
        "until": next(iter(untils)),
        "pages": len(pages),
    }
