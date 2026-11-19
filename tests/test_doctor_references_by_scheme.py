# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-uwggq.1.5 (RDR-241 Phase 2): ``nx doctor --check-references`` reads the engine's by-scheme route, one
read per registered non-file scheme, and never walks the catalog.

Covers the client reader (:func:`nexus.catalog.engine_reads.by_scheme.read_docs_by_scheme`: no request without the
``/version`` flag, paging to the exact count, a body off the contract raises), the candidate list (one read per
scheme in registry order, alias rows dropped, tumbler-text order), the CLI on an engine without the route (not
checked, exit 0, no walk), a row ``urlparse`` cannot parse (reported, never aborting the check), and one HTTP
round-trip of :meth:`HttpCatalogClient.documents_by_scheme` against a local fake engine. The real engine route is
covered by the service's own tests and by ``tests/test_doctor_references.py``'s real-substrate class.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus import aspect_readers, doctor_references
from nexus.aspect_readers import StatOk
from nexus.catalog.engine_reads.by_scheme import DOCS_BY_SCHEME_FLAG, read_docs_by_scheme
from nexus.catalog.http_catalog_client import HttpCatalogClient

_ON = {DOCS_BY_SCHEME_FLAG: True}


def _pager(rows: list[dict], *, count: int | None = None) -> tuple[list[dict], Any]:
    """A fake ``_get`` serving *rows* in pages, recording each call's params."""
    calls: list[dict] = []

    def _get(path: str, **kw: Any) -> dict:
        calls.append({"path": path, **kw})
        off, lim = kw["offset"], kw["limit"]
        return {"count": len(rows) if count is None else count, "limit": lim, "offset": off,
                "documents": rows[off:off + lim]}

    return calls, _get


# ── the reader ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("flags", [{}, {DOCS_BY_SCHEME_FLAG: False}, {DOCS_BY_SCHEME_FLAG: "true"}],
                         ids=["absent", "false", "string-true"])
def test_no_flag_sends_no_request_and_answers_none(flags: dict) -> None:
    calls, get = _pager([{"tumbler": "1.1.1"}])
    assert read_docs_by_scheme(get, flags, "https") is None
    assert calls == []


def test_pages_until_the_exact_count() -> None:
    rows = [{"tumbler": f"1.1.{i}"} for i in range(5)]
    calls, get = _pager(rows)
    assert read_docs_by_scheme(get, _ON, "https", page=2) == rows
    assert [(c["path"], c["scheme"], c["offset"], c["limit"]) for c in calls] == [
        ("/docs/by-scheme", "https", 0, 2), ("/docs/by-scheme", "https", 2, 2), ("/docs/by-scheme", "https", 4, 2)]
    # Sent once each: a retried page the engine cut at its statement bound would run the scan again.
    assert all(c["idempotent"] is False for c in calls)


def test_zero_matches_is_one_request_and_an_empty_list() -> None:
    calls, get = _pager([])
    assert read_docs_by_scheme(get, _ON, "obsidian") == []
    assert len(calls) == 1


def test_rows_deleted_between_pages_end_the_read() -> None:
    """The count is per page: when it falls below the offset already read, the read stops there."""
    pages = iter([
        {"count": 3, "limit": 2, "offset": 0, "documents": [{"tumbler": "1.1.1"}, {"tumbler": "1.1.2"}]},
        {"count": 1, "limit": 2, "offset": 2, "documents": []},
    ])
    assert read_docs_by_scheme(lambda _p, **_k: next(pages), _ON, "https", page=2) == [
        {"tumbler": "1.1.1"}, {"tumbler": "1.1.2"}]


@pytest.mark.parametrize("body", [
    [],
    {"count": -1, "limit": 2, "offset": 0, "documents": []},
    {"count": True, "limit": 2, "offset": 0, "documents": [{}]},
    {"count": 1, "limit": 2, "offset": 7, "documents": [{"tumbler": "1.1.1"}]},
    {"count": 1, "limit": 2, "offset": 0, "documents": "x"},
    {"count": 1, "limit": 2, "offset": 0, "documents": ["1.1.1"]},
    {"count": 9, "limit": 2, "offset": 0, "documents": []},
], ids=["list", "negative-count", "bool-count", "offset-echo", "docs-not-list", "doc-not-object",
        "empty-page-short-of-count"])
