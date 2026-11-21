# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 254-P4-S1 (Gap 4): the name a repository registers its owner under.

``repo_owner_name(cat, name=, repo_hash=, repo_root=)`` is the one rule every repo owner registration calls before its
upsert. The engine matches an owner by ``(name, owner_type)`` first, so a second live clone with the same folder name
would take the first clone's owner over; the client therefore sends ``<name>-<repo_hash>`` when the same-name owner's
recorded root is another live git work tree on this machine, and the plain name otherwise.

Real directories under ``tmp_path`` (the rule reads the filesystem); the catalog is a duck with the two reads the rule
makes, so a test names exactly what the engine answered. No substrate."""
from __future__ import annotations

import errno
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest
import structlog

from nexus.catalog.engine_writes.repo_owner_name import repo_owner_name
from nexus.catalog.tumbler import Tumbler

_HASH = "bbbb2222"


class _Cat:
    """The two reads ``repo_owner_name`` makes: ``owner_tumblers_by_name`` (``/owners/by_name``) and
    ``get_owner_by_prefix`` (``/owners/show``)."""

    def __init__(self, *owners: dict, error: Exception | None = None) -> None:
        self.owners = {o["tumbler_prefix"]: o for o in owners}
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def owner_tumblers_by_name(self, name: str) -> list[Tumbler]:
        self.calls.append(("by_name", name))
        if self.error is not None:
            raise self.error
        return [Tumbler.parse(p) for p, o in self.owners.items() if o["name"] == name]

    def get_owner_by_prefix(self, prefix: str) -> dict | None:
        self.calls.append(("show", prefix))
        return self.owners.get(str(prefix))


def _owner(root: str | Path | None, *, name: str = "nexus", owner_type: str = "repo", prefix: str = "1.1") -> dict:
    return {"tumbler_prefix": prefix, "name": name, "owner_type": owner_type, "repo_hash": "aaaa1111",
            "repo_root": "" if root is None else str(root)}


def _clone(base: Path, where: str) -> Path:
    path = base / where / "nexus"
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def _name(cat: _Cat, root: Path, *, name: str = "nexus") -> str:
    return repo_owner_name(cat, name=name, repo_hash=_HASH, repo_root=root)


def test_a_live_other_clone_splits(tmp_path):
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    assert _name(_Cat(_owner(first)), second) == f"nexus-{_HASH}"


def test_the_same_directory_through_a_symlink_keeps_the_name(tmp_path):
    clone = _clone(tmp_path, "work")
    link = tmp_path / "link"
    link.symlink_to(clone.parent, target_is_directory=True)
    # the owner recorded the real path, the run reaches the clone through the link, and the other way round
    assert _name(_Cat(_owner(clone)), link / "nexus") == "nexus"
    assert _name(_Cat(_owner(link / "nexus")), clone) == "nexus"


def test_a_gone_root_keeps_the_name(tmp_path):
    second = _clone(tmp_path, "scratch")
    assert _name(_Cat(_owner(tmp_path / "work" / "nexus")), second) == "nexus"


@pytest.mark.skipif(
    sys.platform == "win32" or not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="chmod 0 does not make a directory unreadable on Windows, or to root",
)
def test_an_unreadable_root_splits(tmp_path):
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    locked = first.parent
    locked.chmod(0)
    try:
        assert _name(_Cat(_owner(first)), second) == f"nexus-{_HASH}"
    finally:
        locked.chmod(0o755)


def test_no_owner_of_that_name_keeps_the_name(tmp_path):
    cat = _Cat()
    assert _name(cat, _clone(tmp_path, "scratch")) == "nexus"
    assert cat.calls == [("by_name", "nexus")]


def test_a_same_named_owner_of_another_type_is_not_a_clone(tmp_path):
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    assert _name(_Cat(_owner(first, owner_type="curator")), second) == "nexus"


def test_a_same_named_owner_without_a_root_keeps_the_name(tmp_path):
    assert _name(_Cat(_owner(None)), _clone(tmp_path, "scratch")) == "nexus"


def test_a_root_that_is_no_longer_a_git_work_tree_keeps_the_name(tmp_path):
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    (first / ".git").rename(first / "dot-git-gone")
    assert _name(_Cat(_owner(first)), second) == "nexus"


def test_the_rule_names_a_main_repository_whose_dot_git_is_a_file(tmp_path):
    """A recorded root whose ``.git`` is a file (a submodule or a linked checkout) is still a work tree."""
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    git_dir = first / ".git"
    moved = tmp_path / "elsewhere.git"
    git_dir.rename(moved)
    git_dir.write_text(f"gitdir: {moved}\n")
    assert _name(_Cat(_owner(first)), second) == f"nexus-{_HASH}"


def test_an_engine_error_from_the_lookup_propagates(tmp_path):
    with pytest.raises(RuntimeError, match="engine down"):
        _name(_Cat(error=RuntimeError("engine down")), _clone(tmp_path, "scratch"))


def test_a_foreign_machine_path_that_raises_einval_is_not_live_and_keeps_the_name(tmp_path, monkeypatch):
    """A root another machine recorded can hold characters this OS rejects (a Mac path with ':' read on Windows):
    ``os.stat`` raises ``EINVAL`` there, which is not a permission error and not a live clone."""
    second = _clone(tmp_path, "scratch")
    foreign = "/Users/other:machine/work/nexus"
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        if str(path).startswith(foreign):
            raise OSError(errno.EINVAL, "Invalid argument")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("nexus.catalog.engine_writes.repo_owner_name.os.stat", stat)
    assert _name(_Cat(_owner(foreign)), second) == "nexus"


def test_a_repo_root_that_does_not_exist_is_not_read_as_the_recorded_root_being_gone(tmp_path):
    """The recorded root is a live work tree and this run's root cannot be reached: ``samefile`` raises
    ``FileNotFoundError`` for the missing side, which must not read as "the recorded root is gone" (a takeover)."""
    first = _clone(tmp_path, "work")
    assert _name(_Cat(_owner(first)), tmp_path / "scratch" / "nexus") == f"nexus-{_HASH}"


def test_a_split_name_held_by_another_hash_extends_the_suffix(tmp_path):
    """An owner move keeps the name and changes the hash, so ``nexus-<hash8>`` can already name an owner of another
    clone: the second live clone's name takes the full repo hash instead, and stays deterministic."""
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    full = hashlib.sha256(str(second).encode()).hexdigest()
    held = {**_owner(tmp_path / "gone" / "nexus", name=f"nexus-{full[:8]}", prefix="1.2"), "repo_hash": "cccc3333"}
    cat = _Cat(_owner(first), held)
    assert repo_owner_name(cat, name="nexus", repo_hash=full[:8], repo_root=second) == f"nexus-{full}"
    assert repo_owner_name(cat, name="nexus", repo_hash=full[:8], repo_root=second) == f"nexus-{full}"


def test_a_split_name_held_by_an_owner_of_this_hash_is_not_extended(tmp_path):
    """The candidate name held by an owner that already carries this repo's hash is this repo's own owner."""
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    own = {**_owner(second, name=f"nexus-{_HASH}", prefix="1.2"), "repo_hash": _HASH}
    assert _name(_Cat(_owner(first), own), second) == f"nexus-{_HASH}"


def test_a_split_is_logged_with_the_name_the_candidate_and_the_other_root(tmp_path):
    first, second = _clone(tmp_path, "work"), _clone(tmp_path, "scratch")
    with structlog.testing.capture_logs() as logs:
        _name(_Cat(_owner(first)), second)
    (event,) = [e for e in logs if e["event"] == "repo_owner_split"]
    assert (event["log_level"], event["name"], event["candidate"], event["other_root"]) == (
        "info", "nexus", f"nexus-{_HASH}", str(first))


def test_no_split_is_not_logged(tmp_path):
    with structlog.testing.capture_logs() as logs:
        _name(_Cat(), _clone(tmp_path, "scratch"))
    assert [e for e in logs if e["event"] == "repo_owner_split"] == []
