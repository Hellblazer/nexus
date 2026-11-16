# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loading auras from the two library roots (RDR-249 Technical Design 1; bead nexus-mdbds.10.1).

The loader reuses RDR-242's library: the roots, the name rule, the shadow refusal and the
repo-root switch are ``nexus.lua_runtime.library``'s, reached through a suffix, never copied.
Both roots are tmp dirs here; nothing writes into ``src/nexus/procedures/``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from nexus import auras
from nexus.auras import AuraError
from nexus.lua_runtime import library
from nexus.lua_runtime.library import LibraryError

from ._support import TD1_EXAMPLE, minimal

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is a hard dependency of nx")


def _write(root: Path, name: str, text: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.aura.toml"
    path.write_text(minimal(name) if text is None else text, encoding="utf-8")
    return path


def _git(cwd: Path, *argv: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", *argv], cwd=cwd, env=env, check=True, capture_output=True, timeout=60)


@pytest.fixture
def shipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shipped"
    root.mkdir()
    monkeypatch.setattr(library, "shipped_root", lambda: root)
    return root


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
    """A repo with the repo-root switch ON."""
    monkeypatch.setenv(library.REPO_ROOT_ENV, "1")
    return repo_dir


def _repo_root(repo: Path) -> Path:
    return repo / ".nexus" / "procedures"


# -- the shipped root -------------------------------------------------------------


def test_a_shipped_aura_loads_and_round_trips(shipped: Path) -> None:
    _write(shipped, "ci_develop_red", TD1_EXAMPLE)
    aura = auras.load_aura("ci_develop_red", cwd=None)
    assert aura.name == "ci_develop_red"
    assert aura.procedure == "ci_red_notice"
    assert aura.subspace == "board/ci/nexus-develop--done"


def test_a_name_in_neither_root_is_not_found_naming_it(shipped: Path) -> None:
    with pytest.raises(LibraryError) as err:
        auras.load_aura("ghost", cwd=None)
    assert err.value.reason == "not_found"
    assert "ghost" in str(err.value)
    assert "aura" in str(err.value)


@pytest.mark.parametrize("name", ["Bad", "con", "x.aura", "a/b", "../x", ""])
def test_a_name_that_breaks_the_rule_is_refused_before_any_path_is_built(shipped: Path, name: str) -> None:
    with pytest.raises(LibraryError) as err:
        auras.load_aura(name, cwd=None)
    assert err.value.reason == "invalid_name"


def test_the_stem_of_a_double_suffix_is_not_the_name(shipped: Path) -> None:
    # Path("x.aura.toml").stem is "x.aura": the loader and the listing strip the whole suffix.
    _write(shipped, "x")
    assert auras.load_aura("x", cwd=None).name == "x"
    assert [e.name for e in auras.list_auras(cwd=None)] == ["x"]


def test_only_a_file_whose_entry_is_exactly_the_name_is_found(shipped: Path) -> None:
    # A case-insensitive filesystem opens foo.aura.toml as Foo.aura.toml; the listing shows
    # Foo only, so load must not return a file the listing does not show under that name.
    (shipped / "Foo.aura.toml").write_text(minimal("foo"), encoding="utf-8")
    with pytest.raises(LibraryError) as err:
        auras.load_aura("foo", cwd=None)
    assert err.value.reason == "not_found"
    assert auras.list_auras(cwd=None) == []


def test_a_directory_named_like_an_aura_is_not_an_aura(shipped: Path) -> None:
    (shipped / "dir.aura.toml").mkdir()
    with pytest.raises(LibraryError) as err:
        auras.load_aura("dir", cwd=None)
    assert err.value.reason == "not_found"


def test_a_file_that_breaks_the_grammar_is_a_typed_refusal_naming_file_and_key(shipped: Path) -> None:
    _write(shipped, "bad", minimal("bad").replace('threads = "a block"', 'threads = "a block"\nchannel = "yes"'))
    with pytest.raises(AuraError) as err:
        auras.load_aura("bad", cwd=None)
    assert (err.value.kind, err.value.file, err.value.key) == ("wrong_type", "bad.aura.toml", "display.channel")


def test_the_file_name_is_what_the_aura_key_is_checked_against(shipped: Path) -> None:
    _write(shipped, "one", minimal("two"))
    with pytest.raises(AuraError) as err:
        auras.load_aura("one", cwd=None)
    assert err.value.kind == "name_mismatch"


@needs_git
def test_the_suffix_is_part_of_the_entry_so_a_procedure_and_an_aura_never_shadow_each_other(
    shipped: Path, repo: Path
) -> None:
    # foo.lua is not foo.aura.toml: a repo procedure named like a shipped aura is no shadow.
    _write(shipped, "foo")
    (_repo_root(repo)).mkdir(parents=True)
    (_repo_root(repo) / "foo.lua").write_text("-- procedure: foo\n", encoding="utf-8")
    assert auras.load_aura("foo", cwd=repo).name == "foo"


# -- the repo root ----------------------------------------------------------------


@needs_git
def test_a_repo_aura_loads_with_the_switch_on(shipped: Path, repo: Path) -> None:
    _write(_repo_root(repo), "mine")
    assert auras.load_aura("mine", cwd=repo).name == "mine"


@needs_git
def test_a_repo_aura_is_found_from_a_subdirectory(shipped: Path, repo: Path) -> None:
    _write(_repo_root(repo), "mine")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert auras.load_aura("mine", cwd=sub).name == "mine"


@needs_git
def test_a_repo_aura_is_refused_while_the_repo_root_is_off(shipped: Path, repo_dir: Path) -> None:
    _write(_repo_root(repo_dir), "mine")
    with pytest.raises(LibraryError) as err:
        auras.load_aura("mine", cwd=repo_dir)
    assert err.value.reason == "repo_root_off"
    assert library.REPO_ROOT_ENV in str(err.value)


@needs_git
def test_a_repo_aura_that_shadows_a_shipped_aura_is_refused(shipped: Path, repo: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo), "common")
    with pytest.raises(LibraryError) as err:
        auras.load_aura("common", cwd=repo)
    assert err.value.reason == "shadows_shipped"
    assert "common" in str(err.value)


@needs_git
def test_a_shipped_aura_still_loads_beside_a_repo_file_while_the_root_is_off(shipped: Path, repo_dir: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo_dir), "common", minimal("common").replace("does a thing", "SHADOW"))
    assert auras.load_aura("common", cwd=repo_dir).summary == "does a thing"


