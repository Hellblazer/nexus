# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The memory verbs for the sandboxed Lua runtime (RDR-242 Technical Design 6, 9, 10;
nexus-mdbds.7). They are called here as the parent calls them: check_args, then
``fn(*args, deadline=...)``, against the real engine substrate (``t2_tenant``).
Lua-fixture end-to-end waits for the worker (nexus-mdbds.4/.5).
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from nexus.db.limits import QUOTAS
from nexus.db.t2 import T2Database
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.lua_runtime import verb_types
from nexus.lua_runtime.verb_types import (
    ParamSpec,
    VerbArgError,
    VerbContext,
    VerbError,
    VerbTimeout,
    check_args,
)
from nexus.lua_runtime import verbs_memory
from nexus.lua_runtime.verbs_memory import VERBS, _plain

pytestmark = pytest.mark.t2_tenant

_SPECS = {spec.name: spec for spec in VERBS}


_CTX = VerbContext(repo_root=None)


def _call(name: str, *args: object, budget: float = 30.0):
    spec = _SPECS[name]
    return spec.fn(
        *check_args(spec, list(args)), deadline=time.monotonic() + budget, ctx=_CTX
    )


def _seed(db: T2Database, project: str, **titles: str) -> None:
    for title, content in titles.items():
        db.put(project=project, title=title, content=content)


# ── The verbs match the RDR signatures ──────────────────────────────────────


def test_the_params_match_the_rdr_signatures() -> None:
    assert set(_SPECS) == {"memory.get", "memory.list", "memory.search"}
    assert _SPECS["memory.get"].params == (
        ParamSpec("project", "string"),
        ParamSpec("title", "string"),
    )
    assert _SPECS["memory.list"].params == (ParamSpec("project", "string"),)
    assert _SPECS["memory.search"].params == (
        ParamSpec("query", "string"),
        ParamSpec("project", "string"),
        ParamSpec("limit", "integer", optional=True),
    )


# ── memory.get ──────────────────────────────────────────────────────────────


def test_get_returns_the_entry_fields_for_an_exact_title(db: T2Database) -> None:
    db.put(project="lv-get", title="note.md", content="the body", tags="a,b")
    entry = _call("memory.get", "lv-get", "note.md")
    assert entry["project"] == "lv-get"
    assert entry["title"] == "note.md"
    assert entry["content"] == "the body"
    assert entry["tags"] == "a,b"
    assert isinstance(entry["id"], int)
    json.dumps(entry)  # plain JSON, no Python object reaches Lua


def test_get_resolves_a_unique_prefix(db: T2Database) -> None:
    _seed(db, "lv-prefix", **{"088-research-1: full title": "long"})
    entry = _call("memory.get", "lv-prefix", "088-research-1")
    assert entry["content"] == "long"


def test_get_of_a_missing_entry_is_nil(db: T2Database) -> None:
    assert _call("memory.get", "lv-nothing", "such.md") is None


def test_get_of_an_ambiguous_prefix_refuses_and_names_the_candidates(
    db: T2Database,
) -> None:
    _seed(db, "lv-amb", **{"report-a": "1", "report-b": "2"})
    with pytest.raises(VerbError) as exc:
        _call("memory.get", "lv-amb", "report")
    message = str(exc.value)
    assert "ambiguous" in message.lower()
    assert "report-a" in message and "report-b" in message


def test_get_refuses_an_empty_title(db: T2Database) -> None:
    # an empty title is a prefix of every title: it must not resolve one
    _seed(db, "lv-empty", only="x")
    with pytest.raises(VerbArgError, match="title"):
        _call("memory.get", "lv-empty", "")


@pytest.mark.parametrize(
    ("project", "title"), [("", "t"), ("  ", "t"), ("\t\n", "t"), ("p", "  ")]
)
def test_get_refuses_a_blank_project_or_title(
    db: T2Database, project: str, title: str
) -> None:
    # the engine answers a blank project with a 400; the verb refuses first
    with pytest.raises(VerbArgError, match="must not be empty"):
        _call("memory.get", project, title)


def _access_count(db: T2Database, project: str, title: str) -> int:
    (row,) = [r for r in db.memory.get_all(project) if r["title"] == title]
    return row["access_count"]


