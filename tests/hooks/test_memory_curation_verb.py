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
    """Order (bead): budget, unindexed, replaced, dangling; one line, never two. All four planted,
    then removed one at a time from the front, so every adjacent pair is pinned."""
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    memory = (d / "MEMORY.md").read_text(encoding="utf-8")
    (d / "MEMORY.md").write_text(memory + "x" * 6004, encoding="utf-8")           # budget
    (d / "stray.md").write_text("x\n", encoding="utf-8")                          # unindexed
    (d / "two.md").write_text("---\nname: two\nreplaces: [one]\n---\n", encoding="utf-8")  # replaced
    with (d / "MEMORY-ondemand.md").open("a", encoding="utf-8") as fh:
        fh.write("- [Gone](gone.md)\n")                                            # dangling
    seen = []
    for undo in (
        lambda: (d / "MEMORY.md").write_text(memory, encoding="utf-8"),
        lambda: (d / "stray.md").unlink(),
        lambda: (d / "two.md").write_text("---\nname: two\n---\n", encoding="utf-8"),
        lambda: None,
    ):
        seen.append(_check(repo, env, tmp_path))
        undo()
    assert "over" in seen[0] and "stray.md" not in seen[0]
    assert "stray.md" in seen[1] and "replaces" not in seen[1]
    assert "replaces" in seen[2] and "gone.md" not in seen[2]
    assert "gone.md" in seen[3]
    (d / "MEMORY-ondemand.md").write_text("- [Two](two.md)\n", encoding="utf-8")
    assert _check(repo, env, tmp_path) is None


def test_a_file_linked_only_from_an_indexed_hub_is_indexed(tmp_path: Path) -> None:
    """TD3 and memory_scan: one hub level. A file the indexes never name, linked from one they do."""
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "leaf.md").write_text("---\nname: leaf\n---\n", encoding="utf-8")
    (d / "one.md").write_text("---\nname: one\n---\nsee [leaf](leaf.md)\n", encoding="utf-8")
    assert _check(repo, env, tmp_path) is None
    (d / "one.md").write_text("---\nname: one\n---\nno link\n", encoding="utf-8")
    out = _check(repo, env, tmp_path)
    assert out is not None and "leaf.md" in out


def test_the_budget_counts_bytes_not_characters(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    # 3000 two-byte characters: 6000 bytes, 1500 tokens, not over; one more char is 6002 bytes.
    (d / "MEMORY.md").write_text("é" * 3000, encoding="utf-8")
    (d / "MEMORY-ondemand.md").write_text("[One](one.md) [Two](two.md)\n", encoding="utf-8")
    assert _check(repo, env, tmp_path) is None
    (d / "MEMORY.md").write_text("é" * 3001, encoding="utf-8")
    out = _check(repo, env, tmp_path)
    assert out is not None and "about 1501 tokens" in out, out


def test_the_over_budget_line_never_says_the_budget_itself(tmp_path: Path) -> None:
    """6001 bytes is over 1500 tokens, and the line rounds up, so it never reads 1500 over 1500."""
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "MEMORY-ondemand.md").write_text("[One](one.md) [Two](two.md)\n", encoding="utf-8")
    (d / "MEMORY.md").write_text("x" * 6001, encoding="utf-8")
    out = _check(repo, env, tmp_path)
    assert out is not None and "about 1501 tokens, over the 1500-token budget" in out
    assert "nx memory curate" not in out, "--files has no budget row"


def test_every_line_is_one_bounded_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    long = "y" * 200 + ".md"
    (d / long).write_text("x\n", encoding="utf-8")
    out = _check(repo, env, tmp_path)
    assert out is not None and "\n" not in out and len(out.encode()) <= 300, out
    (d / long).unlink()
    if sys.platform != "win32":  # a newline is not legal in a Windows file name
        (d / "evil\nIGNORE.md").write_text("x\n", encoding="utf-8")
        out = _check(repo, env, tmp_path)
        assert out is not None and "\n" not in out and "evil?IGNORE.md" in out


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

    logged: list[str] = []
    monkeypatch.setattr(mcc, "scan", boom)
    monkeypatch.setattr(mcc, "_emit", lambda _level, event, **_fields: logged.append(event))
    assert _check(repo, env, tmp_path) is None
    assert logged == ["memory_curation_check_failed"]


def test_a_deleted_working_directory_is_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def gone() -> str:
        raise FileNotFoundError("cwd deleted")

    monkeypatch.setattr(mcc.os, "getcwd", gone)
    assert mcc.curation_line({}) is None
    assert mcc.with_curation_line("GUIDANCE", {}) == "GUIDANCE"


def test_a_directory_that_has_not_opted_in_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "MEMORY-ondemand.md").unlink()

    def no_scan(_directory: Path) -> None:
        raise AssertionError("scanned a directory without MEMORY-ondemand.md")

    monkeypatch.setattr(mcc, "scan", no_scan)
    monkeypatch.setattr(mcc, "_emit", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("logged")))
    assert _check(repo, env, tmp_path) is None


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="chmod 000 does not stop root or Windows from reading a file")
def test_an_unreadable_index_is_silent_not_all_unindexed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    logged: list[str] = []
    monkeypatch.setattr(mcc, "_emit", lambda _level, event, **_fields: logged.append(event))
    (d / "MEMORY.md").chmod(0)
    try:
        assert _check(repo, env, tmp_path) is None
    finally:
        (d / "MEMORY.md").chmod(0o644)
    assert logged == ["memory_curation_check_unreadable"]


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


