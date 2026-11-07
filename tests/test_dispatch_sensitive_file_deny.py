# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 3 fix round r1 (critic C1): a dispatched ``claude -p`` child that keeps Read, Grep or Glob is
told not to read the source files of the tenant's sensitive documents.

Two layers. ``nexus.operators.sensitive_paths`` turns the catalog's sensitive documents into Claude Code deny
rules (tested against a fake catalog reader). ``CliBackend.run`` puts the rules on the child's argv as
``--disallowedTools`` and, when the catalog cannot be read, removes the file tools from the child altogether
(tested through ``run_case``, the golden harness's fake-subprocess runner)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

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
    def __init__(self, docs=None, error: Exception | None = None) -> None:
        self.docs = docs or []
        self.error = error
        self.calls = 0

    def all_documents(self, limit: int = 0, **kw):
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert limit == 0, "the listing must be unbounded: a capped one misses flagged documents"
        return list(self.docs)


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
    assert r.examined == 4 and r.sensitive == 3, "the listing was examined, not skipped"


def test_a_bare_path_without_a_uri_is_denied_by_its_own_spelling() -> None:
    docs = [
        _Doc(tumbler="1.1.1", file_path="/abs/one.md"),
        _Doc(tumbler="1.1.2", file_path="docs/two.md"),
    ]
    r = sp.file_tool_restriction(reader=_Reader(docs))
    assert set(r.deny_rules) == {"Read(//abs/one.md)", "Read(./docs/two.md)"}


def test_gitignore_metacharacters_in_a_path_are_escaped() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/h/a [x]*?.md")]))
    assert r.deny_rules == ("Read(//h/a \\[x\\]\\*\\?.md)",)


def test_a_path_with_a_comma_is_denied_by_its_nearest_safe_directory() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/h/papers/Smith, 2020.pdf")]))
    assert r.deny_rules == ("Read(//h/papers/**)",)


def test_a_symlinked_path_is_denied_under_both_spellings(tmp_path) -> None:
    real = tmp_path / "real.md"
    real.write_text("x")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    r = sp.file_tool_restriction(reader=_Reader([_file(str(link))]))
    assert f"Read(/{link})" in r.deny_rules and f"Read(/{real.resolve()})" in r.deny_rules


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
    r = sp.file_tool_restriction()
    assert r.drop_file_tools


def test_a_root_level_unsafe_path_fails_closed() -> None:
    r = sp.file_tool_restriction(reader=_Reader([_file("/a,b.md")]))
    assert r.drop_file_tools


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
