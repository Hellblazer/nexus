# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measurement C: cross-family inversions on each ranking surface.

The same corpus sits in a bge collection and a Voyage collection (bge-family and
Voyage-family copies of the same chunk texts). For each query, a surface returns
one merged list over both. An INVERSION is an irrelevant chunk of one family
ranking above the best relevant chunk of the other family; both directions are
counted, per query, out of 20 (210-research-17 counted 12 and 5 of 20 at the old
default factor, and 3 and 1 at factors 1.15 to 1.30).

Surfaces, each driven by the real nexus code (never a re-implementation of its
scoring):

1. ``nx search`` with rerank on: ``commands.search_cmd.search_cmd`` invoked over
   both collections (server rerank, ``rerank_score`` merge, file-diversity cap).
2. MCP ``query`` plain path, rerank off: ``search_engine.search_cross_corpus``
   then ``apply_ranking_boosts`` (the calibrated ordering), as ``query`` does
   before it groups by document. Not ``query`` itself: its identical-chunk
   collapse (GH #1524) would drop the second family's copy of every chunk, and
   the two families hold the same texts here.
3. The catalog-routed tools' merge: ``mcp.core._grouped_combined_query`` over
   ``search_metadata_scoped``, calibrated distance across model groups.

Surface 2 and 3 also run a factor grid (bge factor 1.0 to 1.6) by patching the
calibration table the real code reads; retrieval is done once per query and
re-ordered per factor.
"""
from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import Callable, Iterator, Sequence

import p43_stats as st
from p43_corpus import Corpus
from p43_queries import Query

#: 210-research-17 (spike, chas, develop e34467fdb): queries with an inversion, of 20.
#: ``dir1`` = an irrelevant bge chunk above the best relevant Voyage chunk, ``dir2`` = the reverse.
REFERENCE = {
    "default_factor_0.818": {"dir1": 12, "dir2": 5},
    "factor_1.0": {"dir1": 7, "dir2": 2},
    "factor_1.15_to_1.30": {"dir1": 3, "dir2": 1},
}
#: The P1.5 fixture bound the bead's pass criterion names: no worse than this, per 20.
FIXTURE_BOUND = {"dir1": 3, "dir2": 1}
FACTOR_GRID = tuple(round(1.0 + 0.1 * i, 1) for i in range(7))     # 1.0 .. 1.6
BGE_MODEL = "bge-base-en-v15-768"

#: A ranked list as a surface returns it: ``(collection, chunk_hash)``, best first.
Ranked = list[tuple[str, str]]


def family_of(collection: str) -> str:
    return "bge" if f"__{BGE_MODEL}__" in collection else "voyage"


def to_rows(ranked: Ranked, query: Query, corpus: Corpus, depth: int | None) -> list[st.RankedRow]:
    by_chash = corpus.chash_files()
    rows = ranked if depth is None else ranked[:depth]
    return [st.RankedRow(family_of(c), query.target in by_chash.get(h, set()), h) for c, h in rows]


def score_surface(
    per_query: Sequence[tuple[Query, Ranked]], corpora: dict[str, Corpus], depth: int | None,
) -> dict:
    """Inversion counts for one surface at one depth, per set and overall.

    ``dir1`` counts queries where an irrelevant bge chunk outranks the best relevant
    Voyage chunk; ``dir2`` the reverse. ``*_missing`` counts queries whose list held
    no relevant chunk of that family (that direction cannot be judged there)."""
    out: dict = {"depth": depth}
    allres: list[st.InversionResult] = []
    for kind in ("prose", "code"):
        res = [st.count_inversions(to_rows(r, q, corpora[kind], depth), "bge", "voyage")
               for q, r in per_query if q.set == kind]
        if not res:
            continue
        allres.extend(res)
        t = st.tally_inversions(res)
        out[kind] = {"queries": t.queries, "dir1_bge_over_voyage": t.a_over_b,
                     "dir2_voyage_over_bge": t.b_over_a,
                     "bge_relevant_missing": t.a_missing, "voyage_relevant_missing": t.b_missing}
    if allres:
        t = st.tally_inversions(allres)
        out["all"] = {"queries": t.queries, "dir1_bge_over_voyage": t.a_over_b,
                      "dir2_voyage_over_bge": t.b_over_a,
                      "bge_relevant_missing": t.a_missing, "voyage_relevant_missing": t.b_missing}
    return out


def within_bound(scored: dict) -> dict:
    """Per set: counts no worse than the P1.5 fixture bound (3 and 1 of 20)."""
    out = {}
    for kind in ("prose", "code"):
        s = scored.get(kind)
        if s:
            out[kind] = (s["dir1_bge_over_voyage"] <= FIXTURE_BOUND["dir1"]
                         and s["dir2_voyage_over_bge"] <= FIXTURE_BOUND["dir2"])
    return out


@contextlib.contextmanager
def bge_factor(factor: float) -> Iterator[None]:
    """Patch the calibration table the real scoring code reads so bge's factor is
    *factor* (threshold ``0.45 / factor``), restoring it on exit."""
    from nexus import scoring  # noqa: PLC0415 - nexus is imported only when C runs

    table = scoring._CALIBRATION_THRESHOLDS_BY_MODEL
    old = table[BGE_MODEL]
    table[BGE_MODEL] = scoring._CALIBRATION_BASELINE / factor
    try:
        yield
    finally:
        table[BGE_MODEL] = old


def table_factor() -> float:
    """The bge factor the calibration table holds right now."""
    from nexus import scoring  # noqa: PLC0415

    return scoring._calibration_factor_for_model(BGE_MODEL)


# ── live surfaces (real nexus code) ──────────────────────────────────────────

class LiveSurfaces:
    """The three surfaces, built on the real nexus code paths. Construct only in a
    process whose environment points nexus at the scratch engine."""

    def __init__(self, charge: Callable[[int, str], None] | None = None,
                 query_tokens: Callable[[str, int], int] | None = None,
                 rerank_tokens: Callable[[str, int], int] | None = None):
        # charge(tokens, label) is called BEFORE a call that can reach Voyage.
        # query_tokens(query, n_collections) = embed tokens of the call;
        # rerank_tokens(query, n) = Voyage rerank tokens for a surface-1 page of n.
        self._charge = charge or (lambda tokens, label: None)
        self._query_tokens = query_tokens or (lambda q, k: 0)
        self._rerank_tokens = rerank_tokens or (lambda q, n: 0)
        #: stderr lines of ``nx search`` that say something went wrong (a rerank degrade,
        #: a dropped model group); a clean-looking list over a degraded call is not a result.
        self.warnings: list[str] = []

    # surface 1
    def nx_search(self, query: str, collections: list[str], n: int) -> Ranked:
        from click.testing import CliRunner  # noqa: PLC0415

        from nexus.commands.search_cmd import search_cmd  # noqa: PLC0415

        self._charge(self._query_tokens(query, len(collections)) + self._rerank_tokens(query, n),
                     "surface1-nx-search")
        args = [query, "--json", "--n", str(n)]
        for c in collections:
            args += ["--corpus", c]
        result = CliRunner().invoke(search_cmd, args, catch_exceptions=False)
        out = result.stdout     # the JSON array; warnings go to stderr
        for line in (result.stderr or "").splitlines():
            if "degrad" in line.lower() or line.startswith(("Warning", "note:")):
                self.warnings.append(f"{query[:40]}...: {line.strip()[:240]}")
        start = out.find("[")
        if start < 0:
            raise RuntimeError(f"nx search printed no JSON array: {out[:300]!r}")
        rows = json.JSONDecoder().raw_decode(out[start:])[0]
        return [(r["collection"], r["id"]) for r in rows]

    # surface 2
    def query_retrieve(self, query: str, collections: list[str], fetch_n: int) -> tuple[list, Callable]:
        from nexus.mcp_infra import get_t3  # noqa: PLC0415
        from nexus.search_engine import search_cross_corpus  # noqa: PLC0415

        self._charge(self._query_tokens(query, len(collections)), "surface2-query")
        model_of_out: list = []
        raw = search_cross_corpus(
            query, collections, n_results=fetch_n, t3=get_t3(), where=None, catalog=None,
            link_boost=False, taxonomy=None, telemetry=None, diagnostics_out=[],
            model_of_out=model_of_out, cluster_by=None,
        )
        return raw, (model_of_out[-1] if model_of_out else None)

    def query_rank(self, raw: list, model_of: Callable | None) -> Ranked:
        from nexus.config import get_tuning_config  # noqa: PLC0415
        from nexus.search_engine import apply_ranking_boosts  # noqa: PLC0415

        ranked = apply_ranking_boosts(
            copy.deepcopy(raw), hybrid=False, tuning=get_tuning_config(), catalog=None,
            model_of=model_of,
        )
        return [(r.collection, r.id) for r in ranked]

    # surface 3
    def catalog_routed(self, query: str, collections: list[str], fetch_n: int,
                       cache: dict) -> Ranked:
        from nexus.mcp import core  # noqa: PLC0415
        from nexus.mcp_infra import get_t3  # noqa: PLC0415

        t3 = get_t3()

        def call(group: list[str]) -> list[dict]:
            key = (query, tuple(group))
            if key not in cache:
                self._charge(self._query_tokens(query, len(group)), "surface3-catalog-routed")
                cache[key] = t3.search_metadata_scoped(query, group, n_results=fetch_n)
            return cache[key]

        rows = core._grouped_combined_query(collections, call, t3=t3)
        return [(r["collection"], r.get("chash", "")) for r in rows]


def run_c(
    live: LiveSurfaces, queries: list[Query], corpora: dict[str, Corpus],
    names: dict[tuple[str, str], str], *, depths: Sequence[int] = (10, 20),
    fetch_n: int = 100, grid: Sequence[float] = FACTOR_GRID, log=print,
) -> dict:
    """Run the three surfaces over *queries* and the factor grid on 2 and 3."""
    depths = tuple(sorted(depths))
    top = depths[-1]
    table_value = table_factor()
    s1: list[tuple[Query, Ranked]] = []
    raw2: list[tuple[Query, list, Callable | None]] = []
    cache3: dict = {}
    for q in queries:
        cols = [names[(q.set, "bge")], names[(q.set, "voyage")]]
        s1.append((q, live.nx_search(q.text, cols, top)))
        raw, model_of = live.query_retrieve(q.text, cols, fetch_n)
        raw2.append((q, raw, model_of))
        log(f"[p43] C: {q.id} retrieved")
    result: dict = {
        "calibration_table_bge_factor": table_value,
        "reference_research_17": REFERENCE, "fixture_bound_per_20": FIXTURE_BOUND,
        "depths": list(depths), "surfaces": {},
        "surface1_stderr_warnings": live.warnings,
    }

    def block(per_query: list[tuple[Query, Ranked]]) -> dict:
        scored = {str(d): score_surface(per_query, corpora, d) for d in depths}
        return {"by_depth": scored,
                "within_p1_5_bound": {d: within_bound(scored[d]) for d in scored},
                "ranked_top": {q.id: r[:top] for q, r in per_query}}

    result["surfaces"]["1_nx_search_rerank_on"] = block(s1)
    # Surface 2 at the table's factor, then the grid.
    s2 = [(q, live.query_rank(raw, mo)) for q, raw, mo in raw2]
    result["surfaces"]["2_mcp_query_plain_rerank_off"] = block(s2)
    grid2: dict[str, dict] = {}
    for f in grid:
        with bge_factor(f):
            gq = [(q, live.query_rank(raw, mo)) for q, raw, mo in raw2]
        grid2[str(f)] = {str(d): score_surface(gq, corpora, d) for d in depths}
    result["factor_grid_surface_2"] = grid2
    # Surface 3 at the table's factor, then the grid (retrieval cached per group).
    def run3() -> list[tuple[Query, Ranked]]:
        return [(q, live.catalog_routed(q.text, [names[(q.set, "bge")], names[(q.set, "voyage")]],
                                        fetch_n, cache3)) for q in queries]
    result["surfaces"]["3_catalog_routed_grouped_combined_query"] = block(run3())
    grid3: dict[str, dict] = {}
    for f in grid:
        with bge_factor(f):
            gq = run3()
        grid3[str(f)] = {str(d): score_surface(gq, corpora, d) for d in depths}
    result["factor_grid_surface_3"] = grid3
    return result
