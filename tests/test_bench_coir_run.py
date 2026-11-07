# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-244 Track A harness (nexus-2k66t.2): the pure parts, and one engine-backed end-to-end run.

The engine run is the Minimum Viable Validation on a few documents: the loader writes title keys,
``nx search`` returns rows whose keys map, the scorer scores, ``results.json`` is written, and a
second query pass over the same loaded collection reproduces the ranked keys and metrics exactly.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import nexus.config
import nexus.db
import tests._engine_substrate as substrate
from tests._module_seam import setattr_in
from nexus.catalog.factory import make_catalog_reader
from nexus.commands._helpers import default_db_path
from nexus.corpus import t3_collection_name
from nexus.db import make_t3
from nexus.db.t2 import T2Database
from nexus.hook_registry import HookRegistry, install_default_hooks
from nexus.catalog.note_write import fire_note_chains, put_note, stamp_note
from scripts.bench import coir_run
from scripts.bench.coir_run import (
    POOL,
    Dataset,
    HarnessRefusal,
    KeyMap,
    OverCap,
    _single,
    check_embedding,
    check_engine_floor,
    check_licences,
    load,
    nx_binary,
    run,
    run_dataset,
    search,
    summarise,
)
from scripts.bench.coir_sample import draw


def test_dataset_without_licence_refuses_and_names_it() -> None:
    """Test Plan: a subset dataset with no recorded licence; the run refuses and names it."""
    with pytest.raises(HarnessRefusal, match="no recorded licence for nolic"):
        check_licences([POOL["codetrans-dl"], Dataset("nolic", "x", None, {})])
    check_licences(list(POOL.values()))


def test_ingest_over_the_wall_clock_cap_raises() -> None:
    corpus = [{"_id": f"c{i}", "text": f"t{i}"} for i in range(5)]
    sample = draw("ds", corpus, [{"query_id": "q0", "corpus_id": "c0", "score": 1}], queries=1)
    ticks = iter([0.0, 0.0, 0.5, 2.0, 9.0, 9.0])
    written: list[str] = []
    with pytest.raises(OverCap, match="ds: ingest passed 1 s"):
        load(sample, {d["_id"]: d["text"] for d in corpus}, "col", cap_s=1.0,
             put=lambda _c, title, _t: written.append(title), clock=lambda: next(ticks))
    assert written == ["ds:c0", "ds:c1"]


def test_summary_reports_intervals_per_metric_and_latency() -> None:
    per_query = [{"ndcg@10": v, "recall@10": v, "rr": v, "latency_s": 0.1 * (i + 1), "short_by": i % 2,
                  "shared_rows": 0} for i, v in enumerate([1.0, 0.0, 0.5, 1.0])]
    s = summarise(per_query, resamples=200)
    assert s["n"] == 4 and s["ndcg@10"]["value"] == pytest.approx(0.625)
    assert s["mrr"]["ci95"][0] <= 0.625 <= s["mrr"]["ci95"][1]
    assert s["latency_p50_s"]["value"] == pytest.approx(0.2) and s["short_queries"] == 2


class _Cat:
    def __init__(self, docs: dict[str, list[str]], entries: dict[str, tuple[str, str]]) -> None:
        self._docs, self._entries = docs, entries

    def docs_for_chashes(self, chashes):
        return {c: self._docs[c] for c in chashes if c in self._docs}

    def by_doc_id(self, d):
        title, col = self._entries[d]
        return type("E", (), {"title": title, "physical_collection": col})()


def test_keymap_drops_documents_in_other_collections() -> None:
    """Residual .5: a chunk shared with another collection's document never credits that document."""
    cat = _Cat({"c1": ["1.1", "1.2"], "c2": ["1.3"]},
               {"1.1": ("ds:a", "col"), "1.2": ("other:z", "elsewhere"), "1.3": ("other:y", "elsewhere")})
    keys = KeyMap(cat, "col")
    keys.prime(["c1", "c2"])
    assert keys("c1") == ["ds:a"] and keys("c2") == []


