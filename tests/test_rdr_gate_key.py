# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-3jjkg: the key an RDR gate record carries (``blob:`` over ``commit:``)
and the blob comparison, against a real tmp git repo."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.rdr_gate_key import (
    blob_delta,
    blob_range,
    commit_resolves,
    file_blob,
    gated_key,
    prefix_match,
)

REL = "docs/rdr/rdr-001-x.md"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    (tmp_path / REL).write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "c1")
    return tmp_path


def test_gated_key_prefers_the_blob_and_falls_back_to_the_commit() -> None:
    both = gated_key('"0123456789ab"', "abc1234")
    assert (both.kind, both.value, both.commit) == ("blob", "0123456789ab", "abc1234")
    only = gated_key(None, "`abc1234`")
    assert (only.kind, only.value) == ("commit", "abc1234")
    empty = gated_key("", "")
    assert (empty.kind, empty.value) == ("", "")
    assert not empty.matches("abc1234")


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("0123456789ab", "0123456789abcdef", True),
        ("0123456", "0123456789ab", True),
        ("012345", "012345", False),  # under the 7-digit floor, even when equal
        ("0123456", "0123457", False),
    ],
)
def test_prefix_match(a: str, b: str, expected: bool) -> None:
    assert prefix_match(a, b) is expected


def test_blob_delta_same_changed_unknown(repo: Path) -> None:
    gated = file_blob(str(repo), REL)
    assert gated and len(gated) == 12
    assert blob_delta(str(repo), REL, gated).state == "same"

    # same text, history rewritten: still the same
    _git(repo, "commit", "-q", "--amend", "-m", "rebased")
    assert blob_delta(str(repo), REL, gated).state == "same"

    (repo / REL).write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "edit")
    delta = blob_delta(str(repo), REL, gated)
    assert delta.state == "changed" and delta.error == ""
    assert delta.current == file_blob(str(repo), REL)
    assert delta.stat and "1 file changed" in delta.stat[-1]
    assert blob_range(gated, delta.current) == f"git diff {gated} {delta.current}"

    assert blob_delta(str(repo), "docs/rdr/missing.md", gated).state == "unknown"


def test_a_gated_blob_this_clone_lacks_is_changed_with_a_named_error(repo: Path) -> None:
    delta = blob_delta(str(repo), REL, "deadbeef0123")
    assert delta.state == "changed" and delta.error


def test_commit_resolves_follows_the_object_store(repo: Path) -> None:
    c1 = _git(repo, "rev-parse", "--short=9", "HEAD")
    assert commit_resolves(str(repo), c1)
    assert not commit_resolves(str(repo), "")
    _git(repo, "commit", "-q", "--amend", "-m", "rebased")
    _git(repo, "reflog", "expire", "--expire=now", "--all")
    _git(repo, "gc", "-q", "--prune=now")
    assert not commit_resolves(str(repo), c1)


def test_file_blob_reads_a_windows_relpath(repo: Path) -> None:
    """os.path.relpath gives backslashes on Windows; git's HEAD:<path> reads
    only forward slashes, so an unnormalised path resolved to nothing and the
    caller saw an untracked file (nexus-3jjkg, caught on qwent native Windows)."""
    assert file_blob(str(repo), REL.replace("/", "\\")) == file_blob(str(repo), REL)
    assert file_blob(str(repo), REL) is not None


def test_fix_commits_stop_at_the_gated_blob_and_never_read_a_gate_commit(repo: Path) -> None:
    from nexus.rdr_gate_key import fix_commits

    gated = file_blob(str(repo), REL)
    for n in (1, 2):
        (repo / REL).write_text("one\n" + "more\n" * n, encoding="utf-8")
        _git(repo, "commit", "-q", "-am", f"fix {n}")
    found = fix_commits(str(repo), REL, gated)
    assert found.found and [c.split(" ", 1)[1] for c in found.commits] == ["fix 2", "fix 1"]

    absent = fix_commits(str(repo), REL, "deadbeef0123")
    assert not absent.found and absent.error == ""
    capped = fix_commits(str(repo), REL, "deadbeef0123", cap=2)
    assert not capped.found and capped.capped


def test_fix_commits_and_the_dirty_check_never_raise_on_a_git_timeout(repo: Path, monkeypatch) -> None:
    """Review item 3: a TimeoutExpired from run_bounded is a named result."""
    import nexus.rdr_gate_key as key_mod

    def slow(argv, **_k):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(key_mod, "run_bounded", slow)
    found = key_mod.fix_commits(str(repo), REL, "0123456789ab")
    assert not found.found and "git log failed" in found.error
    assert key_mod.has_uncommitted_edits(str(repo), REL) is False
    assert key_mod.file_blob(str(repo), REL) is None


def test_has_uncommitted_edits(repo: Path) -> None:
    from nexus.rdr_gate_key import has_uncommitted_edits

    assert has_uncommitted_edits(str(repo), REL) is False
    (repo / REL).write_text("edited\n", encoding="utf-8")
    assert has_uncommitted_edits(str(repo), REL) is True
    assert has_uncommitted_edits(str(repo), "docs/rdr/untracked.md") is False
