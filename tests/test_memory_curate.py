# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx memory curate --files --t2 --agents`` and the S-10 memory-directory mapping
(RDR-238 Phase 3 Step 2, first part, nexus-qo7ie.17).

Every check has a planted case that must be reported and a clean case that must not, so a
check that reports nothing at all, or everything, fails one of the pair. The S-10 mapping and
the file scan are stdlib-only modules under ``nexus._hook_runtime`` (the future SessionStart
verb imports them); they run against real tmp git repositories and tmp directories. The
``--t2`` tests write to the self-provisioned test engine (``db``), never the live store.
"""
from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from nexus import memory_curate as mc
from nexus._hook_runtime import memory_dir as md
from nexus._hook_runtime import memory_scan as ms
from nexus.cli import main
from nexus.commands import command_context
from nexus.db.t2 import T2Database
from tests._child_encoding import PYTHON_CHILD_ENCODING
from tests._t2_fixture_ops import backdate_memory
from tests.hooks.test_hook_runtime_thin import _package_modules

_GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *_GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True,
        encoding=PYTHON_CHILD_ENCODING,
    ).stdout.strip()


def _make_repo(tmp_path: Path, name: str = "curaterepo") -> Path:
    repo = (tmp_path / name).resolve()
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _memdir(config: Path, repo: Path) -> Path:
    d = config / "projects" / md.mangle_path(repo) / "memory"
    d.mkdir(parents=True)
    return d


# ── S-10: the memory-directory mapping ───────────────────────────────────────────

def test_mangle_replaces_every_non_alphanumeric_character_with_a_dash() -> None:
    assert md.mangle_path("/Users/a.b/git/my-repo_x") == "-Users-a-b-git-my-repo-x"
    assert md.mangle_path(Path("/tmp/plain")) == "-tmp-plain"
    assert md.mangle_path("C:\\Users\\me\\r") == "C--Users-me-r"
    # clean: letters and digits survive untouched
    assert md.mangle_path("abc123XYZ") == "abc123XYZ"


def test_the_session_context_block_uses_the_one_directory_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``command_context`` asks :func:`memory_dir`, not its own cwd and ``~/.claude`` (one rule, one place)."""
    src = inspect.getsource(command_context._feedback_memories_block)
    assert "memory_dir(" in src and "Path.home()" not in src and "re.sub(" not in src
    # behaviour: a linked worktree and a relocated config dir find the PRIMARY's directory
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt-ctx"
    _git(repo, "worktree", "add", "-q", "-b", "ctx", str(wt))
    config = tmp_path / "cfg"
    mem = _memdir(config, repo)
    (mem / "feedback_one.md").write_text("x")
    (mem / "other.md").write_text("x")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_COMMON_DIR", raising=False)
    got = command_context._feedback_memories_block(wt)
    assert got[1:] == ["- feedback_one.md"], got
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))  # no directory there
    assert "(none" in command_context._feedback_memories_block(wt)[1]


def test_primary_checkout_maps_to_its_projects_directory(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    config = tmp_path / "cfg"
    want = _memdir(config, repo)
    got = md.memory_dir(repo, env={"CLAUDE_CONFIG_DIR": str(config)}, home=tmp_path / "nohome")
    assert got == want


def test_a_linked_worktree_resolves_to_its_primarys_directory(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt-one"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    config = tmp_path / "cfg"
    want = _memdir(config, repo)  # keyed on the PRIMARY path only
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    assert md.memory_dir(wt, env=env, home=tmp_path / "nohome") == want
    # and a subdirectory of the worktree does too
    (wt / "sub").mkdir()
    assert md.memory_dir(wt / "sub", env=env, home=tmp_path / "nohome") == want


def test_a_relocated_config_dir_is_honoured_and_default_is_home_dot_claude(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    home = tmp_path / "home"
    default_dir = _memdir(home / ".claude", repo)
    assert md.memory_dir(repo, env={}, home=home) == default_dir
    # relocated: CLAUDE_CONFIG_DIR wins over the default, and the default is then NOT consulted
    moved = tmp_path / "elsewhere"
    moved_dir = _memdir(moved, repo)
    assert md.memory_dir(repo, env={"CLAUDE_CONFIG_DIR": str(moved)}, home=home) == moved_dir
    only_default = tmp_path / "empty-cfg"
    only_default.mkdir()
    assert md.memory_dir(repo, env={"CLAUDE_CONFIG_DIR": str(only_default)}, home=home) is None


def test_a_custom_memory_path_from_settings_overrides_the_mangled_directory(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    config = tmp_path / "cfg"
    _memdir(config, repo)  # the default exists too: the setting must win
    home = tmp_path / "home"
    custom = tmp_path / "my memory"
    custom.mkdir()
    env = {"CLAUDE_CONFIG_DIR": str(config)}

    (config).mkdir(exist_ok=True)
    (config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(custom)}))
    assert md.memory_dir(repo, env=env, home=home) == custom  # user settings

    # project settings beat user settings, local beats project
    proj = tmp_path / "proj-mem"
    proj.mkdir()
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(proj)}))
    assert md.memory_dir(repo, env=env, home=home) == proj
    loc = tmp_path / "local-mem"
    loc.mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryDirectory": str(loc)}))
    assert md.memory_dir(repo, env=env, home=home) == loc


def test_a_custom_path_expands_tilde_and_a_bad_setting_falls_back(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    config = tmp_path / "cfg"
    default = _memdir(config, repo)
    home = tmp_path / "home"
    (home / "mem").mkdir(parents=True)
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    (config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/mem"}))
    assert md.memory_dir(repo, env=env, home=home) == home / "mem"
    # a relative path, a non-string, malformed JSON, or a directory that is absent: ignored
    for bad in ('{"autoMemoryDirectory": "relative/dir"}', '{"autoMemoryDirectory": 7}',
                "{not json", '{"autoMemoryDirectory": "/no/such/dir/at/all"}'):
        (config / "settings.json").write_text(bad)
        assert md.memory_dir(repo, env=env, home=home) == default, bad


def test_a_relative_custom_path_is_ignored_even_when_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The relative-path refusal must not lean on is_dir(): here the relative directory
    # really exists under the cwd, so only the refusal keeps it out.
    repo = _make_repo(tmp_path)
    config = tmp_path / "cfg"
    default = _memdir(config, repo)
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    (tmp_path / "relmem").mkdir()
    monkeypatch.chdir(tmp_path)
    (config / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "relmem"}))
    assert md.memory_dir(repo, env=env, home=tmp_path / "home") == default


def test_a_missing_directory_or_a_non_repository_returns_none(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}  # no projects/<key>/memory beneath it
    assert md.memory_dir(repo, env=env, home=tmp_path / "h") is None
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert md.memory_dir(plain, env=env, home=tmp_path / "h") is None
    assert md.memory_dir(tmp_path / "does-not-exist", env=env, home=tmp_path / "h") is None


