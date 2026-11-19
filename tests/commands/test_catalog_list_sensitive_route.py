# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog list --sensitive`` reads the engine's sensitive list, then ``resolve_many`` (RDR-241 Phase 1,
nexus-uwggq.1.7), instead of walking every document of the type.

The real ``HttpCatalogClient`` runs over a fake engine (``_get`` and ``_post`` replaced; the engine's SQL
semantics are modelled: live rows only, the caller's tenant only, ``ORDER BY tumbler`` as TEXT, a ``/list``
with no limit holding 200 rows). Equality is proven against the OLD walk, run through the same client over the
same fake: ``all_documents(limit=0, content_type=...)`` then the sensitive and meta filters and the slice, as
``list_cmd`` did before this bead. ``/resolve_many`` answers in the REVERSE of the order asked, so a command
that trusted the dict order of the answer would fail the order checks. The fixture's tumblers (``1.1.2`` and
``1.1.10``) sort differently as text and as numbers: the walk's order is the text order, and the route's is
the same SQL, so the command keeps the route's order.
"""
from __future__ import annotations

import json
from typing import Any

import click
import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.meta_filter import documents_with_meta, parse_meta_filters
from nexus.cli import main
from nexus.commands import catalog as catalog_cmd

_TENANT = "t-self"
_NEEDS_ROUTE = "nx catalog list --sensitive needs an engine with the sensitive list route (engine-service 0.1.188 or later)"


def _row(tumbler: str, title: str, content_type: str = "knowledge", *, sensitive: bool = False, deleted: bool = False,
         tenant: str = _TENANT, meta: dict | None = None, alias_of: str = "") -> dict[str, Any]:
    return {
        "tumbler": tumbler, "title": title, "content_type": content_type, "sensitive": sensitive,
        "deleted": deleted, "tenant": tenant, "meta": meta or {}, "alias_of": alias_of,
        "physical_collection": f"{content_type}__c__bge-base-en-v15-768__v1", "chunk_count": 3,
    }


def _fixture_rows() -> list[dict[str, Any]]:
    return [
        _row("1.1.2", "two", sensitive=True, meta={"extraction_method": "mineru"}),
        _row("1.1.10", "ten", sensitive=True),
        _row("1.1.3", "three", "rdr", sensitive=True, meta={"extraction_method": "mineru", "quality_gate_overridden": True}),
        _row("1.2.1", "docs-one", "docs", sensitive=True),
        _row("1.2.4", "paper", "paper"),
        _row("1.3.1", "plain"),
        _row("1.3.2", "tombstoned", sensitive=True, deleted=True),
        _row("1.4.1", "alias-of-two", sensitive=True, alias_of="1.1.2"),
        _row("9.9.1", "another tenant", sensitive=True, tenant="t-other"),
    ]


class FakeEngine:
    """The catalog routes this command can reach, over a row store; every request is recorded in ``calls``."""

    def __init__(self, rows: list[dict[str, Any]], *, flag: bool | None = True, sensitive_status: int | None = None,
                 sensitive_body: Any = None, drop_from_resolve: tuple[str, ...] = (),
                 unflag_in_resolve: tuple[str, ...] = (), probe_failed: bool = False) -> None:
        self.rows = rows
        self.flag = flag
        self.sensitive_status = sensitive_status
        self.sensitive_body = sensitive_body
        self.drop_from_resolve = drop_from_resolve
        self.unflag_in_resolve = unflag_in_resolve
        self.probe_failed = probe_failed
        self.calls: list[tuple] = []

    def live(self) -> list[dict[str, Any]]:
        return sorted((r for r in self.rows if r["tenant"] == _TENANT and not r["deleted"]), key=lambda r: r["tumbler"])

    @staticmethod
    def wire(r: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in r.items() if k not in ("deleted", "tenant")}

    def flags(self) -> dict[str, Any]:
        return {} if self.flag is None else {"catalog_sensitive_list_supported": self.flag, "release_version": "0.1.188"}

    def get(self, path: str, **kw: Any) -> Any:
        if path == "/list":
            self.calls.append(("GET", "/list", {k: v for k, v in kw.items()}))
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
        assert path == "/sensitive", path
        self.calls.append(("GET", "/sensitive", {"limit": kw["limit"]}))
        if self.sensitive_status is not None:
            request = httpx.Request("GET", "http://engine/v1/catalog/sensitive")
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(self.sensitive_status, request=request))
        if self.sensitive_body is not None:
            return self.sensitive_body
        flagged = [r for r in self.live() if r["sensitive"]]
        listed = flagged[:kw["limit"]]
        return {"count": len(flagged), "limit": kw["limit"], "capped": len(flagged) > kw["limit"],
                "documents": [{"tumbler": r["tumbler"], "physical_collection": r["physical_collection"]} for r in listed]}

    def post(self, path: str, body: dict | None = None, **kw: Any) -> Any:
        assert path == "/resolve_many", path
        ids = list(body["doc_ids"])
        assert len(ids) <= 1000, "the engine refuses more than 1000 doc_ids a call"
        self.calls.append(("POST", "/resolve_many", ids))
        by_tumbler = {r["tumbler"]: r for r in self.live() if r["tumbler"] not in self.drop_from_resolve}
        found = [by_tumbler[t] for t in ids if t in by_tumbler]
        wire = {r["tumbler"]: self.wire(r) for r in reversed(found)}
        for t in self.unflag_in_resolve:
            if t in wire:
                wire[t] = {**wire[t], "sensitive": False}  # cleared between the list read and this one
        return {"entries": wire}

    def client(self, monkeypatch: pytest.MonkeyPatch) -> HttpCatalogClient:
        c = object.__new__(HttpCatalogClient)
        monkeypatch.setattr(c, "_version_flags", self.flags, raising=False)
        monkeypatch.setattr(c, "_version_probe_failed", self.probe_failed, raising=False)
        monkeypatch.setattr(c, "_get", self.get, raising=False)
        monkeypatch.setattr(c, "_post", self.post, raising=False)
        return c

    def wired(self, monkeypatch: pytest.MonkeyPatch) -> HttpCatalogClient:
        c = self.client(monkeypatch)
        monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: c)
        return c


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    # RDR-243 Phase 3: `catalog list` withholds a sensitive row under Claude Code (Q2); these tests read the rows.
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")


def _run(*args: str):
    return CliRunner().invoke(main, ["catalog", "list", "--sensitive", *args])


def _old_walk(cat: HttpCatalogClient, *, content_type: str = "", meta: tuple[str, ...] = (), offset: int = 0,
              limit: int = 50) -> tuple[list, int]:
    """The command's no-``--owner`` body before nexus-uwggq.1.7, run on the real client over the fake."""
    entries = cat.all_documents(limit=0, content_type=content_type)
    entries = [e for e in entries if e.sensitive]
    if meta:
        entries = documents_with_meta(entries, parse_meta_filters(meta))
    return entries[offset:offset + limit], len(entries)


