# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog list --meta`` asks the engine's metadata route (RDR-241 Phase 2, nexus-uwggq.2.14) instead of
reading every document of the type and filtering on the client.

The real ``HttpCatalogClient`` runs over a fake engine (``_get`` and ``_post`` replaced; the engine's SQL semantics
are modelled: live rows only, the caller's tenant only, ``ORDER BY tumbler`` as TEXT, ``limit`` 1..1000, an exact
``count``, a ``/list`` with no limit holding 200 rows). Equality is proven against the OLD walk, run through the
same client over the same fake: ``all_documents(limit=0, content_type=...)`` then the meta filter and the slice,
as ``list_cmd`` did before this bead. The fixture's tumblers (``1.1.2`` and ``1.1.10``) sort differently as text
and as numbers, so a client-side re-sort would show.
"""
from __future__ import annotations

import json
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.engine_reads.route_unavailable import PHASE2_ROUTES_ENGINE
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.meta_filter import documents_with_meta, meta_matches, parse_meta_filters
from nexus.cli import main
from nexus.commands import catalog as catalog_cmd

_TENANT = "t-self"
_ROUTE = "GET /v1/catalog/docs/by-meta"


def _row(tumbler: str, title: str, content_type: str = "knowledge", *, sensitive: bool = False, deleted: bool = False,
         tenant: str = _TENANT, meta: dict | None = None) -> dict[str, Any]:
    return {
        "tumbler": tumbler, "title": title, "content_type": content_type, "sensitive": sensitive,
        "deleted": deleted, "tenant": tenant, "meta": meta or {}, "alias_of": "",
        "physical_collection": f"{content_type}__c__bge-base-en-v15-768__v1", "chunk_count": 3,
    }


def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _row("1.1.2", "two", meta={"extraction_method": "mineru"}),
        _row("1.1.10", "ten", meta={"extraction_method": "mineru", "extraction_source": "dt_content"}),
        _row("1.1.3", "three", "rdr", sensitive=True, meta={"extraction_method": "mineru", "quality_gate_overridden": True}),
        _row("1.2.1", "docs-one", "docs", sensitive=True, meta={"extraction_method": "docling"}),
        _row("1.2.4", "paper", "paper", meta={"quality_gate_overridden": False}),
        _row("1.3.1", "plain"),
        _row("1.3.2", "tombstoned", deleted=True, meta={"extraction_method": "mineru"}),
        _row("9.9.1", "another tenant", tenant="t-other", meta={"extraction_method": "mineru"}),
    ]


class FakeEngine:
    """The catalog routes this command can reach, over a row store; every request is recorded in ``calls``."""

    def __init__(self, rows: list[dict[str, Any]], *, flag: bool | None = True, status: int | None = None,
                 body: Any = None, probe_failed: bool = False) -> None:
        self.rows, self.flag, self.status, self.body, self.probe_failed = rows, flag, status, body, probe_failed
        self.calls: list[tuple] = []

    def live(self) -> list[dict[str, Any]]:
        return sorted((r for r in self.rows if r["tenant"] == _TENANT and not r["deleted"]), key=lambda r: r["tumbler"])

    @staticmethod
    def wire(r: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in r.items() if k not in ("deleted", "tenant")}

    def flags(self) -> dict[str, Any]:
        return {} if self.flag is None else {"catalog_docs_by_meta_supported": self.flag, "release_version": "0.1.196"}

    def get(self, path: str, **kw: Any) -> Any:
        if path == "/list":
            self.calls.append(("GET", "/list", dict(kw)))
            rows = self.live()
            if kw.get("owner"):
                rows = [r for r in rows if r["tumbler"].startswith(str(kw["owner"]) + ".")]
            elif kw.get("content_type"):
                rows = [r for r in rows if r["content_type"] == kw["content_type"]]
                if kw.get("limit"):
                    rows = rows[kw.get("offset", 0):kw.get("offset", 0) + kw["limit"]]
            else:
                limit, offset = kw.get("limit", 200), kw.get("offset", 0)
                rows = rows[offset:offset + limit]
            return {"documents": [self.wire(r) for r in rows], "count": len(rows)}
        assert path == "/docs/by-meta", path
        request_args = {k: v for k, v in kw.items()}
        self.calls.append(("GET", "/docs/by-meta", request_args))
        if self.status is not None:
            request = httpx.Request("GET", "http://engine/v1/catalog/docs/by-meta")
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(self.status, request=request))
        if self.body is not None:
            return self.body
        limit, offset = kw["limit"], kw["offset"]
        assert 1 <= limit <= 1000 and offset >= 0, "the engine refuses a page outside 1..1000"
        eq = [(k[5:], v) for k, v in kw.items() if k.startswith("meta.")]
        rows = [r for r in self.live()
                if (not kw.get("content_type") or r["content_type"] == kw["content_type"])
                and (kw.get("sensitive") != "true" or r["sensitive"])
                and meta_matches(r["meta"], eq)]
        return {"count": len(rows), "limit": limit, "offset": offset, "documents": [self.wire(r) for r in rows[offset:offset + limit]]}

    def post(self, path: str, body: dict | None = None, **kw: Any) -> Any:
        raise AssertionError(f"unexpected POST {path}")

    def wired(self, monkeypatch: pytest.MonkeyPatch) -> HttpCatalogClient:
        c = object.__new__(HttpCatalogClient)
        monkeypatch.setattr(c, "_version_flags", self.flags, raising=False)
        monkeypatch.setattr(c, "_version_probe_failed", self.probe_failed, raising=False)
        monkeypatch.setattr(c, "_get", self.get, raising=False)
        monkeypatch.setattr(c, "_post", self.post, raising=False)
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: c)
        return c

    def by_meta_calls(self) -> list[dict]:
        return [c[2] for c in self.calls if c[1] == "/docs/by-meta"]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    # RDR-243 Phase 3: `catalog list` withholds a sensitive row under Claude Code (Q2); these tests read the rows.
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")


def _run(*args: str):
    return CliRunner().invoke(main, ["catalog", "list", *args])


def _old_output(cat: HttpCatalogClient, capsys: pytest.CaptureFixture[str], *, as_json: bool, meta: tuple[str, ...], offset: int,
                limit: int, content_type: str = "", sensitive: bool = False) -> str:
    """The command's no-``--owner`` body before nexus-uwggq.2.14, run on the real client over the fake."""
    entries = cat.all_documents(limit=0, content_type=content_type)
    if sensitive:
        entries = [e for e in entries if e.sensitive]
    entries = documents_with_meta(entries, parse_meta_filters(meta))
    total, page = len(entries), entries[offset:offset + limit]
    presented = catalog_cmd._present_cli_rows(cat, page)
    capsys.readouterr()
    if as_json:
        click.echo(json.dumps(presented.rows, indent=2))
    else:
        catalog_cmd._echo_document_rows(presented)
        if offset + limit < total:
            click.echo(f"\n  Next page: --offset {offset + limit}")
    return capsys.readouterr().out


