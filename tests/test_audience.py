# SPDX-License-Identifier: AGPL-3.0-or-later
"""T8, T10, T11 and the D11 file (nexus-sby49; reworked in nexus-sby49.4): the settings helper.

``conexus/hooks/scripts/_audience.py`` reads a project's tracked ``.nexus/project.json`` and the user's own
``~/.config/nexus/conexus.json``; nothing in it asks who the user is. These tests pin its contract:

* the defaults, the keys of each file, and TIGHTEN-ONLY: a project file can make the subagent git guard strict and add text,
  never loosen anything or turn telemetry on; a file that cannot be read gives the strict reading;
* T8: a broken helper never widens anything. ``guard_mode`` answers strict on error, ``telemetry_enabled`` answers False, a
  missing passwd entry is "no user settings", and a plugin copy with no helper (or one that cannot import) runs the full guard;
* T10: the account home comes from the account database, not from ``HOME`` or ``USERPROFILE``; the only seam is the
  in-process override;
* T11: the walk edges (nested repo, submodule, linked worktree, ``.git/`` cwd, symlink, unsubstituted ``${cwd}``);
* the remote-test-hosts file (D11): a repository's own env cannot name an exempt ssh host.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

from tests._module_seam import setattr_in
from tests.hooks.audience_harness import LEGACY_MARKER, legacy_marked_repo, plain_repo, strict_repo, write_project_settings, write_user_settings

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "conexus" / "hooks" / "scripts"
ROUTING = SCRIPTS / "routing"
GUARD = ROUTING / "subagent_git_write_requires_orchestrator.py"
HARNESS = PROJECT_ROOT / "tests" / "hooks" / "audience_harness.py"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import _audience  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch) -> pathlib.Path:
    """An account home with no settings file, installed through the in-process seam."""
    h = tmp_path / "acct"
    h.mkdir()
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(h))
    return h


def _repo(path: pathlib.Path, *, git_file: bool = False) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    if git_file:
        (path / ".git").write_text("gitdir: /nonexistent\n")
    else:
        (path / ".git").mkdir()
    return path


def _p(cwd) -> dict:
    return {"cwd": str(cwd)}


# ---------------------------------------------------------------------------
# The defaults and the two files
# ---------------------------------------------------------------------------


def test_a_plain_repo_on_a_plain_account_reads_every_default(home, tmp_path):
    repo = plain_repo(tmp_path / "r")
    assert _audience.project_settings(_p(repo)) == {}
    assert _audience.user_settings() == {
        "subagent_git": "seatbelt", "telemetry": False, "t1_nudge": True, "advice_block": [], "brief_questions": [],
    }
    assert _audience.guard_mode(_p(repo)) == "seatbelt"
    assert _audience.telemetry_enabled() is False
    assert _audience.t1_nudge_enabled() is True
    assert _audience.advice_blocked(_p(repo)) == frozenset()
    assert _audience.project_text(_p(repo), "ci_wait") is None
    assert _audience.project_brief_questions(_p(repo)) == []


def test_a_project_file_carries_every_project_key(home, tmp_path):
    repo = strict_repo(
        tmp_path / "r", preflight="tools/pre.sh", ci_wait="ci-wait <sha>", credential_helper="Use tools/cred.sh.",
        brief_questions=["One?", "Two?"], advice_block=["pattern_kill"],
    )
    got = _audience.project_settings(_p(repo))
    assert got == {
        "subagent_git": "strict", "preflight": "tools/pre.sh", "ci_wait": "ci-wait <sha>",
        "credential_helper": "Use tools/cred.sh.", "brief_questions": ["One?", "Two?"], "advice_block": ["pattern_kill"],
    }, "the validated keys, without version"
    assert _audience.guard_mode(_p(repo)) == "strict"
    assert _audience.project_text(_p(repo), "preflight") == "tools/pre.sh"
    assert _audience.project_brief_questions(_p(repo)) == ["One?", "Two?"]
    assert _audience.advice_blocked(_p(repo)) == frozenset({"pattern_kill"})


def test_a_user_file_carries_every_user_key(home, tmp_path):
    write_user_settings(home, subagent_git="strict", telemetry=True, t1_nudge=False, advice_block=["whole_disk_scan"])
    repo = plain_repo(tmp_path / "r")
    assert _audience.guard_mode(_p(repo)) == "strict"
    assert _audience.telemetry_enabled() is True
    assert _audience.t1_nudge_enabled() is False
    assert _audience.advice_blocked(_p(repo)) == frozenset({"whole_disk_scan"})


def test_the_two_advice_lists_are_added_together(home, tmp_path):
    write_user_settings(home, advice_block=["pattern_kill"])
    repo = strict_repo(tmp_path / "r", advice_block=["whole_disk_scan"])
    assert _audience.advice_blocked(_p(repo)) == frozenset({"pattern_kill", "whole_disk_scan"})


# ---------------------------------------------------------------------------
# Tighten-only
# ---------------------------------------------------------------------------


def test_a_project_file_cannot_loosen_or_turn_telemetry_on(home, tmp_path):
    repo = tmp_path / "r"
    plain_repo(repo)
    write_project_settings(repo, subagent_git="seatbelt", telemetry=True, t1_nudge=False, auto_approve=["x"])
    assert _audience.project_settings(_p(repo)) == {"subagent_git": "seatbelt"}, "a user key or an unknown key in a project file is ignored"
    assert sorted(p.key for p in _audience.read_settings(_p(repo)).problems) == ["auto_approve", "t1_nudge", "telemetry"]
    assert _audience.guard_mode(_p(repo)) == "seatbelt"
    assert _audience.telemetry_enabled() is False
    assert _audience.t1_nudge_enabled() is True


def test_a_user_seatbelt_does_not_loosen_a_project_strict(home, tmp_path):
    write_user_settings(home, subagent_git="seatbelt")
    repo = strict_repo(tmp_path / "r")
    assert _audience.guard_mode(_p(repo)) == "strict"


def test_a_value_of_the_wrong_type_is_ignored_not_trusted(home, tmp_path):
    repo = tmp_path / "r"
    plain_repo(repo)
    write_project_settings(repo, subagent_git=1, preflight=5, ci_wait=["a"], credential_helper="", brief_questions="one",
                           advice_block=[3, None, "pattern_kill", " "])
    assert _audience.project_settings(_p(repo)) == {"subagent_git": "strict", "advice_block": ["pattern_kill"], "brief_questions": []}, (
        "a bad subagent_git is strict, a bad list is empty, a bad text is absent"
    )
    write_user_settings(home, subagent_git="loose", telemetry="yes", t1_nudge=0, advice_block="all")
    assert _audience.user_settings() == {
        "subagent_git": "strict", "telemetry": False, "t1_nudge": True, "advice_block": [], "brief_questions": [],
    }


@pytest.mark.parametrize("body", ["{not json", "[1, 2]", "null", '"strict"', ""])
def test_a_project_file_that_cannot_be_read_gives_the_strict_reading(home, tmp_path, body):
    repo = plain_repo(tmp_path / "r")
    (pathlib.Path(repo) / ".nexus").mkdir()
    (pathlib.Path(repo) / ".nexus" / "project.json").write_text(body)
    assert _audience.project_settings(_p(repo)) == {"subagent_git": "strict"}
    assert _audience.guard_mode(_p(repo)) == "strict"


def test_a_project_file_that_is_a_directory_gives_the_strict_reading(home, tmp_path):
    repo = plain_repo(tmp_path / "r")
    (pathlib.Path(repo) / ".nexus" / "project.json").mkdir(parents=True)
    assert _audience.guard_mode(_p(repo)) == "strict"


@pytest.mark.parametrize("body", ["{not json", "[1]", "null"])
def test_a_user_file_that_cannot_be_read_reads_strict_and_leaves_the_other_defaults(home, tmp_path, body):
    """Sam's S2 (RDR-259 Decision 4): a broken conexus.json is most likely a failed edit toward strict."""
    (home / ".config" / "nexus").mkdir(parents=True)
    (home / ".config" / "nexus" / "conexus.json").write_text(body)
    assert _audience.user_settings() == {
        "subagent_git": "strict", "telemetry": False, "t1_nudge": True, "advice_block": [], "brief_questions": [],
    }
    assert _audience.guard_mode(_p(plain_repo(tmp_path / "r"))) == "strict"
    assert _audience.telemetry_enabled() is False