def _old_output(cat: HttpCatalogClient, capsys: pytest.CaptureFixture[str], *, as_json: bool, offset: int, limit: int,
                content_type: str = "", meta: tuple[str, ...] = ()) -> str:
    page, total = _old_walk(cat, content_type=content_type, meta=meta, offset=offset, limit=limit)
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
    pytest.param({}, id="all"),
    pytest.param({"limit": 2}, id="first-page"),
    pytest.param({"limit": 2, "offset": 2}, id="second-page"),
    pytest.param({"limit": 2, "offset": 4}, id="last-page"),
    pytest.param({"limit": 2, "offset": 40}, id="past-the-end"),
    pytest.param({"limit": 0}, id="zero-page"),
    pytest.param({"limit": 0, "offset": 3}, id="zero-page-offset"),
    pytest.param({"limit": -1}, id="negative-limit"),
    pytest.param({"limit": 2, "offset": -1}, id="negative-offset"),
    pytest.param({"content_type": "knowledge"}, id="type"),
    pytest.param({"content_type": "knowledge", "limit": 1, "offset": 1}, id="type-paged"),
    pytest.param({"content_type": "paper"}, id="type-with-no-sensitive-row"),
]
# --sensitive with --meta is not this route's: since nexus-uwggq.2.14 it is one by-meta request with the sensitive
# clause (tests/commands/test_catalog_list_meta_route.py), so no case here carries --meta.


def _argv(case: dict, *, as_json: bool) -> list[str]:
    argv = ["--json"] if as_json else []
    if "limit" in case:
        argv += ["-n", str(case["limit"])]
    if "offset" in case:
        argv += ["--offset", str(case["offset"])]
    if "content_type" in case:
        argv += ["--type", case["content_type"]]
    for spec in case.get("meta", ()):
        argv += ["--meta", spec]
    return argv


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("case", _CASES)
def test_output_equals_the_old_walk(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
                                    case: dict, as_json: bool) -> None:
    engine = FakeEngine(_fixture_rows())
    cat = engine.wired(monkeypatch)
    expected = _old_output(cat, capsys, as_json=as_json, offset=case.get("offset", 0), limit=case.get("limit", 50),
                           content_type=case.get("content_type", ""), meta=case.get("meta", ()))
    engine.calls.clear()
    result = _run(*_argv(case, as_json=as_json))
    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected
    assert [c for c in engine.calls if c[1] == "/list"] == [], "the command must not read the catalog's documents"