_CASES = [
    pytest.param({"meta": ("extraction_method=mineru",)}, id="meta"),
    pytest.param({"meta": ("extraction_method=mineru",), "limit": 1}, id="meta-paged"),
    pytest.param({"meta": ("extraction_method=mineru",), "limit": 1, "offset": 1}, id="meta-second-page"),
    pytest.param({"meta": ("extraction_method=mineru",), "limit": 1, "offset": 2}, id="meta-last-page"),
    pytest.param({"meta": ("extraction_method=mineru",), "limit": 2, "offset": 40}, id="past-the-end"),
    pytest.param({"meta": ("extraction_method=mineru", "quality_gate_overridden=true")}, id="anded"),
    pytest.param({"meta": ("quality_gate_overridden=false",)}, id="explicit-false"),
    pytest.param({"meta": ("extraction_method=mineru",), "content_type": "rdr"}, id="meta-and-type"),
    pytest.param({"meta": ("extraction_method=nobody",)}, id="no-match"),
    pytest.param({"meta": ("extraction_source=file",)}, id="absent-source-reads-as-file"),
    pytest.param({"meta": ("extraction_source=dt_content",)}, id="stated-source"),
]


def _argv(case: dict, *, as_json: bool) -> list[str]:
    argv = ["--json"] if as_json else []
    if "limit" in case:
        argv += ["-n", str(case["limit"])]
    if "offset" in case:
        argv += ["--offset", str(case["offset"])]
    if "content_type" in case:
        argv += ["--type", case["content_type"]]
    for spec in case["meta"]:
        argv += ["--meta", spec]
    return argv


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("case", _CASES)
def test_output_equals_the_old_walk_in_one_request(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                                   case: dict, as_json: bool) -> None:
    engine = FakeEngine(_fixture_rows())
    cat = engine.wired(monkeypatch)
    expected = _old_output(cat, capsys, as_json=as_json, meta=case["meta"], offset=case.get("offset", 0),
                           limit=case.get("limit", 50), content_type=case.get("content_type", ""))
    engine.calls.clear()
    result = _run(*_argv(case, as_json=as_json))
    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected
    assert [c[1] for c in engine.calls] == ["/docs/by-meta"], "one request, and no read of the catalog's documents"


