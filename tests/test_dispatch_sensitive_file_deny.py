# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (critic C1): a dispatched ``claude -p`` child that keeps Read, Grep or Glob is
told not to read the source files of the tenant's sensitive documents.

Two layers. ``nexus.operators.sensitive_paths`` turns the catalog's sensitive documents into Claude Code deny
rules (tested against a fake catalog reader). ``CliBackend.run`` puts the rules on the child's argv as
``--disallowedTools`` and, when the catalog cannot be read, removes the file tools from the child altogether
(tested through ``run_case``, the golden harness's fake-subprocess runner)."""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from nexus.catalog.engine_reads.route_unavailable import PHASE2_ROUTES_ENGINE, EngineRouteUnavailable
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.operators import sensitive_paths as sp
from nexus.operators.sensitive_paths import FileToolRestriction
from tests.test_llm_backend_cli_identity import run_case


@dataclass
class _Doc:
    tumbler: str = "1.1.1"
    sensitive: bool = True
    source_uri: str = ""
    file_path: str = ""
    alias_of: str = ""


class _Reader:
    """A catalog read handle that answers only the sensitive-locator route (RDR-241 Phase 2, nexus-uwggq.2.13).

    *docs* is the whole catalog; like the engine, the route returns the live sensitive documents and the live
    documents whose ``alias_of`` names one, each as a dict that carries the row's own ``sensitive`` flag.
    ``all_documents`` is the walk this change removed: calling it is counted, and the tests assert the count."""

    def __init__(self, docs=None, error: Exception | None = None, capped: bool = False) -> None:
        self.docs = docs or []
        self.error = error
        self.capped = capped
        self.locator_calls: list[int] = []
        self.walks = 0

    def all_documents(self, limit: int = 0, **kw):
        self.walks += 1
        raise AssertionError("the whole-catalog walk is retired: file_tool_restriction reads the locator route")

    def sensitive_locators(self, limit: int = 10_000) -> dict:
        self.locator_calls.append(limit)
        if self.error is not None:
            raise self.error
        flagged = {d.tumbler for d in self.docs if d.sensitive is True}
        rows = [_row(d) for d in self.docs if d.sensitive is True or d.alias_of in flagged]
        return {"count": len(self.docs) if self.capped else len(rows), "limit": limit, "capped": self.capped,
                "documents": rows}


def _row(d: "_Doc") -> dict:
    return {"tumbler": d.tumbler, "source_uri": d.source_uri or None, "file_path": d.file_path or None,
            "alias_of": d.alias_of or None, "physical_collection": "docs__a__bge-768__v1", "sensitive": d.sensitive}


def _file(path: str, n: int = 1, **kw) -> _Doc:
    return _Doc(tumbler=f"1.1.{n}", source_uri="file://" + path, **kw)


# ---- the rule builder -------------------------------------------------------------------------------------


def test_a_sensitive_file_becomes_one_absolute_read_rule() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/home/a/notes/secret.md")]))
    assert r.deny_rules == ("Read(//home/a/notes/secret.md)",)
    assert not r.drop_file_tools


def test_plain_documents_and_other_schemes_are_not_denied() -> None:
    docs = [
        _file("/home/a/ok.md", 1, sensitive=False),
        _Doc(tumbler="1.1.2", source_uri="https://example.com/x"),
        _Doc(tumbler="1.1.3", source_uri="x-devonthink-item://ABCD"),
        _Doc(tumbler="1.1.4", source_uri="", file_path=""),
    ]
    r = sp.file_tool_restriction(reader=_Reader(docs))
    assert r.deny_rules == () and not r.drop_file_tools
    # the route answers the three sensitive rows, not the whole catalog: examined counts locator rows
    assert r.examined == 3 and r.sensitive == 3, "the answer was examined, not skipped"


def test_a_bare_path_without_a_uri_is_denied_by_its_own_spelling() -> None:
    docs = [
        _Doc(tumbler="1.1.1", file_path="/abs/one.md"),
        _Doc(tumbler="1.1.2", file_path="docs/two.md"),
    ]
    r = sp.file_tool_restriction(reader=_Reader(docs))
    assert set(r.deny_rules) == {"Read(//abs/one.md)", "Read(./docs/two.md)"}


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(_file("/C:/Users/a/notes/s.md"), id="file-uri-drive-letter"),
        pytest.param(_file("/c:/Users/a/notes/s.md"), id="file-uri-lowercase-drive"),
        pytest.param(_Doc(file_path="C:\\Users\\a\\notes\\s.md"), id="bare-backslash-path"),
        pytest.param(_Doc(file_path="C:/Users/a/notes/s.md"), id="bare-forward-slash-path"),
        # the raw spelling every registration path mints on Windows: "file://" + the backslash path
        pytest.param(_Doc(source_uri="file://C:\\Users\\a\\notes\\s.md"), id="raw-registration-spelling"),
        pytest.param(_Doc(source_uri="file://c:/Users/a/notes/s.md"), id="two-slash-forward-slash"),
        pytest.param(_Doc(source_uri="FILE:///C:/Users/a/notes/s.md"), id="upper-case-scheme"),
    ],
)
def test_a_windows_drive_letter_path_is_written_in_the_documented_posix_form(doc) -> None:
    """Claude Code normalises ``C:\\Users\\alice`` to ``/c/Users/alice`` before matching and documents
    ``Read(//c/...)`` for it (code.claude.com/docs/en/permissions, "Where a pattern anchors"). ``Read(//C:/...)``
    or ``Read(./C:/...)`` matches nothing, so the dispatch would have shipped a rule that denies nothing."""
    r = sp.file_tool_restriction(reader=_Reader([doc]))
    assert r.deny_rules == ("Read(//c/Users/a/notes/s.md)",)
    assert not r.drop_file_tools


