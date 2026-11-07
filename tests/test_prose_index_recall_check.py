# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/prose_index_recall_check.py (nexus-a6mys): the RDR-227 Gap 1 recall check, run on a numpy fixture.

``FixtureClient`` has the signatures of the ``HttpVectorClient`` methods the script calls and the engine's
documented behaviour for them: ``chash_allow`` ranks exactly, refuses more than 3000 hashes, and a plain search is
the served path. Its served path is exact unless a test makes it lossy, so a green run proves the exact arm agrees
with numpy and a lossy one proves the check goes red. No engine, no embedding call, no network.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import sys
from pathlib import Path

import numpy as np
import pytest

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prose_index_recall_check.py"
_spec = importlib.util.spec_from_file_location("prose_index_recall_check", _PATH)
rc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rc  # dataclasses resolve their module through sys.modules
_spec.loader.exec_module(rc)  # type: ignore[union-attr]

COLL = "docs__1-9__voyage-context-3__v1"
ROWS = 7000  # three allow-list slices at the 3000 cap
DIM = 16


class FixtureClient:
    def __init__(self, *, lossy_drop: int = 0, chash_allow_supported: bool = True, rows: int = ROWS) -> None:
        rng = np.random.default_rng(20261107)
        self.vectors = rng.normal(size=(rows, DIM)).astype(np.float64)
        self.vectors /= np.linalg.norm(self.vectors, axis=1, keepdims=True)
        self.ids = [hashlib.sha256(f"chunk-{i}".encode()).hexdigest() for i in range(rows)]
        self.texts = [f"chunk text {i}" for i in range(rows)]
        self.query_vectors: dict[str, np.ndarray] = {}
        self.lossy_drop = lossy_drop
        self.chash_allow_supported = chash_allow_supported
        self.searches: list[dict] = []
        self.max_allow_seen = 0

    def add_query(self, text: str, seed: int) -> None:
        v = np.random.default_rng(seed).normal(size=DIM)
        self.query_vectors[text] = v / np.linalg.norm(v)

    def supports_chash_allow(self) -> bool:
        return self.chash_allow_supported

    def list_collections(self) -> list[dict]:
        return [{"name": COLL, "count": len(self.ids), "stored_count": len(self.ids)}]

    def list_store(self, collection: str, limit: int = 200, offset: int = 0) -> list[dict]:
        assert collection == COLL
        return [{"id": i} for i in self.ids[offset:offset + limit]]

    def get(self, ids=None, where=None, include=None, limit=None, offset=0, *, sensitivity):
        assert sensitivity == "exclude", "sampled query text must never come from a sensitive chunk"
        pos = {i: n for n, i in enumerate(self.ids)}
        return {"ids": list(ids), "documents": [self.texts[pos[i]] for i in ids]}

    def search(self, query, collection_names, n_results=10, *, chash_allow=None, sensitivity):
        assert collection_names == [COLL]
        self.searches.append({"k": n_results, "allow": None if chash_allow is None else len(chash_allow)})
        q = self.query_vectors.get(query)
        if q is None:  # a sampled chunk's text is its own query: use that chunk's vector
            q = self.vectors[self.texts.index(query)]
        dist = 1.0 - self.vectors @ q
        order = np.argsort(dist, kind="stable")
        if chash_allow is not None:
            assert len(chash_allow) <= 3000, "the engine refuses more than 3000 hashes"
            self.max_allow_seen = max(self.max_allow_seen, len(chash_allow))
            allowed = set(chash_allow)
            order = np.array([i for i in order if self.ids[i] in allowed])
        elif self.lossy_drop:
            # a filtered walk that loses some of the nearest rows and pads with farther ones
            keep = np.concatenate([order[: n_results // 2], order[n_results // 2 + self.lossy_drop:]])
            order = keep
        return [{"id": self.ids[i], "distance": float(dist[i]), "collection": COLL} for i in order[:n_results]]


def _client(**kw) -> FixtureClient:
    c = FixtureClient(**kw)
    for n in range(12):
        c.add_query(f"own topic query {n}", seed=n)
    return c


def _queries_file(tmp_path: Path, n: int = 12) -> str:
    p = tmp_path / "q.txt"
    p.write_text("# own-topic queries\n\n" + "\n".join(f"own topic query {i}" for i in range(n)) + "\n")
    return str(p)


def _run(client, tmp_path, *, threshold=100, extra=(), n=12):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rc.main([COLL, "--queries-file", _queries_file(tmp_path, n), "--threshold", str(threshold), *extra],
                       client=client)
    return code, out.getvalue(), err.getvalue()


def test_exact_served_path_scores_one_at_every_k(tmp_path):
    c = _client()
    code, out, _ = _run(c, tmp_path)
    assert code == rc.EXIT_OK, out
    for k in rc.KS:
        assert f"k={k:<4} min 1.0000" in out
    assert "minimum 1.0000" in out and "top-1 miss: 0" in out
    # non-vacuity of the green: both arms really ran, with the allow-list sliced under the cap
    assert c.max_allow_seen == 3000
    assert sum(1 for s in c.searches if s["allow"] is None) == 12 * len(rc.KS)
    assert sum(1 for s in c.searches if s["allow"] is not None) == 12 * 3


def test_exact_arm_equals_numpy_top_k():
    c = _client()
    chashes = rc.list_chashes(c, COLL)
    assert len(chashes) == ROWS
    got = rc.exact_arm(c, COLL, "own topic query 3", chashes, 120)
    q = c.query_vectors["own topic query 3"]
    want = np.argsort(1.0 - c.vectors @ q, kind="stable")[:120]
    assert [h.id for h in got] == [c.ids[i] for i in want]


def test_lossy_index_exits_below_target(tmp_path):
    code, out, _ = _run(_client(lossy_drop=4), tmp_path)
    assert code == rc.EXIT_BELOW, out
    assert "FAIL: below target" in out
    # k=40 loses 4 of the 20..24 ranks: 36/40 = 0.9; larger k lose a smaller share
    assert "k=40   min 0.9000" in out
    assert "k=120  min 0.9667" in out


def test_vacuous_when_collection_is_not_above_threshold(tmp_path):
    c = _client()
    code, out, _ = _run(c, tmp_path, threshold=ROWS)
    assert code == rc.EXIT_VACUOUS
    assert "VACUOUS" in out
    assert c.searches == []  # nothing was measured, so nothing was sent
    code, out, _ = _run(c, tmp_path, threshold=ROWS, extra=["--allow-exact-route"])
    assert code == rc.EXIT_OK


def test_engine_without_chash_allow_cannot_run(tmp_path):
    c = _client(chash_allow_supported=False)
    code, _, err = _run(c, tmp_path)
    assert code == rc.EXIT_CANNOT_RUN and "chash_allow" in err
    assert c.searches == []


def test_too_few_queries_cannot_run(tmp_path):
    code, _, err = _run(_client(), tmp_path, n=3)
    assert code == rc.EXIT_CANNOT_RUN and "at least" in err


def test_dry_run_sends_no_search(tmp_path):
    c = _client()
    code, out, _ = _run(c, tmp_path, extra=["--dry-run"])
    assert code == rc.EXIT_OK and "plan:" in out and "96 searches" in out  # 12 x (5 + 3)
    assert c.searches == []


def test_sample_source_runs_and_labels_itself(tmp_path):
    c = FixtureClient()
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = rc.main([COLL, "--sample", "10", "--seed", "5", "--threshold", "100"], client=c)
    assert code == rc.EXIT_OK, out.getvalue()
    assert "sampled from the collection (seed 5)" in out.getvalue() and "weaker evidence" in out.getvalue()


def test_query_over_cap_is_refused(tmp_path):
    p = tmp_path / "long.txt"
    p.write_text("x" * (rc.MAX_QUERY_CHARS + 1) + "\n")
    with pytest.raises(rc.CannotRun):
        rc.read_queries_file(str(p))


def test_score_k_counts_a_boundary_tie_as_a_hit_and_flags_a_top1_miss():
    exact = [rc.Hit("a", 0.1), rc.Hit("b", 0.2), rc.Hit("c", 0.3)]
    # page holds a, b and a different row at exactly the 3rd exact distance: a tie, not a miss
    assert rc.score_k([rc.Hit("a", 0.1), rc.Hit("b", 0.2), rc.Hit("z", 0.3)], exact, 3) == (1.0, False)
    # page lacks the nearest row and every row is farther: recall 2/3, top-1 miss
    recall, miss = rc.score_k([rc.Hit("b", 0.2), rc.Hit("c", 0.3), rc.Hit("y", 0.5)], exact, 3)
    assert recall == pytest.approx(2 / 3) and miss is True