@needs_git
def test_a_repo_root_that_is_a_link_out_of_the_repo_is_refused(shipped: Path, repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write(outside, "mine")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "procedures").symlink_to(outside, target_is_directory=True)
    with pytest.raises(LibraryError) as err:
        auras.load_aura("mine", cwd=repo)
    assert err.value.reason == "outside_root"


# -- the listing --------------------------------------------------------------------


def test_the_listing_is_sorted_and_carries_each_summary_and_root(shipped: Path) -> None:
    _write(shipped, "zeta", minimal("zeta").replace("does a thing", "last one"))
    _write(shipped, "alpha", minimal("alpha").replace("does a thing", "first one"))
    assert auras.list_auras(cwd=None) == [
        library.Procedure("alpha", "first one", "shipped"),
        library.Procedure("zeta", "last one", "shipped"),
    ]


@needs_git
def test_the_listing_joins_both_roots_and_hides_a_shadowing_repo_file(shipped: Path, repo: Path) -> None:
    _write(shipped, "common", minimal("common").replace("does a thing", "shipped common"))
    _write(_repo_root(repo), "common", minimal("common").replace("does a thing", "shadow"))
    _write(_repo_root(repo), "mine", minimal("mine").replace("does a thing", "repo mine"))
    assert auras.list_auras(cwd=repo) == [
        library.Procedure("common", "shipped common", "shipped"),
        library.Procedure("mine", "repo mine", "repo"),
    ]