def test_a_windows_path_with_a_comma_is_denied_by_its_safe_directory_in_the_same_form() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/C:/papers/Smith, 2020.pdf")]))
    assert r.deny_rules == ("Read(//c/papers/**)",)


def test_a_windows_path_with_only_the_drive_above_it_is_denied_by_the_drive_like_any_top_level_directory() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/C:/a,b.pdf")]))
    assert r.deny_rules == ("Read(//c/**)",) and not r.drop_file_tools


def test_gitignore_metacharacters_in_a_path_are_escaped() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/h/a [x]*?.md")]))
    assert r.deny_rules == ("Read(//h/a \\[x\\]\\*\\?.md)",)


def test_a_path_with_a_comma_is_denied_by_its_nearest_safe_directory() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/h/papers/Smith, 2020.pdf")]))
    assert r.deny_rules == ("Read(//h/papers/**)",)


def _read_rule(path) -> str:
    """The deny rule for the absolute local path *path* in this platform's documented form: ``Read(//abs/path)``
    on POSIX, ``Read(//c/Users/...)`` (lowercase drive, forward slashes) for a ``C:`` path on Windows."""
    text = str(path)
    if os.name == "nt":
        drive, rest = os.path.splitdrive(text)
        return f"Read(//{drive[0].lower()}{rest.replace(chr(92), '/')})"
    return f"Read(/{text})"


def test_a_symlinked_path_is_denied_under_both_spellings(tmp_path) -> None:
    real = tmp_path / "real.md"
    real.write_text("x")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(real)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")
    # "file://" + the OS path is the raw spelling registration mints (on Windows ``file://C:\\...``)
    r = sp.file_tool_restriction(reader=_Reader([_file(str(link))]))
    assert not r.drop_file_tools
    assert _read_rule(link) in r.deny_rules and _read_rule(real.resolve()) in r.deny_rules
    assert len(r.deny_rules) == 2, "the link and the real path, no more"


@pytest.mark.parametrize(
    "doc",
    [
        pytest.param(_Doc(source_uri="file://\\\\server\\share\\s.md"), id="unc-share-in-a-file-uri"),
        pytest.param(_Doc(source_uri="file:////server/share/s.md"), id="empty-authority-unc"),
        pytest.param(_Doc(source_uri="file://host/share/s.md"), id="authority-host"),
        pytest.param(_Doc(source_uri="file://relative/s.md"), id="not-an-absolute-path"),
        pytest.param(_Doc(source_uri="file:/C:/s.md"), id="single-slash-file-uri"),
        pytest.param(_Doc(file_path="\\\\server\\share\\s.md"), id="bare-unc-path"),
        pytest.param(_Doc(file_path="//server/share/s.md"), id="bare-slash-unc-path"),
    ],
)
def test_a_sensitive_location_that_cannot_be_read_as_a_path_fails_closed(doc) -> None:
    """A sensitive document whose location is a file location this module cannot write as a rule must take the
    file tools away. It must never come back as ``deny_rules=()`` with ``drop_file_tools=False``: that is a
    sensitive document protected by nothing (the Windows raw ``file://C:\\...`` spelling did exactly that)."""
    r = sp.file_tool_restriction(reader=_Reader([doc, _file("/h/also-sensitive.md", 2)]))
    assert r.drop_file_tools and r.deny_rules == ()
    assert "cannot be written as a deny rule" in r.reason
    assert r.examined == 2 and r.sensitive == 2


