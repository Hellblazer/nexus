# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-242 Technical Design 4: run_script end to end, through the real worker (nexus-mdbds.9).

Every case runs ``nexus.lua_runtime.run_script`` with no test double: the library lookup, the
header, the argument check, then the supervisor and a ``python -I`` worker running the runtime.
The procedures used need no store, so no test mints a tenant: the shipped ``gate_record`` and
small procedures written into a temporary repo's ``.nexus/procedures/``.
"""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

import pytest

import nexus.lua_runtime as lua_runtime
from nexus.lua_runtime import library
from nexus.lua_runtime.errors import ScriptError, ScriptLimitError
from nexus.lua_runtime.run import _run_script_with_stats

GATE_ARGS = {"kind": "review", "bead": "nexus-qo7ie.31", "sha": "abc1234", "outcome": "partial", "body": "b"}


def _git(cwd: Path, *argv: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_AUTHOR_NAME="t",
               GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *argv], cwd=cwd, env=env, check=True, capture_output=True, timeout=60)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    r = tmp_path / "myrepo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "commit", "-q", "--allow-empty", "-m", "init")
    monkeypatch.chdir(r)
    return r


def _procedure(repo: Path, name: str, args: str, grants: str, body: str) -> None:
    d = repo / ".nexus" / "procedures"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.lua").write_text(
        f"-- procedure: {name}\n-- summary: a test procedure.\n-- args: {args}\n-- grants: {grants}\n{body}\n",
        encoding="ascii")


def _refusal(name: str, args: dict | None = None, limits: dict | None = None) -> ScriptError:
    with pytest.raises(ScriptError) as e:
        lua_runtime.run_script(name, args, limits)
    return e.value


# ---- the shipped root ----

def test_a_shipped_procedure_runs_through_the_real_worker(repo: Path) -> None:
    out, stats = _run_script_with_stats("gate_record", GATE_ARGS)
    assert out["project"] == "myrepo" and out["tags"] == "kind:gate,bead:nexus-qo7ie.31"
    assert out["title"].startswith("review-abc1234-nexus-qo7ie.31-")
    # The hook counts every 10,000 instructions, so a small run reports 0; peak memory is real.
    assert stats["instructions"] >= 0 and stats["peak_lua_bytes"] > 0


def test_repo_from_a_linked_worktree_is_the_main_checkouts_name(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wt = repo.parent / "wt-agent-1"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "side")
    monkeypatch.chdir(wt)
    assert lua_runtime.run_script("gate_record", GATE_ARGS)["project"] == "myrepo"


def test_outside_a_repo_a_required_repo_is_refused_before_any_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    plain = tmp_path / "Downloads"
    plain.mkdir()
    monkeypatch.chdir(plain)
    err = _refusal("gate_record", GATE_ARGS)
    assert err.kind == "args" and "repo" in str(err) and "no repo here" in str(err)


# ---- refusals before any worker, each with its kind ----

@pytest.mark.parametrize("name,reason", [("no_such_procedure", "not_found"), ("../x", "invalid_name"),
                                         ("con", "invalid_name")])
def test_a_name_the_library_refuses_is_kind_name(repo: Path, name: str, reason: str) -> None:
    err = _refusal(name, {})
    assert err.kind == "name" and str(err).startswith(f"name: {reason}: ")


def test_a_repo_procedure_is_refused_while_the_repo_root_is_off(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)
    _procedure(repo, "echo_back", "x:string", "none", "local args = ...\nreturn args.x")
    err = _refusal("echo_back", {"x": "hi"})
    assert err.kind == "name" and "repo_root_off" in str(err) and library.REPO_ROOT_ENV in str(err)


def test_a_repo_name_that_shadows_a_shipped_one_is_refused(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    _procedure(repo, "gate_record", "x:string", "none", "return 1")
    err = _refusal("gate_record", GATE_ARGS)
    assert err.kind == "name" and "shadows_shipped" in str(err)


def test_a_header_refusal_reaches_the_caller_unchanged(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    d = repo / ".nexus" / "procedures"
    d.mkdir(parents=True)
    (d / "bad_header.lua").write_text("-- procedure: bad_header\n-- summary: s\nreturn 1\n", encoding="ascii")
    err = _refusal("bad_header", {})
    assert err.kind == "header" and str(err).startswith("header: line 3: ")


@pytest.mark.parametrize("given,words", [({}, "'outcome' is required"), ({**GATE_ARGS, "nope": 1}, "'nope'"),
                                         ({**GATE_ARGS, "repo": "x"}, "reserved")])
def test_an_args_refusal_reaches_the_caller_unchanged(repo: Path, given: dict, words: str) -> None:
    args = {k: v for k, v in GATE_ARGS.items() if k != "outcome"} if not given else given
    err = _refusal("gate_record", args)
    assert err.kind == "args" and words in str(err)


@pytest.mark.parametrize("limits", [{"instructions": 10**12}, {"instructions": -1}, {"no_such_limit": 1}])
def test_a_raised_or_invalid_limit_is_an_args_refusal(repo: Path, limits: dict) -> None:
    assert _refusal("gate_record", GATE_ARGS, limits).kind == "args"


# ---- refusals from the worker ----

def test_an_undeclared_verb_call_fails_naming_the_verb(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    _procedure(repo, "sneaky", "none", "none", "return memory.get('p', 't')")
    err = _refusal("sneaky", {})
    assert err.kind == "static_check" and "verb-ungranted" in str(err) and "memory.get" in str(err)


def test_a_lowered_limit_ends_the_run_on_that_limit(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    _procedure(repo, "spin", "none", "none", "local n = 0\nwhile true do n = n + 1 end")
    with pytest.raises(ScriptLimitError) as e:
        lua_runtime.run_script("spin", {}, {"instructions": 200_000})
    assert e.value.limit == "instructions"


def test_a_repo_procedure_runs_with_the_switch_on(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    _procedure(repo, "echo_back", "x:string, repo:string", "none", "local args = ...\nreturn {args.x, args.repo}")
    assert lua_runtime.run_script("echo_back", {"x": "hi"}) == ["hi", "myrepo"]


# ---- the public surface ----

def test_the_package_exposes_run_script_without_importing_the_parent_side() -> None:
    # The worker imports the package under python -I; run.py must not load with it.
    out = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys, nexus.lua_runtime as p; print(hasattr(p, 'run_script'), 'nexus.lua_runtime.run' in sys.modules)"],
        capture_output=True, text=True, timeout=60, env={"PATH": os.environ.get("PATH", "")})
    assert out.stdout.split() == ["True", "False"], out.stderr
