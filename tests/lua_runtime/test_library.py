# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two procedure library roots (RDR-242 Technical Design 4; RDR-240 Technical Design 1; nexus-qo7ie.31).

The shipped root is the packaged ``nexus/procedures`` directory; the repo root is
``<repo top>/.nexus/procedures``. The name rule, the containment check (symbolic links
followed) and the shadow refusal are each pinned with a mutant in the hand-back of the
bead: remove the guard and the test named here goes red.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus.lua_runtime import library
from nexus.lua_runtime.header import RepoProbe, RepoProbeError
from nexus.lua_runtime.library import LibraryError

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is a hard dependency of nx")

HEADER = (
    "-- procedure: {name}\n-- summary: {summary}\n-- args: none\n-- grants: none\n"
)


def _proc(name: str, summary: str = "does a thing") -> bytes:
    return (HEADER.format(name=name, summary=summary) + "return 1\n").encode("ascii")


def _write(root: Path, name: str, data: bytes | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.lua"
    path.write_bytes(_proc(name) if data is None else data)
    return path


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shipped"
    root.mkdir()
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    return root


def _git(cwd: Path, *argv: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *argv], cwd=cwd, env=env, check=True, capture_output=True, timeout=60)


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(library.REPO_ROOT_ENV, raising=False)


@pytest.fixture
def repo_dir(tmp_path: Path) -> Path:
    top = tmp_path / "work" / "myrepo"
    top.mkdir(parents=True)
    _git(top, "init", "-q", "-b", "main")
    return top