def test_the_walk_order_is_text_order_and_the_command_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``1.1.10`` sorts before ``1.1.2`` as text, which is the walk's order; a numeric re-sort would flip them."""
    engine = FakeEngine(_fixture_rows())
    cat = engine.wired(monkeypatch)
    walked, _total = _old_walk(cat, limit=50)
    assert [str(e.tumbler) for e in walked] == ["1.1.10", "1.1.2", "1.1.3", "1.2.1", "1.4.1"]
    rows = json.loads(_run("--json").stdout)
    assert [r["tumbler"] for r in rows] == [str(e.tumbler) for e in walked]


def test_request_sequence_without_a_filter_resolves_only_the_page(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("-n", "2", "--offset", "1", "--json")
    assert result.exit_code == 0, result.stderr
    assert engine.calls == [("GET", "/sensitive", {"limit": 3}), ("POST", "/resolve_many", ["1.1.2", "1.1.3"])]
    assert "Next page" not in result.stdout  # --json prints rows only


def test_next_page_line_uses_the_routes_count(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("-n", "2")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.rstrip().endswith("Next page: --offset 2")
    assert engine.calls == [("GET", "/sensitive", {"limit": 2}), ("POST", "/resolve_many", ["1.1.10", "1.1.2"])]


def test_request_sequence_with_a_type_resolves_every_listed_tumbler(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("--type", "knowledge", "-n", "1")
    assert result.exit_code == 0, result.stderr
    assert engine.calls == [
        ("GET", "/sensitive", {"limit": 10_000}),
        ("POST", "/resolve_many", ["1.1.10", "1.1.2", "1.1.3", "1.2.1", "1.4.1"]),
    ]


def test_more_tumblers_than_one_resolve_page_still_equal_the_old_walk(monkeypatch: pytest.MonkeyPatch,
                                                                       capsys: pytest.CaptureFixture[str]) -> None:
    rows = [_row(f"2.1.{i}", f"n{i}", sensitive=i % 5 != 0) for i in range(1, 2501)] + _fixture_rows()
    engine = FakeEngine(rows)
    cat = engine.wired(monkeypatch)
    expected = _old_output(cat, capsys, as_json=True, offset=0, limit=100_000, content_type="knowledge")
    engine.calls.clear()
    result = _run("--type", "knowledge", "-n", "100000", "--json")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected
    sizes = [len(c[2]) for c in engine.calls if c[1] == "/resolve_many"]
    assert len(sizes) == 3 and max(sizes) == 1000, sizes


def test_a_document_deleted_between_the_two_reads_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), drop_from_resolve=("1.1.2",))
    engine.wired(monkeypatch)
    rows = json.loads(_run("--json").stdout)
    assert [r["tumbler"] for r in rows] == ["1.1.10", "1.1.3", "1.2.1", "1.4.1"]


def test_owner_with_sensitive_still_reads_the_owner_then_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows())
    engine.wired(monkeypatch)
    result = _run("--owner", "1.1", "--json")
    assert result.exit_code == 0, result.stderr
    assert [r["tumbler"] for r in json.loads(result.stdout)] == ["1.1.10", "1.1.2", "1.1.3"]
    assert engine.calls == [("GET", "/list", {"owner": "1.1"})], "no sensitive list read on the --owner path"


@pytest.mark.parametrize("flag", [False, None], ids=["flag-false", "flag-absent"])
def test_an_engine_without_the_flag_exits_with_the_message(monkeypatch: pytest.MonkeyPatch, flag: bool | None) -> None:
    engine = FakeEngine(_fixture_rows(), flag=flag)
    engine.wired(monkeypatch)
    result = _run()
    assert result.exit_code != 0
    assert _NEEDS_ROUTE in result.stderr
    assert engine.calls == [], "no request goes out: not the route, not a walk"


def test_a_404_from_the_route_exits_with_the_same_message(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), sensitive_status=404)
    engine.wired(monkeypatch)
    result = _run()
    assert result.exit_code != 0
    assert _NEEDS_ROUTE in result.stderr
    assert engine.calls == [("GET", "/sensitive", {"limit": 50})]


def test_a_reader_without_the_method_exits_with_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reader:
        def all_documents(self, *a: Any, **kw: Any) -> list:
            raise AssertionError("must not walk")

    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: _Reader())
    result = _run()
    assert result.exit_code != 0
    assert _NEEDS_ROUTE in result.stderr


