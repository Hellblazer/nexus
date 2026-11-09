# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SessionStart memory curation check (RDR-238 Phase 3 Step 1, nexus-qo7ie.16).

Test Plan "Hook" bullet: over budget, an unindexed file, a replaced file still present and an
index entry naming a missing file each print one line; a clean directory, a repo without
``MEMORY-ondemand.md`` and an unreadable directory print nothing; a linked worktree checks its
primary's directory; a bad ``memory.index_budget_tokens`` means 1500.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus._hook_runtime import memory_dir as md
from nexus._hook_runtime.entry import VERB_TABLE
from nexus.commands import hook as hook_cmd
from nexus.hooks import memory_curation_check as mcc
from nexus.hooks import session_start_verb
from tests._child_encoding import PYTHON_CHILD_ENCODING

_GIT_ID = ("-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *_GIT_ID, *args], cwd=cwd, check=True, capture_output=True,
                   text=True, encoding=PYTHON_CHILD_ENCODING)


def _repo(tmp_path: Path) -> Path:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _clean_memdir(tmp_path: Path, repo: Path) -> tuple[Path, dict[str, str]]:
    """A memory directory that passes every check: two indexed files, both indexes present."""
    config = tmp_path / "cfg"
    d = config / "projects" / md.mangle_path(repo) / "memory"
    d.mkdir(parents=True)
    (d / "MEMORY.md").write_text("- [One](one.md) — first\n", encoding="utf-8")
    (d / "MEMORY-ondemand.md").write_text("- [Two](two.md) — second\n", encoding="utf-8")
    (d / "one.md").write_text("---\nname: one\n---\nbody\n", encoding="utf-8")
    (d / "two.md").write_text("---\nname: two\n---\nbody\n", encoding="utf-8")
    return d, {"CLAUDE_CONFIG_DIR": str(config)}


def _check(cwd: Path, env: dict[str, str], tmp_path: Path) -> str | None:
    return mcc.check(cwd, env=env, home=tmp_path / "nohome")


# ── the four lines ─────────────────────────────────────────────────────────────────

def test_over_budget_prints_one_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    with (d / "MEMORY.md").open("a", encoding="utf-8") as fh:
        fh.write("x" * (1500 * 4 + 4))  # one token past 1500 at four bytes a token
    out = _check(repo, env, tmp_path)
    assert out is not None and "\n" not in out
    assert "over" in out and "1500" in out


def test_exactly_at_budget_is_not_over(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    size = (d / "MEMORY.md").stat().st_size
    with (d / "MEMORY.md").open("a", encoding="utf-8") as fh:
        fh.write("x" * (1500 * 4 - size))
    assert _check(repo, env, tmp_path) is None


def test_an_unindexed_file_prints_one_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "stray.md").write_text("---\nname: stray\n---\n", encoding="utf-8")
    out = _check(repo, env, tmp_path)
    assert out is not None and "\n" not in out
    assert "stray.md" in out and "index" in out


def test_a_replaced_file_still_present_prints_one_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "two.md").write_text("---\nname: two\nreplaces: [one]\n---\nbody\n", encoding="utf-8")
    out = _check(repo, env, tmp_path)
    assert out is not None and "\n" not in out
    assert "one.md" in out and "two.md" in out and "replaces" in out


def test_an_index_entry_naming_a_missing_file_prints_one_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    with (d / "MEMORY-ondemand.md").open("a", encoding="utf-8") as fh:
        fh.write("- [Gone](gone.md) — deleted\n")
    out = _check(repo, env, tmp_path)
    assert out is not None and "\n" not in out
    assert "gone.md" in out and "MEMORY-ondemand.md" in out


def test_only_the_first_check_that_applies_prints(tmp_path: Path) -> None:
    """Order (bead): budget, unindexed, replaced, dangling; one line, never two."""
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "stray.md").write_text("x\n", encoding="utf-8")
    with (d / "MEMORY-ondemand.md").open("a", encoding="utf-8") as fh:
        fh.write("- [Gone](gone.md)\n")
    out = _check(repo, env, tmp_path)
    assert out is not None and "stray.md" in out and "gone.md" not in out
    with (d / "MEMORY.md").open("a", encoding="utf-8") as fh:
        fh.write("x" * 6004)
    out = _check(repo, env, tmp_path)
    assert out is not None and "over" in out and "stray.md" not in out


# ── silence ────────────────────────────────────────────────────────────────────────

def test_a_clean_directory_prints_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _d, env = _clean_memdir(tmp_path, repo)
    assert _check(repo, env, tmp_path) is None


def test_a_repo_without_the_ondemand_index_prints_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "MEMORY-ondemand.md").unlink()
    (d / "stray.md").write_text("x\n", encoding="utf-8")  # would print if the gate were open
    with (d / "MEMORY.md").open("a", encoding="utf-8") as fh:
        fh.write("x" * 6004)
    assert _check(repo, env, tmp_path) is None