def test_a_body_off_the_contract_raises(body: Any) -> None:
    with pytest.raises(ValueError):
        read_docs_by_scheme(lambda _p, **_k: body, _ON, "https", page=2)


def test_a_page_larger_than_asked_raises() -> None:
    """The body echoes each requested offset, so only the page-size check can refuse it."""
    def _get(_p: str, **kw: Any) -> dict:
        return {"count": 9, "limit": 2, "offset": kw["offset"], "documents": [{}, {}, {}]}

    with pytest.raises(ValueError):
        read_docs_by_scheme(_get, _ON, "https", page=2)


# ── the candidate list and the CLI ──────────────────────────────────────────


class _Entry:
    def __init__(self, tumbler: str, source_uri: str, *, alias_of: str = "") -> None:
        self.tumbler, self.source_uri, self.alias_of = tumbler, source_uri, alias_of
        self.title, self.source_mtime, self.meta = f"doc {tumbler}", 0.0, {}


class _Cat:
    def __init__(self, entries: list[_Entry], *, supported: bool = True) -> None:
        self._entries, self._supported = entries, supported
        self.schemes: list[str] = []

    def documents_by_scheme(self, scheme: str) -> list[_Entry] | None:
        self.schemes.append(scheme)
        if not self._supported:
            return None
        return [e for e in self._entries if doctor_references._scheme_of(e.source_uri) == scheme.lower()]

    def all_documents(self, limit: int = 0) -> list[_Entry]:
        raise AssertionError("--check-references must not walk the catalog (RDR-241)")


def test_one_read_per_registered_non_file_scheme_in_registry_order() -> None:
    cat = _Cat([])
    assert doctor_references.reference_only_candidates(cat) == []
    expected = [s for s in aspect_readers.registered_stat_schemes() if s != "file"]
    assert cat.schemes == expected
    assert len(expected) == 5, expected  # obsidian, x-devonthink-item, nx-scratch, nx-note, https


def test_candidates_drop_aliases_and_come_in_tumbler_text_order() -> None:
    cat = _Cat([
        _Entry("1.1.2", "https://a/2"), _Entry("1.1.10", "obsidian://open?file=x"),
        _Entry("1.1.3", "https://a/3", alias_of="1.1.2"), _Entry("1.1.1", "HTTPS://a/1"),
        _Entry("1.1.4", "chroma://x/y"),
    ])
    got = doctor_references.reference_only_candidates(cat)
    assert got is not None
    assert [e.tumbler for e in got] == ["1.1.1", "1.1.10", "1.1.2"]


def test_an_engine_without_the_route_is_not_checked(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    cat = _Cat([_Entry("1.1.1", "https://a/1")], supported=False)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: cat)
    with pytest.raises(SystemExit) as exc:
        doctor_references.run_check_references(sample=10, seed=1)
    assert exc.value.code == 3  # could not check
    out = capsys.readouterr().out
    assert out.startswith("[ ] Reference staleness: not checked")
    assert "catalog_docs_by_scheme_supported" in out
    assert cat.schemes == ["obsidian"]  # the first scheme's None stops the reads


def test_a_row_the_engine_should_not_have_returned_is_dropped() -> None:
    class _Wide(_Cat):
        def documents_by_scheme(self, scheme: str) -> list[_Entry] | None:
            return [_Entry("1.1.1", "file:///a"), _Entry("1.1.2", f"{scheme}://x")]

    got = doctor_references.reference_only_candidates(_Wide([]))
    assert got is not None and {e.tumbler for e in got} == {"1.1.2"}
    assert len(got) == 5  # one per registered non-file scheme, the file:// row dropped each time


def test_a_failed_version_probe_is_an_unreadable_catalog_exit_1(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    client = _client_with({}, True, [])
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: client)
    with pytest.raises(SystemExit) as exc:
        doctor_references.run_check_references(sample=10, seed=1)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "UNREADABLE" in out and "GET /version failed" in out


def test_a_route_error_is_an_unreadable_catalog(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class _Broken(_Cat):
        def documents_by_scheme(self, scheme: str) -> list[_Entry] | None:
            raise ValueError("docs/by-scheme body is not the contract")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _Broken([]))
    with pytest.raises(SystemExit) as exc:
        doctor_references.run_check_references(sample=10, seed=1)
    assert exc.value.code == 1
    assert "UNREADABLE" in capsys.readouterr().out