def test_a_503_exits_saying_the_engine_could_not_answer_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), sensitive_status=503)
    engine.wired(monkeypatch)
    result = _run()
    assert result.exit_code != 0
    assert "could not answer in time" in result.stderr
    assert _NEEDS_ROUTE not in result.stderr
    assert [c for c in engine.calls if c[1] != "/sensitive"] == [], "no walk, no resolve"


def _big_tenant(n: int = 12_000) -> FakeEngine:
    return FakeEngine([_row(f"4.1.{i:05d}", f"b{i}", sensitive=True) for i in range(1, n + 1)] + _fixture_rows())


def test_a_first_page_works_on_a_tenant_with_more_than_ten_thousand(monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """The route's count is exact when capped and its order is the walk's, so ``-n 50`` needs only 50 tumblers."""
    engine = _big_tenant()
    cat = engine.wired(monkeypatch)
    expected = _old_output(cat, capsys, as_json=False, offset=0, limit=50)
    engine.calls.clear()
    result = _run("-n", "50")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected
    assert "Next page: --offset 50" in result.stdout
    assert [c[:2] for c in engine.calls] == [("GET", "/sensitive"), ("POST", "/resolve_many")]
    assert engine.calls[0][2] == {"limit": 50}


def test_a_page_ending_at_ten_thousand_still_works_and_one_past_it_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _big_tenant()
    engine.wired(monkeypatch)
    ok = _run("--offset", "9950", "-n", "50", "--json")
    assert ok.exit_code == 0, ok.stderr
    assert len(json.loads(ok.stdout)) == 50
    assert engine.calls[0] == ("GET", "/sensitive", {"limit": 10_000})
    engine.calls.clear()
    refused = _run("--offset", "9951", "-n", "50")
    assert refused.exit_code != 0
    assert "more than 10000 sensitive documents" in refused.stderr
    assert "reaches past the first 10000" in refused.stderr and "--owner" in refused.stderr
    assert [c[1] for c in engine.calls] == ["/sensitive"], "refused before anything is resolved"


def test_a_type_filter_on_a_capped_tenant_is_refused_naming_the_limit_and_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _big_tenant()
    engine.wired(monkeypatch)
    result = _run("--type", "knowledge")
    assert result.exit_code != 0
    assert "more than 10000 sensitive documents" in result.stderr
    assert "--type and --meta need the whole list" in result.stderr and "--owner" in result.stderr
    assert engine.calls == [("GET", "/sensitive", {"limit": 10_000})]


def test_a_huge_page_size_on_a_small_tenant_is_not_refused(monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    engine = FakeEngine(_fixture_rows())
    cat = engine.wired(monkeypatch)
    expected = _old_output(cat, capsys, as_json=True, offset=0, limit=1_000_000)
    engine.calls.clear()
    result = _run("-n", "1000000", "--json")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == expected
    assert engine.calls[0] == ("GET", "/sensitive", {"limit": 10_000})


def test_a_listed_tumbler_that_resolves_unflagged_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), unflag_in_resolve=("1.1.2",))
    engine.wired(monkeypatch)
    assert [r["tumbler"] for r in json.loads(_run("--json").stdout)] == ["1.1.10", "1.1.3", "1.2.1", "1.4.1"]
    assert [r["tumbler"] for r in json.loads(_run("--type", "knowledge", "--json").stdout)] == ["1.1.10", "1.4.1"]


def test_a_failed_version_probe_says_so_and_does_not_say_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), flag=None, probe_failed=True)
    engine.wired(monkeypatch)
    result = _run()
    assert result.exit_code != 0
    assert "capabilities could not be read" in result.stderr
    assert "0.1.188" not in result.stderr
    assert engine.calls == []


@pytest.mark.parametrize("status", [500, 403])
def test_a_5xx_or_other_status_reads_as_could_not_be_reached_or_failed(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    engine = FakeEngine(_fixture_rows(), sensitive_status=status)
    engine.wired(monkeypatch)
    result = _run()
    assert result.exit_code != 0
    assert "could not be reached or failed" in result.stderr
    assert "could not be read" not in result.stderr and "--owner" not in result.stderr


def test_a_malformed_answer_exits_without_walking(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine(_fixture_rows(), sensitive_body={"count": "many"})
    engine.wired(monkeypatch)
    result = _run()
    assert result.exit_code != 0
    assert "answer could not be read" in result.stderr
    assert "reached or failed" not in result.stderr
    assert [c for c in engine.calls if c[1] != "/sensitive"] == []


def test_the_nothing_flagged_answer_lists_nothing_and_resolves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = FakeEngine([_row("1.1.1", "plain")])
    engine.wired(monkeypatch)
    result = _run("--json")
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout) == []
    assert engine.calls == [("GET", "/sensitive", {"limit": 50})]