def test_get_counts_one_access_on_an_exact_hit(db: T2Database) -> None:
    _seed(db, "lv-ac-exact", **{"entry.md": "x"})
    assert _access_count(db, "lv-ac-exact", "entry.md") == 0
    _call("memory.get", "lv-ac-exact", "entry.md")
    assert _access_count(db, "lv-ac-exact", "entry.md") == 1


def test_get_counts_one_access_on_a_unique_prefix_hit(db: T2Database) -> None:
    _seed(db, "lv-ac-prefix", **{"088-research-1: full title": "x"})
    title = "088-research-1: full title"
    assert _access_count(db, "lv-ac-prefix", title) == 0
    _call("memory.get", "lv-ac-prefix", "088-research-1")
    assert _access_count(db, "lv-ac-prefix", title) == 1


def test_get_counts_nothing_on_a_miss(db: T2Database) -> None:
    _seed(db, "lv-ac-miss", **{"bystander.md": "x"})
    assert _call("memory.get", "lv-ac-miss", "absent.md") is None
    assert _access_count(db, "lv-ac-miss", "bystander.md") == 0


def test_get_counts_nothing_on_an_ambiguous_prefix(db: T2Database) -> None:
    _seed(db, "lv-ac-amb", **{"report-a": "1", "report-b": "2"})
    with pytest.raises(VerbError):
        _call("memory.get", "lv-ac-amb", "report")
    assert _access_count(db, "lv-ac-amb", "report-a") == 0
    assert _access_count(db, "lv-ac-amb", "report-b") == 0


def test_get_names_at_most_twenty_ambiguous_candidates(db: T2Database) -> None:
    _seed(db, "lv-many", **{f"cand-{i:02d}": str(i) for i in range(25)})
    with pytest.raises(VerbError) as exc:
        _call("memory.get", "lv-many", "cand")
    message = str(exc.value)
    assert "25 entries match" in message
    assert message.count("'cand-") == 20
    assert "and 5 more" in message


def test_get_names_every_candidate_when_there_are_twenty_or_fewer(
    db: T2Database,
) -> None:
    _seed(db, "lv-few", **{f"few-{i:02d}": str(i) for i in range(20)})
    with pytest.raises(VerbError) as exc:
        _call("memory.get", "lv-few", "few")
    assert str(exc.value).count("'few-") == 20
    assert "more" not in str(exc.value)


def test_plain_drops_a_non_scalar_field_and_keeps_scalars() -> None:
    row = {
        "a": "s",
        "b": 1,
        "c": 1.5,
        "d": True,
        "e": None,
        "f": [1, 2],
        "g": {"k": "v"},
        "h": ("t",),
        "i": object(),
    }
    assert _plain(row) == {"a": "s", "b": 1, "c": 1.5, "d": True, "e": None}


# ── memory.list ─────────────────────────────────────────────────────────────


def test_list_returns_the_projects_entries_newest_first(db: T2Database) -> None:
    _seed(db, "lv-list", first="1", second="2")
    _seed(db, "lv-other", elsewhere="3")
    rows = _call("memory.list", "lv-list")
    assert [r["title"] for r in rows] == ["second", "first"]
    assert {r["project"] for r in rows} == {"lv-list"}
    json.dumps(rows)


def test_list_refuses_an_empty_project(db: T2Database) -> None:
    # list_entries treats an empty project as every project
    _seed(db, "lv-list-all", t="1")
    with pytest.raises(VerbArgError, match="project"):
        _call("memory.list", "")


@pytest.mark.parametrize("project", ["  ", "\t", " \n "])
def test_list_refuses_a_whitespace_only_project(db: T2Database, project: str) -> None:
    # the engine reads a blank project as no filter: every project's entries
    _seed(db, "lv-list-blank", t="1")
    with pytest.raises(VerbArgError, match="project"):
        _call("memory.list", project)


# ── memory.search ───────────────────────────────────────────────────────────


def test_search_finds_entries_with_their_content(db: T2Database) -> None:
    _seed(db, "lv-search", hit="a zebrafish study", miss="nothing relevant")
    rows = _call("memory.search", "zebrafish", "lv-search", None)
    assert [r["title"] for r in rows] == ["hit"]
    assert rows[0]["content"] == "a zebrafish study"
    json.dumps(rows)