def test_the_request_carries_the_clauses_the_page_and_the_stated_default(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("--meta", "extraction_source=file", "--meta", "extraction_method=mineru", "--type", "knowledge",
                  "-n", "7", "--offset", "3")
    assert result.exit_code == 0, result.stderr
    [sent] = engine.by_meta_calls()
    assert sent["meta.extraction_source"] == "file" and sent["meta_default.extraction_source"] == "file"
    assert sent["meta.extraction_method"] == "mineru" and "meta_default.extraction_method" not in sent
    assert (sent["content_type"], sent["limit"], sent["offset"]) == ("knowledge", 7, 3)
    assert "sensitive" not in sent


def test_the_walk_order_is_text_order_and_the_command_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``1.1.10`` sorts before ``1.1.2`` as text, which is the walk's order; a numeric re-sort would flip them."""
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    rows = json.loads(_run("--meta", "extraction_method=mineru", "--json").stdout)
    assert [r["tumbler"] for r in rows] == ["1.1.10", "1.1.2", "1.1.3"]


def test_the_next_page_line_uses_the_routes_count_and_the_page_is_not_sliced_again(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    first = _run("--meta", "extraction_method=mineru", "-n", "2")
    assert first.stdout.rstrip().endswith("Next page: --offset 2")
    second = _run("--meta", "extraction_method=mineru", "-n", "2", "--offset", "2")
    assert "1.1.3" in second.stdout and "Next page" not in second.stdout, "the engine applied the offset; the command must not"


def test_a_page_larger_than_the_routes_bound_is_paged_by_offset(monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    rows = [_row(f"2.1.{i}", f"n{i}", meta={"batch": "x"}) for i in range(1, 2501)]
    engine = FakeEngine(rows)
    cat = engine.wired(monkeypatch)
    expected = _old_output(cat, capsys, as_json=True, meta=("batch=x",), offset=5, limit=2200)
    engine.calls.clear()
    result = _run("--meta", "batch=x", "-n", "2200", "--offset", "5", "--json")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected and len(json.loads(result.stdout)) == 2200
    assert [(c["limit"], c["offset"]) for c in engine.by_meta_calls()] == [(1000, 5), (1000, 1005), (200, 2005)]
    assert [c[1] for c in engine.calls] == ["/docs/by-meta"] * 3


def test_paging_stops_when_the_matches_run_out(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [_row(f"2.1.{i}", f"n{i}", meta={"batch": "x"}) for i in range(1, 1201)]
    engine = FakeEngine(rows)
    engine.wired(monkeypatch)
    result = _run("--meta", "batch=x", "-n", "5000", "--json")
    assert result.exit_code == 0, result.stderr
    assert len(json.loads(result.stdout)) == 1200
    assert [(c["limit"], c["offset"]) for c in engine.by_meta_calls()] == [(1000, 0), (1000, 1000)]


def test_owner_with_meta_still_reads_the_owner_and_asks_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("--owner", "1.1", "--meta", "extraction_method=mineru", "--json")
    assert result.exit_code == 0, result.stderr
    assert [r["tumbler"] for r in json.loads(result.stdout)] == ["1.1.10", "1.1.2", "1.1.3"]
    assert engine.calls == [("GET", "/list", {"owner": "1.1"})]


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_meta_with_sensitive_is_one_by_meta_request_with_the_sensitive_clause(monkeypatch: pytest.MonkeyPatch,
                                                                              capsys: pytest.CaptureFixture[str], as_json: bool) -> None:
    engine = FakeEngine(_fixture_rows())
    cat = engine.wired(monkeypatch)
    expected = _old_output(cat, capsys, as_json=as_json, meta=("extraction_method=mineru",), offset=0, limit=50, sensitive=True)
    engine.calls.clear()
    argv = ["--sensitive", "--meta", "extraction_method=mineru"] + (["--json"] if as_json else [])
    result = _run(*argv)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected and "1.1.3" in result.stdout and "1.1.2" not in result.stdout
    [sent] = engine.by_meta_calls()
    assert sent["sensitive"] == "true"
    assert [c[1] for c in engine.calls] == ["/docs/by-meta"], "no /sensitive read, no resolve_many, no walk"


def test_meta_with_sensitive_works_past_ten_thousand_sensitive_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Phase 1 path refused a filter on a tenant over the sensitive list's 10,000 cap; by-meta has no such cap."""
    rows = [_row(f"4.1.{i}", f"b{i}", sensitive=True, meta={"extraction_method": "mineru"}) for i in range(1, 12_001)]
    engine = FakeEngine(rows + _fixture_rows())
    engine.wired(monkeypatch)
    result = _run("--sensitive", "--meta", "extraction_method=mineru", "-n", "3", "--json")
    assert result.exit_code == 0, result.stderr
    assert [r["tumbler"] for r in json.loads(result.stdout)] == ["1.1.3", "4.1.1", "4.1.10"]
    assert [c[1] for c in engine.calls] == ["/docs/by-meta"]


@pytest.mark.parametrize("flag", [False, None], ids=["flag-false", "flag-absent"])
def test_an_engine_without_the_flag_exits_naming_the_route_and_the_engine(monkeypatch: pytest.MonkeyPatch, flag: bool | None) -> None:
    engine = FakeEngine(_fixture_rows(), flag=flag)
    engine.wired(monkeypatch)
    result = _run("--meta", "extraction_method=mineru")
    assert result.exit_code != 0
    assert _ROUTE in result.stderr and PHASE2_ROUTES_ENGINE in result.stderr
    assert engine.calls == [], "no request goes out: not the route, not a walk"


def test_a_404_from_the_route_exits_with_the_same_message(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), status=404)
    engine.wired(monkeypatch)
    result = _run("--meta", "extraction_method=mineru")
    assert result.exit_code != 0
    assert _ROUTE in result.stderr and PHASE2_ROUTES_ENGINE in result.stderr
    assert [c[1] for c in engine.calls] == ["/docs/by-meta"], "the one route request; never a walk"


def test_a_503_exits_saying_the_engine_could_not_answer_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), status=503)
    engine.wired(monkeypatch)
    result = _run("--meta", "extraction_method=mineru")
    assert result.exit_code != 0
    assert "could not answer" in result.stderr and "in time" in result.stderr
    assert PHASE2_ROUTES_ENGINE not in result.stderr
    assert [c[1] for c in engine.calls] == ["/docs/by-meta"]


def test_a_failed_version_probe_says_so_and_does_not_say_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), flag=None, probe_failed=True)
    engine.wired(monkeypatch)
    result = _run("--meta", "extraction_method=mineru")
    assert result.exit_code != 0
    assert "GET /version failed" in result.stderr and PHASE2_ROUTES_ENGINE not in result.stderr
    assert engine.calls == []