@pytest.mark.parametrize("uri", ["  https://[bad/x", "ht\ntps://[bad/x", "\x01HTTPS://[bad/x"],
                         ids=["leading-spaces", "interior-newline", "leading-control-capitals"])
def test_the_lexical_scheme_fallback_strips_as_the_route_does(uri: str) -> None:
    with pytest.raises(ValueError):
        urlparse(uri)
    assert doctor_references._scheme_of(uri) == "https"


def test_a_row_urlparse_rejects_is_reported_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """``urlsplit`` raises ValueError on an invalid bracketed host; the route still answers the row's scheme,
    so stat_one must report it instead of raising out of the check."""
    bad = "https://[not-an-ip/x"
    with pytest.raises(ValueError):
        urlparse(bad)
    assert doctor_references.is_reference_only(bad) is True
    monkeypatch.setitem(aspect_readers._STAT_HANDLERS, "https", lambda uri, **_kw: StatOk(current_mtime=None))
    result = doctor_references.stat_one(_Entry("1.1.1", bad), http_client=None)
    assert (result.tumbler, result.scheme, result.signal) == ("1.1.1", "https", None)
    assert result.error is not None and result.error.startswith("ValueError")


# ── one HTTP round-trip of the client method ────────────────────────────────


class _FakeEngine(BaseHTTPRequestHandler):
    rows: list[dict] = []
    requests: list[str] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler contract
        u = urlparse(self.path)
        type(self).requests.append(u.path)
        if u.path.endswith("/version"):
            self._send({DOCS_BY_SCHEME_FLAG: True})
            return
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        off, lim = int(q["offset"]), int(q["limit"])
        hits = [r for r in type(self).rows if urlparse(r["source_uri"]).scheme == q["scheme"].lower()]
        self._send({"count": len(hits), "limit": lim, "offset": off, "documents": hits[off:off + lim]})


def _client_with(flags: dict, probe_failed: bool, rows: list[dict]) -> HttpCatalogClient:
    client = HttpCatalogClient("http://127.0.0.1:9", _token="test-token")
    client._version_flags = lambda: flags  # type: ignore[method-assign]
    client._version_probe_failed = probe_failed
    client._get = _pager(rows)[1]  # type: ignore[method-assign]
    return client


def test_a_failed_version_probe_raises_instead_of_reading_as_an_old_engine() -> None:
    with pytest.raises(RuntimeError, match="GET /version failed"):
        _client_with({}, True, []).documents_by_scheme("https")
    assert _client_with({}, False, []).documents_by_scheme("https") is None


def test_a_row_with_no_tumbler_raises() -> None:
    with pytest.raises(ValueError, match="no tumbler"):
        _client_with(_ON, False, [{"tumbler": "1.1.1"}, {"title": "x"}]).documents_by_scheme("https")


def test_a_row_repeated_across_pages_is_kept_once_in_first_seen_order() -> None:
    rows = [{"tumbler": "1.1.1", "title": "first"}, {"tumbler": "1.1.2"}, {"tumbler": "1.1.1", "title": "again"},
            {"tumbler": "1.1.3"}]
    got = _client_with(_ON, False, rows).documents_by_scheme("https")
    assert got is not None and [str(e.tumbler) for e in got] == ["1.1.1", "1.1.2", "1.1.3"]
    assert got[0].title == "first"


def test_client_method_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:

    _FakeEngine.rows = [
        {"tumbler": "1.1.1", "title": "a", "source_uri": "HTTPS://a/1", "alias_of": ""},
        {"tumbler": "1.1.2", "title": "b", "source_uri": "obsidian://open?file=b", "alias_of": ""},
    ]
    _FakeEngine.requests = []
    server = HTTPServer(("127.0.0.1", 0), _FakeEngine)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = HttpCatalogClient(f"http://127.0.0.1:{server.server_port}", _token="test-token")
        got = client.documents_by_scheme("https")
    finally:
        server.shutdown()
    assert got is not None and [str(e.tumbler) for e in got] == ["1.1.1"]
    assert got[0].source_uri == "HTTPS://a/1"
    assert any(p.endswith("/docs/by-scheme") for p in _FakeEngine.requests)