def _fake_nx(tmp_path: Path, stdout: str, stderr: str = "", code: int = 0) -> Path:
    """A stand-in nx that records its argv and prints what it is told."""
    nx = tmp_path / "nx"
    nx.write_text(f"""#!{sys.executable}
import json, sys
open({str(tmp_path / 'argv.json')!r}, 'w').write(json.dumps(sys.argv[1:]))
sys.stdout.write({stdout!r}); sys.stderr.write({stderr!r}); sys.exit({code})
""")
    nx.chmod(0o755)
    return nx


def test_search_passes_a_dash_query_after_the_options(tmp_path: Path) -> None:
    rows, _ = search(_fake_nx(tmp_path, json.dumps([{"id": "c", "distance": 0.1}])), "--help me", "col")
    argv = json.loads((tmp_path / "argv.json").read_text())
    assert rows == [{"id": "c", "distance": 0.1}]
    assert argv == ["search", "--corpus", "col", "-m", "100", "--json", "--", "--help me"]


@pytest.mark.parametrize(("stdout", "stderr", "code", "match"), [
    ("[]", "", 0, "no rows"),
    ("No results\n", "", 0, "Expecting value|no rows"),
    (json.dumps([{"id": "c", "distance": 0.1}]), "note: 1 collection excluded", 0, "warned"),
    (json.dumps([{"id": "c", "distance": 0.1}]), "Warning: server rerank degraded", 0, "warned"),
    ("", "boom", 2, "exited 2"),
])
def test_search_refuses_what_would_score_silently(tmp_path: Path, stdout, stderr, code, match) -> None:
    with pytest.raises((HarnessRefusal, json.JSONDecodeError), match=match):
        search(_fake_nx(tmp_path, stdout, stderr, code), "q", "col")


def test_unmapped_row_fails_the_dataset_and_counts(tmp_path: Path) -> None:
    """Test Plan: a row with no match in docs_for_chashes fails the run and names it."""
    sample = draw("ds", [{"_id": "c0", "text": "t"}], [{"query_id": "q0", "corpus_id": "c0", "score": 1}])
    nx = _fake_nx(tmp_path, json.dumps([{"id": "unmapped", "distance": 0.1}]))
    with pytest.raises(HarnessRefusal, match=r"ds query q0: 1 result rows map to no document.*unmapped"):
        run_dataset(sample, {"q0": "t"}, "col", nx=nx, catalog=_Cat({}, {}))


def test_library_warnings_on_stderr_do_not_stop_the_run(tmp_path: Path) -> None:
    nx = _fake_nx(tmp_path, json.dumps([{"id": "c", "distance": 0.1}]),
                  "/x/onnx.py:3: DeprecationWarning: old API\n")
    assert search(nx, "q", "col")[0] == [{"id": "c", "distance": 0.1}]


@pytest.mark.parametrize(("release", "ok"), [
    ("0.1.175", True), ("v0.2.0", True), ("0.1.174", False), ("", False), (None, False), ("dev", False)])
def test_engine_floor(release, ok) -> None:
    if ok:
        check_engine_floor({"release_version": release})
    else:
        with pytest.raises(HarnessRefusal, match="below the floor"):
            check_engine_floor({"release_version": release})


_BGE = "knowledge__code-snippets__bge-base-en-v15-768__v1"
_LOCAL = {"embedding_mode": "onnx-local", "embedding_profile": "bge"}


@pytest.mark.parametrize(("collection", "engine", "match"), [
    ("knowledge__code-snippets__minilm-l6-v2-384__v1", _LOCAL, "is not a bge-base-en-v15-768 collection"),
    (_BGE, {"embedding_mode": "voyage", "embedding_profile": "bge"}, "engine embeds as"),
    (_BGE, {"embedding_mode": "onnx-local", "embedding_profile": "minilm"}, "engine embeds as"),
    (_BGE, {}, "engine embeds as"),
])
def test_check_embedding_refuses_anything_but_local_bge(collection, engine, match) -> None:
    with pytest.raises(HarnessRefusal, match=match):
        check_embedding(collection, engine)
    check_embedding(_BGE, _LOCAL)


@pytest.mark.parametrize("paths", [[], ["a.parquet", "b.parquet"]])
def test_parquet_glob_must_match_exactly_one_file(paths) -> None:
    with pytest.raises(HarnessRefusal, match="expected one parquet file"):
        _single(paths)
    assert _single(["a.parquet"]) == "a.parquet"