@needs_git
def test_the_repo_root_is_not_listed_while_it_is_off(shipped: Path, repo_dir: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo_dir), "mine")
    assert [e.name for e in auras.list_auras(cwd=repo_dir)] == ["common"]


def test_a_file_that_breaks_the_grammar_is_listed_with_the_refusal_not_dropped(shipped: Path) -> None:
    _write(shipped, "bad", 'zzz = "x"\n')
    [entry] = auras.list_auras(cwd=None)
    assert entry.name == "bad"
    assert entry.summary.startswith("refused: ")
    assert "zzz" in entry.summary


def test_a_file_whose_name_breaks_the_rule_is_not_listed(shipped: Path) -> None:
    (shipped / "Bad-Name.aura.toml").write_text(minimal("Bad-Name"), encoding="utf-8")
    (shipped / "con.aura.toml").write_text(minimal("con"), encoding="utf-8")
    assert auras.list_auras(cwd=None) == []


def test_the_listing_shows_auras_only_and_the_procedure_listing_shows_procedures_only(shipped: Path) -> None:
    _write(shipped, "an_aura")
    (shipped / "a_proc.lua").write_text(
        "-- procedure: a_proc\n-- summary: a proc\n-- args: none\n-- grants: none\nreturn 1\n", encoding="utf-8"
    )
    assert [e.name for e in auras.list_auras(cwd=None)] == ["an_aura"]
    assert [p.name for p in library.procedures(cwd=None)] == ["a_proc"]


def test_the_real_shipped_root_lists_without_failing() -> None:
    entries = auras.list_auras(cwd=None)
    assert "ci_develop_red" in {e.name for e in entries}  # the first shipped aura (nexus-mdbds.12.1)
    assert all(isinstance(e, library.Procedure) and e.root == "shipped" for e in entries)


def test_a_file_nested_too_deeply_is_refused_by_load_and_listed_as_refused(shipped: Path) -> None:
    depth = 30_000
    _write(shipped, "deep", "a = " + "[" * depth + "]" * depth)
    _write(shipped, "fine")
    with pytest.raises(AuraError) as err:
        auras.load_aura("deep", cwd=None)
    assert (err.value.kind, err.value.file) == ("bad_toml", "deep.aura.toml")
    listing = {e.name: e.summary for e in auras.list_auras(cwd=None)}
    assert listing["deep"].startswith("refused: ")
    assert listing["fine"] == "does a thing"


def test_a_file_over_the_read_bound_is_refused_even_when_what_was_read_parses(shipped: Path) -> None:
    # The library reads READ_LIMIT bytes; a file longer than that reaches the parser with
    # exactly READ_LIMIT. Here the first READ_LIMIT bytes are a valid aura and the [clear]
    # table (which would refuse the file) comes after the cut, so only the size check refuses.
    base = minimal("big")
    head = base + "#" * (library.READ_LIMIT - len(base) - 1) + "\n"
    assert len(head.encode()) == library.READ_LIMIT
    _write(shipped, "big", head + "[clear]\nmatch = {}\n")
    with pytest.raises(AuraError) as err:
        auras.load_aura("big", cwd=None)
    assert err.value.kind == "too_large"


# -- the structured enumeration (nexus-mdbds.10.2: what nx aura check walks) -------------------------


def _by_key(entries: list[auras.AuraEntry]) -> dict[tuple[str, str], object]:
    return {(e.name, e.root): e.result for e in entries}


def test_the_enumeration_yields_an_aura_a_grammar_refusal_and_a_name_refusal_per_file(shipped: Path) -> None:
    _write(shipped, "good")
    _write(shipped, "bad", 'zzz = "x"\n')
    (shipped / "Bad-Name.aura.toml").write_text(minimal("Bad-Name"), encoding="utf-8")
    got = _by_key(auras.enumerate_auras(cwd=None))
    assert isinstance(got[("good", "shipped")], auras.Aura)
    assert isinstance(got[("bad", "shipped")], AuraError)
    refused = got[("Bad-Name", "shipped")]
    assert isinstance(refused, LibraryError) and refused.reason == "invalid_name"


