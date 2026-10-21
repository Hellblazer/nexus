# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-pxp44: the rdr SessionStart hook reads the two row kinds it uses, not the project.

``nexus_rdr`` is 1,527 rows and 4.6 MB on the cloud tenant, of which 378 (the
status rows titled ``NNN`` / ``RDR-NNN`` and the ``*-gate-latest`` rows) are
read; the whole-project fetch took 1.7-8.9 s against a 7 s budget. The engine
now filters ``GET /v1/memory/all?title_kinds=rdr_status,gate_latest``. An older
engine ignores the parameter, so the client cuts the rows itself and callers get
the same rows either way.
"""
from __future__ import annotations

import json

import httpx
import pytest

from nexus.db.t2.http_memory_store import TITLE_KINDS, HttpMemoryStore

#: One project's worth of titles: three status rows (two shapes), two gate rows,
#: and the titles neither kind may return. The near-misses are there to expose a
#: pattern that lost its ``^`` (``x-42``, ``xgate-latest``), its ``$`` or its
#: ``-`` (``042-research``, ``RDR-12-notes``), or that took any Unicode digit
#: or a trailing newline (Postgres ``$`` and Python ``fullmatch`` reject both;
#: Python ``\d`` and ``re.match(...$)`` accept them).
_STATUS = ["042", "7", "RDR-105"]
_GATE = ["042-gate-latest", "RDR-105-gate-latest"]
_NEAR_MISS = ["x-42", "xgate-latest", "042-research", "RDR-12-notes", "٣٤", "RDR-٣٤", "42\n", "RDR-42\n"]
_OTHER = ["RDR-105-notes", "42-final", "plain title", "gate-latest-not", "RDR-", *_NEAR_MISS]


def _row(i: int, title: str) -> dict:
    return {
        "id": i, "project": "p", "title": title, "content": f"status: draft\nbody of {title}\n",
        "tags": "", "session": None, "agent": None, "timestamp": "2026-10-17T00:00:00Z",
        "ttl": 30, "access_count": 0, "last_accessed": None,
    }


def _all_rows() -> list[dict]:
    return [_row(i, t) for i, t in enumerate(_STATUS + _GATE + _OTHER)]


def _store(monkeypatch, handler) -> tuple[HttpMemoryStore, list[dict]]:
    store = HttpMemoryStore.__new__(HttpMemoryStore)
    sent: list[dict] = []

    def _get(path, params=None, **kw):
        sent.append(dict(params or {}))
        return handler(path, params or {})

    monkeypatch.setattr(store, "_get", _get)
    return store, sent


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://engine/v1/memory/all")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))


def test_the_kinds_reach_the_engine_and_only_matching_rows_come_back(monkeypatch):
    def engine(path, params):
        wanted = set(params["title_kinds"].split(","))
        assert wanted == {"rdr_status", "gate_latest"}
        keep = set(_STATUS) | set(_GATE)
        return [r for r in _all_rows() if r["title"] in keep]

    store, sent = _store(monkeypatch, engine)
    rows = store.get_all("p", title_kinds=["gate_latest", "rdr_status"])

    assert sorted(r["title"] for r in rows) == sorted(_STATUS + _GATE)
    assert sent == [{"project": "p", "title_kinds": "rdr_status,gate_latest"}]


def test_an_engine_that_ignores_the_parameter_yields_the_same_rows(monkeypatch):
    store, sent = _store(monkeypatch, lambda path, params: _all_rows())
    rows = store.get_all("p", title_kinds=TITLE_KINDS)

    assert sorted(r["title"] for r in rows) == sorted(_STATUS + _GATE)
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [(["rdr_status"], _STATUS), (["gate_latest"], _GATE)],
)
def test_one_kind_returns_only_that_kind_whatever_the_engine_sent(monkeypatch, kinds, expected):
    store, _ = _store(monkeypatch, lambda path, params: _all_rows())
    assert sorted(r["title"] for r in store.get_all("p", title_kinds=kinds)) == sorted(expected)


def test_a_400_to_the_parameter_is_read_again_without_it(monkeypatch):
    calls: list[dict] = []

    def engine(path, params):
        calls.append(dict(params))
        if "title_kinds" in params:
            raise _status_error(400)
        return _all_rows()

    store, _ = _store(monkeypatch, engine)
    rows = store.get_all("p", title_kinds=TITLE_KINDS)

    assert sorted(r["title"] for r in rows) == sorted(_STATUS + _GATE)
    assert calls == [{"project": "p", "title_kinds": "rdr_status,gate_latest"}, {"project": "p"}]


@pytest.mark.parametrize("code", [401, 404, 500, 503])
def test_any_other_error_still_raises(monkeypatch, code):
    def engine(path, params):
        raise _status_error(code)

    store, sent = _store(monkeypatch, engine)
    with pytest.raises(httpx.HTTPStatusError):
        store.get_all("p", title_kinds=TITLE_KINDS)
    assert len(sent) == 1, "only a 400 is worth a second, unfiltered read"


def test_no_kinds_sends_no_parameter_and_keeps_every_row(monkeypatch):
    for kinds in (None, [], ()):
        store, sent = _store(monkeypatch, lambda path, params: _all_rows())
        rows = store.get_all("p", title_kinds=kinds)
        assert len(rows) == len(_all_rows())
        assert sent == [{"project": "p"}]


@pytest.mark.parametrize("bad", [["bogus"], ["rdr_status", "bogus"], "rdr_status"])
def test_an_unknown_kind_or_a_bare_string_is_refused_before_any_request(monkeypatch, bad):
    store, sent = _store(monkeypatch, lambda path, params: _all_rows())
    with pytest.raises(ValueError):
        store.get_all("p", title_kinds=bad)
    assert sent == []


def test_the_client_filter_rejects_every_near_miss_title(monkeypatch):
    """ASCII digits, whole title: neither kind takes a near-miss, in any combination."""
    store, _ = _store(monkeypatch, lambda path, params: _all_rows())
    for kinds in (["rdr_status"], ["gate_latest"], TITLE_KINDS):
        titles = {r["title"] for r in store.get_all("p", title_kinds=kinds)}
        assert titles.isdisjoint({"RDR-", "42-final", *_NEAR_MISS}), (kinds, titles)


def test_the_client_filter_and_the_hook_loader_share_one_status_definition(monkeypatch):
    """A Unicode-digit title and a trailing-newline title are rejected by both
    the client cut and the hook's status loader, and the shapes that are status
    rows are accepted by both."""
    from nexus.hooks import rdr_verb

    cases = {"042": True, "7": True, "RDR-105": True, "٣٤": False, "RDR-٣٤": False,
             "42\n": False, "RDR-42\n": False, "x-42": False, "042-research": False,
             "RDR-12-notes": False, "RDR-": False}
    rows = [dict(_row(i, t), content="status: draft\n") for i, t in enumerate(cases)]

    store, _ = _store(monkeypatch, lambda path, params: rows)
    client = {r["title"] for r in store.get_all("p", title_kinds=["rdr_status"])}

    monkeypatch.setattr(rdr_verb, "_fetch_rdr_rows", lambda repo: rows)
    hook = rdr_verb._load_all_t2_statuses("p")

    assert client == {t for t, ok in cases.items() if ok}
    assert set(hook) == {"42", "7", "105"}, "only the accepted titles reach the status map, keyed on the bare number"


@pytest.mark.parametrize("err", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_a_transport_error_propagates_with_no_unfiltered_retry(monkeypatch, err):
    """Only a 400 earns the second, unfiltered read; a dead or slow engine must
    not be asked for the whole project."""
    def engine(path, params):
        raise err

    store, sent = _store(monkeypatch, engine)
    with pytest.raises(type(err)):
        store.get_all("p", title_kinds=TITLE_KINDS)
    assert len(sent) == 1 and "title_kinds" in sent[0]


def test_rdr_rows_logs_why_it_returned_nothing_and_never_writes_stdout(monkeypatch, capsys):
    from structlog.testing import capture_logs

    from nexus.db import t2_reads

    class _DeadDb:
        def __init__(self, path):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr("nexus.db.t2.T2Database", _DeadDb)
    with capture_logs() as logs:
        assert t2_reads.rdr_rows("nexus_rdr") == []

    assert [(e["event"], e["log_level"], e["error_class"], e["project"]) for e in logs] == [
        ("rdr_rows_unreadable", "warning", "ConnectError", "nexus_rdr")
    ]
    assert capsys.readouterr().out == "", "a hook's stdout is its decision channel"


def test_rdr_rows_asks_for_exactly_the_status_and_gate_kinds(monkeypatch, tmp_path):
    from nexus.db import t2_reads

    seen: list[tuple] = []

    class _FakeDb:
        def __init__(self, path):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_all(self, project, *, title_kinds=None):
            seen.append((project, tuple(title_kinds)))
            return [_row(1, "042")]

    monkeypatch.setattr("nexus.db.t2.T2Database", _FakeDb)
    assert [r["title"] for r in t2_reads.rdr_rows("nexus_rdr")] == ["042"]
    assert seen == [("nexus_rdr", ("rdr_status", "gate_latest"))]
    assert set(t2_reads.RDR_ROW_KINDS) == {"rdr_status", "gate_latest"}


# ── The engine, end to end ─────────────────────────────────────────────────────


@pytest.mark.integration
def test_the_engine_returns_exactly_the_status_and_gate_rows(t2_service_env, tmp_path, monkeypatch):
    """The titles asserted are the RAW wire titles, read before the client-side
    cut, so an engine that over-returns (a dropped ``^`` or ``-``, a Unicode
    digit, a trailing newline) fails here instead of being masked."""
    from nexus.db.t2 import T2Database

    store = T2Database(tmp_path / "t2.db").memory
    project = "pxp44_rdr"
    for title in _STATUS + _GATE + _OTHER:
        store.put(project=project, title=title, content=f"status: draft\n{'x' * 400}\n{title}\n")
    store.put(project="pxp44_other", title="042", content="another project's status row")

    wire: list[tuple[dict, list[str], int]] = []
    real_get = store._get

    def _spy(path, params=None, **kw):
        out = real_get(path, params=params, **kw)
        wire.append((dict(params or {}), [r["title"] for r in out], len(json.dumps(out))))
        return out

    monkeypatch.setattr(store, "_get", _spy)

    narrow = store.get_all(project, title_kinds=TITLE_KINDS)
    whole = store.get_all(project)

    (narrow_params, narrow_wire_titles, narrow_bytes), (whole_params, whole_wire_titles, whole_bytes) = wire
    assert narrow_params["title_kinds"] == "rdr_status,gate_latest"
    assert "title_kinds" not in whole_params
    # Raw, pre-cut: what the engine itself answered.
    assert sorted(narrow_wire_titles) == sorted(_STATUS + _GATE), sorted(set(narrow_wire_titles) ^ set(_STATUS + _GATE))
    # The store kept every title it was given verbatim (a stored "42\n" is the
    # test's only way to expose a trailing-newline-tolerant pattern), so the
    # near-miss fixtures are really in the project.
    assert sorted(whole_wire_titles) == sorted(_STATUS + _GATE + _OTHER)
    assert sorted(r["title"] for r in narrow) == sorted(_STATUS + _GATE)
    assert len(whole) == len(_STATUS + _GATE + _OTHER)
    assert narrow_bytes < whole_bytes * 0.6, (narrow_bytes, whole_bytes)
    print(f"pxp44 measurement: rows {len(whole)} -> {len(narrow)}, bytes {whole_bytes} -> {narrow_bytes}")
    for r in narrow:
        assert r["content"].startswith("status: draft"), "narrowed rows still carry full content"


@pytest.mark.integration
def test_the_engine_refuses_an_unknown_kind_with_a_400(t2_service_env, tmp_path):
    from nexus.db.t2 import T2Database

    store = T2Database(tmp_path / "t2.db").memory
    with pytest.raises(httpx.HTTPStatusError) as exc:
        store._get("/v1/memory/all", params={"project": "pxp44_x", "title_kinds": "bogus"})
    assert exc.value.response.status_code == 400
