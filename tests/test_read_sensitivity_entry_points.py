# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F3 (nexus-rfvyy.7.11): every LLM-bound read entry point passes ``exclude`` down.

The suite pins ``nexus.sensitivity.read_sensitivity`` to ``include`` for every test that does not carry
``sensitivity_reads`` (``tests/conftest.py``), so a regression that hard-codes ``"include"`` at an entry point left
the suite green: the AST test in ``tests/db/test_vector_read_sensitivity_argument.py`` checks that the keyword is
NAMED, not what value it carries. Each test below carries the marker (the real function, with
``NX_SENSITIVITY_POSTURE=llm_bound`` in the environment), runs one entry point, and asserts the value that reached
the next layer down (the client method, or the helper that does the read) is ``exclude``. Each also runs the same
call with the posture ``default`` and asserts ``include``, so the recorder is shown to see the argument.
"""
from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from nexus.mcp import core
from nexus.sensitivity import POSTURE_ENV

pytestmark = pytest.mark.sensitivity_reads

H64 = "ab" * 32


class _Recorder:
    """A T3 stand-in: records the ``sensitivity`` each read method received, answers 'nothing found'."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []

    def _note(self, name: str, kw: dict[str, Any]) -> None:
        self.seen.append((name, kw["sensitivity"]))  # KeyError when an entry point omitted it: loud

    def get_by_id(self, collection, doc_id, **kw):
        self._note("get_by_id", kw)
        return None

    def find_ids_by_title(self, collection, title, **kw):
        return []

    def search_metadata_scoped(self, query, collection_names, **kw):
        self._note("search_metadata_scoped", kw)
        return []

    def search_topic_scoped(self, query, topic, collection, **kw):
        self._note("search_topic_scoped", kw)
        return []

    def search_graph_hop(self, query, seeds, collection_names, **kw):
        self._note("search_graph_hop", kw)
        return []

    def search_aspect_scoped(self, query, collection_names, **kw):
        self._note("search_aspect_scoped", kw)
        return []

    def list_collections(self, *a, **kw):
        return []

    def collection_exists_raw(self, name):
        return True


@pytest.fixture(params=["llm_bound", "default"])
def posture(request, monkeypatch):
    """('llm_bound' -> exclude) and ('default' -> include): the second leg proves the recorder sees the argument."""
    if request.param == "llm_bound":
        monkeypatch.setenv(POSTURE_ENV, "llm_bound")
        return "exclude"
    monkeypatch.delenv(POSTURE_ENV, raising=False)
    return "include"


def _service_t3(monkeypatch, t3: _Recorder, target=("knowledge__x__bge-base-en-v15-768__v1",)) -> _Recorder:
    monkeypatch.setattr(core, "_get_t3", lambda: t3)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **kw: list(target))
    monkeypatch.setattr(core, "_read_scope", lambda t3, collection: [collection or target[0]])
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda db: True)
    # The model grouping reads the catalog's collection rows (a tenant); the one group is the whole target here.
    monkeypatch.setattr(core, "_grouped_combined_query", lambda target, fn, *, t3: fn(list(target)))
    return t3


def _values(t3: _Recorder, name: str) -> set[str]:
    return {v for n, v in t3.seen if n == name}


# ── the MCP tools ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("tool,kwargs,method", [
    ("search_metadata_scoped", {"query": "q"}, "search_metadata_scoped"),
    ("search_topic_scoped", {"query": "q", "topic": "t"}, "search_topic_scoped"),
    ("search_graph_hop", {"query": "q", "seeds": "1.1.1"}, "search_graph_hop"),
    ("search_aspect_scoped", {"query": "q"}, "search_aspect_scoped"),
])
def test_the_combined_query_tools_read_with_the_posture(monkeypatch, posture, tool, kwargs, method):
    t3 = _service_t3(monkeypatch, _Recorder())
    getattr(core, tool)(**kwargs)

    assert _values(t3, method) == {posture}, f"{tool} handed {_values(t3, method)} to the client"


