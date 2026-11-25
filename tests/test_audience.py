# SPDX-License-Identifier: AGPL-3.0-or-later
"""T8, T10, T11 and the D11 file (nexus-sby49): the audience helper.

``conexus/hooks/scripts/_audience.py`` answers who a hook runs for. These tests
pin the contract of its three predicates and the walk edges of the repo marker:

* T8: a broken helper never widens anything. Tightening predicates answer True
  on error, ``account_flag`` answers False, a missing passwd entry is "no flag",
  and a plugin copy with no helper (or one that cannot import) runs the full guard.
* T10: the account home comes from the account database, not from ``HOME`` or
  ``USERPROFILE``; the only seam is the in-process override.
* T11: the walk edges (nested repo, submodule, linked worktree, ``.git/``
  cwd, symlink, unsubstituted ``${cwd}``).
* The remote-test-hosts file (D11): a repository's own env cannot name an
  exempt ssh host.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SCRIPTS = PROJECT_ROOT / "conexus" / "hooks" / "scripts"
ROUTING = SCRIPTS / "routing"
GUARD = ROUTING / "subagent_git_write_requires_orchestrator.py"
HARNESS = PROJECT_ROOT / "tests" / "hooks" / "audience_harness.py"
MARKER = "scripts/agent-worktree-preflight.sh"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import _audience  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch) -> pathlib.Path:
    """An account home with no flag, installed through the in-process seam."""
    h = tmp_path / "acct"
    h.mkdir()
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(h))
    return h


def _flag(h: pathlib.Path) -> None:
    f = h / ".config" / "nexus" / "maintainer-profile"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")


def _repo(path: pathlib.Path, *, marker: bool = False, git_file: bool = False) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    if git_file:
        (path / ".git").write_text("gitdir: /nonexistent\n")
    else:
        (path / ".git").mkdir()
    if marker:
        m = path / MARKER
        m.parent.mkdir(parents=True, exist_ok=True)
        m.write_text("#!/bin/sh\n")
    return path


def _p(cwd) -> dict:
    return {"cwd": str(cwd)}


# ---------------------------------------------------------------------------
# The predicates
# ---------------------------------------------------------------------------


def test_a_plain_repo_with_no_flag_is_a_user_repo(home, tmp_path):
    repo = _repo(tmp_path / "r")
    assert _audience.nexus_repo(_p(repo)) is False
    assert _audience.account_flag() is False
    assert _audience.maintainer(_p(repo)) is False


def test_the_marker_makes_a_repo_nexus_and_its_owner_a_maintainer(home, tmp_path):
    repo = _repo(tmp_path / "r", marker=True)
    assert _audience.nexus_repo(_p(repo)) is True
    assert _audience.maintainer(_p(repo)) is True


def test_the_phase_two_marker_is_accepted_too(home, tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".nexus").mkdir()
    (repo / ".nexus" / "dev-process.json").write_text("{}")
    assert _audience.nexus_repo(_p(repo)) is True


def test_the_account_flag_makes_any_repo_a_maintainer_repo(home, tmp_path):
    repo = _repo(tmp_path / "r")
    _flag(home)
    assert _audience.account_flag() is True
    assert _audience.nexus_repo(_p(repo)) is False
    assert _audience.maintainer(_p(repo)) is True


# ---------------------------------------------------------------------------
# T8: failure goes in the safe direction
# ---------------------------------------------------------------------------


def test_a_tightening_predicate_answers_true_when_it_raises(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(_audience.os.path, "realpath", boom)
    assert _audience.nexus_repo(_p(repo)) is True
    assert _audience.maintainer(_p(repo)) is True


def test_maintainer_answers_true_when_both_halves_raise(home, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(_audience, "account_flag", boom)
    monkeypatch.setattr(_audience, "nexus_repo", boom)
    assert _audience.maintainer(_p(tmp_path)) is True


def test_account_flag_answers_false_when_it_raises(home, monkeypatch):
    _flag(home)

    def boom(*a, **k):
        raise RuntimeError("broken")

    monkeypatch.setattr(_audience, "account_home", boom)
    assert _audience.account_flag() is False


@pytest.mark.skipif(os.name == "nt", reason="passwd entries are POSIX")
def test_a_missing_passwd_entry_means_no_flag_and_maintainer_equals_nexus_repo(tmp_path, monkeypatch):
    import pwd

    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", None)

    def no_entry(uid):
        raise KeyError(uid)

    monkeypatch.setattr(pwd, "getpwuid", no_entry)
    assert _audience.account_home() is None
    assert _audience.account_flag() is False
    plain = _repo(tmp_path / "plain")
    marked = _repo(tmp_path / "marked", marker=True)
    assert _audience.maintainer(_p(plain)) is _audience.nexus_repo(_p(plain)) is False
    assert _audience.maintainer(_p(marked)) is _audience.nexus_repo(_p(marked)) is True


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
@pytest.mark.parametrize("marked", [True, False])
def test_a_broken_or_missing_helper_gives_the_full_deny_everywhere(tmp_path, variant, marked):
    source = {
        "missing": None,
        "import_error": "raise RuntimeError('cannot import')\n",
        "syntax_error": "def (:\n",
    }[variant]
    copy = _plugin_copy(tmp_path, source)
    repo = _repo(tmp_path / "r", marker=marked)
    out = _run_guard(copy, "git commit -am x", repo)
    assert out.get("permissionDecision") == "deny"
    assert "STRUCTURE-AGNOSTIC" in out["permissionDecisionReason"]


def test_a_missing_seatbelt_module_also_gives_the_full_deny(tmp_path):
    copy = _plugin_copy(tmp_path, (SCRIPTS / "_audience.py").read_text())
    (copy / "routing" / "_user_git_seatbelt.py").unlink()
    repo = _repo(tmp_path / "r")  # a user repo: the seatbelt would pass a commit
    out = _run_guard(copy, "git commit -am x", repo, {"HOME": str(tmp_path / "nohome")})
    assert out.get("permissionDecision") == "deny"


# ---------------------------------------------------------------------------
# T10: the account home is the account database's, never an env variable
# ---------------------------------------------------------------------------


def test_the_seam_wins_and_env_home_variables_are_ignored(tmp_path, monkeypatch):
    plain = _repo(tmp_path / "plain")
    empty = tmp_path / "empty-env-home"
    empty.mkdir()
    flagged = tmp_path / "flagged"
    flagged.mkdir()
    _flag(flagged)
    monkeypatch.setenv("HOME", str(empty))
    monkeypatch.setenv("USERPROFILE", str(empty))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(empty))
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(flagged))
    assert _audience.maintainer(_p(plain)) is True


def test_an_env_home_holding_the_flag_does_not_make_a_maintainer(tmp_path, monkeypatch):
    plain = _repo(tmp_path / "plain")
    decoy = tmp_path / "decoy-home"
    decoy.mkdir()
    _flag(decoy)
    clean = tmp_path / "clean"
    clean.mkdir()
    monkeypatch.setenv("HOME", str(decoy))
    monkeypatch.setenv("USERPROFILE", str(decoy))
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(decoy / ".config" / "nexus"))
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", str(clean))
    assert _audience.account_flag() is False
    assert _audience.maintainer(_p(plain)) is False


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
    for name in ("HOME", "USERPROFILE", "NEXUS_CONFIG_DIR", "XDG_CONFIG_HOME"):
        assert f'"{name}"' not in src and f"'{name}'" not in src, name
    monkeypatch.setenv("_ACCOUNT_HOME_OVERRIDE", "/nonexistent")
    monkeypatch.setattr(_audience, "_ACCOUNT_HOME_OVERRIDE", None)
    assert _audience.account_home() != "/nonexistent"


# ---------------------------------------------------------------------------
# T11: the walk edges
# ---------------------------------------------------------------------------


def test_a_subdirectory_of_a_marked_checkout_is_nexus(home, tmp_path):
    repo = _repo(tmp_path / "r", marker=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert _audience.nexus_repo(_p(sub)) is True


def test_a_nested_repo_inside_nexus_is_its_own_project(home, tmp_path):
    outer = _repo(tmp_path / "outer", marker=True)
    inner = _repo(outer / "vendor" / "lib")
    assert _audience.nexus_repo(_p(inner)) is False
    assert _audience.maintainer(_p(inner)) is False


def test_a_submodule_stops_at_its_git_file(home, tmp_path):
    outer = _repo(tmp_path / "outer", marker=True)
    sub = _repo(outer / "sub", git_file=True)
    assert _audience.nexus_repo(_p(sub)) is False


def test_a_linked_worktree_of_a_marked_checkout_is_nexus(home, tmp_path):
    # A real linked worktree: its `.git` is a file, the marker is a tracked file.
    primary = _repo(tmp_path / "primary", marker=True)
    shutil.rmtree(primary / ".git")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    cfg = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*cfg, "init", "-q", "-b", "main"], cwd=primary, check=True, env=env, capture_output=True)
    subprocess.run([*cfg, "add", MARKER], cwd=primary, check=True, env=env, capture_output=True)
    subprocess.run([*cfg, "commit", "-q", "-m", "m"], cwd=primary, check=True, env=env, capture_output=True)
    wt = tmp_path / "wt"
    subprocess.run([*cfg, "worktree", "add", "-q", "-b", "b", str(wt)], cwd=primary, check=True, env=env, capture_output=True)
    assert (wt / ".git").is_file()
    assert _audience.nexus_repo(_p(wt)) is True
    # A worktree on a branch that predates the marker has none, and the flag decides.
    old = tmp_path / "old"
    subprocess.run([*cfg, "worktree", "add", "-q", "--orphan", "-b", "old", str(old)], cwd=primary, check=True, env=env, capture_output=True)
    assert not (old / MARKER).exists()
    assert _audience.nexus_repo(_p(old)) is False
    _flag(home)
    assert _audience.maintainer(_p(old)) is True


def test_a_cwd_inside_the_git_directory_reaches_the_checkout_root(home, tmp_path):
    repo = _repo(tmp_path / "r", marker=True)
    hooks = repo / ".git" / "hooks"
    hooks.mkdir()
    assert _audience.nexus_repo(_p(hooks)) is True
    assert _audience.nexus_repo(_p(repo / ".git")) is True


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_cwd_is_resolved_first(home, tmp_path):
    repo = _repo(tmp_path / "r", marker=True)
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert _audience.nexus_repo(_p(link)) is True


@pytest.mark.parametrize("cwd", ["${cwd}", "$CLAUDE_PROJECT_DIR/x", "relative/path", "", "\x00bad"])
def test_an_unusable_cwd_counts_as_no_path(home, cwd):
    assert _audience.nexus_repo({"cwd": cwd}) is False
    assert _audience.maintainer({"cwd": cwd}) is False


@pytest.mark.parametrize("payload", [None, {}, [], "text", 3])
def test_a_payload_without_a_cwd_names_no_checkout(home, payload, monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    assert _audience.nexus_repo(payload) is False


def test_claude_project_dir_is_the_fallback_and_the_process_cwd_never_is(home, tmp_path, monkeypatch):
    marked = _repo(tmp_path / "marked", marker=True)
    plain = _repo(tmp_path / "plain")
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(marked)
    assert _audience.nexus_repo({}) is False  # the process cwd is a nexus checkout, and is ignored
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(marked))
    assert _audience.nexus_repo({}) is True
    assert _audience.nexus_repo({"cwd": str(plain)}) is False  # the payload wins


def test_a_path_with_no_repo_above_it_is_not_nexus(home, tmp_path):
    bare = tmp_path / "no" / "repo"
    bare.mkdir(parents=True)
    assert _audience.nexus_repo(_p(bare)) is False
    assert _audience.nexus_repo(_p(tmp_path / "does" / "not" / "exist")) is False


# ---------------------------------------------------------------------------
# The tripwire
# ---------------------------------------------------------------------------


def _hook_log(h: pathlib.Path) -> str:
    f = h / ".config" / "nexus" / "logs" / "hook.log"
    return f.read_text() if f.exists() else ""


def test_the_tripwire_writes_once_per_session_through_an_exclusive_sentinel(home, tmp_path):
    repo = _repo(tmp_path / "r", marker=True)
    (home / ".config" / "nexus" / "logs").mkdir(parents=True)
    payload = {"cwd": str(repo), "session_id": "sess-1"}
    _audience.note_marker_only(payload)
    _audience.note_marker_only(payload)
    assert _hook_log(home).count(_audience.TRIPWIRE_LINE) == 1
    assert (home / ".config" / "nexus" / "logs" / "audience-tripwire" / "sess-1").is_file()
    _audience.note_marker_only({**payload, "session_id": "sess-2"})
    assert _hook_log(home).count(_audience.TRIPWIRE_LINE) == 2


def test_the_tripwire_writes_nothing_when_the_logs_directory_is_absent(home, tmp_path):
    repo = _repo(tmp_path / "r", marker=True)
    _audience.note_marker_only({"cwd": str(repo), "session_id": "s"})
    assert not (home / ".config").exists()


def test_the_tripwire_is_silent_with_the_flag_or_without_the_marker(home, tmp_path):
    (home / ".config" / "nexus" / "logs").mkdir(parents=True)
    marked = _repo(tmp_path / "m", marker=True)
    plain = _repo(tmp_path / "p")
    _audience.note_marker_only({"cwd": str(plain), "session_id": "s"})
    assert _hook_log(home) == ""
    _flag(home)
    _audience.note_marker_only({"cwd": str(marked), "session_id": "s"})
    assert _hook_log(home) == ""


def test_the_tripwire_sanitises_the_session_id(home, tmp_path):
    repo = _repo(tmp_path / "r", marker=True)
    (home / ".config" / "nexus" / "logs").mkdir(parents=True)
    _audience.note_marker_only({"cwd": str(repo), "session_id": "../../evil"})
    sentinels = home / ".config" / "nexus" / "logs" / "audience-tripwire"
    assert [p.name for p in sentinels.iterdir()] == ["evilevil"] or len(list(sentinels.iterdir())) == 1
    assert not (home / ".config" / "evil").exists()


def test_the_tripwire_never_raises(home, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r", marker=True)
    (home / ".config" / "nexus" / "logs").mkdir(parents=True)

    def boom(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(_audience.os, "open", boom)
    _audience.note_marker_only({"cwd": str(repo), "session_id": "s"})


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
    repo = _repo(tmp_path / "r", marker=True)  # the maintainer path owns the ssh exemption
    acct = tmp_path / "acct"
    acct.mkdir()
    assert _ssh_verdict("devbox", repo, acct, None).get("permissionDecision") == "deny"
    _hosts_file(acct, "devbox\nci-box\n")
    assert _ssh_verdict("devbox", repo, acct, None).get("permissionDecision") != "deny"
    assert _ssh_verdict("ci-box", repo, acct, None).get("permissionDecision") != "deny"
    assert _ssh_verdict("chas-test", repo, acct, None).get("permissionDecision") == "deny"


@pytest.mark.parametrize("env_hosts", ["devbox", "chas,chas-test,devbox", ""])
def test_a_project_env_naming_a_host_does_not_exempt_it(tmp_path, env_hosts):
    repo = _repo(tmp_path / "r", marker=True)
    acct = tmp_path / "acct"
    acct.mkdir()
    assert _ssh_verdict("devbox", repo, acct, env_hosts).get("permissionDecision") == "deny"
    # and the env value cannot widen a file that names other hosts
    _hosts_file(acct, "ci-box\n")
    assert _ssh_verdict("devbox", repo, acct, env_hosts).get("permissionDecision") == "deny"
    assert _ssh_verdict("ci-box", repo, acct, env_hosts).get("permissionDecision") != "deny"