def test_an_unrecognisable_location_on_a_clean_document_is_not_a_reason_to_drop_the_tools() -> None:
    """Non-vacuity of the case above: the same location on a document that is not sensitive changes nothing."""
    r = sp.file_tool_restriction(reader=_Reader([_Doc(source_uri="file://host/share/s.md", sensitive=False)]))
    # the route never lists a clean document, so there is nothing to examine
    assert not r.drop_file_tools and r.deny_rules == () and r.examined == 0 and r.sensitive == 0


def test_a_merged_duplicate_of_a_flagged_document_is_denied_too() -> None:
    docs = [
        _file("/h/canon.md", 1),
        _file("/h/copy.md", 2, sensitive=False, alias_of="1.1.1"),
        _file("/h/other.md", 3, sensitive=False, alias_of="1.1.9"),
    ]
    r = sp.file_tool_restriction(reader=_Reader(docs))
    assert set(r.deny_rules) == {"Read(//h/canon.md)", "Read(//h/copy.md)"}


def test_the_bound_is_per_file_then_per_directory_then_fail_closed() -> None:
    n = sp.MAX_DENY_RULES
    # at the bound: per-file rules
    at = [_file(f"/h/d{i}/f.md", i) for i in range(n)]
    r = sp.file_tool_restriction(reader=_Reader(at))
    assert len(r.deny_rules) == n and r.deny_rules[0].endswith("f.md)")
    # over the bound but few directories: one rule per directory
    over = [_file(f"/h/d{i % 5}/f{i}.md", i) for i in range(n + 1)]
    r = sp.file_tool_restriction(reader=_Reader(over))
    assert set(r.deny_rules) == {f"Read(//h/d{i}/**)" for i in range(5)} and not r.drop_file_tools
    # over the bound in directories as well: the file tools are taken away
    wide = [_file(f"/h/d{i}/f.md", i) for i in range(n + 1)]
    r = sp.file_tool_restriction(reader=_Reader(wide))
    assert r.drop_file_tools and r.deny_rules == () and "rules" in r.reason


def test_an_unreadable_catalog_takes_the_file_tools_away() -> None:
    r = sp.file_tool_restriction(reader=_Reader(error=RuntimeError("503 from the engine")))
    assert r.drop_file_tools and r.deny_rules == ()
    assert "503" in r.reason


def test_a_failed_reader_construction_takes_the_file_tools_away(monkeypatch) -> None:
    def boom():
        raise RuntimeError("no storage backend")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", boom)
    # The suite stands in "no sensitive documents" at the _default_reader seam (tests/conftest.py); this test is
    # about the factory, so route the seam back to it.
    from nexus.catalog import factory  # noqa: PLC0415 — test-local

    monkeypatch.setattr(sp, "_default_reader", lambda: factory.make_catalog_reader())
    r = sp.file_tool_restriction()
    assert r.drop_file_tools


def test_a_root_level_unsafe_path_fails_closed() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/a,b.md")]))
    assert r.drop_file_tools


# ---- the locator route (RDR-241 Phase 2, nexus-uwggq.2.13) ------------------------------------------------


def _fixture_catalog() -> list[_Doc]:
    """A catalog with a flagged canonical, its merged duplicate (an alias row, flag false), a plain document, an
    alias of something unflagged, and a symlink-free spread of schemes."""
    return [
        _file("/h/canon.md", 1),
        _file("/h/copy.md", 2, sensitive=False, alias_of="1.1.1"),
        _file("/h/plain.md", 3, sensitive=False),
        _file("/h/other-copy.md", 4, sensitive=False, alias_of="1.1.3"),
        _Doc(tumbler="1.1.5", file_path="docs/rel.md"),
        _Doc(tumbler="1.1.6", source_uri="https://example.com/x"),
    ]


def test_dispatch_reads_the_locator_route_once_with_the_route_bound_and_never_walks_the_catalog() -> None:
    reader = _Reader(_fixture_catalog())
    r = sp.file_tool_restriction(reader=reader)
    assert reader.locator_calls == [10_000] and reader.walks == 0
    assert not r.drop_file_tools and r.deny_rules


