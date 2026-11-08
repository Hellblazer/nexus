# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-240 Technical Design 1 and 2 on RDR-242's run path (nexus-mdbds.9, the half with no worker).

``header.parse`` is the only reader of a procedure's four header lines; ``prepare_args``
checks the caller's arguments against them and fills the reserved ``now`` and ``repo``.
Both refuse with ``ScriptError`` of kind ``header`` or ``args``. The run_script wiring
(the refusals arrive before any worker starts) is tested with the worker of nexus-mdbds.5.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

import nexus.bounded_subprocess as bs
from nexus.lua_runtime import header as hd
from nexus.lua_runtime.errors import ScriptError

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is a hard dependency of nx")

GOOD = (
    b"-- procedure: research_add\n"
    b"-- summary: Build an RDR research record; the caller writes it with memory_put.\n"
    b"-- args: rdr:integer, classification:string, method:string, finding:string, repo:string\n"
    b"-- grants: memory.list, tables.get\n"
    b"return 1\n"
)


def _refusal(src: bytes, name: str = "research_add") -> tuple[str, str]:
    with pytest.raises(ScriptError) as e:
        hd.parse(src, name=name)
    return e.value.kind, str(e.value)


def _lines(*lines: str, body: str = "return 1\n") -> bytes:
    return ("\n".join(lines) + "\n" + body).encode("latin-1")


P = "-- procedure: research_add"
S = "-- summary: One line."
A = "-- args: rdr:integer"
G = "-- grants: memory.list"


# ---- a valid header ----

def test_a_valid_header_parses_to_its_fields() -> None:
    h = hd.parse(GOOD, name="research_add")
    assert h.procedure == "research_add"
    assert h.summary == "Build an RDR research record; the caller writes it with memory_put."
    assert h.args == (
        hd.ArgSpec("rdr", "integer", False),
        hd.ArgSpec("classification", "string", False),
        hd.ArgSpec("method", "string", False),
        hd.ArgSpec("finding", "string", False),
        hd.ArgSpec("repo", "string", False),
    )
    assert h.grants == frozenset({"memory.list", "tables.get"})


def test_none_optional_and_crlf_line_ends_parse() -> None:
    src = b"-- procedure: p\r\n-- summary: s\r\n-- args: none\r\n-- grants: none\r\nreturn 1\r\n"
    h = hd.parse(src, name="p")
    assert (h.args, h.grants) == ((), frozenset())
    h = hd.parse(_lines("-- procedure: p", S, "-- args: since:string?, n:number?, b:boolean", G), name="p")
    assert h.args == (hd.ArgSpec("since", "string", True), hd.ArgSpec("n", "number", True),
                      hd.ArgSpec("b", "boolean", False))


def test_a_header_with_no_body_parses() -> None:
    assert hd.parse(_lines(P, S, A, G, body=""), name="research_add").procedure == "research_add"
    assert hd.parse((P + "\n" + S + "\n" + A + "\n" + G).encode(), name="research_add").grants == {"memory.list"}


def test_a_tab_in_the_summary_is_allowed() -> None:
    assert hd.parse(_lines(P, "-- summary: a\tb", A, G), name="research_add").summary == "a\tb"


def test_the_summary_may_be_exactly_100_characters() -> None:
    assert len(hd.parse(_lines(P, "-- summary: " + "x" * 100, A, G), name="research_add").summary) == 100


# ---- one refusal per rule of Technical Design 1, each kind header naming its line ----

