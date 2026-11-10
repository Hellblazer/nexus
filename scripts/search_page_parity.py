# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tu8wp.3: pure result-parity arithmetic for ``measure_search_per_collection.py``.

The route (``search-per-collection``) caps each collection at ``per_collection_k``
rows; the batched path runs one flat LIMIT per batch, so one dense collection can
supply far more rows there. Raw row counts therefore differ (138 vs 165 on the cloud)
while the page a user sees can still match. This module answers the question that
counts, with no network and no engine:

- ``kept_counts``: per-collection rows each path kept, from ``SearchDiagnostics``;
- ``page_rows`` / ``compare_pages``: the top-n page of each path after the normal
  ranking and boosts, their overlap, and the rows only one path returned;
- the boost-window case: a batched-only page row whose rank inside its own
  collection is past that collection's route ``k``. The route can never return such
  a row (it stops at ``k`` per collection), so any count above 0 is a recall signal.
  The boost window (``boost_window_size(30) == 90``) is wider than the code ``k``
  (60), which is how a code row ranked 61-90 in its collection can reach the page on
  the batched path only.

Rows are duck-typed (``id``, ``collection``, ``distance``), and diagnostics too
(``kept_for`` / ``per_collection`` / ``kept``), so the tests need no engine.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

#: Collections named per line before a line says "+N more".
_LINE_NAMES = 4


def row_key(row: Any) -> str:
    """A page row's identity: collection and id (one chunk hash can sit in two collections)."""
    return f"{row.collection}::{row.id}"


def collection_ranks(pool: Iterable[Any]) -> dict[str, int]:
    """``row_key -> 1-based rank`` of each row among ITS OWN collection's rows in *pool*,
    nearest first (ties by id). Calibration scales a collection's distances by one
    factor, so the within-collection order is the raw-distance order."""
    by_col: dict[str, list[Any]] = {}
    for r in pool:
        by_col.setdefault(r.collection, []).append(r)
    ranks: dict[str, int] = {}
    for rows in by_col.values():
        for i, r in enumerate(sorted(rows, key=lambda x: (x.distance, x.id)), start=1):
            ranks[row_key(r)] = i
    return ranks


@dataclass(frozen=True)
class PageRow:
    key: str
    collection: str
    #: 1-based rank within its own collection, in the pool of the path that returned it
    rank: int


def page_rows(page: Sequence[Any], pool: Iterable[Any]) -> list[PageRow]:
    """The *page* rows (already ranked and boosted) with their in-collection rank in *pool*."""
    ranks = collection_ranks(pool)
    return [PageRow(row_key(r), r.collection, ranks.get(row_key(r), 0)) for r in page]


def kept_counts(diags: Iterable[Any]) -> dict[str, int]:
    """Per-collection rows kept, summed over the ``SearchDiagnostics`` a search returned.

    Anything without ``kept_for`` is skipped. A collection is read through ``kept_for``
    (the engine's ``kept`` figure, else ``raw - dropped``)."""
    out: dict[str, int] = {}
    for d in diags:
        if not hasattr(d, "kept_for"):
            continue
        for name in {*getattr(d, "per_collection", {}), *getattr(d, "kept", {})}:
            out[name] = out.get(name, 0) + d.kept_for(name)
    return out


def kept_diff(route: dict[str, int], batched: dict[str, int]) -> list[tuple[str, int, int]]:
    """``(collection, route kept, batched kept)`` where the two differ, largest gap first."""
    rows = [
        (c, route.get(c, 0), batched.get(c, 0))
        for c in {*route, *batched}
        if route.get(c, 0) != batched.get(c, 0)
    ]
    return sorted(rows, key=lambda t: (-abs(t[1] - t[2]), t[0]))


@dataclass(frozen=True)
class PageParity:
    #: the page size asked for
    n: int
    route_size: int
    batched_size: int
    overlap: int
    route_only: list[str]
    batched_only: list[str]
    #: batched-only page rows ranked past their collection's route k:
    #: ``{"key", "collection", "rank", "k"}``
    past_k: list[dict[str, Any]] = field(default_factory=list)
    #: both pages have a top row and it is the same row
    top1_agree: bool = False
    #: how many of the first three rows the two pages share (0..3, order ignored)
    top3_overlap: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def compare_pages(
    route: Sequence[PageRow], batched: Sequence[PageRow], n: int, route_k: Callable[[str], int],
) -> PageParity:
    """Overlap of the two pages, the exclusive rows, and the past-k rows.

    *route_k* maps a collection to the per-collection cap the route sends for it. A
    batched-only row whose rank in its collection exceeds that cap is one the route
    cannot return at all, whatever the ranking does with it."""
    route_keys = {r.key for r in route}
    batched_keys = {r.key for r in batched}
    batched_only = [r for r in batched if r.key not in route_keys]
    past = [
        {"key": r.key, "collection": r.collection, "rank": r.rank, "k": route_k(r.collection)}
        for r in batched_only
        if r.rank > route_k(r.collection)
    ]
    return PageParity(
        n=n, route_size=len(route), batched_size=len(batched),
        overlap=len(route_keys & batched_keys),
        route_only=[r.key for r in route if r.key not in batched_keys],
        batched_only=[r.key for r in batched_only],
        past_k=past,
        top1_agree=bool(route and batched and route[0].key == batched[0].key),
        top3_overlap=len({r.key for r in route[:3]} & {r.key for r in batched[:3]}),
    )