@needs_git
def test_the_enumeration_keeps_a_repo_file_that_shadows_a_shipped_aura(shipped: Path, repo: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo), "common")
    _write(_repo_root(repo), "mine")
    got = _by_key(auras.enumerate_auras(cwd=repo))
    assert isinstance(got[("common", "shipped")], auras.Aura)
    shadow = got[("common", "repo")]
    assert isinstance(shadow, LibraryError) and shadow.reason == "shadows_shipped"
    assert isinstance(got[("mine", "repo")], auras.Aura)


@needs_git
def test_the_enumeration_does_not_walk_the_repo_root_while_it_is_off(shipped: Path, repo_dir: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo_dir), "mine")
    assert [(e.name, e.root) for e in auras.enumerate_auras(cwd=repo_dir)] == [("common", "shipped")]


@needs_git
def test_an_enumerated_file_that_leaves_its_root_is_a_library_refusal_not_dropped(
    shipped: Path, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    _write(outside, "mine")
    _write(_repo_root(repo), "other")
    (_repo_root(repo) / "mine.aura.toml").symlink_to(outside / "mine.aura.toml")
    got = _by_key(auras.enumerate_auras(cwd=repo))
    escaped = got[("mine", "repo")]
    assert isinstance(escaped, LibraryError) and escaped.reason == "outside_root"
    assert isinstance(got[("other", "repo")], auras.Aura)


def test_the_enumeration_is_sorted_by_name_then_root(shipped: Path) -> None:
    for name in ("zeta", "alpha", "mid"):
        _write(shipped, name)
    assert [e.name for e in auras.enumerate_auras(cwd=None)] == ["alpha", "mid", "zeta"]


def test_the_procedure_listing_still_hides_what_the_enumeration_shows(shipped: Path) -> None:
    # .lua behavior is unchanged: library.procedures drops a name that breaks the rule silently.
    (shipped / "Bad-Name.lua").write_text("-- procedure: x\n", encoding="utf-8")
    assert library.procedures(cwd=None) == []
    assert [e.name for e in library.scan_files(cwd=None, suffix=".lua", noun="procedure")] == ["Bad-Name"]


# -- review fixes (nexus-mdbds.10.2) -------------------------------------------------------------------------------


@needs_git
def test_one_name_in_both_roots_sorts_shipped_then_repo(shipped: Path, repo: Path) -> None:
    _write(shipped, "common")
    _write(_repo_root(repo), "common")
    _write(shipped, "aaa")
    assert [(s.name, s.root) for s in library.scan_files(cwd=repo, suffix=".aura.toml", noun="aura")] == [
        ("aaa", "shipped"), ("common", "shipped"), ("common", "repo"),
    ]
    assert [(e.name, e.root) for e in auras.enumerate_auras(cwd=repo)] == [
        ("aaa", "shipped"), ("common", "shipped"), ("common", "repo"),
    ]


def test_a_directory_named_like_an_aura_is_reported_not_dropped(shipped: Path) -> None:
    (shipped / "dir.aura.toml").mkdir()
    [entry] = auras.enumerate_auras(cwd=None)
    assert (entry.name, entry.root) == ("dir", "shipped")
    assert isinstance(entry.result, LibraryError) and entry.result.reason == "unreadable"
    assert "not a regular file" in entry.result.detail


def test_a_dangling_link_named_like_an_aura_is_reported_not_dropped(shipped: Path, tmp_path: Path) -> None:
    (shipped / "gone.aura.toml").symlink_to(tmp_path / "nowhere.aura.toml")
    [entry] = auras.enumerate_auras(cwd=None)
    assert entry.name == "gone"
    assert isinstance(entry.result, LibraryError) and entry.result.reason == "unreadable"


def test_the_lua_listing_still_ignores_such_entries(shipped: Path) -> None:
    (shipped / "dir.lua").mkdir()
    assert library.procedures(cwd=None) == []