def test_no_memory_directory_prints_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _check(repo, {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}, tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="chmod 000 does not stop root or Windows from listing a directory")
def test_an_unreadable_directory_is_silent_and_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "stray.md").write_text("x\n", encoding="utf-8")
    logged: list[str] = []
    monkeypatch.setattr(mcc, "_emit", lambda _level, event, **_fields: logged.append(event))
    d.chmod(0)
    try:
        assert _check(repo, env, tmp_path) is None
        assert logged == ["memory_curation_check_unreadable"], "silent, and logged to hook.log"
        proc = subprocess.run(
            [sys.executable, "-m", "nexus._hook_runtime.entry", "memory-curation-check"],
            input='{"cwd": "%s"}' % repo.as_posix(), capture_output=True, text=True,
            encoding=PYTHON_CHILD_ENCODING, cwd=repo, env={**os.environ, **env}, timeout=60,
        )
    finally:
        d.chmod(0o755)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_an_exception_inside_the_check_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    _d, env = _clean_memdir(tmp_path, repo)

    def boom(_directory: Path) -> None:
        raise RuntimeError("deliberate")

    monkeypatch.setattr(mcc, "scan", boom)
    assert _check(repo, env, tmp_path) is None


# ── where it looks ─────────────────────────────────────────────────────────────────

def test_a_linked_worktree_checks_its_primarys_memory_directory(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "stray.md").write_text("x\n", encoding="utf-8")
    wt = (tmp_path / "wt").resolve()
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    out = _check(wt, env, tmp_path)
    assert out is not None and "stray.md" in out


@pytest.mark.parametrize("yml", [
    None,                                              # no .nexus.yml
    "memory:\n  other: 3\n",                           # key missing
    "memory:\n  index_budget_tokens: lots\n",          # not an integer
    "memory:\n  index_budget_tokens: 0\n",             # not positive
    "memory:\n  index_budget_tokens: -5\n",            # not positive
    "other:\n  index_budget_tokens: 10\n",             # not under memory:
], ids=["missing-file", "missing-key", "non-integer", "zero", "negative", "other-block"])
def test_a_bad_budget_means_1500(tmp_path: Path, yml: str | None) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    if yml is not None:
        (repo / ".nexus.yml").write_text(yml, encoding="utf-8")
    size = (d / "MEMORY.md").stat().st_size
    with (d / "MEMORY.md").open("a", encoding="utf-8") as fh:
        fh.write("x" * (1500 * 4 - size))
    assert _check(repo, env, tmp_path) is None, "at 1500 exactly: not over"
    with (d / "MEMORY.md").open("a", encoding="utf-8") as fh:
        fh.write("x" * 4)
    out = _check(repo, env, tmp_path)
    assert out is not None and "1500" in out


def test_a_set_budget_is_read_from_the_memory_block(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (repo / ".nexus.yml").write_text(
        "indexing:\n  rdr_paths: [docs/rdr]\nmemory:\n  # a comment\n  index_budget_tokens: 10  # tight\n",
        encoding="utf-8",
    )
    (d / "MEMORY.md").write_text("- [One](one.md)\n- [Two](two.md)\n" + "x" * 40, encoding="utf-8")
    out = _check(repo, env, tmp_path)
    assert out is not None and "10" in out and "over" in out


# ── dispatch ───────────────────────────────────────────────────────────────────────

def test_the_verb_is_registered() -> None:
    assert VERB_TABLE["memory-curation-check"] == "nexus.hooks.memory_curation_check"


def test_run_reads_cwd_from_the_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "stray.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
    monkeypatch.chdir(tmp_path)  # not the repo: the payload's cwd must decide
    result = mcc.run({"cwd": str(repo)})
    assert result.stdout is not None and "stray.md" in result.stdout
    assert mcc.run({"cwd": 7}).stdout is None  # a non-string cwd falls back to the process cwd


def test_session_start_appends_the_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wired through the existing session-start verb: no new hooks.json entry, no new process."""
    monkeypatch.setattr(session_start_verb.hooks, "session_start", lambda **_kw: "GUIDANCE")
    monkeypatch.setattr(session_start_verb, "curation_line", lambda _payload: "CURATION LINE")
    assert session_start_verb.run({}).stdout == "GUIDANCE\n\nCURATION LINE"
    monkeypatch.setattr(session_start_verb, "curation_line", lambda _payload: None)
    assert session_start_verb.run({}).stdout == "GUIDANCE"
    monkeypatch.setattr(session_start_verb.hooks, "session_start", lambda **_kw: "")
    monkeypatch.setattr(session_start_verb, "curation_line", lambda _payload: "CURATION LINE")
    assert session_start_verb.run({}).stdout == "CURATION LINE"


def test_the_command_tier_appends_the_same_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """``nx hook session-start`` and ``nx-hook session-start`` print the same thing."""
    monkeypatch.setattr(hook_cmd.hooks, "session_start", lambda **_kw: "GUIDANCE")
    monkeypatch.setattr(mcc, "curation_line", lambda _payload: "CURATION LINE")
    result = CliRunner().invoke(hook_cmd.session_start_cmd, [], input="{}")
    assert result.exit_code == 0, result.output
    assert result.output == "GUIDANCE\n\nCURATION LINE\n"