@pytest.fixture
def repo(repo_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo with the repo-root switch ON (the tests of the off states use ``repo_dir``)."""
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    return repo_dir


# -- the real shipped root ----------------------------------------------------


def test_the_packaged_root_holds_the_shipped_procedures() -> None:
    root = library.shipped_root()
    assert root.is_dir()
    names = {p.name for p in root.glob("*.lua")}
    assert {"research_add.lua", "gate_record.lua"} <= names


def test_find_reads_a_shipped_procedure_from_the_package() -> None:
    path, data = library.find("research_add", cwd=None)
    assert path == (library.shipped_root() / "research_add.lua").resolve()
    assert data.startswith(b"-- procedure: research_add\n")


# -- the name rule -----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "", "A", "Upper", "1abc", "_x", "a-b", "a b", "a.b", "a/b", "../x", "..", ".", "/etc/passwd",
        "a\\b", "x" * 65, "a\x00b", "a\n", "é", "a.lua",
    ],
)
def test_a_name_that_breaks_the_rule_is_refused_before_any_path_is_built(
    name: str, shipped: Path
) -> None:
    with pytest.raises(LibraryError) as err:
        library.find(name, cwd=None)
    assert err.value.reason == "invalid_name"
    assert "[a-z][a-z0-9_]{0,63}" in err.value.detail


@pytest.mark.parametrize(
    "name", ["con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"],
)
def test_a_windows_device_name_is_refused_as_a_name(name: str, shipped: Path) -> None:
    _write(shipped, name)  # even a file by that name is no procedure: it cannot be opened on Windows
    with pytest.raises(LibraryError) as err:
        library.find(name, cwd=None)
    assert err.value.reason == "invalid_name"
    assert "reserved" in err.value.detail
    assert library.procedures(cwd=None) == []


@pytest.mark.parametrize("name", ["com0", "com10", "lpt0", "console", "auxx", "nu", "com"])
def test_a_name_that_only_resembles_a_device_name_is_accepted(name: str, shipped: Path) -> None:
    _write(shipped, name)
    assert library.find(name, cwd=None)[0].name == f"{name}.lua"


def test_a_name_of_the_longest_length_is_accepted(shipped: Path) -> None:
    name = "a" * 64
    _write(shipped, name)
    assert library.find(name, cwd=None)[0].name == f"{name}.lua"


def test_a_name_that_is_not_text_is_refused(shipped: Path) -> None:
    with pytest.raises(LibraryError) as err:
        library.find(None, cwd=None)  # type: ignore[arg-type]
    assert err.value.reason == "invalid_name"


# -- lookup ------------------------------------------------------------------


def test_a_name_in_neither_root_is_refused_naming_it(shipped: Path) -> None:
    with pytest.raises(LibraryError) as err:
        library.find("missing", cwd=None)
    assert err.value.reason == "not_found"
    assert "missing" in err.value.detail


def test_a_directory_named_like_a_procedure_is_not_a_procedure(shipped: Path) -> None:
    (shipped / "adir.lua").mkdir()
    with pytest.raises(LibraryError) as err:
        library.find("adir", cwd=None)
    assert err.value.reason == "not_found"


@needs_git
def test_a_repo_procedure_is_found_from_a_subdirectory(shipped: Path, repo: Path) -> None:
    mine = _write(repo / ".nexus" / "procedures", "mine")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    path, data = library.find("mine", cwd=sub)
    assert path == mine.resolve()
    assert data == mine.read_bytes()


@needs_git
def test_a_shipped_name_resolves_to_the_shipped_file_when_the_repo_has_none(shipped: Path, repo: Path) -> None:
    s = _write(shipped, "common")
    assert library.find("common", cwd=repo)[0] == s.resolve()


def test_outside_a_repo_only_the_shipped_root_exists(shipped: Path, tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _write(plain / ".nexus" / "procedures", "mine")  # not in a repo, so not a root
    with pytest.raises(LibraryError) as err:
        library.find("mine", cwd=plain)
    assert err.value.reason == "not_found"


def test_a_missing_cwd_means_no_repo_root_not_a_failure(shipped: Path, tmp_path: Path) -> None:
    _write(shipped, "common")
    assert library.find("common", cwd=tmp_path / "gone")[0].name == "common.lua"


def test_only_a_file_whose_entry_is_exactly_the_name_is_found(shipped: Path) -> None:
    # On a case-insensitive filesystem root/foo.lua opens Foo.lua; the listing shows Foo.lua
    # only, so find must not run a file `procedures` does not list under that name.
    _write(shipped, "Foo")
    with pytest.raises(LibraryError) as err:
        library.find("foo", cwd=None)
    assert err.value.reason == "not_found"
    assert library.procedures(cwd=None) == []


needs_posix_perms = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs permission bits that bind the caller",
)


def test_unreadable_is_a_reason() -> None:
    assert "unreadable" in library.REASONS


@needs_posix_perms
def test_a_file_that_cannot_be_read_is_unreadable_not_an_oserror(shipped: Path) -> None:
    path = _write(shipped, "locked")
    path.chmod(0)
    try:
        with pytest.raises(LibraryError) as err:
            library.find("locked", cwd=None)
    finally:
        path.chmod(0o600)
    assert err.value.reason == "unreadable"
    assert "locked" in err.value.detail


@needs_posix_perms
def test_a_root_that_cannot_be_listed_is_unreadable_for_find_and_empty_for_the_listing(shipped: Path) -> None:
    _write(shipped, "hidden")
    shipped.chmod(0)
    try:
        with pytest.raises(LibraryError) as err:
            library.find("hidden", cwd=None)
        assert library.procedures(cwd=None) == []
    finally:
        shipped.chmod(0o700)
    assert err.value.reason == "unreadable"


def test_a_listing_the_system_refuses_lists_nothing_on_any_python(
    shipped: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Python 3.13's glob swallows a PermissionError itself; 3.12's does not, and the listing
    # behind `nx run --list` and the tool description must not raise at import.
    _write(shipped, "mine")

    def refuse(self: Path, pattern: str) -> object:
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(Path, "glob", refuse)
    assert library.procedures(cwd=None) == []


@needs_posix_perms
def test_a_listed_file_that_cannot_be_read_is_listed_with_the_reason(shipped: Path) -> None:
    path = _write(shipped, "locked")
    path.chmod(0)
    try:
        (entry,) = library.procedures(cwd=None)
    finally:
        path.chmod(0o600)
    assert entry.name == "locked" and entry.summary.startswith("unreadable: ")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a named pipe")
def test_the_read_refuses_anything_but_a_regular_file_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "pipe.lua"
    os.mkfifo(fifo)  # opened for reading without O_NONBLOCK this would wait for a writer
    with pytest.raises(LibraryError) as err:
        library._read(fifo)
    assert err.value.reason == "unreadable"
    assert "regular" in err.value.detail


@pytest.mark.skipif(os.name == "nt", reason="needs symbolic links")
def test_the_read_does_not_follow_a_link_swapped_in_after_the_check(tmp_path: Path) -> None:
    target = tmp_path / "target.lua"
    target.write_bytes(b"secret")
    link = tmp_path / "swapped.lua"
    link.symlink_to(target)
    with pytest.raises(LibraryError) as err:
        library._read(link)  # find reads the resolved path; a link there is a swap
    assert err.value.reason == "unreadable"


# -- shadowing ---------------------------------------------------------------


@needs_git
def test_a_repo_name_that_shadows_a_shipped_one_is_refused(shipped: Path, repo: Path) -> None:
    _write(shipped, "common")
    _write(repo / ".nexus" / "procedures", "common", _proc("common", "the repo's own"))
    with pytest.raises(LibraryError) as err:
        library.find("common", cwd=repo)
    assert err.value.reason == "shadows_shipped"
    assert "common" in err.value.detail


@needs_git
def test_a_shadow_is_refused_even_when_the_repo_file_is_a_symbolic_link(shipped: Path, repo: Path) -> None:
    _write(shipped, "common")
    root = repo / ".nexus" / "procedures"
    real = _write(root, "real", _proc("common"))
    (root / "common.lua").symlink_to(real)
    with pytest.raises(LibraryError) as err:
        library.find("common", cwd=repo)
    assert err.value.reason == "shadows_shipped"


# -- containment -------------------------------------------------------------


@needs_git
def test_a_repo_file_that_is_a_link_out_of_its_root_is_refused(
    shipped: Path, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.lua"
    outside.write_bytes(_proc("evil"))
    root = repo / ".nexus" / "procedures"
    root.mkdir(parents=True)
    (root / "evil.lua").symlink_to(outside)
    with pytest.raises(LibraryError) as err:
        library.find("evil", cwd=repo)
    assert err.value.reason == "outside_root"
    assert "evil" in err.value.detail


def test_a_shipped_file_that_is_a_link_out_of_its_root_is_refused(shipped: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.lua"
    outside.write_bytes(_proc("evil"))
    (shipped / "evil.lua").symlink_to(outside)
    with pytest.raises(LibraryError) as err:
        library.find("evil", cwd=None)
    assert err.value.reason == "outside_root"


@needs_git
def test_a_link_to_a_file_inside_the_same_root_is_accepted(shipped: Path, repo: Path) -> None:
    root = repo / ".nexus" / "procedures"
    real = _write(root, "real")
    (root / "alias.lua").symlink_to(real)
    path, _ = library.find("alias", cwd=repo)
    assert path == real.resolve()


def test_a_shipped_root_that_is_itself_a_link_is_judged_by_where_it_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_root = tmp_path / "realroot"
    _write(real_root, "mine")
    link = tmp_path / "shippedlink"
    link.symlink_to(real_root)
    monkeypatch.setattr(library, "shipped_root", lambda: link)
    assert library.find("mine", cwd=None)[0] == (real_root / "mine.lua").resolve()


@needs_git
def test_a_repo_root_that_is_a_link_inside_the_repo_is_accepted(shipped: Path, repo: Path) -> None:
    real_root = repo / "tools" / "procs"
    _write(real_root, "mine")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "procedures").symlink_to(real_root)
    assert library.find("mine", cwd=repo)[0] == (real_root / "mine.lua").resolve()


@needs_git
def test_a_repo_root_that_is_a_link_out_of_the_repo_is_refused(
    shipped: Path, repo: Path, tmp_path: Path
) -> None:
    # RDR-240 leaves this open; the safe side: a checkout cannot point its procedure root at
    # files that are not part of the checkout (a cloned repo's .nexus -> ~/.ssh, say).
    real_root = tmp_path / "realroot"
    _write(real_root, "mine")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "procedures").symlink_to(real_root)
    with pytest.raises(LibraryError) as err:
        library.find("mine", cwd=repo)
    assert err.value.reason == "outside_root"
    assert library.procedures(cwd=repo) == []


@needs_git
def test_a_dot_nexus_that_is_a_link_out_of_the_repo_is_refused(
    shipped: Path, repo: Path, tmp_path: Path
) -> None:
    outer = tmp_path / "outer"
    _write(outer / "procedures", "mine")
    (repo / ".nexus").symlink_to(outer)
    with pytest.raises(LibraryError) as err:
        library.find("mine", cwd=repo)
    assert err.value.reason == "outside_root"


# -- the repo-root switch (RDR-242 Open Question S5) --------------------------


def test_the_switch_is_named_by_one_constant() -> None:
    assert library.REPO_ROOT_ENV == "NX_PROCEDURES_REPO_ROOT"


@needs_git
def test_the_repo_root_is_off_by_default(shipped: Path, repo_dir: Path) -> None:
    _write(repo_dir / ".nexus" / "procedures", "mine")
    assert library.repo_root_enabled() is False
    with pytest.raises(LibraryError) as err:
        library.find("mine", cwd=repo_dir)
    assert err.value.reason == "repo_root_off"
    assert err.value.detail == (
        "mine is in .nexus/procedures/, which is off; set NX_PROCEDURES_REPO_ROOT=1 "
        "to run repo procedures (RDR-242 S5)"
    )
    assert library.procedures(cwd=repo_dir) == []


@needs_git
def test_the_repo_root_is_on_with_exactly_1(
    shipped: Path, repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = _write(repo_dir / ".nexus" / "procedures", "mine")
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    assert library.repo_root_enabled() is True
    assert library.find("mine", cwd=repo_dir)[0] == mine.resolve()
    assert [p.name for p in library.procedures(cwd=repo_dir)] == ["mine"]


@needs_git
@pytest.mark.parametrize("value", ["0", "true", "TRUE", "yes", "on", "", " 1", "1 ", "11"])
def test_any_other_value_leaves_the_repo_root_off(
    value: str, shipped: Path, repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo_dir / ".nexus" / "procedures", "mine")
    monkeypatch.setenv(library.REPO_ROOT_ENV, value)
    assert library.repo_root_enabled() is False
    with pytest.raises(LibraryError) as err:
        library.find("mine", cwd=repo_dir)
    assert err.value.reason == "repo_root_off"
    assert library.procedures(cwd=repo_dir) == []


@needs_git
def test_the_switch_is_read_at_lookup_time(
    shipped: Path, repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo_dir / ".nexus" / "procedures", "mine")
    with pytest.raises(LibraryError):
        library.find("mine", cwd=repo_dir)
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    library.find("mine", cwd=repo_dir)
    monkeypatch.setenv(library.REPO_ROOT_ENV, "0")
    with pytest.raises(LibraryError):
        library.find("mine", cwd=repo_dir)


@needs_git
def test_the_shipped_root_runs_with_the_switch_off_even_beside_a_repo_file(
    shipped: Path, repo_dir: Path
) -> None:
    s = _write(shipped, "common")
    _write(repo_dir / ".nexus" / "procedures", "common", _proc("common", "shadow"))
    assert library.find("common", cwd=repo_dir)[0] == s.resolve()  # the shadow refusal is a switch-on rule
    assert [p.name for p in library.procedures(cwd=repo_dir)] == ["common"]


@needs_git
def test_a_name_in_neither_root_is_not_found_with_the_switch_off(shipped: Path, repo_dir: Path) -> None:
    with pytest.raises(LibraryError) as err:
        library.find("nothing", cwd=repo_dir)
    assert err.value.reason == "not_found"


def test_the_switch_off_outside_a_repo_is_not_found(shipped: Path, tmp_path: Path) -> None:
    with pytest.raises(LibraryError) as err:
        library.find("mine", cwd=tmp_path)
    assert err.value.reason == "not_found"


# -- one probe per run --------------------------------------------------------


def _boom(cwd: object) -> RepoProbe | None:
    raise AssertionError("probe_repo must not run when the caller passed the probe")


@needs_git
def test_a_probe_the_caller_passes_is_used_and_probe_repo_is_not_run(
    shipped: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = _write(repo / ".nexus" / "procedures", "mine")
    monkeypatch.setattr(library, "probe_repo", _boom)
    probe = RepoProbe(repo.resolve(), "myrepo")
    assert library.find("mine", cwd=repo, probe=probe)[0] == mine.resolve()


def test_a_probe_of_none_means_no_repo_root(
    shipped: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    monkeypatch.setattr(library, "probe_repo", _boom)
    with pytest.raises(LibraryError) as err:
        library.find("mine", cwd=tmp_path, probe=None)
    assert err.value.reason == "not_found"


@needs_git
def test_a_passed_probe_tells_a_repo_root_off_name_without_probing(
    shipped: Path, repo_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(repo_dir / ".nexus" / "procedures", "mine")
    monkeypatch.setattr(library, "probe_repo", _boom)
    with pytest.raises(LibraryError) as err:
        library.find("mine", cwd=repo_dir, probe=RepoProbe(repo_dir.resolve(), "myrepo"))
    assert err.value.reason == "repo_root_off"


def _probe_fails(cwd: object) -> RepoProbe | None:
    raise RepoProbeError("git did not run")


def test_a_failing_probe_with_the_root_off_reads_a_mistyped_name_as_not_found(
    shipped: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(library, "probe_repo", _probe_fails)
    with pytest.raises(LibraryError) as err:
        library.find("typo", cwd=tmp_path)
    assert err.value.reason == "not_found"


def test_a_failing_probe_is_not_needed_for_a_shipped_name_with_the_root_off(
    shipped: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = _write(shipped, "common")
    monkeypatch.setattr(library, "probe_repo", _probe_fails)
    assert library.find("common", cwd=tmp_path)[0] == s.resolve()


def test_a_failing_probe_with_the_root_on_propagates(
    shipped: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    monkeypatch.setattr(library, "probe_repo", _probe_fails)
    with pytest.raises(RepoProbeError):
        library.find("typo", cwd=tmp_path)


# -- a bounded read ----------------------------------------------------------


def test_a_file_over_the_source_bound_is_read_only_up_to_one_byte_past_it(shipped: Path) -> None:
    from nexus.lua_runtime import static_check

    _write(shipped, "big", _proc("big") + b"-- " + b"x" * (static_check.MAX_SOURCE * 2) + b"\n")
    _, data = library.find("big", cwd=None)
    assert len(data) == static_check.MAX_SOURCE + 1  # the static check's `size` rule refuses it


# -- the listing -------------------------------------------------------------


def test_the_listing_is_sorted_and_carries_each_summary_and_root(shipped: Path) -> None:
    _write(shipped, "zeta", _proc("zeta", "last one"))
    _write(shipped, "alpha", _proc("alpha", "first one"))
    assert library.procedures(cwd=None) == [
        library.Procedure("alpha", "first one", "shipped"),
        library.Procedure("zeta", "last one", "shipped"),
    ]


@needs_git
def test_the_listing_joins_both_roots_and_hides_a_shadowing_repo_file(shipped: Path, repo: Path) -> None:
    _write(shipped, "common", _proc("common", "shipped common"))
    root = repo / ".nexus" / "procedures"
    _write(root, "common", _proc("common", "shadow"))
    _write(root, "mine", _proc("mine", "repo mine"))
    assert library.procedures(cwd=repo) == [
        library.Procedure("common", "shipped common", "shipped"),
        library.Procedure("mine", "repo mine", "repo"),
    ]


def test_a_file_with_a_bad_header_is_listed_with_the_refusal_not_dropped(shipped: Path) -> None:
    _write(shipped, "bad", b"return 1\n")
    (entry,) = library.procedures(cwd=None)
    assert entry.name == "bad" and entry.root == "shipped"
    assert entry.summary.startswith("header refused: ")
    assert "line 1" in entry.summary


def test_a_file_whose_name_breaks_the_rule_is_not_listed(shipped: Path) -> None:
    _write(shipped, "good")
    (shipped / "Bad-Name.lua").write_bytes(_proc("Bad-Name"))
    (shipped / "notes.txt").write_text("x")
    assert [p.name for p in library.procedures(cwd=None)] == ["good"]


def test_a_listed_link_out_of_the_root_is_not_listed(shipped: Path, tmp_path: Path) -> None:
    outside = tmp_path / "o.lua"
    outside.write_bytes(_proc("o"))
    (shipped / "o.lua").symlink_to(outside)
    assert library.procedures(cwd=None) == []


def test_the_real_listing_names_the_shipped_procedures() -> None:
    names = {p.name for p in library.procedures(cwd=None)}
    assert {"research_add", "gate_record"} <= names


def test_the_read_bound_is_the_static_checks_source_bound_plus_one() -> None:
    from nexus.lua_runtime import static_check

    assert library.READ_LIMIT == static_check.MAX_SOURCE + 1