def test_a_sensitive_document_and_an_alias_row_both_become_deny_rules() -> None:
    reader = _Reader([_file("/h/canon.md", 1), _file("/h/copy.md", 2, sensitive=False, alias_of="1.1.1")])
    rows = reader.sensitive_locators()["documents"]
    assert [(x["tumbler"], x["sensitive"], x["alias_of"]) for x in rows] == [("1.1.1", True, None),
                                                                               ("1.1.2", False, "1.1.1")]
    r = sp.file_tool_restriction(reader=reader)
    assert set(r.deny_rules) == {"Read(//h/canon.md)", "Read(//h/copy.md)"}
    assert r.examined == 2 and r.sensitive == 2


def test_the_route_rows_give_the_deny_rules_the_old_walk_gave_on_the_same_fixture() -> None:
    docs = _fixture_catalog()
    walked = sp._restrict(docs)  # the retired path: _restrict fed the full catalog entries
    assert walked.deny_rules, "non-vacuity: the fixture has something to deny"
    routed = sp.file_tool_restriction(reader=_Reader(docs))
    assert routed.deny_rules == walked.deny_rules
    assert routed.drop_file_tools == walked.drop_file_tools and routed.sensitive == walked.sensitive
    assert routed.examined < walked.examined, "the route answers only the rows that matter"


def test_a_capped_answer_takes_the_file_tools_away_and_says_so() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/h/a.md")], capped=True))
    assert r.drop_file_tools and r.deny_rules == ()
    assert "more than 10,000" in r.reason


@pytest.mark.parametrize("reason", sorted([
    EngineRouteUnavailable.UNSUPPORTED, EngineRouteUnavailable.PROBE_FAILED, EngineRouteUnavailable.TIMEOUT,
    EngineRouteUnavailable.FAILED, EngineRouteUnavailable.MALFORMED]))
def test_every_route_failure_takes_the_file_tools_away_and_the_reason_names_the_route(reason: str) -> None:
    exc = EngineRouteUnavailable("/sensitive/locators", "catalog_sensitive_locators_supported", reason)
    r = sp.file_tool_restriction(reader=_Reader(error=exc))
    assert r.drop_file_tools and r.deny_rules == ()
    assert "/v1/catalog/sensitive/locators" in r.reason
    if reason == EngineRouteUnavailable.UNSUPPORTED:
        assert PHASE2_ROUTES_ENGINE in r.reason, "an engine without the route is named by its release"


def test_a_reader_without_the_route_takes_the_file_tools_away() -> None:
    class _Old:
        def all_documents(self, limit: int = 0):
            raise AssertionError("must not walk")

    r = sp.file_tool_restriction(reader=_Old())
    assert r.drop_file_tools and r.deny_rules == () and "sensitive_locators" in r.reason


def test_a_malformed_row_is_not_read_as_no_sensitive_documents() -> None:
    class _Bad:
        def sensitive_locators(self, limit: int = 10_000):
            return {"count": 1, "limit": limit, "capped": False, "documents": ["not a row"]}

    r = sp.file_tool_restriction(reader=_Bad())
    assert r.drop_file_tools and r.deny_rules == ()


@pytest.mark.parametrize("flag", ["missing", None, "true", 1])
def test_a_row_without_a_boolean_sensitive_flag_drops_the_file_tools(flag) -> None:
    """A dict row whose ``sensitive`` is absent or not a bool would read as "not sensitive" and fail open; the guard
    in file_tool_restriction drops the tools instead (review r1 of 4978f3985)."""
    row = {"tumbler": "1.1.1", "source_uri": "file:///srv/secret/a.md", "file_path": "", "alias_of": "",
           "physical_collection": "docs__a__bge-768__v1"}
    if flag != "missing":
        row["sensitive"] = flag

    class _NoFlag:
        def sensitive_locators(self, limit: int = 10_000):
            return {"count": 1, "limit": limit, "capped": False, "documents": [row]}

    r = sp.file_tool_restriction(reader=_NoFlag())
    assert r.drop_file_tools and r.deny_rules == ()


_FLAG = "catalog_sensitive_locators_supported"


def _engine(monkeypatch: pytest.MonkeyPatch, *, flags: dict, body=None) -> tuple[HttpCatalogClient, list[str]]:
    """An HttpCatalogClient over a stubbed transport that records every path it is asked for."""
    c = object.__new__(HttpCatalogClient)
    c._version_probe_failed = False
    paths: list[str] = []
    monkeypatch.setattr(c, "_version_flags", lambda: flags, raising=False)

    def get(path, **kw):
        paths.append(path)
        return body

    monkeypatch.setattr(c, "_get", get, raising=False)
    return c, paths