@pytest.mark.parametrize("src,line,why", [
    pytest.param(_lines(P, S, A, body=""), 4, 'missing', id="missing-grants-line"),
    pytest.param(b"", 1, 'missing', id="empty-file"),
    pytest.param(_lines(P, S, body=""), 3, 'missing', id="missing-args-and-grants"),
    pytest.param(_lines(S, P, A, G), 1, 'out of order', id="key-out-of-order"),
    pytest.param(_lines(P, S, G, A), 3, 'out of order', id="grants-before-args"),
    pytest.param(_lines(P, S, "-- args: rdr:int", G), 3, 'unknown type', id="unknown-type"),
    pytest.param(_lines(P, S, "-- args: rdr:integer, rdr:string", G), 3, 'named twice', id="repeated-argument"),
    pytest.param(_lines("-- procedure: other_name", S, A, G), 1, 'is not the file name', id="procedure-not-the-file-name"),
    pytest.param(_lines(P, "-- summary: " + "x" * 101, A, G), 2, 'over 100', id="summary-over-100"),
    pytest.param(_lines(P, "-- summary: caf\xe9", A, G), 2, 'not printable ASCII', id="non-ascii-byte"),
    pytest.param(_lines(P, S, "-- args: rdr:integer\x07", G), 3, 'not printable ASCII', id="control-byte"),
    pytest.param(_lines(P, S, A, "-- grants: memory.list\x7f"), 4, 'not printable ASCII', id="delete-byte"),
    pytest.param(_lines(P, "-- summary: a\rb", A, G), 2, 'not printable ASCII', id="bare-cr-inside-a-line"),
    pytest.param(_lines("--procedure: research_add", S, A, G), 1, "must be '-- procedure: <value>'", id="no-space-after-dashes"),
    pytest.param(_lines("-- procedure:research_add", S, A, G), 1, "must be '-- procedure: <value>'", id="no-space-after-colon"),
    pytest.param(_lines("-- procedure: research_add ", S, A, G), 1, 'surrounding whitespace', id="trailing-space"),
    pytest.param(_lines("procedure: research_add", S, A, G), 1, "must be '-- procedure: <value>'", id="not-a-comment"),
    pytest.param(_lines(P, "-- summary: ", A, G), 2, 'value is empty', id="empty-summary"),
    pytest.param(_lines(P, S, "-- args: ", G), 3, 'value is empty', id="empty-args"),
    pytest.param(_lines(P, S, "-- args: Rdr:integer", G), 3, 'breaks [a-z]', id="argument-name-upper-case"),
    pytest.param(_lines(P, S, "-- args: 1rdr:integer", G), 3, 'breaks [a-z]', id="argument-name-digit-first"),
    pytest.param(_lines(P, S, "-- args: " + "a" * 33 + ":integer", G), 3, 'breaks [a-z]', id="argument-name-over-32"),
    pytest.param(_lines(P, S, "-- args: end:string", G), 3, 'Lua keyword', id="argument-name-lua-keyword"),
    pytest.param(_lines(P, S, "-- args: rdr", G), 3, "has no ':type'", id="argument-without-type"),
    pytest.param(_lines(P, S, "-- args: rdr:integer,, x:string", G), 3, 'an empty args item', id="empty-argument-item"),
    pytest.param(_lines(P, S, "-- args: none, rdr:integer", G), 3, "'none' must stand alone", id="none-with-others"),
    pytest.param(_lines(P, S, A, "-- grants: memory"), 4, 'not a verb name', id="grant-not-a-verb-name"),
    pytest.param(_lines(P, S, A, "-- grants: memory.list, memory.list"), 4, 'named twice', id="repeated-grant"),
    pytest.param(_lines(P, S, A, "-- grants: "), 4, 'value is empty', id="empty-grants"),
    pytest.param(_lines(P, S, A, "-- grants: Memory.list"), 4, 'not a verb name', id="grant-upper-case"),
])
def test_a_header_breaking_a_rule_is_refused_naming_its_line(src: bytes, line: int, why: str) -> None:
    kind, msg = _refusal(src)
    assert kind == "header"
    assert msg.startswith(f"header: line {line}: "), msg
    assert why in msg, msg


def test_the_procedure_name_follows_the_run_path_name_rule() -> None:
    kind, msg = _refusal(_lines("-- procedure: ../x", S, A, G), name="../x")
    assert (kind, msg.startswith("header: line 1: ")) == ("header", True)