def _linked_worktree(tmp_path: Path, repo: Path, name: str = "wt-link") -> Path:
    wt = tmp_path / name
    _git(repo, "worktree", "add", "-q", "-b", name, str(wt))
    return wt


def _custom_dir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


@pytest.mark.parametrize("where", ["worktree", "primary"])
@pytest.mark.parametrize("filename", ["settings.local.json", "settings.json"])
def test_each_project_settings_source_decides_alone_from_a_linked_worktree(
    tmp_path: Path, where: str, filename: str,
) -> None:
    """``settings.local.json`` is untracked, so it lives ONLY in the primary; a tracked
    ``settings.json`` is in both. Every one of the four lookups must work with cwd != primary."""
    repo = _make_repo(tmp_path)
    wt = _linked_worktree(tmp_path, repo)
    config = tmp_path / "cfg"
    default = _memdir(config, repo)  # the default exists too: the setting must win
    custom = _custom_dir(tmp_path, "custom-mem")
    base = wt if where == "worktree" else repo
    (base / ".claude").mkdir(exist_ok=True)
    (base / ".claude" / filename).write_text(json.dumps({"autoMemoryDirectory": str(custom)}))
    env = {"CLAUDE_CONFIG_DIR": str(config)}
    assert md.memory_dir(wt, env=env, home=tmp_path / "h") == custom
    # clean: the same worktree without the setting falls back to the primary's default directory
    (base / ".claude" / filename).unlink()
    assert md.memory_dir(wt, env=env, home=tmp_path / "h") == default


def test_a_worktrees_own_settings_beat_the_primarys(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    wt = _linked_worktree(tmp_path, repo)
    config = tmp_path / "cfg"
    _memdir(config, repo)
    mine, theirs = _custom_dir(tmp_path, "mine"), _custom_dir(tmp_path, "theirs")
    (wt / ".claude").mkdir()
    (repo / ".claude").mkdir()
    (wt / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(mine)}))
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryDirectory": str(theirs)}))
    assert md.memory_dir(wt, env={"CLAUDE_CONFIG_DIR": str(config)}, home=tmp_path / "h") == mine


def test_the_read_agrees_with_git_on_every_layout(tmp_path: Path) -> None:
    """``primary_checkout_root`` reads ``.git``; ``_primary_via_git`` asks git. They must agree."""
    repo = _make_repo(tmp_path)
    (repo / "sub").mkdir()
    wt = _linked_worktree(tmp_path, repo)
    (wt / "sub").mkdir()
    link = tmp_path / "link-to-repo"
    link.symlink_to(repo, target_is_directory=True)
    work = tmp_path / "sepwork"
    sep = tmp_path / "sepgit"
    _git(tmp_path, "init", "-q", "--separate-git-dir", str(sep), str(work))
    for cwd in (repo, repo / "sub", wt, wt / "sub", link / "sub", work):
        by_reading = md.primary_checkout_root(cwd, {})
        assert by_reading is not None, cwd
        assert by_reading == md._primary_via_git(cwd), cwd
    assert md.primary_checkout_root(repo / "sub", {}) == repo
    assert md.primary_checkout_root(wt, {}) == repo
    assert md.primary_checkout_root(work, {}) == tmp_path  # a separate git dir is its own common directory
    assert md.primary_checkout_root(tmp_path / "not-a-repo", {}) is None


def test_the_read_does_not_ask_git_but_the_fallback_cases_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path)
    wt = _linked_worktree(tmp_path, repo)
    asked: list[Path] = []
    monkeypatch.setattr(md, "_primary_via_git", lambda cwd: asked.append(cwd) or Path("/via/git"))
    for cwd in (repo, repo, wt):
        assert md.primary_checkout_root(cwd, {}) == repo
    assert md.primary_checkout_root(tmp_path / "plain", {}) is None  # no .git above: not a repository
    assert asked == [], "a recognised layout is read, never asked of git"
    # GIT_DIR / GIT_COMMON_DIR redirect git away from the layout on disk: ask git
    assert md.primary_checkout_root(repo, {"GIT_DIR": str(repo / ".git")}) == Path("/via/git")
    assert md.primary_checkout_root(repo, {"GIT_COMMON_DIR": str(repo / ".git")}) == Path("/via/git")
    assert len(asked) == 2
    # an unrecognised layout: ask git
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text("this is not a gitdir line\n")
    dangling = tmp_path / "dangling"
    dangling.mkdir()
    (dangling / ".git").write_text(f"gitdir: {tmp_path / 'nowhere'}\n")
    headless = tmp_path / "headless"
    (headless / ".git").mkdir(parents=True)  # a .git directory that is not a git directory (no HEAD)
    badcommon = tmp_path / "badcommon"
    badcommon.mkdir()
    gitdir = tmp_path / "badcommon-gitdir"
    gitdir.mkdir()
    (gitdir / "commondir").write_text("../no-such-common\n")
    (badcommon / ".git").write_text(f"gitdir: {gitdir}\n")
    for cwd in (broken, dangling, headless, badcommon):
        asked.clear()
        assert md.primary_checkout_root(cwd, {}) == Path("/via/git"), cwd
        assert asked == [cwd], cwd