def test_store_get_reads_with_the_posture(monkeypatch, posture):
    t3 = _service_t3(monkeypatch, _Recorder())
    core.store_get(H64, collection="knowledge__x__bge-base-en-v15-768__v1")

    assert _values(t3, "get_by_id") == {posture}


def test_the_store_get_many_tool_reads_with_the_posture(monkeypatch, posture):
    seen: list[str] = []

    def impl(*a, sensitivity, **kw):
        seen.append(sensitivity)
        return {"contents": [], "missing": []}

    monkeypatch.setattr(core, "_store_get_many_impl", impl)
    core.store_get_many([H64], "knowledge__x__bge-base-en-v15-768__v1", structured=True)

    assert seen == [posture]


def test_the_search_tool_reads_with_the_posture(monkeypatch, posture):
    seen: list[str] = []

    def render(*a, sensitivity, **kw):
        seen.append(sensitivity)
        return {"ids": []}

    monkeypatch.setattr(core, "_search_render", render)
    core.search("q", structured=True)

    assert seen == [posture]


def test_query_reads_with_the_posture(monkeypatch, posture):
    t3 = _service_t3(monkeypatch, _Recorder())
    seen: list[str] = []

    def cross(*a, sensitivity, **kw):
        seen.append(sensitivity)
        return []

    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", cross)
    core.query("q", corpus="knowledge")  # the plain corpus path
    core.query("q", corpus="knowledge", content_type="paper")  # the catalog-param path

    assert t3.seen == [] or _values(t3, "search_metadata_scoped") == {posture}
    assert set(seen) <= {posture} and (seen or t3.seen), "an entry point reached a read at all"
    assert (set(seen) | _values(t3, "search_metadata_scoped")) == {posture}


@pytest.mark.asyncio
async def test_the_primary_evidence_fetcher_reads_with_the_posture(monkeypatch, posture):
    seen: list[str] = []

    class _Resolution:
        caps = None

        def resolved(self):
            return True

    def fetcher(t3, *, sensitivity):
        seen.append(sensitivity)
        raise RuntimeError("stop: the read posture is what is under test")  # disclosed as an unread source

    monkeypatch.setattr("nexus.plans.primary_evidence.t3_chunk_fetcher", fetcher)
    monkeypatch.setattr(core, "_get_t3", lambda: object())
    monkeypatch.setattr(core, "_get_catalog", lambda: object())
    from nexus.plans.primary_evidence import DEFAULT_CAPS

    res = _Resolution()
    res.caps = DEFAULT_CAPS
    await core._nx_answer_primary_stream(res)

    assert seen == [posture]


# ── the CLI ──────────────────────────────────────────────────────────────────

def test_nx_store_get_reads_with_the_posture(monkeypatch, posture):
    from nexus.commands import store as store_cmd

    t3 = _Recorder()
    monkeypatch.setattr(store_cmd, "_t3", lambda **kw: t3)
    monkeypatch.setattr(store_cmd, "_store_scope", lambda collection, db: (["knowledge__x__bge-base-en-v15-768__v1"], []))

    CliRunner().invoke(store_cmd.get_cmd, [H64])  # not found -> a ClickException, after the read

    assert _values(t3, "get_by_id") == {posture}


def test_nx_search_reads_with_the_posture(monkeypatch, posture):
    from nexus.commands import search_cmd as sc

    seen: list[str] = []

    class _Stop(Exception):
        pass

    def cross(*a, sensitivity, **kw):
        seen.append(sensitivity)
        raise _Stop

    class _T2:
        taxonomy = telemetry = None

        def __init__(self, *_a, **_k) -> None: ...
        def __enter__(self): return self
        def __exit__(self, *_exc): return False

    monkeypatch.setattr("nexus.db.t2.T2Database", _T2)  # the command opens T2 around the read: no tenant here
    monkeypatch.setattr(sc, "search_cross_corpus", cross)
    monkeypatch.setattr(sc, "_t3", lambda **kw: _Recorder())
    result = CliRunner().invoke(sc.search_cmd, ["a query", "--corpus", "knowledge__x__bge-base-en-v15-768__v1"])

    assert seen == [posture], f"nx search reached search_cross_corpus: {result.output!r} {result.exception!r}"