def test_bytes_after_the_header_are_not_the_parsers_business() -> None:
    # The static check's bytes rule judges the body; the parser reads four lines.
    assert hd.parse(_lines(P, S, A, G, body="-- caf\xe9\nreturn 1\n"), name="research_add")


# ---- arguments (RDR-240 Technical Design 2) ----

FIXED = datetime(2026, 11, 8, 1, 2, 3, 456789, tzinfo=UTC)


def _hdr(args: str) -> hd.Header:
    return hd.parse(_lines("-- procedure: p", S, f"-- args: {args}", "-- grants: none"), name="p")


def _prep(args: str, given: dict, cwd: Path | None = None) -> dict:
    return hd.prepare_args(_hdr(args), given, cwd=cwd or Path.cwd(), clock=lambda: FIXED)


def _args_refusal(args: str, given: dict, cwd: Path | None = None) -> str:
    with pytest.raises(ScriptError) as e:
        _prep(args, given, cwd)
    assert e.value.kind == "args"
    return str(e.value)


def test_declared_arguments_come_back_in_header_order() -> None:
    assert list(_prep("b:string, a:integer", {"a": 1, "b": "x"})) == ["b", "a"]


def test_an_unknown_key_is_refused_naming_it() -> None:
    assert "nope" in _args_refusal("a:integer", {"a": 1, "nope": 2})


def test_a_missing_required_argument_is_refused_naming_it() -> None:
    assert "a" in _args_refusal("a:integer, b:string?", {})


def test_an_absent_optional_argument_is_omitted_and_null_is_absent() -> None:
    assert _prep("a:integer, b:string?", {"a": 1}) == {"a": 1}
    assert _prep("a:integer, b:string?", {"a": 1, "b": None}) == {"a": 1}


def test_null_for_a_required_argument_is_missing() -> None:
    assert "a" in _args_refusal("a:integer", {"a": None})


@pytest.mark.parametrize("value", [1.5, "1", True, False, [], {}, math.nan, math.inf, 2**63, -(2**63) - 1, 1e300])
def test_a_non_integer_for_an_integer_is_refused(value: object) -> None:
    assert "a" in _args_refusal("a:integer", {"a": value})


@pytest.mark.parametrize("value", [2**63 - 1, -(2**63), float(-(2**63)), 0])
def test_the_64_bit_integer_bounds_are_accepted(value: object) -> None:
    assert _prep("a:integer", {"a": value}) == {"a": int(value)}


@pytest.mark.parametrize("value", [2**63, -(2**63) - 1, 10**400])
def test_a_number_outside_what_lua_holds_is_refused(value: int) -> None:
    assert "a" in _args_refusal("a:number", {"a": value})


def test_negative_zero_keeps_its_sign_as_a_number() -> None:
    assert math.copysign(1.0, _prep("a:number", {"a": -0.0})["a"]) == -1.0


def test_a_string_that_is_not_utf8_is_refused() -> None:
    assert "a" in _args_refusal("a:string", {"a": "\ud800"})


def test_argument_names_must_be_strings() -> None:
    assert "names" in _args_refusal("a:integer", {1: 2, "a": 3})  # type: ignore[dict-item]


def test_unknown_names_are_refused_before_missing_ones_and_missing_before_types() -> None:
    # Whole passes, so header order cannot change which error the caller sees.
    assert "'nope'" in _args_refusal("a:integer, b:string", {"a": "x", "nope": 1})
    assert "'b' is required" in _args_refusal("a:integer, b:string", {"a": "x"})
    assert "'b' is required" in _args_refusal("b:string, a:integer", {"a": "x"})


def test_an_integer_may_arrive_as_a_whole_float() -> None:
    got = _prep("a:integer", {"a": 2.0})
    assert got == {"a": 2} and type(got["a"]) is int


@pytest.mark.parametrize("value,ok", [(1, True), (1.5, True), (-0.0, True), (True, False), ("1", False),
                                      (math.nan, False), (math.inf, False)])