def aggregate_parity(parities: Sequence[PageParity]) -> dict[str, Any]:
    """Run-level summary: mean overlap, runs with any batched-only past-k row, and the max."""
    if not parities:
        return {
            "runs": 0, "n": None, "mean_overlap": None, "min_overlap": None,
            "mean_batched_only": None, "runs_with_past_k": 0, "max_past_k_rows": 0,
            "max_past_k_rank": 0, "top1_agree_rate": None, "mean_top3_overlap": None,
        }
    runs = len(parities)
    return {
        "runs": runs,
        "n": parities[0].n,
        "mean_overlap": sum(p.overlap for p in parities) / runs,
        "min_overlap": min(p.overlap for p in parities),
        "mean_batched_only": sum(len(p.batched_only) for p in parities) / runs,
        "runs_with_past_k": sum(1 for p in parities if p.past_k),
        "max_past_k_rows": max(len(p.past_k) for p in parities),
        "max_past_k_rank": max((f["rank"] for p in parities for f in p.past_k), default=0),
        "top1_agree_rate": sum(1 for p in parities if p.top1_agree) / runs,
        "mean_top3_overlap": sum(p.top3_overlap for p in parities) / runs,
    }


@dataclass(frozen=True)
class PairRecord:
    """One round's route/batched pair: its parity (``None`` when it was skipped) and the
    failed-collection counts of each run. A pair where either run had failed collections is
    EXCLUDED from the summary (a cold route that exhausts its fan-out budget compares fewer
    collections, and that says nothing about pool depth); a pair whose page could not be
    built or whose rerank degraded is SKIPPED (*skipped* names why)."""

    round: int
    parity: PageParity | None
    route_failed: int
    batched_failed: int
    skipped: str | None = None

    @property
    def excluded(self) -> bool:
        return self.parity is not None and bool(self.route_failed or self.batched_failed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "round": self.round, "route_failed": self.route_failed,
            "batched_failed": self.batched_failed, "excluded": self.excluded,
            "skipped": self.skipped, **(self.parity.as_dict() if self.parity else {}),
        }


def summarize_pairs(records: Sequence[PairRecord]) -> dict[str, Any]:
    """:func:`aggregate_parity` over the pairs that count, plus how many did not."""
    counted = [r.parity for r in records if r.parity is not None and not r.excluded]
    return {
        **aggregate_parity(counted),
        "pairs": len(records),
        "pairs_excluded_failed": sum(1 for r in records if r.excluded),
        "pairs_skipped": sum(1 for r in records if r.skipped),
    }


def boosted_page(results: list, limit: int, model_of: Callable[[str], str | None] | None = None) -> list:
    """The first page the MCP ``search`` tool returns for *limit* (mcp/core.py ``search``):
    ``apply_ranking_boosts`` with the config's ``hybrid_default``, ``get_tuning_config()``,
    ``window=boost_window_size(limit)`` and the search's own *model_of*, then
    ``apply_file_diversity_cap(window=...)``, cut to *limit*. The tool inlines these calls
    (there is no helper to share), so the same calls are made here, in the same order."""
    from nexus.config import get_tuning_config, load_config  # noqa: PLC0415
    from nexus.search_engine import (  # noqa: PLC0415
        apply_file_diversity_cap,
        apply_ranking_boosts,
        boost_window_size,
    )

    window = boost_window_size(limit)
    boosted = apply_ranking_boosts(
        list(results),
        hybrid=load_config().get("search", {}).get("hybrid_default", False),
        tuning=get_tuning_config(),
        window=window,
        model_of=model_of,
    )
    return apply_file_diversity_cap(boosted, window=window)[:limit]


def shape_corpora(shape: str) -> tuple[str, ...]:
    """The corpus prefixes the real surface searches by default for *shape*, read from where it
    defines them (never copied): ``mcp`` is the default of the MCP ``search`` tool's ``corpus``
    parameter (a comma-separated string), ``cli`` the default of ``nx search``'s ``--corpus``
    option."""
    if shape == "mcp":
        import inspect  # noqa: PLC0415

        from nexus.mcp.core import search  # noqa: PLC0415

        default = inspect.signature(search).parameters["corpus"].default
        return tuple(p.strip() for p in default.split(",") if p.strip())
    if shape == "cli":
        from nexus.commands.search_cmd import search_cmd  # noqa: PLC0415

        default = next(p.default for p in search_cmd.params if p.name == "corpus")
        return tuple(part.strip() for raw in default for part in raw.split(",") if part.strip())
    raise ValueError(f"unknown shape {shape!r}")