def test_a_malformed_answer_exits_without_walking(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), body={"count": "many"})
    engine.wired(monkeypatch)
    result = _run("--meta", "extraction_method=mineru")
    assert result.exit_code != 0
    assert "not the route's contract" in result.stderr
    assert [c[1] for c in engine.calls] == ["/docs/by-meta"]


def test_a_reader_without_the_method_exits_with_the_message_and_does_not_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reader:
        def all_documents(self, *a: Any, **kw: Any) -> list:
            raise AssertionError("must not walk")

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: _Reader())
    result = _run("--meta", "extraction_method=mineru")
    assert result.exit_code != 0
    assert _ROUTE in result.stderr and PHASE2_ROUTES_ENGINE in result.stderr


@pytest.mark.parametrize("argv", [["-n", "0"], ["-n", "-1"], ["--offset", "-1"]], ids=["zero", "negative-limit", "negative-offset"])
def test_a_page_the_route_cannot_serve_is_a_usage_error_with_no_request(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("--meta", "extraction_method=mineru", *argv)
    assert result.exit_code == 2 and "--limit" in result.stderr
    assert engine.calls == []


def test_an_empty_value_is_a_usage_error_with_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("--meta", "extraction_method=")
    assert result.exit_code == 2 and "non-empty value" in result.stderr
    assert engine.calls == []


def test_the_bounded_list_still_reads_one_page_of_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``--meta``, no ``--sensitive``, no ``--owner``: the one-page list, which is all ``all_documents`` is left for."""
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("--type", "knowledge", "-n", "1")
    assert result.exit_code == 0, result.stderr
    assert [c[1] for c in engine.calls] == ["/list"]
    assert engine.calls[0][2]["limit"] == 2, "limit + offset + 1"