def test_number_accepts_finite_json_numbers_only(value: object, ok: bool) -> None:
    if ok:
        assert _prep("a:number", {"a": value}) == {"a": value}
    else:
        _args_refusal("a:number", {"a": value})


@pytest.mark.parametrize("value,ok", [(True, True), (False, True), (0, False), ("true", False)])
def test_boolean_accepts_json_booleans_only(value: object, ok: bool) -> None:
    if ok:
        assert _prep("a:boolean", {"a": value}) == {"a": value}
    else:
        _args_refusal("a:boolean", {"a": value})


@pytest.mark.parametrize("value,ok", [("x", True), ("", True), (1, False), (None, False)])
def test_string_accepts_json_strings_only(value: object, ok: bool) -> None:
    if ok:
        assert _prep("a:string", {"a": value}) == {"a": value}
    else:
        _args_refusal("a:string", {"a": value})


def test_arguments_must_be_a_mapping_of_names() -> None:
    with pytest.raises(ScriptError) as e:
        hd.prepare_args(_hdr("a:integer"), [1], cwd=Path.cwd(), clock=lambda: FIXED)  # type: ignore[arg-type]
    assert e.value.kind == "args"
    with pytest.raises(ScriptError) as e:
        hd.prepare_args(_hdr("a:integer"), ["a"], cwd=Path.cwd(), clock=lambda: FIXED)  # type: ignore[arg-type]
    assert (e.value.kind, "object of name to value" in str(e.value)) == ("args", True)


# ---- the reserved arguments now and repo ----

@pytest.mark.parametrize("reserved", ["now", "repo"])
def test_a_caller_passing_a_reserved_name_is_refused_first(reserved: str) -> None:
    # Refused before anything else, even with another error present and even when declared.
    msg = _args_refusal(f"{reserved}:string, a:integer", {reserved: "x", "nope": 1})
    assert reserved in msg and "reserved" in msg
    msg = _args_refusal("a:integer", {reserved: "x", "a": 1})
    assert f"'{reserved}' is reserved" in msg


def test_now_is_the_host_utc_time_when_declared() -> None:
    assert _prep("now:string", {}) == {"now": "2026-11-08T01:02:03Z"}


def test_now_converts_an_aware_clock_to_utc_and_refuses_a_naive_one() -> None:
    east = FIXED.astimezone(timezone(timedelta(hours=5)))
    assert hd.prepare_args(_hdr("now:string"), {}, cwd=Path.cwd(), clock=lambda: east) == {"now": "2026-11-08T01:02:03Z"}
    with pytest.raises(ValueError):
        hd.prepare_args(_hdr("now:string"), {}, cwd=Path.cwd(), clock=lambda: datetime(2026, 1, 1))


@needs_git
def test_neither_reserved_value_is_added_when_not_declared(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path / "main")
    assert _prep("a:integer", {"a": 1}, cwd=repo) == {"a": 1}


def test_the_default_clock_is_the_real_utc_now() -> None:
    got = hd.prepare_args(_hdr("now:string"), {}, cwd=Path.cwd())["now"]
    t = datetime.fromisoformat(got.replace("Z", "+00:00"))
    assert abs((datetime.now(UTC) - t).total_seconds()) < 60 and got.endswith("Z")


def _git(cwd: Path, *argv: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *argv], cwd=cwd, env=env, check=True, capture_output=True, timeout=60)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "f").write_text("x", encoding="utf-8")
    _git(path, "add", "f")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A basetemp inside some checkout (a lab-remote copy) must not make tmp_path a repo.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return tmp_path


@needs_git
def test_repo_is_the_checkout_name_when_declared(ceiling: Path) -> None:
    repo = _git_repo(ceiling / "myrepo")
    assert _prep("repo:string", {}, cwd=repo) == {"repo": "myrepo"}
    (repo / "sub").mkdir()
    assert _prep("repo:string", {}, cwd=repo / "sub") == {"repo": "myrepo"}