def cols_for(cols: "Sequence[str] | dict[str, list[str]]", shape: str) -> list[str]:
    """The collections to search for *shape*: *cols* itself when one list serves every shape, else
    the shape's own entry (each shape has its own default corpora)."""
    return list(cols[shape] if isinstance(cols, dict) else cols)


def shape_k_cap(shape: str) -> int | None:
    """The ``per_collection_k_cap`` the real surface passes for *shape*: the MCP tool's own
    constant for ``mcp`` (read lazily, never copied), none for ``cli`` (``nx search`` is
    uncapped). Without server rerank only: the MCP leaves the cap off when it reranks."""
    if shape != "mcp":
        return None
    from nexus.mcp.core import _MCP_PER_COLLECTION_K  # noqa: PLC0415

    return _MCP_PER_COLLECTION_K


def make_route_k(
    cols: "Sequence[str] | dict[str, list[str]]", shapes: dict[str, tuple[int, bool]],
) -> Callable[[str], Callable[[str], int]]:
    """``shape -> (collection -> route k)``: the per-collection cap the route sends for
    each collection, from the client's own sizing (``_per_collection_request_sizes`` over
    the collection's embedding-model group, whose widest over-fetch multiplier it takes),
    lowered by the shape's ``per_collection_k_cap`` the way ``search_cross_corpus`` does
    (``min(k, max(cap, n))``)."""

    def route_k_of(shape: str) -> Callable[[str], int]:
        from nexus.search_engine import (  # noqa: PLC0415
            _group_collections_by_embedding_model,
            _overfetch_multiplier,
            _per_collection_request_sizes,
        )

        n, rerank = shapes[shape][:2]
        cap = shape_k_cap(shape)
        shape_cols = cols_for(cols, shape)
        ks: dict[str, int] = {}
        for group in _group_collections_by_embedding_model(shape_cols):
            mult = max((_overfetch_multiplier(c) for c in group), default=2)
            per_k, _limit = _per_collection_request_sizes(n, mult, rerank=rerank)
            if cap is not None:
                per_k = min(per_k, max(cap, n))
            ks.update({c: per_k for c in group})
        return lambda collection: ks[collection]

    return route_k_of


def _names(items: Sequence[str]) -> str:
    head = ", ".join(items[:_LINE_NAMES])
    more = len(items) - _LINE_NAMES
    return f"[{head}{f', +{more} more' if more > 0 else ''}]"


def format_parity_line(shape: str, round_no: int, p: PageParity, note: str = "") -> str:
    """One per-pair line: page size, overlap, top-1 and top-3 agreement, the exclusive ids
    and the past-k rows. *note* is appended (``excluded: ...``, ``pre-rerank pool order``)."""
    past = (
        " " + _names([f"{f['key']} rank {f['rank']}>{f['k']}" for f in p.past_k])
        if p.past_k else ""
    )
    return (
        f"  {shape:<4} parity  round {round_no:>2}  page {p.n}  overlap {p.overlap}  "
        f"top1 {'same' if p.top1_agree else 'diff'}  top3 {p.top3_overlap}/3  "
        f"route-only {len(p.route_only)} {_names(p.route_only)}  "
        f"batched-only {len(p.batched_only)} {_names(p.batched_only)}  "
        f"past-k {len(p.past_k)}{past}" + (f"  ({note})" if note else "")
    )


def format_summary_line(shape: str, s: dict[str, Any], skipped_why: str = "") -> str:
    """The per-shape summary line, including how many pairs did not count."""
    why = f" ({skipped_why})" if skipped_why else ""
    return (
        f"[{shape}] PARITY (timed rounds) {s['pairs_excluded_failed']} of {s['pairs']} pairs excluded "
        f"(failed collections), {s['pairs_skipped']} skipped{why}; counted {s['runs']}  page n={s['n']}  "
        f"mean overlap {s['mean_overlap']}  min {s['min_overlap']}  top1 agree {s['top1_agree_rate']}  "
        f"mean top3 {s['mean_top3_overlap']}/3  "
        f"runs with a batched-only row past route k {s['runs_with_past_k']}  "
        f"max past-k rows {s['max_past_k_rows']}  deepest rank {s['max_past_k_rank']}"
    )


def format_kept_line(shape: str, round_no: int, route: dict[str, int], batched: dict[str, int]) -> str:
    """One per-pair line: kept totals per path and the collections whose counts differ."""
    diff = kept_diff(route, batched)
    shown = [f"{c} {r}/{b}" for c, r, b in diff[:_LINE_NAMES]]
    more = len(diff) - _LINE_NAMES
    tail = f" +{more} more" if more > 0 else ""
    return (
        f"  {shape:<4} kept    round {round_no:>2}  kept route {sum(route.values())} "
        f"batched {sum(batched.values())}  differ {len(diff)}"
        + (f"  [{', '.join(shown)}{tail}] (collection route/batched)" if diff else "")
    )