def test_no_nx_beside_the_interpreter_refuses(tmp_path: Path, monkeypatch) -> None:
    setattr_in(monkeypatch, coir_run, "sys.executable", str(tmp_path / "python"))
    with pytest.raises(HarnessRefusal, match="no nx beside"):
        nx_binary()
    (tmp_path / "nx").write_text("")
    assert nx_binary() == tmp_path / "nx"


def test_hybrid_default_true_refuses_before_any_engine_call(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(nexus.config, "load_config", lambda: {"search": {"hybrid_default": True}})
    monkeypatch.setattr(nexus.db, "make_t3", lambda: pytest.fail("reached the engine"))
    with pytest.raises(HarnessRefusal, match="hybrid_default is true"):
        run([POOL["codetrans-dl"]], tmp_path, tmp_path / "results.json", nx=tmp_path / "nx")


def _main_args(tmp_path: Path) -> list[str]:
    return ["--data", str(tmp_path), "--out", str(tmp_path / "results.json")]


def test_main_refuses_a_voyage_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NX_VOYAGE_API_KEY", "k")
    setattr_in(monkeypatch, coir_run, "subprocess.run", lambda *a, **k: pytest.fail("got past the key check"))
    with pytest.raises(SystemExit, match="NX_VOYAGE_API_KEY is set"):
        coir_run.main(_main_args(tmp_path))


def test_main_refuses_a_dirty_checkout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    setattr_in(monkeypatch, coir_run, "subprocess.run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="?? new.py\n", stderr=""))
    with pytest.raises(SystemExit, match="uncommitted changes"):
        coir_run.main(_main_args(tmp_path))


def test_main_refuses_an_engine_off_loopback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NX_VOYAGE_API_KEY", raising=False)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    setattr_in(monkeypatch, coir_run, "subprocess.run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""))
    monkeypatch.setattr(substrate, "ensure_engine", lambda: {"base_url": "http://10.0.0.5:8080"})
    monkeypatch.setattr(substrate, "mint_test_tenant", lambda _s: pytest.fail("minted off loopback"))
    with pytest.raises(SystemExit, match="not on loopback"):
        coir_run.main(_main_args(tmp_path))


# ── engine-backed ─────────────────────────────────────────────────────────────

_TOPICS = [
    "def quicksort(xs): return xs if len(xs) < 2 else quicksort([a for a in xs[1:] if a < xs[0]])",
    "SELECT name, COUNT(*) FROM orders GROUP BY name HAVING COUNT(*) > 3",
    "torch.nn.Conv2d(3, 16, kernel_size=3) followed by a ReLU and max pooling layer",
    "binary search over a sorted array returns the index of the target or minus one",
    "a breadth first traversal of a graph using a deque as the frontier queue",
    "parse a CSV file with the csv module and sum the third column as floats",
    "compute the Levenshtein edit distance between two strings with dynamic programming",
    "an LRU cache built from an ordered dict with move_to_end on every access",
]


_FILLER = [
    "a red-black tree insert that recolours and rotates up to the root",
    "tokenise an arithmetic expression and evaluate it with the shunting-yard algorithm",
    "a Bloom filter with k hash functions over a bit array of size m",
    "matrix multiplication in numpy with einsum and broadcasting",
    "a trie that stores words and answers prefix queries",
    "Dijkstra's shortest path with a binary heap priority queue",
    "a JSON schema validator that checks required keys and types",
    "a token bucket rate limiter refilled at a fixed rate",
    "topological sort of a directed acyclic graph by Kahn's algorithm",
    "a regular expression that matches ISO 8601 dates",
    "merge two sorted linked lists into one sorted list",
]


def _fixture_dataset() -> tuple[list[dict], list[dict], list[dict]]:
    """Twenty documents (the Test Plan's ingest size), one an identical-text twin of d1; six judged
    queries, of which the seeded draw of five leaves q4 unscored for the warm-up."""
    corpus = [{"_id": f"d{i}", "text": t} for i, t in enumerate(_TOPICS + _FILLER)]
    corpus.append({"_id": "d19", "text": _TOPICS[1]})
    queries = [{"_id": f"q{i}", "text": _TOPICS[i]} for i in range(6)]
    queries[0]["text"] = " ".join([_TOPICS[0]] * 4)  # past QUOTAS.MAX_QUERY_STRING_CHARS; passed whole
    qrels = [{"query_id": f"q{i}", "corpus_id": "d19" if i == 1 else f"d{i}", "score": 1} for i in range(6)]
    return corpus, queries, qrels