@needs_git
def test_repo_from_a_linked_worktree_is_the_main_checkouts_name(ceiling: Path) -> None:
    main = _git_repo(ceiling / "mainrepo")
    _git(main, "worktree", "add", "-q", str(ceiling / "wt-agent-1"), "-b", "side")
    assert hd.repo_name(ceiling / "wt-agent-1") == "mainrepo"
    assert _prep("repo:string", {}, cwd=ceiling / "wt-agent-1") == {"repo": "mainrepo"}


@needs_git
def test_outside_a_repo_repo_is_absent_never_the_directory_name(ceiling: Path) -> None:
    plain = ceiling / "Downloads"
    plain.mkdir()
    assert hd.repo_name(plain) is None
    assert _prep("repo:string?", {}, cwd=plain) == {}
    msg = _args_refusal("repo:string", {}, cwd=plain)
    assert "repo" in msg and "reserved" in msg


def test_a_reserved_name_with_a_non_string_type_is_refused_in_the_header() -> None:
    kind, msg = _refusal(_lines("-- procedure: p", S, "-- args: now:integer", "-- grants: none"), name="p")
    assert (kind, msg.startswith("header: line 3: ")) == ("header", True)


@needs_git
def test_a_callers_git_dir_does_not_change_the_repo(ceiling: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _git_repo(ceiling / "mine")
    other = _git_repo(ceiling / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    probe = hd.probe_repo(mine)
    assert probe is not None and probe.name == "mine" and probe.root.resolve() == mine.resolve()


@needs_git
def test_inside_a_git_directory_or_a_bare_repo_there_is_no_repo(ceiling: Path) -> None:
    repo = _git_repo(ceiling / "r")
    assert hd.probe_repo(repo / ".git") is None
    bare = ceiling / "bare.git"
    bare.mkdir()
    _git(bare, "init", "-q", "--bare")
    assert hd.probe_repo(bare) is None


def test_a_probe_git_cannot_answer_is_refused_even_for_an_optional_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*a: object, **k: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(bs, "run_bounded", no_git)
    with pytest.raises(hd.RepoProbeError):
        hd.probe_repo(Path.cwd())
    for decl in ("repo:string", "repo:string?"):
        assert "could not be determined" in _args_refusal(decl, {})


def test_a_git_refusal_other_than_not_a_repo_is_a_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def dubious(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(a, 128, "", "fatal: detected dubious ownership in repository")

    monkeypatch.setattr(bs, "run_bounded", dubious)
    with pytest.raises(hd.RepoProbeError):
        hd.probe_repo(Path.cwd())


def test_run_script_can_probe_once_and_reuse_it() -> None:
    calls: list[Path] = []
    probe = hd.RepoProbe(Path("/x/top"), "top")

    def once(cwd: Path) -> hd.RepoProbe | None:
        calls.append(cwd)
        return probe

    got = hd.prepare_args(_hdr("repo:string"), {}, cwd=Path("/x/top/sub"), clock=lambda: FIXED, repo_probe=once)
    assert got == {"repo": "top"} and calls == [Path("/x/top/sub")]


def test_the_probe_reads_git_untranslated() -> None:
    env = hd._git_env()
    assert env["LC_ALL"] == "C" and env["LANGUAGE"] == "C"


def test_a_localized_git_refusal_would_read_as_a_probe_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # Why the C locale matters: only git's English words mean "no work tree".
    def german(*a: object, **k: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(a, 128, "", "fatal: kein Git-Repository")

    monkeypatch.setattr(bs, "run_bounded", german)
    with pytest.raises(hd.RepoProbeError):
        hd.probe_repo(Path.cwd())


@needs_git
def test_a_repo_whose_path_holds_a_line_separator_is_found(ceiling: Path) -> None:
    repo = _git_repo(ceiling / "a\u2028b")
    probe = hd.probe_repo(repo)
    assert probe is not None and probe.name == "a\u2028b"


def test_an_unknown_name_that_is_not_utf8_still_gives_a_utf8_error() -> None:
    msg = _args_refusal("a:integer", {"a": 1, "\ud800": 2})
    msg.encode("utf-8")
