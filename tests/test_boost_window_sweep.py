# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cbg2s: the k sweep of nexus-ann50, run against the self-provisioned
local engine on a seeded corpus, old boosts against windowed boosts.

The 2026-10-09 sweep (T2 nexus/ann50-k-sweep-2026-10-09) ran 8 queries at
per-collection k in {40, 60, 80, 120, 300} on the managed service and found
the pool's nearest 30 rows identical at every k while the boosted page was
not: the client boosts were min-max normalised over the whole pool, so a
deeper fetch moved every score. This runs the same shape through the real MCP
``search`` render and the real engine route, on a corpus of four collections
(two code with frecency, one docs, one knowledge) seeded from a fixed seed.

"Old" is today's behaviour, reproduced by dropping the ``window`` argument at
both call sites in ``_search_render``; "new" is the code under test. The noise
control is old against old (two runs of the same code at the same k), the
control nexus-abdp2 used (T2 nexus/measurements-abdp2-final-order-2026-10-05).

``slow``: seeding embeds ~1.4k chunks. Run it with::

    uv run pytest tests/test_boost_window_sweep.py -o addopts="-m slow" -s
"""
from __future__ import annotations

import hashlib
import random
import re

import pytest

import nexus.config as nxconfig
import nexus.db.http_vector_client as hvc
from nexus import search_engine
from nexus.mcp import core as mcp_core
from tests._catalog_fixture_ops import give_chunks_a_live_owner
from tests._chunk_seed import seed_chunks_direct

pytestmark = pytest.mark.slow

_MODEL = "bge-base-en-v15-768"
_KS = (40, 60, 80, 120, 300)
_COLLECTIONS = {
    f"code__bwa__{_MODEL}__v1": ("code", 360, True),
    f"code__bwb__{_MODEL}__v1": ("code", 360, True),
    f"docs__bwc__{_MODEL}__v1": ("docs", 360, False),
    f"knowledge__bwd__{_MODEL}__v1": ("knowledge", 320, False),
}
_TOPICS = [
    "lease renewal and the attempts counter",
    "retry backoff after a failed delivery",
    "tenant isolation in row level security",
    "vector index recall at high collection counts",
    "schema migration ordering with liquibase",
    "token expiry and credential refresh",
    "cache invalidation after a write",
    "log rotation and disk pressure",
]
_QUERIES = [
    "how does the lease renewal change the attempts counter",
    "what is the retry backoff after a failed delivery",
    "how is tenant isolation enforced with row level security",
    "why does vector index recall drop with many collections",
    "in what order do schema migrations run",
    "what happens when a token expires",
    "when is the cache invalidated after a write",
    "how are logs rotated when the disk fills",
]
_FILLER = (
    "the a of to in for with on by from queue worker handler record batch index window page "
    "score value field table column row lock claim ack nack lease token tenant store client "
    "server engine route probe check limit quota ratio rank order sort merge split fetch send "
    "read write flush sync async retry timeout error warn info debug trace span metric gauge"
).split()


def _chunk_text(rng: random.Random) -> str:
    primary = rng.choice(_TOPICS)
    parts = [primary] if rng.random() < 0.75 else []
    for _ in range(rng.randint(0, 2)):
        parts.append(rng.choice(_TOPICS))
    words = [rng.choice(_FILLER) for _ in range(rng.randint(12, 40))]
    for p in parts:
        words.insert(rng.randint(0, len(words)), p)
    return " ".join(words)


def _seed_corpus(rng: random.Random) -> list[str]:
    for coll, (ctype, count, with_frecency) in _COLLECTIONS.items():
        ids = [hashlib.sha256(f"{coll}:{i}".encode()).hexdigest() for i in range(count)]
        texts = [_chunk_text(rng) for _ in ids]
        metas = [
            {"chunk_text_hash": c, "title": f"{coll.split('__')[1]}-{i // 4}.txt",
             **({"frecency_score": rng.uniform(0.0, 100.0)} if with_frecency else {}),
             **({} if with_frecency or rng.random() < 0.5 else {
                 "bib_citation_count": rng.randint(1, 5000), "bib_year": str(rng.randint(2008, 2025)),
             })}
            for i, c in enumerate(ids)
        ]
        seed_chunks_direct(coll, ids=ids, documents=texts, embed=True, metadatas=metas)
        owner = coll.split("__")[1]
        # four chunks per document, so the 2-per-file diversity cap bites as it does on a real corpus
        for g in range(0, count, 4):
            give_chunks_a_live_owner(coll, ids[g:g + 4], content_type=ctype, owner_name=f"{owner}-{g // 4}")
    return list(_COLLECTIONS)


_S_RE = re.compile(r" s=-?\d+\.\d+")


def _strip(text: str) -> str:
    """The s=-stripped page: no display score, no ``of N`` footer."""
    return "\n".join(
        _S_RE.sub("", ln) for ln in text.splitlines() if not ln.startswith("--- showing")
    )


def _jaccard(a: list[str], b: list[str]) -> float:
    return len(set(a) & set(b)) / len(set(a) | set(b)) if (a or b) else 1.0


@pytest.fixture
def swept(t2_service_env, monkeypatch):
    client = hvc.HttpVectorClient(tenant=t2_service_env)
    cols = _seed_corpus(random.Random(20261009))
    corpus = ",".join(cols)
    monkeypatch.setattr(mcp_core, "_get_t3", lambda: client)

    real_boosts = search_engine.apply_ranking_boosts
    real_cap = search_engine.apply_file_diversity_cap

    def run(*, new: bool, query: str, k: int, hybrid: bool):
        real_cfg = nxconfig.load_config

        def cfg(*a, **kw):
            c = dict(real_cfg(*a, **kw))
            c["search"] = {**c.get("search", {}), "hybrid_default": hybrid}
            return c

        monkeypatch.setattr(nxconfig, "load_config", cfg)
        if new:
            monkeypatch.setattr(search_engine, "apply_ranking_boosts", real_boosts)
            monkeypatch.setattr(search_engine, "apply_file_diversity_cap", real_cap)
        else:
            monkeypatch.setattr(
                search_engine, "apply_ranking_boosts",
                lambda r, **kw: real_boosts(r, **{a: v for a, v in kw.items() if a != "window"}),
            )
            monkeypatch.setattr(
                search_engine, "apply_file_diversity_cap",
                lambda r, **kw: real_cap(r, **{a: v for a, v in kw.items() if a != "window"}),
            )
        monkeypatch.setattr(mcp_core, "_MCP_PER_COLLECTION_K", k)
        mcp_core._reset_page_cache_for_tests()
        text = mcp_core._search_render(query=query, corpus=corpus, cluster_by="", limit=10)
        mcp_core._reset_page_cache_for_tests()
        ids = mcp_core._search_render(
            query=query, corpus=corpus, cluster_by="", limit=10, structured=True,
        )["ids"]
        return text, ids

    return run


@pytest.mark.parametrize("hybrid", [False, True])
def test_k_sweep_old_page_moves_with_k_new_page_does_not(swept, capsys, hybrid):
    rows = []
    old_distinct = new_distinct = 0
    j_new_vs_old, j_old_vs_old, j_old_spread = [], [], []
    for qi, q in enumerate(_QUERIES):
        old = {k: swept(new=False, query=q, k=k, hybrid=hybrid) for k in _KS}
        new = {k: swept(new=True, query=q, k=k, hybrid=hybrid) for k in _KS}
        old_again = swept(new=False, query=q, k=120, hybrid=hybrid)
        o_pages = {_strip(t) for t, _ in old.values()}
        n_pages = {_strip(t) for t, _ in new.values()}
        old_distinct += len(o_pages) > 1
        new_distinct += len(n_pages) > 1
        # today's default page (k=120) against the new page, and old against old
        j_new_vs_old.append(_jaccard(new[120][1], old[120][1]))
        j_old_vs_old.append(_jaccard(old_again[1], old[120][1]))
        # how far today's own page moves with k: the spread the windowed page removes
        j_old_spread.append(sum(_jaccard(old[k][1], old[120][1]) for k in _KS if k != 120) / (len(_KS) - 1))
        # the new page's display scores are identical at every k too, not only its rows
        new_s = {tuple(_S_RE.findall(t.split("--- showing")[0])) for t, _ in new.values()}
        rows.append((qi, len(o_pages), len(n_pages), len(new_s), j_new_vs_old[-1], j_old_vs_old[-1]))
    with capsys.disabled():
        print(f"\nhybrid={hybrid}\nquery  old-distinct-pages  new-distinct-pages  new-distinct-s=  j(new120,old120)  j(old,old)")  # noqa: T201
        for r in rows:
            print(f"  q{r[0]}        {r[1]}                  {r[2]}                  {r[3]}              {r[4]:.3f}             {r[5]:.3f}")  # noqa: T201
        print(f"old pages differ across k on {old_distinct}/8 queries; new on {new_distinct}/8")  # noqa: T201
        print(f"mean j(new120,old120) {sum(j_new_vs_old)/8:.3f}  mean j(old,old) {sum(j_old_vs_old)/8:.3f}  "  # noqa: T201
              f"mean j(old_k,old120) over k!=120 {sum(j_old_spread)/8:.3f}")
    if hybrid:
        # With the boosts off the page is ordered by distance alone and a local
        # single-model pool cannot move it (the managed service's k dependence
        # came from calibration across two models: tests/test_search_engine.py
        # ::TestBoostWindow::test_mixed_model_window_is_cut_on_the_calibrated_scale).
        assert old_distinct >= 1, "non-vacuity: the corpus must make the OLD page move with k"
    assert new_distinct == 0, rows
    assert all(r[3] == 1 for r in rows), rows