def test_search_does_not_track_access(db: T2Database) -> None:
    """The store's default access="track" increments access_count: a write."""
    _seed(db, "lv-silent", hit="a narwhal study")

    def count() -> int:
        (row,) = [r for r in db.memory.get_all("lv-silent") if r["title"] == "hit"]
        return row["access_count"]

    assert count() == 0
    _call("memory.search", "narwhal", "lv-silent", None)
    _call("memory.search", "narwhal", "lv-silent", 5)
    assert count() == 0
    # positive control: the store's own default does count, so the zero above
    # is the verb's doing and not an engine that never counts
    db.memory.search("narwhal", project="lv-silent")
    assert count() == 1


def test_search_limit_cuts_and_nil_means_the_store_default(db: T2Database) -> None:
    for i in range(3):
        db.put(project="lv-limit", title=f"n{i}", content=f"quokka {i}")
    assert len(_call("memory.search", "quokka", "lv-limit", 2)) == 2
    assert len(_call("memory.search", "quokka", "lv-limit", None)) == 3
    assert len(_call("memory.search", "quokka", "lv-limit")) == 3
    assert len(_call("memory.search", "quokka", "lv-limit", 300)) == 3


@pytest.mark.parametrize("limit", [0, -1, 301, 1000])
def test_search_refuses_a_limit_outside_one_to_300(db: T2Database, limit: int) -> None:
    with pytest.raises(VerbArgError, match="limit"):
        _call("memory.search", "quokka", "lv-limit", limit)


def test_search_with_no_limit_returns_at_most_max_query_results(db: T2Database) -> None:
    cap = QUOTAS.MAX_QUERY_RESULTS
    assert cap == 300
    for i in range(cap + 1):
        db.put(project="lv-cap", title=f"c{i}", content=f"axolotl {i}")
    assert len(_call("memory.search", "axolotl", "lv-cap", None)) == cap
    assert len(_call("memory.search", "axolotl", "lv-cap", cap)) == cap


def test_search_across_every_project_with_an_empty_project(db: T2Database) -> None:
    _seed(db, "lv-all-a", a="a tapir")
    _seed(db, "lv-all-b", b="a tapir too")
    rows = _call("memory.search", "tapir", "", None)
    assert {r["project"] for r in rows} == {"lv-all-a", "lv-all-b"}


def test_search_of_a_stopword_only_query_is_a_verb_error(db: T2Database) -> None:
    _seed(db, "lv-stop", t="content")
    with pytest.raises(VerbError, match="searchable"):
        _call("memory.search", "and", "lv-stop", None)


def test_search_refuses_a_blank_query(db: T2Database) -> None:
    with pytest.raises(VerbArgError, match="query"):
        _call("memory.search", "   ", "lv-stop", None)