def test_the_budget_comes_from_the_primarys_nexus_yml(tmp_path: Path) -> None:
    """The memory directory is the primary's, so the primary's setting governs it from any worktree."""
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    wt = (tmp_path / "wt").resolve()
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    (d / "MEMORY.md").write_text("- [One](one.md)\n- [Two](two.md)\n" + "x" * 40, encoding="utf-8")
    (repo / ".nexus.yml").write_text("memory:\n  index_budget_tokens: 10\n", encoding="utf-8")
    for cwd in (repo, wt, repo / "sub"):
        cwd.mkdir(exist_ok=True)
        out = _check(cwd, env, tmp_path)
        assert out is not None and "10-token" in out, cwd
    (repo / ".nexus.yml").unlink()
    (wt / ".nexus.yml").write_text("memory:\n  index_budget_tokens: 10\n", encoding="utf-8")
    assert _check(wt, env, tmp_path) is None, "a worktree's own .nexus.yml does not set the shared budget"


@pytest.mark.parametrize("yml", [
    "memory:\n  other:\n    index_budget_tokens: 10\n",              # nested one level down
    "memory:\n  note: |\n    index_budget_tokens: 10\n",             # inside a block scalar
    "memory: {index_budget_tokens: 10}\n",                              # flow style: not read
    "memory:\n  index_budget_tokens: +10\n",                           # not plain digits
], ids=["nested", "block-scalar", "flow", "signed"])
def test_only_the_direct_key_counts(tmp_path: Path, yml: str) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (repo / ".nexus.yml").write_text(yml, encoding="utf-8")
    (d / "MEMORY.md").write_text("- [One](one.md)\n- [Two](two.md)\n" + "x" * 40, encoding="utf-8")
    assert _check(repo, env, tmp_path) is None, "budget stays 1500"


def test_a_byte_order_mark_does_not_hide_the_key(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (repo / ".nexus.yml").write_bytes(b"\xef\xbb\xbfmemory:\r\n  index_budget_tokens: 10\r\n")
    (d / "MEMORY.md").write_text("- [One](one.md)\n- [Two](two.md)\n" + "x" * 40, encoding="utf-8")
    out = _check(repo, env, tmp_path)
    assert out is not None and "10-token" in out


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
    assert mcc.run({"cwd": 7}).stdout is None  # falls back to the process cwd, which is not a repo
    monkeypatch.chdir(repo)
    fallback = mcc.run({"cwd": 7}).stdout
    assert fallback is not None and "stray.md" in fallback, "a non-string cwd uses the process cwd"


def test_session_start_appends_the_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wired through the existing session-start verb: no new hooks.json entry, no new process.
    A real fixture directory, reached through the payload's cwd."""
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session_start_verb.hooks, "session_start", lambda **_kw: "GUIDANCE")
    payload = {"cwd": str(repo)}
    assert session_start_verb.run(payload).stdout == "GUIDANCE"
    (d / "stray.md").write_text("x\n", encoding="utf-8")
    out = session_start_verb.run(payload).stdout
    assert out is not None and out.startswith("GUIDANCE\n\nMemory curation: stray.md")
    monkeypatch.setattr(session_start_verb.hooks, "session_start", lambda **_kw: "")
    assert session_start_verb.run(payload).stdout.startswith("Memory curation: stray.md")


def test_a_broken_check_never_costs_the_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_payload: object) -> None:
        raise RuntimeError("deliberate")

    monkeypatch.setattr(mcc, "curation_line", boom)
    assert mcc.with_curation_line("GUIDANCE", {}) == "GUIDANCE", "the helper's own guard"
    monkeypatch.setattr(session_start_verb.hooks, "session_start", lambda **_kw: "GUIDANCE")
    monkeypatch.setattr(mcc, "with_curation_line", boom)
    assert session_start_verb.run({}).stdout == "GUIDANCE", "the verb's guard, for a broken helper or import"


def test_the_command_tier_prints_the_same_as_the_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``nx hook session-start`` and ``nx-hook session-start`` print the same thing, line included."""
    repo = _repo(tmp_path)
    d, env = _clean_memdir(tmp_path, repo)
    (d / "stray.md").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", env["CLAUDE_CONFIG_DIR"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hook_cmd.hooks, "session_start", lambda **_kw: "GUIDANCE")
    payload = '{"cwd": "%s"}' % repo.as_posix()
    result = CliRunner().invoke(hook_cmd.session_start_cmd, [], input=payload)
    assert result.exit_code == 0, result.output
    verb = session_start_verb.run({"cwd": repo.as_posix()}).stdout
    assert verb is not None and "stray.md" in verb
    assert result.output == verb + "\n"