def test_the_fallback_still_answers_correctly_under_git_dir(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    wt = _linked_worktree(tmp_path, repo)
    assert md.primary_checkout_root(repo, {"GIT_DIR": str(repo / ".git")}) == repo
    assert md.primary_checkout_root(wt, {"GIT_COMMON_DIR": str(repo / ".git")}) == repo


_CALL_PROBE = (
    "import json, sys\n"
    "from pathlib import Path\n"
    "from nexus._hook_runtime import memory_dir as md\n"
    "home = Path(sys.argv[1])\n"
    "out = {}\n"
    "for key, cwd in (('primary', sys.argv[2]), ('worktree', sys.argv[3]), ('subdir', sys.argv[4])):\n"
    "    got = md.memory_dir(Path(cwd), home=home)\n"
    "    out[key] = None if got is None else str(got)\n"
    "heavy = ('structlog', 'rich', 'nexus.bounded_subprocess', 'nexus.util.nx_argv')\n"
    "out['loaded'] = sorted(m for m in heavy if m in sys.modules)\n"
    "print(json.dumps(out))\n"
)


def _call_probe(tmp_path: Path, repo: Path, wt: Path, config: Path, **extra_env: str) -> dict[str, object]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["CLAUDE_CONFIG_DIR"] = str(config)
    env.update(extra_env)
    proc = subprocess.run(
        [sys.executable, "-c", _CALL_PROBE, str(tmp_path / "nohome"), str(repo), str(wt), str(wt / "sub")],
        capture_output=True, text=True, encoding=PYTHON_CHILD_ENCODING, check=False, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_memory_dir_is_stdlib_only_at_call_time_in_a_fresh_interpreter(tmp_path: Path) -> None:
    """The SessionStart verb calls memory_dir() on every session start. Importing the module was
    already cheap; the CALL used to spawn git through run_bounded and load structlog, rich and ~230
    modules (about 55 ms). A real repository and a real linked worktree, a fresh interpreter."""
    repo = _make_repo(tmp_path)
    wt = _linked_worktree(tmp_path, repo)
    (wt / "sub").mkdir()
    config = tmp_path / "cfg"
    want = _memdir(config, repo)
    got = _call_probe(tmp_path, repo, wt, config)
    assert got["primary"] == str(want) and got["worktree"] == str(want) and got["subdir"] == str(want), got
    assert got["loaded"] == [], f"memory_dir() loaded heavy modules at call time: {got['loaded']}"


def test_the_call_time_probe_can_see_a_leak_the_fallback_loads_run_bounded(tmp_path: Path) -> None:
    """Positive control for the probe above: with GIT_DIR set the fallback runs, and the probe sees it."""
    repo = _make_repo(tmp_path)
    wt = _linked_worktree(tmp_path, repo)
    (wt / "sub").mkdir()
    config = tmp_path / "cfg"
    want = _memdir(config, repo)
    got = _call_probe(tmp_path, repo, wt, config, GIT_DIR=str(repo / ".git"))
    assert got["primary"] == str(want), got  # the fallback answers too
    assert "nexus.bounded_subprocess" in got["loaded"], got


# ── the S-10 and scan modules stay hook-thin ─────────────────────────────────────

def test_the_s10_modules_are_covered_by_the_thin_hook_census_and_import_cheaply() -> None:
    names = {p.name for p in _package_modules()}
    assert {"memory_dir.py", "memory_scan.py"} <= names, (
        "the thin-hook census must list the new modules, or its AST check would not cover them"
    )
    probe = (
        "import sys; import nexus._hook_runtime.memory_dir, nexus._hook_runtime.memory_scan; "
        "print('\\n'.join(m for m in ('structlog','rich','click','yaml','httpx','nexus.session',"
        "'nexus.hooks','nexus.cli','nexus.db','nexus.config') if m in sys.modules))"
    )
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          encoding=PYTHON_CHILD_ENCODING, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", f"hook-thin import leaked: {proc.stdout}"


# ── --files ──────────────────────────────────────────────────────────────────────

def _w(d: Path, name: str, text: str = "x\n") -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def _fm(name: str, desc: str, extra: str = "") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n{extra}---\nbody\n"


def _planted_dir(tmp_path: Path) -> Path:
    d = tmp_path / "mem-planted"
    d.mkdir()
    _w(d, "MEMORY.md", "- [Kept](kept.md) - indexed\n- [Gone](gone.md) - the file is missing\n"
                       + "pad line\n" * 5)
    _w(d, "MEMORY-ondemand.md", "- [Later](later.md) - on demand\n")
    _w(d, "kept.md", _fm("Kept", "small indexed file"))
    _w(d, "later.md", _fm("Later", "named only in the on-demand index"))
    _w(d, "stray.md", _fm("Stray", "named in no index"))
    _w(d, "huge.md", "---\nname: Huge\n---\n" + "z" * (ms.OVERSIZE_BYTES + 1))
    _w(d, "new_one.md", _fm("New one", "the replacement", "replaces: old_one\n"))
    _w(d, "old_one.md", _fm("Old one", "should have been deleted"))
    return d


def _clean_dir(tmp_path: Path) -> Path:
    d = tmp_path / "mem-clean"
    d.mkdir()
    # the index files may exceed 8 KB without being reported (the carve-out is for size only)
    _w(d, "MEMORY.md", "- [A](a.md) - hook\n" + ("# filler\n" * 1500))
    _w(d, "MEMORY-ondemand.md", "- [B](b.md) - hook\n")
    _w(d, "a.md", _fm("A", "alpha"))
    _w(d, "b.md", _fm("B", "beta", "replaces: ghost\n"))  # target absent: nothing to report
    return d


def test_files_scan_reports_each_planted_defect(tmp_path: Path) -> None:
    scan = ms.scan(_planted_dir(tmp_path))
    assert scan.unindexed == ["huge.md", "new_one.md", "old_one.md", "stray.md"]
    assert scan.oversized == [("huge.md", ms.OVERSIZE_BYTES + 1 + len("---\nname: Huge\n---\n"))]
    assert scan.dangling == [("MEMORY.md", "gone.md")]
    assert scan.replaced_present == [("new_one.md", "old_one", "old_one.md")]
    assert scan.index_files == ["MEMORY-ondemand.md", "MEMORY.md"]


def test_a_file_linked_from_an_indexed_hub_is_indexed_one_level_only(tmp_path: Path) -> None:
    # Sam, 2026-11-05 (nexus-qo7ie.29): one hub level counts as indexed.
    d = tmp_path / "mem-hub"
    d.mkdir()
    _w(d, "MEMORY.md", "- [Facts](hub.md) - index of technical facts\n- prose hub: hub2.md\n")
    _w(d, "hub.md", _fm("Facts", "hub") + "- [Child](child.md)\n- see prose_only.md\n"
                    "- [Archived](archive/elsewhere.md)\n- [Up](../up.md)\n")
    _w(d, "hub2.md", _fm("Hub two", "named by an index in prose") + "- [Dot](./dot_child.md)\n")
    _w(d, "dot_child.md", _fm("Dot child", "linked as ./dot_child.md from a prose-named hub"))
    # same base names as the hub's subdirectory and parent links, but not their targets
    _w(d, "elsewhere.md", _fm("Elsewhere", "a top-level file, not archive/elsewhere.md"))
    _w(d, "up.md", _fm("Up", "a top-level file, not ../up.md"))
    _w(d, "child.md", _fm("Child", "linked from the hub") + "- [Grandchild](grandchild.md)\n")
    _w(d, "grandchild.md", _fm("Grandchild", "two levels down"))
    _w(d, "prose_only.md", _fm("Prose", "named by the hub in prose, not linked"))
    _w(d, "orphan_hub.md", _fm("Orphan hub", "not indexed") + "- [Via orphan](via_orphan.md)\n")
    _w(d, "via_orphan.md", _fm("Via orphan", "linked only from an unindexed file"))
    assert ms.scan(d).unindexed == [
        "elsewhere.md", "grandchild.md", "orphan_hub.md", "prose_only.md", "up.md", "via_orphan.md",
    ]


def test_files_scan_is_silent_on_a_clean_directory(tmp_path: Path) -> None:
    scan = ms.scan(_clean_dir(tmp_path))
    assert scan.unindexed == [] and scan.oversized == []
    assert scan.dangling == [] and scan.replaced_present == []
    assert (tmp_path / "mem-clean" / "MEMORY.md").stat().st_size > ms.OVERSIZE_BYTES, (
        "fixture: the index file must be over 8 KB to prove the carve-out"
    )


def test_the_two_index_files_are_never_unindexed_even_when_neither_names_the_other(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    _w(d, "MEMORY.md", "nothing here\n")
    _w(d, "MEMORY-ondemand.md", "nothing here\n")
    assert ms.scan(d).unindexed == []


@pytest.mark.parametrize(
    ("frontmatter_line", "expected"),
    [
        ("replaces: old_one", ["old_one"]),
        ("replaces: [old_one, other.md]", ["old_one", "other.md"]),
        ('replaces: "old_one"', ["old_one"]),
        ("replaces:\n  - old_one\n  - other", ["old_one", "other"]),
        ("replaced_by: x", []),
        ("", []),
    ],
)
def test_the_replaces_frontmatter_forms(frontmatter_line: str, expected: list[str]) -> None:
    text = f"---\nname: n\n{frontmatter_line}\n---\nbody\nreplaces: not-frontmatter\n"
    assert ms.declared_replaces(text) == expected
    # a replaces line that is not inside the frontmatter block is not a declaration
    assert ms.declared_replaces("body\nreplaces: x\n") == []


def test_a_declared_target_is_found_by_file_stem_or_by_frontmatter_name(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    _w(d, "MEMORY.md", "- [N](n.md)\n- [O](o.md)\n- [P](p.md)\n")
    _w(d, "n.md", _fm("New", "d", "replaces: [slug_stem, Display Name]\n"))
    _w(d, "slug_stem.md", _fm("whatever", "d"))
    _w(d, "o.md", _fm("Display Name", "d"))
    _w(d, "p.md", _fm("P", "d", "replaces: n\n"))  # a file never replaces itself by name collision
    got = ms.scan(d).replaced_present
    assert ("n.md", "slug_stem", "slug_stem.md") in got
    assert ("n.md", "Display Name", "o.md") in got
    assert ("p.md", "n", "n.md") in got
    assert all(src != tgt for src, _slug, tgt in got)


def test_near_duplicates_lists_the_planted_pair_and_not_the_clean_pair(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    _w(d, "MEMORY.md", "- a\n")
    _w(d, "uv_a.md", _fm("Use uv for Python packages",
                         "Always run Python tooling through uv, never pip install"))
    _w(d, "uv_b.md", _fm("Use uv, not pip, for Python packages",
                         "Run Python tooling through uv and never pip install anything"))
    _w(d, "release.md", _fm("Release cadence", "Releases are cut by Sam only; the AI prepares the branch"))
    _w(d, "t2.md", _fm("T2 write-back titles", "Gate records carry kind:gate and a bead tag"))
    before = {p.name: p.read_bytes() for p in d.iterdir()}
    pairs = mc.near_duplicates(d)
    assert [(a, b) for a, b, _s in pairs] == [("uv_a.md", "uv_b.md")]
    assert pairs[0][2] >= mc.NEAR_DUP_THRESHOLD
    # report only: nothing on disk changed
    assert {p.name: p.read_bytes() for p in d.iterdir()} == before


def test_near_duplicates_ignores_files_with_too_few_tokens_and_is_deterministic(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    _w(d, "x.md", _fm("a", "b"))
    _w(d, "y.md", _fm("a", "b"))
    assert mc.near_duplicates(d) == []  # identical but tiny: not a signal
    assert mc.near_duplicates(d) == mc.near_duplicates(d)


def test_the_oversize_boundary_is_exactly_8192_bytes(tmp_path: Path) -> None:
    d = tmp_path / "m"
    d.mkdir()
    _w(d, "MEMORY.md", "- [E](exact.md)\n- [O](over.md)\n")
    _w(d, "exact.md", "x" * ms.OVERSIZE_BYTES)
    _w(d, "over.md", "x" * (ms.OVERSIZE_BYTES + 1))
    assert (d / "exact.md").stat().st_size == 8192
    assert ms.scan(d).oversized == [("over.md", 8193)]  # 8192 is allowed; 8193 is over


def _pair(d: Path, name_a: str, desc_a: str, name_b: str, desc_b: str) -> list[tuple[str, str, float]]:
    _w(d, "a.md", _fm(name_a, desc_a))
    _w(d, "b.md", _fm(name_b, desc_b))
    return mc.near_duplicates(d)


def test_near_duplicates_need_at_least_four_tokens_each(tmp_path: Path) -> None:
    three = tmp_path / "three"
    three.mkdir()
    assert _pair(three, "alpha beta", "gamma", "alpha beta", "gamma") == []  # identical, 3 tokens: not compared
    four = tmp_path / "four"
    four.mkdir()
    assert _pair(four, "alpha beta", "gamma delta", "alpha beta", "gamma delta") == [("a.md", "b.md", 1.0)]


def test_near_duplicates_threshold_is_inclusive_at_exactly_one_half(tmp_path: Path) -> None:
    half = tmp_path / "half"
    half.mkdir()
    # 4 shared tokens of 8 in the union: exactly 0.5
    assert _pair(half, "aa bb cc dd", "", "aa bb cc dd", "ee ff gg hh") == [("a.md", "b.md", 0.5)]
    under = tmp_path / "under"
    under.mkdir()
    # 4 of 9: just under
    assert _pair(under, "aa bb cc dd", "", "aa bb cc dd", "ee ff gg hh ii") == []


def test_files_section_text_reports_planted_and_says_none_for_clean(tmp_path: Path) -> None:
    out = "\n".join(mc.files_section(_planted_dir(tmp_path)))
    for needle in ("stray.md", "huge.md", "gone.md", "old_one", "unread files: not available"):
        assert needle in out, needle
    assert "RDR-240" in out  # the reason the read counts are absent
    # The rendered listings themselves, line by line: the needles above are also
    # satisfied by the unindexed listing, so they cannot see a blanked listing.
    lines = out.splitlines()
    oversized = lines.index(f"oversized (over {ms.OVERSIZE_BYTES} bytes, index files excluded): 1")
    assert lines[oversized + 1].startswith("  - huge.md (")
    replaced = lines.index("declared replaces still present (delete the old file): 1")
    assert lines[replaced + 1] == "  - new_one.md replaces old_one -> old_one.md"
    dup_dir = tmp_path / "mem-dup"
    dup_dir.mkdir()
    _w(dup_dir, "MEMORY.md", "- a\n")
    _w(dup_dir, "uv_a.md", _fm("Use uv for Python packages",
                               "Always run Python tooling through uv, never pip install"))
    _w(dup_dir, "uv_b.md", _fm("Use uv, not pip, for Python packages",
                               "Run Python tooling through uv and never pip install anything"))
    dup_lines = mc.files_section(dup_dir)
    head = next(i for i, ln in enumerate(dup_lines) if ln.startswith("likely near-duplicates ("))
    assert dup_lines[head].endswith(": 1"), dup_lines[head]
    assert dup_lines[head + 1].startswith("  - uv_a.md ~ uv_b.md ("), dup_lines[head + 1]
    clean ="\n".join(mc.files_section(_clean_dir(tmp_path)))
    assert "stray.md" not in clean and "huge.md" not in clean
    assert "unindexed: none" in clean and "oversized: none" in clean
    assert "unread files: not available" in clean


def test_files_section_without_a_memory_directory_is_not_available() -> None:
    out = mc.files_section(None)
    assert len(out) == 1 and "not available" in out[0]


# ── --agents ─────────────────────────────────────────────────────────────────────

def _agent(root: Path, name: str, index: str, files: dict[str, str] | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "MEMORY.md").write_text(index, encoding="utf-8")
    for fname, text in (files or {}).items():
        (d / fname).write_text(text, encoding="utf-8")
    return d


_E1 = "- [Heuristic one](h1.md) - learned in the nexus repo\n"
_E2 = "- [Heuristic two](h2.md) - (repo: nexus) check callers first\n"


def test_agents_clean_directory_has_no_findings(tmp_path: Path) -> None:
    d = _agent(tmp_path, "clean", _E1 + _E2, {"h1.md": "a\n", "h2.md": "b\n"})
    f = mc.scan_agent_dir(d)
    assert f.line_count == 2 and not f.over_cap
    assert f.no_repo == [] and f.duplicates == [] and f.truncated is None
    assert f.dangling == [] and f.orphans == []
    assert f.clean


def _index_of(n: int) -> tuple[str, dict[str, str]]:
    return ("".join(f"- [E{i}](e{i}.md) - repo: nexus\n" for i in range(n)), {f"e{i}.md": "x\n" for i in range(n)})


def test_agents_index_must_stay_under_40_lines_so_forty_is_flagged(tmp_path: Path) -> None:
    """The reviewer definitions say "keep its index under 40 lines": 39 is clean, 40 and 41 are not."""
    for n, flagged in ((39, False), (40, True), (41, True)):
        index, files = _index_of(n)
        got = mc.scan_agent_dir(_agent(tmp_path, f"idx{n}", index, files))
        assert got.line_count == n and got.over_cap is flagged, n
    index, files = _index_of(40)
    root = tmp_path / "agent-memory"
    _agent(root, "forty", index, files)
    assert "must stay under 40" in "\n".join(mc.agents_section(root))


@pytest.mark.parametrize(
    "text",
    [
        "learned in nexus",
        "(learned in nexus)",
        "learned in the nexus repo",
        "Learned in Nexus.",
        "repo: nexus",
        "(repository=nexus) check callers",
        "found in the nexus repo",
        "nexus repository conventions",
    ],
)
def test_agents_names_a_repo_forms(text: str) -> None:
    assert mc.names_a_repo(text), text


@pytest.mark.parametrize(
    "text",
    [
        "applies to whole repo",
        "found in the Java repo conventions",
        "any git repo",
        "this repo",
        "learned in the repo",
        "learned in a session",
        "learned in practice",
        "repo: any",
        "check callers before renaming",
        "",
    ],
)
def test_agents_generic_phrases_name_no_repo(text: str) -> None:
    assert not mc.names_a_repo(text), text


def test_agents_the_learned_in_form_clears_an_entry_and_a_generic_phrase_does_not(tmp_path: Path) -> None:
    idx = ("- [Named](a.md) - learned in nexus\n"
           "- [Generic](b.md) - applies to whole repo\n"
           "- [Language](c.md) - found in the Java repo conventions\n")
    d = _agent(tmp_path, "forms", idx, {"a.md": "x\n", "b.md": "x\n", "c.md": "x\n"})
    assert mc.scan_agent_dir(d).no_repo == ["Generic", "Language"]


def test_agents_entries_with_no_repo_named(tmp_path: Path) -> None:
    idx = ("- [Named in line](a.md) - learned in the nexus repo\n"
           "- [Named in file](b.md) - hook only\n"
           "- [Unnamed](c.md) - check callers before renaming\n")
    d = _agent(tmp_path, "norepo", idx, {"a.md": "x\n", "b.md": "repo: nexus\nbody\n", "c.md": "a heuristic\n"})
    f = mc.scan_agent_dir(d)
    assert f.no_repo == ["Unnamed"]


def test_agents_duplicate_entry_lines_and_double_links(tmp_path: Path) -> None:
    same_line = mc.scan_agent_dir(_agent(tmp_path, "dup", _E1 + _E1, {"h1.md": "x\n"}))
    assert same_line.duplicates and "repeated line" in same_line.duplicates[0]
    # two DIFFERENT lines that link one file are damage too (a reworded entry written twice)
    double = mc.scan_agent_dir(_agent(
        tmp_path, "double", _E1 + "- [Same file again](h1.md) - (repo: nexus) reworded\n", {"h1.md": "x\n"}))
    assert len(double.duplicates) == 1 and "2 entries link h1.md" in double.duplicates[0]
    assert not mc.scan_agent_dir(_agent(tmp_path, "nodup", _E1 + _E2, {"h1.md": "a", "h2.md": "b"})).duplicates


def test_agents_truncated_last_line(tmp_path: Path) -> None:
    d = _agent(tmp_path, "trunc", _E1 + "- [Heuristic two](h2.m", {"h1.md": "a", "h2.md": "b"})
    assert mc.scan_agent_dir(d).truncated == "- [Heuristic two](h2.m"
    # a complete last entry with no trailing newline is not truncation
    ok = _agent(tmp_path, "notrunc", _E1 + _E2.rstrip("\n"), {"h1.md": "a", "h2.md": "b"})
    assert mc.scan_agent_dir(ok).truncated is None


@pytest.mark.parametrize("cut", ["-", "*", "- [", "* [Heuristic two]"])
def test_agents_truncated_last_line_cut_back_to_the_bullet(tmp_path: Path, cut: str) -> None:
    d = _agent(tmp_path, "cut", _E1 + cut, {"h1.md": "a"})
    assert mc.scan_agent_dir(d).truncated == cut


@pytest.mark.parametrize("last", ["---", "**bold closing line**", "plain prose"])
def test_agents_a_last_line_that_is_not_a_bullet_is_not_truncation(tmp_path: Path, last: str) -> None:
    d = _agent(tmp_path, "notbullet", _E1 + last, {"h1.md": "a"})
    assert mc.scan_agent_dir(d).truncated is None


def test_agents_dangling_entry_and_orphan_file(tmp_path: Path) -> None:
    d = _agent(tmp_path, "dmg", _E1 + _E2, {"h1.md": "a", "stray.md": "never indexed"})
    f = mc.scan_agent_dir(d)
    assert f.dangling == ["h2.md"]
    assert f.orphans == ["stray.md"]
    ok = mc.scan_agent_dir(_agent(tmp_path, "ok", _E1, {"h1.md": "a"}))
    assert ok.dangling == [] and ok.orphans == []


def test_agents_section_walks_every_dir_with_a_memory_md_and_skips_the_rest(tmp_path: Path) -> None:
    root = tmp_path / "agent-memory"
    _agent(root, "conexus-a", _E1, {"h1.md": "x"})
    _agent(root, "conexus-b", _E1 + _E1, {"h1.md": "x"})
    (root / "no-index").mkdir()
    (root / "no-index" / "notes.md").write_text("not an index\n")
    out = "\n".join(mc.agents_section(root))
    assert "conexus-a" in out and "conexus-b" in out and "no-index" not in out
    assert "duplicate" in out  # conexus-b's damage is named
    assert "directories scanned: 2" in out


def test_agents_section_missing_root_is_not_available(tmp_path: Path) -> None:
    out = mc.agents_section(tmp_path / "absent")
    assert len(out) == 1 and "not available" in out[0]


# ── --t2 ─────────────────────────────────────────────────────────────────────────

REPO = "curaterepo"


def _put(db: T2Database, title: str, content: str = "body", tags: str = "", project: str = REPO,
         ttl: int | None = None, age_days: float | None = None) -> None:
    db.put(project=project, title=title, content=content, tags=tags, ttl=ttl)
    if age_days is not None:
        backdate_memory(db, project, title, days=age_days)


def _t2(db: T2Database, closed: set[str] | None = None) -> mc.T2Findings:
    return mc.t2_findings(db, REPO, closed_rdrs=closed or set(), now=datetime.now(UTC))


@pytest.mark.t2_tenant
def test_t2_replaced_row_named_in_a_later_rows_body(db: T2Database) -> None:
    _put(db, "alpha-notes", age_days=5)
    _put(db, "alpha-notes-v2", "new text\nreplaces: alpha-notes")
    _put(db, "untouched", "nobody names this", age_days=5)
    got = _t2(db)
    assert [(c.project, c.title, c.named_by) for c in got.replaced] == [(REPO, "alpha-notes", "alpha-notes-v2")]


@pytest.mark.t2_tenant
def test_t2_replaced_row_named_in_a_later_rows_title_and_by_supersedes(db: T2Database) -> None:
    _put(db, "beta-old", age_days=5)
    _put(db, "beta-new (supersedes beta-old)", "text")
    _put(db, "gamma-old", age_days=5)
    _put(db, "gamma-new", "this is the new one; it supersedes: gamma-old.")
    got = {c.title: c.named_by for c in _t2(db).replaced}
    assert got == {"beta-old": "beta-new (supersedes beta-old)", "gamma-old": "gamma-new"}


@pytest.mark.t2_tenant
def test_t2_a_row_older_than_the_one_naming_it_is_required_and_the_clean_case_lists_nothing(db: T2Database) -> None:
    # the namer is OLDER than the row it names: not a later row, not listed
    _put(db, "delta-namer", "replaces: delta-target", age_days=5)
    _put(db, "delta-target", "newer than its namer")
    # a naming of a title that exists nowhere, and a similar-but-not-equal title
    _put(db, "epsilon-namer", "replaces: epsilon-missing\nsupersedes epsilon-targ")
    _put(db, "epsilon-target-long", age_days=5)
    assert _t2(db).replaced == []


@pytest.mark.t2_tenant
def test_t2_audit_tagged_and_audit_titled_rows_are_skipped_and_counted(db: T2Database) -> None:
    _put(db, "zeta-gate-record", tags="rdr,kind:gate,bead:x", age_days=5)
    _put(db, "eta-critique-x", tags="critique", age_days=5)
    _put(db, "theta-code-review", tags="", age_days=5)      # untagged, caught by the title backstop
    _put(db, "iota-plain", tags="notes,gates", age_days=5)   # 'gates' is not the whole tag 'gate'
    _put(db, "namer", "replaces: zeta-gate-record\nreplaces: eta-critique-x\n"
                      "replaces: theta-code-review\nreplaces: iota-plain")
    got = _t2(db)
    assert [c.title for c in got.replaced] == ["iota-plain"]
    assert got.skipped_audit == 3


@pytest.mark.t2_tenant
def test_t2_a_row_in_another_project_is_not_named_across_projects(db: T2Database) -> None:
    _put(db, "kappa-old", project=REPO, age_days=5)
    _put(db, "kappa-new", "replaces: kappa-old", project="otherrepo")
    assert _t2(db).replaced == []


@pytest.mark.t2_tenant
def test_t2_only_the_repos_own_projects_are_read(db: T2Database) -> None:
    _put(db, "lam-old", project=f"{REPO}_active", age_days=5)
    _put(db, "lam-new", "replaces: lam-old", project=f"{REPO}_active")
    _put(db, "mu-old", project=f"{REPO}extra", age_days=5)   # shares the prefix, is not this repo's
    _put(db, "mu-new", "replaces: mu-old", project=f"{REPO}extra")
    got = _t2(db)
    assert [(c.project, c.title) for c in got.replaced] == [(f"{REPO}_active", "lam-old")]


@pytest.mark.t2_tenant
def test_t2_census_flags_old_ttl_free_gate_records_of_a_closed_phase(db: T2Database) -> None:
    gate_tags = "kind:gate,bead:b1,rdr:238"
    _put(db, "planted-gate", tags=gate_tags, age_days=40)
    _put(db, "238-gate-critique-x", tags="rdr,gate,critique", project=f"{REPO}_rdr", age_days=40)
    # the clean cases, one per way a record is not flagged
    _put(db, "recent-gate", tags=gate_tags, age_days=10)                              # newer than 30 days
    _put(db, "has-ttl-gate", tags=gate_tags, age_days=40, ttl=30)                     # already on the TTL path
    _put(db, "open-phase-gate", tags="kind:gate,bead:b2,rdr:777", age_days=40)        # RDR 777 is not closed
    _put(db, "no-rdr-gate", tags="kind:gate,bead:b3", age_days=40)                    # cannot be mapped
    _put(db, "mentions-gate", "kind:gate in the body only", tags="notes,rdr:238", age_days=40)
    _put(db, "near-miss-tags", tags="gates,kind:gate-x,rdr:238", age_days=40)          # not whole tags
    _put(db, "333-open-gate", tags="rdr,gate", project=f"{REPO}_rdr", age_days=40)    # RDR 333 not closed
    got = _t2(db, closed={"238"})
    flagged = sorted((c.project, c.title) for c in got.census)
    assert flagged == [(REPO, "planted-gate"), (f"{REPO}_rdr", "238-gate-critique-x")]
    assert got.census_unmapped == 1  # no-rdr-gate: counted, never flagged


@pytest.mark.t2_tenant
def test_t2_census_with_no_closed_rdr_flags_nothing(db: T2Database) -> None:
    _put(db, "planted-gate", tags="kind:gate,bead:b1,rdr:238", age_days=40)
    assert _t2(db, closed=set()).census == []


@pytest.mark.t2_tenant
def test_t2_section_text(db: T2Database) -> None:
    _put(db, "alpha-notes", age_days=5)
    _put(db, "alpha-notes-v2", "replaces: alpha-notes")
    text = "\n".join(mc.t2_section(db, REPO, closed_rdrs=set(), now=datetime.now(UTC)))
    assert "alpha-notes" in text and "alpha-notes-v2" in text
    assert "never deletes" in text or "report only" in text
    clean = "\n".join(mc.t2_section(db, "norowsrepo", closed_rdrs=set(), now=datetime.now(UTC)))
    assert "replaced candidates: none" in clean and "census: none" in clean


def test_named_titles_forms() -> None:
    f = mc._named_titles
    assert "old title" in f("n", "this supersedes `old title` because it was wrong")   # backtick form
    assert {"first-title", "second-title"} <= f("n", "replaces: first-title; second-title")  # ; list
    assert {"first-title", "second-title"} <= f("n", "replaces: first-title, second-title")  # , list
    assert "beta-old" in f("beta-new (supersedes beta-old)", "text")                    # title form
    # clean: a line that says neither word names nothing
    assert f("n", "this row is about replacing nothing in particular") == set()


@pytest.mark.t2_tenant
def test_t2_a_backtick_title_and_a_semicolon_list_name_their_rows(db: T2Database) -> None:
    _put(db, "old title with spaces", age_days=5)
    _put(db, "semi-one", age_days=5)
    _put(db, "semi-two", age_days=5)
    _put(db, "comma-one", age_days=5)
    _put(db, "comma-two", age_days=5)
    _put(db, "namer-one", "this supersedes `old title with spaces` because it was wrong")
    _put(db, "namer-two", "replaces: semi-one; semi-two")
    _put(db, "namer-three", "replaces: comma-one, comma-two")
    _put(db, "bystander", "replaces: nothing-by-that-name", age_days=5)
    got = {c.title: c.named_by for c in _t2(db).replaced}
    assert got == {
        "old title with spaces": "namer-one",
        "semi-one": "namer-two", "semi-two": "namer-two",
        "comma-one": "namer-three", "comma-two": "namer-three",
    }


@pytest.mark.t2_tenant
def test_t2_skips_bare_number_titles_and_rows_already_tagged_superseded(db: T2Database) -> None:
    _put(db, "100", age_days=5)                                        # an incidental number, not a row
    _put(db, "RDR-107", age_days=5)
    _put(db, "100-notes", age_days=5)                                  # a number is only part of the title: kept
    _put(db, "marked-old", tags="notes,superseded", age_days=5)        # already marked as replaced
    _put(db, "almost-marked", tags="notes,not-superseded", age_days=5)  # not the whole tag: kept
    _put(db, "namer", "supersedes: 100\nsupersedes: RDR-107\nreplaces: 100-notes\n"
                      "replaces: marked-old\nreplaces: almost-marked")
    assert sorted(c.title for c in _t2(db).replaced) == ["100-notes", "almost-marked"]


@pytest.mark.t2_tenant
def test_t2_census_reads_only_the_repo_and_repo_rdr_projects(db: T2Database) -> None:
    gate = "kind:gate,bead:b1,rdr:238"
    _put(db, "in-repo-gate", tags=gate, age_days=40)
    _put(db, "in-rdr-gate", tags=gate, project=f"{REPO}_rdr", age_days=40)
    _put(db, "in-active-gate", tags=gate, project=f"{REPO}_active", age_days=40)  # a repo project, not a census one
    got = _t2(db, closed={"238"})
    assert sorted((c.project, c.title) for c in got.census) == [(REPO, "in-repo-gate"), (f"{REPO}_rdr", "in-rdr-gate")]
    assert f"{REPO}_active" in got.projects  # it was read (for replaced candidates), just not for the census


def _snapshot(db: T2Database) -> list[tuple[object, ...]]:
    rows = []
    for project in mc._repo_projects(db, REPO):
        for r in db.memory.get_all(project):
            rows.append((r["id"], r["project"], r["title"], r["content"], r["tags"], r["ttl"]))
    return sorted(rows, key=repr)


@pytest.mark.t2_tenant
def test_t2_section_never_deletes_or_writes(db: T2Database) -> None:
    gate = "kind:gate,bead:b1,rdr:238"
    _put(db, "alpha-notes", age_days=5)
    _put(db, "alpha-notes-v2", "replaces: alpha-notes")
    _put(db, "audited-old", tags="kind:gate", age_days=5)
    _put(db, "audited-namer", "replaces: audited-old")
    _put(db, "census-gate", tags=gate, age_days=40)
    _put(db, "census-gate-active", tags=gate, project=f"{REPO}_active", age_days=40)
    _put(db, "marked", tags="superseded", age_days=5)
    _put(db, "marked-namer", "replaces: marked")
    before = _snapshot(db)
    assert len(before) == 8
    text = "\n".join(mc.t2_section(db, REPO, closed_rdrs={"238"}, now=datetime.now(UTC)))
    assert "alpha-notes" in text and "census-gate" in text, "non-vacuous: the section found its plants"
    assert _snapshot(db) == before, "--t2 changed T2 rows: it must only read"


@pytest.mark.t2_tenant
def test_the_command_adds_exactly_one_row_the_report_and_deletes_nothing(
    db: T2Database, curate_world: dict[str, Path],
) -> None:
    _put(db, "alpha-notes", age_days=5)
    _put(db, "alpha-notes-v2", "replaces: alpha-notes")
    _put(db, "census-gate", tags="kind:gate,bead:b,rdr:238", age_days=40)
    before = _snapshot(db)
    res = _curate(db)
    assert res.exit_code == 0, res.output
    after = _snapshot(db)
    title = f"curation-report-{datetime.now(UTC):%Y-%m-%d}"
    # The no-delete clause runs first, so a planted delete fails with its own message
    # instead of on the row count.
    assert all(r in after for r in before), "a row the command did not own was removed or changed"
    assert [r for r in after if r not in before] == [r for r in after if r[2] == title] and len(after) == len(before) + 1
    assert "alpha-notes" in next(r for r in after if r[2] == title)[3]


# ── RDR phase closure for the census ─────────────────────────────────────────────

def test_closed_rdrs_reads_frontmatter_status_only(tmp_path: Path) -> None:
    rdr = tmp_path / "docs" / "rdr"
    rdr.mkdir(parents=True)
    (rdr / "rdr-238-a.md").write_text("---\nid: RDR-238\nstatus: closed\n---\n# x\n")
    (rdr / "rdr-239-b.md").write_text("---\nid: RDR-239\nstatus: accepted\n---\n# x\n")
    (rdr / "rdr-240-c.md").write_text("---\nid: RDR-240\nstatus: draft\n---\nstatus: closed\n")  # body line
    (rdr / "README.md").write_text("---\nstatus: closed\n---\n")
    assert mc.closed_rdr_numbers(tmp_path) == {"238"}
    assert mc.closed_rdr_numbers(tmp_path / "no-such-root") == set()


# ── the command ──────────────────────────────────────────────────────────────────

def _t2_cm(db: object) -> MagicMock:
    return MagicMock(__enter__=MagicMock(return_value=db), __exit__=MagicMock(return_value=False))


@pytest.fixture
def curate_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    repo = _make_repo(tmp_path, REPO)
    config = tmp_path / "cfg"
    mem = _memdir(config, repo)
    (mem / "MEMORY.md").write_text("- [Kept](kept.md) - x\n- [Gone](gone.md) - missing\n")
    (mem / "kept.md").write_text(_fm("Kept", "small"))
    (mem / "stray.md").write_text(_fm("Stray", "unindexed"))
    agents = config / "agent-memory"
    _agent(agents, "conexus-code-review-expert", _E1 + _E1, {"h1.md": "x"})
    (repo / "docs" / "rdr").mkdir(parents=True)
    (repo / "docs" / "rdr" / "rdr-238-x.md").write_text("---\nid: RDR-238\nstatus: closed\n---\n")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("NX_NO_TELEMETRY", "1")
    return {"repo": repo, "config": config, "mem": mem}


def _curate(db: object, *args: str):
    with patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)):
        return CliRunner().invoke(main, ["memory", "curate", *args])


@pytest.mark.t2_tenant
def test_command_runs_all_sections_stores_the_report_for_90_days_and_exits_0(
    db: T2Database, curate_world: dict[str, Path],
) -> None:
    _put(db, "alpha-notes", age_days=5)
    _put(db, "alpha-notes-v2", "replaces: alpha-notes")
    _put(db, "census-gate", tags="kind:gate,bead:b,rdr:238", age_days=40)
    res = _curate(db)
    assert res.exit_code == 0, res.output
    for needle in ("== --files", "== --t2", "== --agents", "stray.md", "gone.md",
                   "alpha-notes", "census-gate", "duplicate"):
        assert needle in res.output, needle
    title = f"curation-report-{datetime.now(UTC):%Y-%m-%d}"
    row = next(r for r in db.memory.get_all(REPO) if r["title"] == title)
    assert row["ttl"] == 90
    assert "stray.md" in row["content"] and "alpha-notes" in row["content"]
    assert res.output.startswith(row["content"]), "stdout carries exactly the report that was stored"
    assert f"report stored: {REPO}/{title}" in res.output


@pytest.mark.t2_tenant
def test_command_flags_select_sections_and_cold_is_not_available(
    db: T2Database, curate_world: dict[str, Path],
) -> None:
    only_files = _curate(db, "--files")
    assert only_files.exit_code == 0
    assert "== --files" in only_files.output and "== --t2" not in only_files.output
    assert "== --agents" not in only_files.output
    cold = _curate(db, "--cold", "--days", "7")
    assert cold.exit_code == 0
    assert "== --cold" in cold.output and "not available" in cold.output
    assert "== --files" not in cold.output  # a section flag narrows the run


def test_report_title_full_run_and_partial_runs() -> None:
    now = datetime(2026, 11, 3, tzinfo=UTC)
    t = mc.report_title
    base = "curation-report-2026-11-03"
    assert t(now, files=True, t2=True, agents=True, cold=False) == base
    assert t(now, files=True, t2=True, agents=True, cold=True) == base
    assert t(now, files=True, t2=False, agents=False, cold=False) == f"{base}-files"
    assert t(now, files=False, t2=True, agents=True, cold=False) == f"{base}-t2-agents"
    assert t(now, files=True, t2=True, agents=False, cold=True) == f"{base}-files-t2-cold"
    assert t(now, files=False, t2=False, agents=False, cold=True) == f"{base}-cold"


@pytest.mark.t2_tenant
def test_a_partial_run_does_not_overwrite_the_days_full_report(
    db: T2Database, curate_world: dict[str, Path],
) -> None:
    day = f"{datetime.now(UTC):%Y-%m-%d}"
    assert _curate(db).exit_code == 0
    full = next(r for r in db.memory.get_all(REPO) if r["title"] == f"curation-report-{day}")
    assert "== --t2" in full["content"] and "== --agents" in full["content"]
    for args, suffix in ((("--files",), "-files"), (("--cold",), "-cold"), (("--t2", "--agents"), "-t2-agents")):
        res = _curate(db, *args)
        assert res.exit_code == 0, res.output
        assert f"report stored: {REPO}/curation-report-{day}{suffix} " in res.output
    rows = {r["title"]: r for r in db.memory.get_all(REPO)}
    assert rows[f"curation-report-{day}"]["content"] == full["content"], "a partial run replaced the full report"
    for suffix in ("-files", "-cold", "-t2-agents"):
        assert f"curation-report-{day}{suffix}" in rows


def test_command_exits_0_and_says_not_available_when_t2_and_the_memory_directory_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path, REPO)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg-empty"))

    def boom(*_a: object, **_k: object) -> object:
        raise click.ClickException("T2 storage service unavailable: no lease")

    with patch("nexus.commands.memory.t2_handle", side_effect=boom):
        res = CliRunner().invoke(main, ["memory", "curate"])
    assert res.exit_code == 0, res.output
    assert res.output.count("not available") >= 4  # files dir, t2, agents root, and the store note
    assert "T2 storage service unavailable" in res.output
    assert "== --agents" in res.output  # the other sections still ran


def test_command_a_failing_section_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _make_repo(tmp_path, REPO)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(mc, "files_section", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    with patch("nexus.commands.memory.t2_handle", side_effect=click.ClickException("down")):
        res = CliRunner().invoke(main, ["memory", "curate", "--files", "--agents"])
    assert res.exit_code == 0, res.output
    assert "disk on fire" in res.output and "== --agents" in res.output


def test_the_command_is_registered_and_documented() -> None:
    res = CliRunner().invoke(main, ["memory", "curate", "--help"])
    assert res.exit_code == 0
    for flag in ("--files", "--t2", "--agents", "--cold", "--days"):
        assert flag in res.output
    ref = (Path(__file__).resolve().parents[1] / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert "`nx memory curate`" in ref and "**`curate` flags:**" in ref
    section = ref.split("**`curate` flags:**", 1)[1].split("**`promote` flags:**", 1)[0]
    for flag in ("--files", "--t2", "--agents", "--cold", "--days"):
        assert flag in section, flag
    for needle in ("near-duplicates", "S-7", "90-day TTL", "Jaccard", "closed"):
        assert needle in section, needle