def test_three_engines_with_the_route_without_the_flag_and_answering_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = _fixture_catalog()
    rows = [_row(d) for d in docs if d.sensitive is True or d.alias_of == "1.1.1"]
    expected = sp._restrict(docs)

    with_route, paths = _engine(monkeypatch, flags={_FLAG: True},
                                body={"count": len(rows), "limit": 10_000, "capped": False, "documents": rows})
    got = sp.file_tool_restriction(reader=with_route)
    assert got.deny_rules == expected.deny_rules and not got.drop_file_tools
    assert paths == ["/sensitive/locators"], "one locator read, no GET /list"

    without_flag, paths = _engine(monkeypatch, flags={})
    got = sp.file_tool_restriction(reader=without_flag)
    assert got.drop_file_tools and got.deny_rules == () and PHASE2_ROUTES_ENGINE in got.reason
    assert paths == [], "an engine without the flag is not asked, and nothing is listed"

    capped, paths = _engine(monkeypatch, flags={_FLAG: True},
                            body={"count": 20_000, "limit": 10_000, "capped": True, "documents": rows})
    got = sp.file_tool_restriction(reader=capped)
    assert got.drop_file_tools and got.deny_rules == () and "more than 10,000" in got.reason
    assert paths == ["/sensitive/locators"]


# ---- the argv ---------------------------------------------------------------------------------------------

_GRANT = {
    "allowed_tools": ["Read", "Grep", "Glob", "mcp__nexus", "mcp__nexus_catalog"],
    "mcp_servers": {"nexus": {"command": "nx-mcp", "args": []}},
    "operator": "nx_plan_audit",
}


def _tail(argv: list[str], flag: str) -> list[str]:
    i = argv.index(flag)
    out = []
    for a in argv[i + 1:]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


def test_deny_rules_ride_the_child_argv_one_argument_each() -> None:
    rules = ("Read(//h/a.md)", "Read(//h/with space/b.md)")
    out = run_case(dict(_GRANT), {}, restriction=FileToolRestriction(deny_rules=rules))
    argv = out["argv"]
    assert _tail(argv, "--disallowedTools") == list(rules)
    assert _tail(argv, "--allowedTools") == ["Read,Grep,Glob,mcp__nexus,mcp__nexus_catalog"], \
        "the allow list is unchanged: a deny rule wins over it"


def test_no_sensitive_documents_leaves_the_argv_without_a_deny_flag() -> None:
    out = run_case(dict(_GRANT), {})
    assert "--disallowedTools" not in out["argv"]


def test_an_unreadable_catalog_removes_read_grep_and_glob_from_the_child() -> None:
    out = run_case(
        dict(_GRANT), {},
        restriction=FileToolRestriction(drop_file_tools=True, reason="catalog unreachable"),
    )
    argv = out["argv"]
    assert _tail(argv, "--allowedTools") == ["mcp__nexus,mcp__nexus_catalog"]
    # an allow list never removes a built-in (the child's Read inside its cwd needs no approval), so the
    # tools are removed by name
    assert set(_tail(argv, "--disallowedTools")) == {"Read", "Grep", "Glob"}


def test_a_dispatch_that_grants_no_file_tool_never_reads_the_catalog() -> None:
    calls: list[int] = []
    for kwargs in ({}, {"allowed_tools": ["mcp__nx"], "mcp_servers": {"nx": {"command": "nx-mcp", "args": []}}}):
        out = run_case(kwargs, {}, restriction_calls=calls)
        assert "--disallowedTools" not in out["argv"]
    assert calls == [], "a tool-free or MCP-only dispatch must not pay for a whole-catalog listing"
    run_case(dict(_GRANT), {}, restriction_calls=calls)
    assert calls == [1], "non-vacuity: a file-tool grant does read it, once"


@pytest.mark.parametrize("granted", [["Read"], ["Grep"], ["Glob"], ["Read(//x/**)"]])
def test_any_single_file_tool_triggers_the_restriction(granted) -> None:
    out = run_case(
        {"allowed_tools": granted + ["mcp__nx"], "mcp_servers": {"nx": {"command": "nx-mcp", "args": []}}},
        {}, restriction=FileToolRestriction(deny_rules=("Read(//h/a.md)",)),
    )
    assert _tail(out["argv"], "--disallowedTools") == ["Read(//h/a.md)"]