def _claude_pids() -> set[str]:
    out = subprocess.run(["pgrep", "-x", "claude"], capture_output=True, text=True, encoding="utf-8")
    return set(out.stdout.split())


def _aspect_counts() -> tuple[int, int, int, int]:
    q = T2Database(default_db_path()).tuples.subspace_stats("aspects")
    return q.available, q.claimed, q.dead, q.consumed


@pytest.mark.integration
@pytest.mark.t2_tenant
def test_engine_run_end_to_end_reproduces_and_merges_twins(tmp_path: Path) -> None:
    ds = Dataset("fixture-ds", "code-snippets", "Apache-2.0", {})
    out = tmp_path / "results.json"
    pids = _claude_pids()
    # The floor is not enforced here: CI's pytest jar is unstamped (tests/AGENTS.md); main's run enforces it.
    res = run([ds], tmp_path, out, readers=lambda _d, _n: _fixture_dataset(), resamples=200, queries=5,
              enforce_engine_floor=False)

    assert json.loads(out.read_text()) == json.loads(json.dumps(res, sort_keys=True))
    body = res["datasets"]["fixture-ds"]
    assert body["ingest"]["documents"] == 20 and body["loaded_whole"] and body["corpus_fraction"] == 1.0
    assert body["warmup"] == "unscored test query"
    assert [q["query_id"] for q in body["per_query"]] == ["q0", "q1", "q2", "q3", "q5"]
    assert body["duplicate_classes"] == 1 and body["documents_merged"] == 1
    assert body["taxonomy_assignments"] == 0
    assert (tmp_path / "fixture-ds.manifest.json").exists()
    assert res["pins"]["engine"]["embedding_mode"] == "onnx-local"
    # Test Plan: the loader's empty registry queues no aspect work and starts no claude process.
    assert _aspect_counts() == (0, 0, 0, 0)
    assert _claude_pids() <= pids
    by_q = {q["query_id"]: q for q in body["per_query"]}
    # The twin (d19, named by the qrels) and d1 share one chunk row; their class is named d1 and is a hit.
    assert by_q["q1"]["ranked"][0] == ["fixture-ds:d1", 1] and by_q["q1"]["shared_rows"] == 0
    assert all(q["rr"] == 1.0 for q in body["per_query"])

    # A second query pass over the same loaded collection: identical keys and metrics.
    corpus, queries, qrels = _fixture_dataset()
    qtext = {q["_id"]: q["text"] for q in queries}
    sample = draw(ds.name, corpus, qrels, queries=5, query_text=qtext)
    assert sample.manifest()["sha256"] == body["query_set_sha256"]
    col = t3_collection_name(ds.subject, t3=make_t3(), for_write=True)
    again = run_dataset(sample, qtext, col, nx=nx_binary(), catalog=make_catalog_reader())
    strip = lambda qs: [{k: v for k, v in q.items() if k != "latency_s"} for q in qs]  # noqa: E731
    assert strip(again["per_query"]) == strip(body["per_query"])

    # The harness is the only writer: a second load into the same collection refuses.
    with pytest.raises(HarnessRefusal, match="already holds content"):
        run([ds], tmp_path, tmp_path / "again.json", readers=lambda _d, _n: _fixture_dataset(), resamples=10,
            enforce_engine_floor=False)

    # Positive control: the same put with the default hooks DOES queue aspect work, so the zero above
    # is a measurement and not an instrument that cannot see.
    control = t3_collection_name("code-snippets-control", t3=make_t3(), for_write=True)
    reg = HookRegistry()
    install_default_hooks(reg)
    outcome = put_note(content=_FILLER[0], collection=control, title="control:1")
    fire_note_chains(outcome, _FILLER[0], hooks=reg)
    stamp_note(outcome)
    assert _aspect_counts()[0] >= 1