def test_a_result_over_the_cap_is_refused_never_truncated(
    db: T2Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(db, "lv-bytes", a="gecko " * 70, b="gecko " * 70)
    monkeypatch.setattr(verb_types, "VERB_RESULT_CAP_BYTES", 150)
    with pytest.raises(VerbError, match="cap"):
        _call("memory.search", "gecko", "lv-bytes", None)
    with pytest.raises(VerbError, match="cap"):
        _call("memory.list", "lv-bytes")  # the cap guards the list verb too
    with pytest.raises(VerbError, match="cap"):
        _call("memory.get", "lv-bytes", "a")  # a single over-cap entry too


# ── The deadline ────────────────────────────────────────────────────────────


@pytest.fixture
def store_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Record every request the real store makes: (path, timeout kwarg)."""
    calls: list[tuple[str, object]] = []
    real_get, real_post = HttpMemoryStore._get, HttpMemoryStore._post

    def spy_get(self, path, params=None, **kw):
        calls.append((path, kw.get("timeout")))
        return real_get(self, path, params=params, **kw)

    def spy_post(self, path, payload, **kw):
        calls.append((path, kw.get("timeout")))
        return real_post(self, path, payload, **kw)

    monkeypatch.setattr(HttpMemoryStore, "_get", spy_get)
    monkeypatch.setattr(HttpMemoryStore, "_post", spy_post)
    return calls


_CALLS = [
    ("memory.get", ("lv-dl", "t"), "/v1/memory/resolve"),
    ("memory.list", ("lv-dl",), "/v1/memory/list"),
    ("memory.search", ("probe", "lv-dl", None), "/v1/memory/search"),
]


@pytest.mark.parametrize(("verb", "args", "path"), _CALLS)
def test_every_memory_verb_passes_the_remaining_budget_as_its_timeout(
    db: T2Database, store_calls, verb: str, args: tuple, path: str
) -> None:
    _seed(db, "lv-dl", t="probe")
    store_calls.clear()
    _call(verb, *args, budget=30.0)
    (timeout,) = [t for p, t in store_calls if p == path]
    assert timeout is not None
    # just under the 30 s budget: more than any 3 s reading could be
    assert 3.0 < timeout <= 30.0
    # a smaller budget gives a smaller timeout: it is the remainder, not a constant
    store_calls.clear()
    _call(verb, *args, budget=3.0)
    (small,) = [t for p, t in store_calls if p == path]
    assert 0 < small <= 3.0
    assert small < timeout


@pytest.mark.parametrize(("verb", "args", "path"), _CALLS)
def test_a_deadline_already_past_raises_before_any_request(
    db: T2Database, store_calls, verb: str, args: tuple, path: str
) -> None:
    spec = _SPECS[verb]
    store_calls.clear()
    with pytest.raises(VerbTimeout):
        spec.fn(
            *check_args(spec, list(args)), deadline=time.monotonic() - 1.0, ctx=_CTX
        )
    assert [p for p, _ in store_calls if p == path] == []


@pytest.mark.parametrize(("verb", "args", "path"), _CALLS)
def test_a_deadline_already_past_builds_no_store(
    monkeypatch: pytest.MonkeyPatch, verb: str, args: tuple, path: str
) -> None:
    built: list[bool] = []

    def no_store():
        built.append(True)
        raise AssertionError("the T2 database was built after the deadline")

    monkeypatch.setattr(verbs_memory, "_t2", no_store)
    spec = _SPECS[verb]
    with pytest.raises(VerbTimeout):
        spec.fn(
            *check_args(spec, list(args)), deadline=time.monotonic() - 1.0, ctx=_CTX
        )
    assert built == []


_URL = "http://127.0.0.1:54321/v1/memory/secret-path"


def _status_error(*_a, **_kw):
    request = httpx.Request("GET", _URL)
    raise httpx.HTTPStatusError(
        f"Server error '503 Service Unavailable' for url '{_URL}'",
        request=request,
        response=httpx.Response(503, request=request),
    )


def _connect_error(*_a, **_kw):
    raise httpx.ConnectError(f"All connection attempts failed: {_URL}")


@pytest.mark.parametrize(("verb", "args", "path"), _CALLS)
@pytest.mark.parametrize(
    ("boom", "expect"), [(_status_error, "503"), (_connect_error, "ConnectError")]
)
def test_a_store_failure_is_a_verb_error_that_hides_the_service_address(
    db: T2Database,
    monkeypatch: pytest.MonkeyPatch,
    verb: str,
    args: tuple,
    path: str,
    boom,
    expect: str,
) -> None:
    monkeypatch.setattr(HttpMemoryStore, "_get", boom)
    monkeypatch.setattr(HttpMemoryStore, "_post", boom)
    with pytest.raises(VerbError) as exc:
        _call(verb, *args)
    assert type(exc.value) is VerbError  # not VerbTimeout, not VerbArgError
    message = str(exc.value)
    assert verb in message and expect in message
    for leak in ("http://", "127.0.0.1", "54321", "secret-path"):
        assert leak not in message


@pytest.mark.parametrize(("verb", "args", "path"), _CALLS)
def test_a_store_timeout_is_a_verb_timeout(
    db: T2Database, monkeypatch: pytest.MonkeyPatch, verb: str, args: tuple, path: str
) -> None:
    def boom(self, *a, **kw):
        raise httpx.ReadTimeout("slow engine")

    monkeypatch.setattr(HttpMemoryStore, "_get", boom)
    monkeypatch.setattr(HttpMemoryStore, "_post", boom)
    with pytest.raises(VerbTimeout):
        _call(verb, *args)


def test_the_store_default_timeout_is_unchanged_without_the_keyword(
    db: T2Database, store_calls
) -> None:
    """The new keyword defaults to None: other callers ride the client default."""
    _seed(db, "lv-default", t="walrus")
    store_calls.clear()
    db.memory.resolve_title("lv-default", "t")
    db.memory.search("walrus", project="lv-default", access="silent")
    assert [t for _, t in store_calls] == [None, None]