# ---------------------------------------------------------------------------
# The two old markers
# ---------------------------------------------------------------------------


def test_the_phase_two_file_and_the_phase_one_marker_each_read_as_strict(home, tmp_path):
    phase_two = plain_repo(tmp_path / "two")
    (pathlib.Path(phase_two) / ".nexus").mkdir()
    (pathlib.Path(phase_two) / ".nexus" / "dev-process.json").write_text("{}")
    phase_one = legacy_marked_repo(tmp_path / "one")
    for repo in (phase_two, phase_one):
        assert _audience.project_settings(_p(repo)) == {"subagent_git": "strict"}, repo
        assert _audience.guard_mode(_p(repo)) == "strict"
    assert (pathlib.Path(phase_one) / LEGACY_MARKER).is_file()


def test_the_new_file_decides_alone_when_it_exists(home, tmp_path):
    repo = legacy_marked_repo(tmp_path / "r")
    write_project_settings(repo, preflight="tools/pre.sh")  # a project file with no subagent_git beside the old marker
    assert _audience.project_settings(_p(repo)) == {"preflight": "tools/pre.sh"}
    assert _audience.guard_mode(_p(repo)) == "seatbelt"


# ---------------------------------------------------------------------------
# T8: failure goes in the safe direction
# ---------------------------------------------------------------------------


