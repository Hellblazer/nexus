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


def test_the_one_mangling_is_shared_with_the_session_context_block() -> None:
    """The copy in ``command_context`` was replaced, not duplicated (one rule, one place)."""
    src = inspect.getsource(command_context._feedback_memories_block)
    assert 're.sub(r"[^A-Za-z0-9]"' not in src
    assert "mangle_path" in src


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


def test_a_missing_directory_or_a_non_repository_returns_none(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}  # no projects/<key>/memory beneath it
    assert md.memory_dir(repo, env=env, home=tmp_path / "h") is None
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert md.memory_dir(plain, env=env, home=tmp_path / "h") is None
    assert md.memory_dir(tmp_path / "does-not-exist", env=env, home=tmp_path / "h") is None


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


def test_files_section_text_reports_planted_and_says_none_for_clean(tmp_path: Path) -> None:
    out = "\n".join(mc.files_section(_planted_dir(tmp_path)))
    for needle in ("stray.md", "huge.md", "gone.md", "old_one", "unread files: not available"):
        assert needle in out, needle
    assert "RDR-240" in out  # the reason the read counts are absent
    clean = "\n".join(mc.files_section(_clean_dir(tmp_path)))
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


def test_agents_index_over_the_40_line_cap(tmp_path: Path) -> None:
    lines = "".join(f"- [E{i}](e{i}.md) - repo: nexus\n" for i in range(41))
    files = {f"e{i}.md": "x\n" for i in range(41)}
    over = mc.scan_agent_dir(_agent(tmp_path, "over", lines, files))
    assert over.line_count == 41 and over.over_cap
    exact = mc.scan_agent_dir(_agent(tmp_path, "exact", lines.rsplit("\n", 2)[0] + "\n", {
        f"e{i}.md": "x\n" for i in range(40)}))
    assert exact.line_count == 40 and not exact.over_cap


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