def test_guard_mode_answers_strict_when_it_raises(home, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(_audience, "read_settings", boom)
    assert _audience.guard_mode(_p(tmp_path)) == "strict"


def test_project_settings_answers_the_strict_reading_when_the_walk_raises(home, tmp_path, monkeypatch):
    repo = plain_repo(tmp_path / "r")

    def boom(*a, **k):
        raise RuntimeError("broken")

    setattr_in(monkeypatch, _audience, "os.path.realpath", boom)
    assert _audience.project_settings(_p(repo)) == {"subagent_git": "strict"}
    assert _audience.guard_mode(_p(repo)) == "strict"


def test_the_readers_that_loosen_or_add_nothing_answer_their_default_when_they_raise(home, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(_audience, "read_settings", boom)
    monkeypatch.setattr(_audience, "_read_user", boom)
    assert _audience.telemetry_enabled() is False
    assert _audience.t1_nudge_enabled() is True
    assert _audience.advice_blocked(_p(tmp_path)) == frozenset()
    assert _audience.project_text(_p(tmp_path), "ci_wait") is None
    assert _audience.project_brief_questions(_p(tmp_path)) == []


def test_user_settings_answer_the_defaults_when_the_home_lookup_raises(home, monkeypatch):
    write_user_settings(home, telemetry=True)

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(_audience, "account_home", boom)
    assert _audience.telemetry_enabled() is False


@pytest.mark.skipif(os.name == "nt", reason="passwd entries are POSIX")
def test_a_missing_passwd_entry_means_no_user_settings(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", None)

    def no_entry(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", no_entry)
    assert _audience.account_home() is None
    assert _audience.user_settings()["telemetry"] is False
    plain = plain_repo(tmp_path / "plain")
    strict = strict_repo(tmp_path / "strict")
    assert _audience.guard_mode(_p(plain)) == "seatbelt"
    assert _audience.guard_mode(_p(strict)) == "strict", "the project file does not need an account"


def _plugin_copy(tmp_path: pathlib.Path, audience_source: str | None) -> pathlib.Path:
    """A copy of the hook scripts with ``_audience.py`` replaced, or removed when None."""
    dest = tmp_path / "plugin-copy"
    shutil.copytree(SCRIPTS, dest, ignore=shutil.ignore_patterns("__pycache__"))
    target = dest / "_audience.py"
    if audience_source is None:
        target.unlink()
    else:
        target.write_text(audience_source)
    return dest


def _run_guard(script_dir: pathlib.Path, cmd: str, cwd: pathlib.Path, env_extra: dict | None = None) -> dict:
    payload = {
        "tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd),
        "agent_id": "aworker-x-1", "agent_type": "general-purpose", "session_id": "s1",
    }
    env = {
        **os.environ,
        "NX_HOOK_PYTHON": sys.executable,
        "NX_ROUTING_LOG_PATH": str(cwd.parent / "route.jsonl"),
        "NX_DROPPED_WRITES_LOG_PATH": str(cwd.parent / "dropped.jsonl"),
    }
    env.pop("NX_SERVICE_URL", None)
    env.update(env_extra or {})
    proc = subprocess.run(
        [sys.executable, str(script_dir / "routing" / GUARD.name)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout else {}


@pytest.mark.parametrize("variant", ["missing", "import_error", "syntax_error"])
@pytest.mark.parametrize("strict", [True, False])
def test_a_broken_or_missing_helper_gives_the_full_deny_everywhere(tmp_path, variant, strict):
    source = {
        "missing": None,
        "import_error": "raise RuntimeError('cannot import')\n",
        "syntax_error": "def (:\n",
    }[variant]
    copy = _plugin_copy(tmp_path, source)
    repo = strict_repo(tmp_path / "r") if strict else plain_repo(tmp_path / "r")
    out = _run_guard(copy, "git commit -am x", pathlib.Path(repo))
    assert out.get("permissionDecision") == "deny"
    assert "STRUCTURE-AGNOSTIC" in out["permissionDecisionReason"]


def test_a_missing_seatbelt_module_also_gives_the_full_deny(tmp_path):
    copy = _plugin_copy(tmp_path, (SCRIPTS / "_audience.py").read_text())
    (copy / "routing" / "_user_git_seatbelt.py").unlink()
    repo = plain_repo(tmp_path / "r")  # the seatbelt would pass a commit
    out = _run_guard(copy, "git commit -am x", pathlib.Path(repo), {"HOME": str(tmp_path / "nohome")})
    assert out.get("permissionDecision") == "deny"


# ---------------------------------------------------------------------------
# T10: the account home is the account database's, never an env variable
# ---------------------------------------------------------------------------


def test_the_seam_wins_and_env_home_variables_are_ignored(tmp_path, monkeypatch):
    empty = tmp_path / "empty-env-home"
    empty.mkdir()
    strict = tmp_path / "strict"
    strict.mkdir()
    write_user_settings(strict, subagent_git="strict", telemetry=True)
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("USERPROFILE", str(empty))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(empty))
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(strict))
    assert _audience.guard_mode(_p(plain_repo(tmp_path / "plain"))) == "strict"
    assert _audience.telemetry_enabled() is True


def test_an_env_home_holding_a_settings_file_does_not_count(tmp_path, monkeypatch):
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    write_user_settings(decoy, subagent_git="strict", telemetry=True)
    clean = tmp_path / "clean"
    clean.mkdir()
    monkeypatch.setenv("HOME", str(decoy))
    monkeypatch.setenv("USERPROFILE", str(decoy))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(decoy / ".config" / "nexus"))
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(clean))
    assert _audience.telemetry_enabled() is False
    assert _audience.guard_mode(_p(plain_repo(tmp_path / "plain"))) == "seatbelt"


def test_without_the_seam_the_home_is_the_account_databases_even_with_home_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", None)
    changed = tmp_path / "changed"
    changed.mkdir()
    if os.name == "nt":
        before = os.environ.get("USERPROFILE", "")
        monkeypatch.setenv("USERPROFILE", str(changed))
        got = _audience.account_home()
        assert got and os.path.normcase(got) != os.path.normcase(str(changed))
        if before:
            assert os.path.normcase(got) == os.path.normcase(before)
    else:
        import pwd

        expected = pwd.getpwuid(os.getuid()).pw_dir
        monkeypatch.setenv("HOME", str(changed))
        assert _audience.account_home() == expected
        assert _audience.account_home() != str(changed)


def test_the_override_is_never_read_from_the_environment(monkeypatch):
    src = (SCRIPTS / "_audience.py").read_text()
    assert "environ" in src and "CLAUDE_PROJECT_DIR" in src
    # The names appear only where telemetry_child_env strips or sets them in a copy of the environment it is GIVEN (RDR-259
    # Decision 9); no line reads the process environment for anything but CLAUDE_PROJECT_DIR.
    reads = [ln for ln in src.splitlines() if "os.environ" in ln and not ln.lstrip().startswith(("#", '"""'))]
    assert len(reads) == 1 and "CLAUDE_PROJECT_DIR" in reads[0], reads
    assert "getenv" not in src and "environ[" not in src and "environ.copy" not in src
    monkeypatch.setenv("_ACCOUNT_HOME_OVERRIDE", "/nonexistent")
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", None)
    assert _audience.account_home() != "/nonexistent"


def test_the_helper_asks_nothing_about_who_the_user_is():
    """The rework's point (Sam, 2026-11-25): no flag file, no maintainer predicate, no repo-is-nexus test."""
    src = (SCRIPTS / "_audience.py").read_text()
    body = src.split('"""', 2)[2]
    for gone in ("account_flag", "nexus_repo", "note_marker_only", "TRIPWIRE"):
        assert gone not in body, gone
    # The one place the retired flag file is named is the list `show` reports it from (RDR-259 Decision 5).
    uses = [line for line in body.splitlines() if "maintainer" in line]
    assert len(uses) == 1 and "RETIRED_USER_FILES" in uses[0], uses
    for name in ("account_flag", "maintainer", "nexus_repo", "note_marker_only"):
        assert not hasattr(_audience, name), name


def test_the_two_copies_of_the_helper_are_byte_identical():
    assert (SCRIPTS / "_audience.py").read_bytes() == (PROJECT_ROOT / "src" / "nexus" / "hooks" / "_audience.py").read_bytes()


# ---------------------------------------------------------------------------
# T11: the walk edges
# ---------------------------------------------------------------------------


def _mode(cwd) -> str:
    return _audience.guard_mode(_p(cwd))


def test_a_subdirectory_of_a_project_with_settings_reads_them(home, tmp_path):
    repo = strict_repo(tmp_path / "r")
    sub = pathlib.Path(repo) / "a" / "b"
    sub.mkdir(parents=True)
    assert _mode(sub) == "strict"


def test_a_nested_repo_is_its_own_project(home, tmp_path):
    outer = strict_repo(tmp_path / "outer")
    inner = _repo(pathlib.Path(outer) / "vendor" / "lib")
    assert _mode(inner) == "seatbelt"
    assert _audience.project_settings(_p(inner)) == {}


def test_a_submodule_stops_at_its_git_file(home, tmp_path):
    outer = strict_repo(tmp_path / "outer")
    sub = _repo(pathlib.Path(outer) / "sub", git_file=True)
    assert _mode(sub) == "seatbelt"


def test_a_linked_worktree_of_a_project_with_settings_reads_them(home, tmp_path):
    # A real linked worktree: its `.git` is a file, the settings file is tracked.
    primary = pathlib.Path(strict_repo(tmp_path / "primary"))
    shutil.rmtree(primary / ".git")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    cfg = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*cfg, "init", "-q", "-b", "main"], cwd=primary, check=True, env=env, capture_output=True)
    subprocess.run([*cfg, "add", ".nexus/project.json"], cwd=primary, check=True, env=env, capture_output=True)
    subprocess.run([*cfg, "commit", "-q", "-m", "m"], cwd=primary, check=True, env=env, capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run([*cfg, "worktree", "add", "-q", "-b", "b", str(wt)], cwd=primary, check=True, env=env, capture_output=True)
    assert (wt / ".git").is_file()
    assert _mode(wt) == "strict"
    # A worktree on a branch that predates the file has none, and the defaults decide.
    old = tmp_path / "old"
    subprocess.run([*cfg, "worktree", "add", "-q", "--orphan", "-b", "old", str(old)], cwd=primary, check=True, env=env, capture_output=True)
    assert not (old / ".nexus" / "project.json").exists()
    assert _mode(old) == "seatbelt"


def test_a_cwd_inside_the_git_directory_reaches_the_checkout_root(home, tmp_path):
    repo = pathlib.Path(strict_repo(tmp_path / "r"))
    hooks = repo / ".git" / "hooks"
    hooks.mkdir()
    assert _mode(hooks) == "strict"
    assert _mode(repo / ".git") == "strict"


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_cwd_is_resolved_first(home, tmp_path):
    repo = strict_repo(tmp_path / "r")
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert _mode(link) == "strict"


@pytest.mark.parametrize("cwd", ["${cwd}", "$CLAUDE_PROJECT_DIR/x", "relative/path", "", "\x00bad"])
def test_an_unusable_cwd_counts_as_no_path(home, cwd, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert _audience.project_root({"cwd": cwd}) is None
    assert _audience.project_settings({"cwd": cwd}) == {}
    assert _mode(cwd) == "seatbelt"


@pytest.mark.parametrize("payload", [None, {}, [], "text", 3])
def test_a_payload_without_a_cwd_names_no_checkout(home, payload, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert _audience.project_root(payload) is None
    assert _audience.project_settings(payload) == {}


def test_claude_project_dir_names_the_session_root_and_the_process_cwd_never_is(home, tmp_path, monkeypatch):
    strict = strict_repo(tmp_path / "strict")
    plain = plain_repo(tmp_path / "plain")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(strict)
    assert _audience.guard_mode({}) == "seatbelt"  # the process cwd is a project with settings, and is ignored
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(strict))
    assert _audience.guard_mode({}) == "strict"
    # RDR-259 Decision 1: the session root (CLAUDE_PROJECT_DIR) decides, and a payload cwd elsewhere can only add
    assert _audience.guard_mode({"cwd": str(plain)}) == "strict"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(plain))
    assert _audience.guard_mode({"cwd": str(strict)}) == "strict", "a cwd checkout that says strict adds it"
    assert _audience.guard_mode({"cwd": str(plain)}) == "seatbelt"


def test_a_path_with_no_repo_above_it_has_no_project(home, tmp_path):
    bare = tmp_path / "no" / "repo"
    bare.mkdir(parents=True)
    assert _audience.project_root(_p(bare)) is None
    assert _audience.project_root(_p(tmp_path / "does" / "not" / "exist")) is None


# ---------------------------------------------------------------------------
# The hook-error note
# ---------------------------------------------------------------------------


def _hook_log(h: pathlib.Path) -> str:
    f = h / ".config" / "nexus" / "logs" / "hook.log"
    return f.read_text() if f.exists() else ""


def test_the_hook_error_note_writes_once_per_session_through_an_exclusive_sentinel(home):
    (home / ".config" / "nexus" / "logs").mkdir(parents=True)
    payload = {"session_id": "sess-1"}
    _audience.note_hook_error(payload, "boom")
    _audience.note_hook_error(payload, "boom")
    assert _hook_log(home).count("hook error, call passed: boom") == 1
    assert (home / ".config" / "nexus" / "logs" / "audience-hook-error" / "sess-1").is_file()
    _audience.note_hook_error({"session_id": "sess-2"}, "boom")
    assert _hook_log(home).count("hook error, call passed: boom") == 2


def test_the_note_writes_nothing_when_the_logs_directory_is_absent(home):
    _audience.note_hook_error({"session_id": "s"}, "boom")
    assert not (home / ".config").exists()


def test_the_note_sanitises_the_session_id(home):
    (home / ".config" / "nexus" / "logs").mkdir(parents=True)
    _audience.note_hook_error({"session_id": "../../evil"}, "boom")
    sentinels = home / ".config" / "nexus" / "logs" / "audience-hook-error"
    assert [p.name for p in sentinels.iterdir()] == ["evil"]
    assert not (home / ".config" / "evil").exists()


def test_the_note_never_raises(home, monkeypatch):
    (home / ".config" / "nexus" / "logs").mkdir(parents=True)

    def boom(*a, **k):
        raise OSError("disk")

    setattr_in(monkeypatch, _audience, "os.open", boom)
    _audience.note_hook_error({"session_id": "s"}, "boom")


# ---------------------------------------------------------------------------
# D11: the exempt ssh hosts come from a file under the account home
# ---------------------------------------------------------------------------


def _hosts_file(h: pathlib.Path, text: str) -> None:
    f = h / ".config" / "nexus" / "remote-test-hosts"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def test_the_remote_hosts_file_format(home):
    assert _audience.remote_test_hosts() == frozenset()
    _hosts_file(home, "# my test boxes\nchas\nchas-test, devbox  # trailing note\n\n  qwent\n")
    assert _audience.remote_test_hosts() == frozenset({"chas", "chas-test", "devbox", "qwent"})


def test_a_missing_unreadable_or_broken_hosts_file_exempts_none(home, monkeypatch):
    assert _audience.remote_test_hosts() == frozenset()
    _hosts_file(home, "chas\n")

    def boom(*a, **k):
        raise OSError("no")

    monkeypatch.setattr(_audience, "account_home", boom)
    assert _audience.remote_test_hosts() == frozenset()


_SSH_WRITE = "ssh {host} 'git -C x add f'"


def _ssh_verdict(host: str, repo: pathlib.Path, acct: pathlib.Path, env_hosts: str | None) -> dict:
    payload = {
        "tool_name": "Bash", "tool_input": {"command": _SSH_WRITE.format(host=host)}, "cwd": str(repo),
        "agent_id": "aworker-x-1", "agent_type": "general-purpose", "session_id": "s1",
    }
    env = {
        **os.environ,
        "NX_HOOK_PYTHON": sys.executable,
        "NX_ROUTING_LOG_PATH": str(acct / "route.jsonl"),
        "NX_DROPPED_WRITES_LOG_PATH": str(acct / "dropped.jsonl"),
    }
    env.pop("NX_REMOTE_TEST_HOSTS", None)
    if env_hosts is not None:
        env["NX_REMOTE_TEST_HOSTS"] = env_hosts
    proc = subprocess.run(
        [sys.executable, str(HARNESS), str(acct), str(GUARD)],
        input=json.dumps(payload), capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"] if proc.stdout else {}


def test_the_account_home_file_names_the_exempt_hosts(tmp_path):
    repo = pathlib.Path(strict_repo(tmp_path / "r"))  # the strict guard owns the ssh exemption
    acct = tmp_path / "acct"
    acct.mkdir()
    assert _ssh_verdict("devbox", repo, acct, None).get("permissionDecision") == "deny"
    _hosts_file(acct, "devbox\nci-box\n")
    assert _ssh_verdict("devbox", repo, acct, None).get("permissionDecision") != "deny"
    assert _ssh_verdict("ci-box", repo, acct, None).get("permissionDecision") != "deny"
    assert _ssh_verdict("chas-test", repo, acct, None).get("permissionDecision") == "deny"


@pytest.mark.parametrize("env_hosts", ["devbox", "chas,chas-test,devbox", ""])
def test_a_project_env_naming_a_host_does_not_exempt_it(tmp_path, env_hosts):
    repo = pathlib.Path(strict_repo(tmp_path / "r"))
    acct = tmp_path / "acct"
    acct.mkdir()
    assert _ssh_verdict("devbox", repo, acct, env_hosts).get("permissionDecision") == "deny"
    # and the env value cannot widen a file that names other hosts
    _hosts_file(acct, "ci-box\n")
    assert _ssh_verdict("devbox", repo, acct, env_hosts).get("permissionDecision") == "deny"
    assert _ssh_verdict("ci-box", repo, acct, env_hosts).get("permissionDecision") != "deny"


# ---------------------------------------------------------------------------
# A seatbelt that raises is noted once per session, then the call passes
# ---------------------------------------------------------------------------


def test_a_seatbelt_exception_passes_the_call_and_is_logged_once_per_session(tmp_path):
    copy = _plugin_copy(tmp_path, (SCRIPTS / "_audience.py").read_text())
    (copy / "routing" / "_user_git_seatbelt.py").write_text(
        "def decide(command, cwd):\n    raise RuntimeError('boom')\n\n\ndef deny_text(hit):\n    return ''\n"
    )
    repo = plain_repo(tmp_path / "userrepo")  # no project file, no user file: the seatbelt mode
    acct = tmp_path / "acct"
    (acct / ".config" / "nexus" / "logs").mkdir(parents=True)
    payload = json.dumps({
        "tool_name": "Bash", "tool_input": {"command": "git push --force"}, "cwd": str(repo),
        "agent_id": "aworker-x-1", "agent_type": "general-purpose", "session_id": "s-err",
    })
    env = {**os.environ, "NX_HOOK_PYTHON": sys.executable,
           "NX_ROUTING_LOG_PATH": str(tmp_path / "route.jsonl"), "NX_DROPPED_WRITES_LOG_PATH": str(tmp_path / "dropped.jsonl")}
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, str(HARNESS), str(acct), str(copy / "routing" / GUARD.name)],
            input=payload, capture_output=True, text=True, timeout=30, env=env,
        )
        assert proc.returncode == 0 and proc.stdout == "", (proc.stdout, proc.stderr)
    log = _hook_log(acct)
    assert log.count("user-mode seatbelt raised RuntimeError") == 1, log
    assert not (tmp_path / "route.jsonl").exists(), "an error note must not post telemetry"
