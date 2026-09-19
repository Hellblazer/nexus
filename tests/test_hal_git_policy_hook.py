# SPDX-License-Identifier: AGPL-3.0-or-later
"""Coverage for Hal's extracted personal git-policy hook (nexus-ww9fw,
2026-08-18): wildcard `git add` staging + push-to-main.

These two checks used to live inside the plugin's
``git_add_all_redirects_to_explicit_paths.py`` (RDR-121 Phase 2 hook 3).
Hal ruled 2026-08-18 that they are his own standing workflow preferences,
not general conexus-plugin features, and moved them into a standalone
user-level hook delivered outside this repo. This file tests
``tests/fixtures/hal_git_policy_hook.py`` -- a checked-in COPY of that
extraction (see the fixture's own header) -- so the behavior stays under
CI even though the real installed copy lives outside version control.
Drift between the fixture and Hal's actually-installed copy is accepted;
see the fixture's docstring.

Retargeted from (now deleted) ``tests/test_routing_git_add_all.py`` and
``tests/test_routing_no_direct_push_to_main.py``, minus the tests that
were specific to the PLUGIN's registry/hooks.json wiring and the
nexus-vscgz repo-scope guard -- the extracted hook has neither (it is
installed by Hal wherever he wants, not gated to nexus checkouts).
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
HOOK = PROJECT_ROOT / "tests" / "fixtures" / "hal_git_policy_hook.py"


def _run(payload: dict):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, env=os.environ.copy(),
    )


def _decision(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _bash(cmd: str, cwd: str | None = None) -> dict:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


def _git(*args: str, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))


def test_script_exists():
    assert HOOK.exists()


# ── Rule 1: wildcard `git add` ──────────────────────────────────────────────


def test_git_add_dash_A_denies():
    d = _decision(_run(_bash("git add -A")))
    assert d["permissionDecision"] == "deny"
    assert "explicit" in d["reason"].lower() or "path" in d["reason"].lower()


def test_git_add_dot_denies():
    d = _decision(_run(_bash("git add .")))
    assert d["permissionDecision"] == "deny"


def test_git_add_all_long_flag_denies():
    d = _decision(_run(_bash("git add --all")))
    assert d["permissionDecision"] == "deny"


def test_git_add_all_with_pathspec_denies():
    d = _decision(_run(_bash("git add --all src/")))
    assert d["permissionDecision"] == "deny"


def test_chained_git_add_dot_denies():
    """`git status && git add . && git commit` still triggers."""
    d = _decision(_run(_bash("git status && git add . && git commit -m foo")))
    assert d["permissionDecision"] == "deny"


def test_git_add_explicit_paths_allows():
    d = _decision(_run(_bash("git add src/foo.py tests/test_foo.py")))
    assert d["permissionDecision"] == "allow"


def test_git_add_single_dotfile_allows():
    """`git add .gitignore` is explicit, not wildcard."""
    d = _decision(_run(_bash("git add .gitignore")))
    assert d["permissionDecision"] == "allow"


def test_git_status_allows():
    d = _decision(_run(_bash("git status")))
    assert d["permissionDecision"] == "allow"


def test_non_git_command_allows():
    d = _decision(_run(_bash("ls -A")))
    assert d["permissionDecision"] == "allow"


def test_non_bash_allows():
    d = _decision(_run({"tool_name": "Edit", "tool_input": {"file_path": "x"}}))
    assert d["permissionDecision"] == "allow"


def test_wildcard_add_escape_allows():
    d = _decision(_run(_bash(
        "git add -A  # routing-allow: scripted bootstrap of fresh repo"
    )))
    assert d["permissionDecision"] == "allow"


def test_empty_stdin_allows():
    proc = subprocess.run(
        [sys.executable, str(HOOK)],
        input="", capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0
    d = json.loads(proc.stdout)["hookSpecificOutput"]
    assert d["permissionDecision"] == "allow"


def test_escape_on_nonmatching_command_logs_nothing(tmp_path, monkeypatch):
    """nexus-mzvwa.8's match-first-escape-second lesson applies here too:
    an escape token on a non-matching command must not log a phantom
    escape event."""
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log))
    d = _decision(_run(
        _bash("bd close nexus-xyz --reason done  # routing-allow: gate satisfied")
    ))
    assert d["permissionDecision"] == "allow"
    assert not log.exists() or log.read_text().strip() == "", (
        "non-matching annotated command must log NOTHING (phantom escape)"
    )


def test_escape_on_matching_command_logs_true_escape(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(log))
    d = _decision(_run(
        _bash("git add -A  # routing-allow: scripted bootstrap of fresh repo")
    ))
    assert d["permissionDecision"] == "allow"
    events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(events) == 1
    assert events[0]["outcome"] == "escape"
    assert events[0]["rule"] == "nexus_git_policy"


# ── Rule 2: push-to-main (nexus-vduer) ──────────────────────────────────────
#
# THE INCIDENT (2026-07-23, self-reported, in the nexus repo). During the
# P4b Phase 0c commit the orchestrator pushed directly to main. Session
# restarts had left the working tree on main and verify-branch-before-commit
# was a MEMORY-ONLY control, so it failed the way memory-only controls fail.
# No damage, but the lesson was: mechanize, not write a retro note. The
# checkout was ALREADY on main, so a bare `git push` inherited the target
# from the branch's upstream -- a matcher looking for the literal token
# would have missed the exact event it exists to prevent. So the tests
# drive real git repos rather than asserting on strings.


@pytest.fixture()
def repo_on(tmp_path):
    def _make(branch: str):
        slug = branch.replace("/", "-")
        origin = tmp_path / f"origin-{slug}"
        origin.mkdir(parents=True)
        _git("init", "-q", "--bare", "--initial-branch=main", cwd=origin)

        work = tmp_path / f"work-{slug}"
        work.mkdir()
        _git("init", "-q", "--initial-branch=main", cwd=work)
        _git("remote", "add", "origin", str(origin), cwd=work)
        (work / "f.txt").write_text("x")
        _git("add", "f.txt", cwd=work)
        _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i",
             cwd=work)
        _git("push", "-q", "-u", "origin", "main", cwd=work)
        if branch != "main":
            _git("checkout", "-q", "-b", branch, cwd=work)
            _git("push", "-q", "-u", "origin", branch, cwd=work)
        return work
    return _make


def test_bare_push_from_a_checkout_on_main_is_blocked(repo_on):
    """THE regression. No "main" anywhere in the command."""
    work = repo_on("main")
    out = _decision(_run(_bash("git push", str(work))))
    assert out["permissionDecision"] == "deny", out
    assert "PRs only" in out["permissionDecisionReason"]


def test_bare_push_from_a_feature_branch_is_allowed(repo_on):
    work = repo_on("feature/x")
    out = _decision(_run(_bash("git push", str(work))))
    assert out["permissionDecision"] == "allow", out


@pytest.mark.parametrize("cmd", [
    "git push origin main",
    "git push origin HEAD:main",
    "git push origin +main",
    "git push origin develop:main",
    "git push -f origin main",
    "git push origin refs/heads/main",
])
def test_explicit_main_refspecs_are_blocked(cmd, repo_on):
    work = repo_on("feature/x")   # branch is irrelevant; the refspec decides
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "deny", f"{cmd}: {out}"


@pytest.mark.parametrize("cmd", [
    "git push origin develop",
    "git push origin main:develop",      # main is the SOURCE, develop the target
    "git push origin feature/x",
])
def test_pushes_to_other_branches_are_allowed(cmd, repo_on):
    work = repo_on("feature/x")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


@pytest.mark.parametrize("cmd", [
    "git push origin v1.2.3",
    "git push origin engine-service-v0.1.56",
    "git push --tags",
    "git push origin refs/tags/v1.2.3",
])
def test_tag_pushes_are_allowed_even_from_main(cmd, repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


@pytest.mark.parametrize("cmd", [
    "git push --follow-tags",              # bare: pushes the branch too
    "git push --follow-tags origin main",
    "git push --tags origin main",         # explicit branch refspec alongside tags
    "git push --tags origin HEAD:main",
])
def test_tag_flags_do_not_exempt_a_branch_push(cmd, repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "deny", (
        f"{cmd!r} pushes the BRANCH as well as tags -- a tag flag must not "
        f"blanket-exempt it: {out}"
    )


@pytest.mark.parametrize("cmd", [
    "git push --tags",
    "git push --tags origin",
])
def test_a_bare_tags_push_is_still_allowed(cmd, repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash(cmd, str(work))))
    assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


def test_follow_tags_from_a_feature_branch_is_allowed(repo_on):
    work = repo_on("feature/x")
    out = _decision(_run(_bash("git push --follow-tags", str(work))))
    assert out["permissionDecision"] == "allow", out


def test_push_to_main_escape_permits_the_release_version_bump(repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash(
        "git push origin main  # routing-allow: release version bump",
        str(work),
    )))
    assert out["permissionDecision"] == "allow", out


def test_push_hidden_in_a_compound_command_is_caught(repo_on):
    work = repo_on("main")
    out = _decision(_run(_bash("uv run pytest -q && git push", str(work))))
    assert out["permissionDecision"] == "deny", out


def test_non_push_git_commands_are_untouched(repo_on):
    work = repo_on("main")
    for cmd in ("git status", "git log --oneline -3", "git fetch", "git diff"):
        out = _decision(_run(_bash(cmd, str(work))))
        assert out["permissionDecision"] == "allow", f"{cmd}: {out}"


def test_bare_push_with_stdout_redirect_from_main_is_still_blocked(repo_on):
    """nexus-cr4lp B1: a phantom refspec manufactured from shell redirection
    tokens (``>``, ``/dev/null``) must not defeat the guard."""
    work = repo_on("main")
    out = _decision(_run(_bash("git push > /dev/null", str(work))))
    assert out["permissionDecision"] == "deny", out
    assert "PRs only" in out["permissionDecisionReason"]


def test_fails_open_outside_a_repo(tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    out = _decision(_run(_bash("git push", str(bare))))
    assert out["permissionDecision"] == "allow", out


def test_master_default_branch_is_also_protected(tmp_path):
    """Unlike the plugin's departed check, this hook has no nexus-repo
    scope guard -- Hal installs it wherever he wants, so it must protect
    `master` (his other repos' default) the same as `main`."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git("init", "-q", "--bare", "--initial-branch=master", cwd=origin)
    work = tmp_path / "work"
    work.mkdir()
    _git("init", "-q", "--initial-branch=master", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)
    (work / "f.txt").write_text("x")
    _git("add", "f.txt", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i", cwd=work)
    _git("push", "-q", "-u", "origin", "master", cwd=work)
    out = _decision(_run(_bash("git push", str(work))))
    assert out["permissionDecision"] == "deny", out


# ---------------------------------------------------------------------------
# nexus-2e874: malformed quoting must degrade safely, never silently bypass.
# ---------------------------------------------------------------------------


def test_wildcard_add_with_unbalanced_quote_is_still_denied():
    """nexus-2e874: an unbalanced quote in the same segment used to make
    shlex reject it and the whole segment was silently SKIPPED -- a full
    bypass of rule 1. The degraded whitespace fallback keeps the
    `git add -A` anchor visible."""
    d = _decision(_run(_bash('git add -A "oops')))
    assert d["permissionDecision"] == "deny"


def test_push_to_main_with_unbalanced_quote_is_still_blocked(repo_on):
    """nexus-2e874 live specimen: `git push origin main --receive-pack="x`
    was ALLOWed with zero context (rule 2 fully bypassed)."""
    work = repo_on("feature/x")
    out = _decision(_run(_bash(
        'git push origin main --receive-pack="unterminated', str(work),
    )))
    assert out["permissionDecision"] == "deny", out


def test_unbalanced_quote_on_a_feature_push_is_still_allowed(repo_on):
    """The degraded parse must not over-deny: a malformed-quote push whose
    destination is NOT protected stays allowed."""
    work = repo_on("feature/x")
    out = _decision(_run(_bash(
        'git push origin feature/x --receive-pack="unterminated', str(work),
    )))
    assert out["permissionDecision"] == "allow", out


def test_quote_inside_the_verb_is_still_blocked(repo_on):
    """Review Important-1 (nexus-2e874): a quote INSIDE the verb fractures
    the quote-as-space variant ('gi', 't', ...) -- the quote-removed
    variant must catch it."""
    work = repo_on("feature/x")
    out = _decision(_run(_bash('gi"t push origin main', str(work))))
    assert out["permissionDecision"] == "deny", out


# ── Rule 3: `git commit --amend` on a foreign tip in the primary (nexus-9wxu6) ──
#
# THE INCIDENT (2026-09-07, five sessions in one checkout). An amend in the
# shared primary rewrote a peer's commit, because HEAD had moved under the
# session between its commit and its amend. Ownership is read from the
# companion PostToolUse recorder's per-session file, never from the author
# field (every session commits as the same user).

RECORDER = PROJECT_ROOT / "tests" / "fixtures" / "hal_record_session_commits_hook.py"


def _run_recorder(payload: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RECORDER)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, env={**os.environ, **env},
    )


def _run_with_env(payload: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, env={**os.environ, **env},
    )


def _commit(work, name: str) -> str:
    (work / name).write_text(name)
    _git("add", name, cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name, cwd=work)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def primary(repo_on, tmp_path):
    """A primary checkout that is SHARED, i.e. has a linked worktree.

    Rule 5 only applies where a linked worktree exists (Sam, 2026-09-18) --
    that is the observable signature of a checkout several sessions commit
    into, and without it the rule fired in every throwaway repo on the
    machine. Rule 3's tests are unaffected by the extra worktree; they ask
    about HEAD ownership, not about worktrees.
    """
    work = repo_on("develop")
    _git("worktree", "add", "-q", "--detach", str(tmp_path / "linked-wt"), "HEAD", cwd=work)
    store = tmp_path / "session_commits"
    return work, store, {"NX_SESSION_COMMITS_DIR": str(store)}


def _amend_payload(work, session_id: str | None = "sess-A", cmd: str = "git commit --amend --no-edit") -> dict:
    payload = _bash(cmd, cwd=str(work))
    if session_id is not None:
        payload["session_id"] = session_id
    return payload


def test_amend_in_primary_with_unrecorded_head_is_denied(primary):
    work, _store, env = primary
    d = _decision(_run_with_env(_amend_payload(work), env))
    assert d["permissionDecision"] == "deny"
    assert "nexus-9wxu6" in d["reason"]


def test_amend_in_primary_on_own_recorded_commit_is_allowed(primary):
    work, store, env = primary
    sha = _commit(work, "mine")
    recorded = _run_recorder({**_bash("git commit -q -m mine", cwd=str(work)), "session_id": "sess-A"}, env)
    assert recorded.returncode == 0 and recorded.stdout == ""
    assert (store / "sess-A").read_text().split() == [sha]
    # Rule 5 also applies to an amend -- it re-commits the whole index, so a
    # peer's staged file rides it exactly as it rides a fresh bare commit.
    # Rule 3's "your own tip" permission is necessary, not sufficient.
    d = _decision(_run_with_env(_amend_payload(work, cmd="git commit --amend --no-edit -- mine"), env))
    assert d["permissionDecision"] == "allow", d


def test_amend_after_a_peer_commit_on_top_is_denied(primary):
    work, _store, env = primary
    _commit(work, "mine")
    _run_recorder({**_bash("git commit -q -m mine", cwd=str(work)), "session_id": "sess-A"}, env)
    _commit(work, "peer")
    _run_recorder({**_bash("git commit -q -m peer", cwd=str(work)), "session_id": "sess-B"}, env)
    d = _decision(_run_with_env(_amend_payload(work, "sess-A"), env))
    assert d["permissionDecision"] == "deny"
    d = _decision(_run_with_env(
        _amend_payload(work, "sess-B", cmd="git commit --amend --no-edit -- peer"), env))
    assert d["permissionDecision"] == "allow", d


def test_amend_without_a_session_id_is_denied(primary):
    work, _store, env = primary
    sha = _commit(work, "mine")
    _run_recorder({**_bash("git commit -q -m mine", cwd=str(work)), "session_id": "sess-A"}, env)
    d = _decision(_run_with_env(_amend_payload(work, session_id=None), env))
    assert d["permissionDecision"] == "deny"
    assert sha[:12] in d["reason"]


def test_amend_in_a_linked_worktree_is_never_blocked(primary, tmp_path):
    work, _store, env = primary
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", "--detach", str(wt), "HEAD", cwd=work)
    d = _decision(_run_with_env(_amend_payload(wt), env))
    assert d["permissionDecision"] == "allow"


def test_amend_via_dash_C_targets_the_named_repo(primary, tmp_path):
    work, _store, env = primary
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = _bash(f"git -C {work} commit --amend --no-edit", cwd=str(elsewhere))
    payload["session_id"] = "sess-A"
    d = _decision(_run_with_env(payload, env))
    assert d["permissionDecision"] == "deny"


def test_plain_commit_in_primary_is_now_rule_5s(primary):
    """This test used to assert ALLOW, and RULE 5 deliberately reverses it.

    Rule 3 (amend on a foreign tip) still does not fire here -- there is no
    ``--amend`` -- but a bare commit in the shared primary is exactly the
    nexus-bbriq defect, so it is denied now, by a different rule and with a
    different message.
    """
    work, _store, env = primary
    d = _decision(_run_with_env({**_bash("git commit -m x", cwd=str(work)), "session_id": "s"}, env))
    assert d["permissionDecision"] == "deny", d
    assert "whole index" in d["permissionDecisionReason"]


def test_amend_hidden_in_a_compound_command_is_caught(primary):
    work, _store, env = primary
    cmd = "git status && git commit --amend --no-edit"
    d = _decision(_run_with_env(_amend_payload(work, cmd=cmd), env))
    assert d["permissionDecision"] == "deny"


def test_amend_escape_allows_and_logs(primary, tmp_path, monkeypatch):
    work, _store, env = primary
    log = tmp_path / "log.jsonl"
    env = {**env, "NX_ROUTING_LOG_PATH": str(log)}
    cmd = "git commit --amend --no-edit  # routing-allow: HEAD predates the recorder"
    d = _decision(_run_with_env(_amend_payload(work, cmd=cmd), env))
    assert d["permissionDecision"] == "allow"
    events = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert [e["outcome"] for e in events] == ["escape"]


def test_amend_outside_a_repo_fails_open(tmp_path):
    payload = {**_bash("git commit --amend", cwd=str(tmp_path)), "session_id": "s"}
    d = _decision(_run_with_env(payload, {"NX_SESSION_COMMITS_DIR": str(tmp_path / "sc")}))
    assert d["permissionDecision"] == "allow"


def test_recorder_records_dash_C_and_dedupes(primary, tmp_path):
    work, store, env = primary
    sha = _commit(work, "one")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    payload = {**_bash(f"git -C {work} commit -m one", cwd=str(elsewhere)), "session_id": "sess-A"}
    _run_recorder(payload, env)
    _run_recorder(payload, env)
    assert (store / "sess-A").read_text().split() == [sha]


def test_recorder_ignores_non_commit_commands(primary):
    work, store, env = primary
    _run_recorder({**_bash("git status", cwd=str(work)), "session_id": "sess-A"}, env)
    assert not (store / "sess-A").exists()


def test_amend_glued_to_a_closing_paren_is_caught(primary):
    work, _store, env = primary
    cmd = f"(cd {work} && git commit --amend --no-edit)"
    d = _decision(_run_with_env(_amend_payload(work, cmd=cmd), env))
    assert d["permissionDecision"] == "deny"


def test_recorder_prefers_the_sha_git_commit_printed(primary):
    work, store, env = primary
    mine = _commit(work, "mine")
    peer = _commit(work, "peer-landed-before-the-hook-ran")
    payload = {**_bash("git commit -m mine", cwd=str(work)), "session_id": "sess-A",
               "tool_response": {"stdout": f"[develop {mine[:7]}] mine\n 1 file changed\n"}}
    _run_recorder(payload, env)
    assert (store / "sess-A").read_text().split() == [mine]
    assert peer not in (store / "sess-A").read_text()


# ── Rule 4: bare `git push` to develop where the vouched script exists ──────


def _with_script(work):
    (work / "scripts").mkdir(exist_ok=True)
    (work / "scripts" / "git-push-develop.sh").write_text("#!/bin/sh\n")


@pytest.mark.parametrize("cmd", ["git push", "git push origin develop", "git push -u origin HEAD:develop",
                                 "git fetch && git push origin develop"])
def test_bare_push_to_develop_is_blocked_where_the_script_exists(cmd, repo_on):
    work = repo_on("develop")
    _with_script(work)
    d = _decision(_run(_bash(cmd, cwd=str(work))))
    assert d["permissionDecision"] == "deny"
    assert "git-push-develop.sh" in d["reason"]


def test_bare_push_to_develop_is_allowed_where_no_script_exists(repo_on):
    work = repo_on("develop")
    d = _decision(_run(_bash("git push origin develop", cwd=str(work))))
    assert d["permissionDecision"] == "allow"


@pytest.mark.parametrize("cmd", ["git push origin feature/x", "git push origin v1.2.3", "git push --tags",
                                 "scripts/git-push-develop.sh abc1234"])
def test_other_pushes_and_the_script_itself_are_allowed(cmd, repo_on):
    work = repo_on("develop")
    _with_script(work)
    d = _decision(_run(_bash(cmd, cwd=str(work))))
    assert d["permissionDecision"] == "allow"


def test_bare_push_to_develop_escape_allows(repo_on):
    work = repo_on("develop")
    _with_script(work)
    d = _decision(_run(_bash("git push origin develop  # routing-allow: release back-merge", cwd=str(work))))
    assert d["permissionDecision"] == "allow"


# ── Rule 5: an unscoped `git commit` in the shared primary (nexus-bbriq) ────
#
# THE INCIDENT (2026-09-17). A peer had staged a 740-line
# docs/rdr/rdr-212-*.md draft in the shared index. An accept commit ran
# `git add <two paths>` then a BARE `git commit`, which commits the whole
# index, so the peer's unreviewed draft rode 0249b0c98 to origin/develop.
# The nexus-9wxu6 push script vouches commits, not index contents.


def _commit_payload(work, cmd: str) -> dict:
    return {**_bash(cmd, cwd=str(work)), "session_id": "sess-A"}


@pytest.mark.parametrize("cmd", [
    "git commit -m x",
    'git commit -m "a message"',
    "git commit",
    "git commit --no-verify -m x",
    "git commit -F -",
    "git status --short && git commit -m x",
])
def test_unscoped_commit_in_primary_is_denied(cmd, primary):
    work, _store, env = primary
    d = _decision(_run_with_env(_commit_payload(work, cmd), env))
    assert d["permissionDecision"] == "deny", f"{cmd}: {d}"
    assert "whole index" in d["permissionDecisionReason"], cmd


@pytest.mark.parametrize("cmd", [
    "git commit -a -m x",
    "git commit -am x",
    "git commit --all -m x",
    "git commit -av -m x",
])
def test_stage_all_commit_in_primary_is_denied(cmd, primary):
    """`-a` cannot take a pathspec at all, so it gets its own message."""
    work, _store, env = primary
    d = _decision(_run_with_env(_commit_payload(work, cmd), env))
    assert d["permissionDecision"] == "deny", f"{cmd}: {d}"
    assert "stages every tracked modification" in d["permissionDecisionReason"], cmd


@pytest.mark.parametrize("cmd", [
    "git commit -m x -- f.txt",
    "git commit -m x -- f.txt other.txt",
    'git commit -m "msg with -- inside" -- f.txt',
    "git commit --allow-empty -m x",
])
def test_scoped_or_empty_commit_in_primary_is_allowed(cmd, primary):
    work, _store, env = primary
    d = _decision(_run_with_env(_commit_payload(work, cmd), env))
    assert d["permissionDecision"] == "allow", f"{cmd}: {d}"


def test_unscoped_commit_in_a_linked_worktree_is_allowed(primary, tmp_path):
    """The rule is about the SHARED index, which a linked worktree does not have."""
    work, _store, env = primary
    wt = tmp_path / "wt-rule5"
    _git("worktree", "add", "-q", "--detach", str(wt), "HEAD", cwd=work)
    d = _decision(_run_with_env(_commit_payload(wt, "git commit -m x"), env))
    assert d["permissionDecision"] == "allow", d


def test_unscoped_commit_during_a_merge_is_allowed(primary):
    """git itself refuses a partial commit mid-merge, so demanding one would
    block conflict resolution and the mandatory post-release back-merge."""
    work, _store, env = primary
    _git("checkout", "-q", "-b", "side", cwd=work)
    (work / "f.txt").write_text("side")
    _git("add", "f.txt", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "side", cwd=work)
    _git("checkout", "-q", "develop", cwd=work)
    (work / "f.txt").write_text("develop")
    _git("add", "f.txt", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "dev", cwd=work)
    subprocess.run(["git", "merge", "side"], cwd=work, capture_output=True)
    assert (work / ".git" / "MERGE_HEAD").exists(), "fixture did not produce a conflicted merge"
    d = _decision(_run_with_env(_commit_payload(work, "git commit --no-edit"), env))
    assert d["permissionDecision"] == "allow", d


def test_unscoped_commit_via_dash_C_targets_the_named_repo(primary, tmp_path):
    """`git -C <primary> commit` from elsewhere is still a primary commit."""
    work, _store, env = primary
    outside = tmp_path / "outside-rule5"
    outside.mkdir()
    d = _decision(_run_with_env(
        {**_bash(f"git -C {work} commit -m x", cwd=str(outside)), "session_id": "sess-A"}, env))
    assert d["permissionDecision"] == "deny", d


def test_unscoped_commit_escape_allows(primary):
    work, _store, env = primary
    cmd = "git commit -m x  # routing-allow: rebuilding an index git mangled"
    d = _decision(_run_with_env(_commit_payload(work, cmd), env))
    assert d["permissionDecision"] == "allow", d


def test_rule_5_message_carries_both_pathspec_traps(primary):
    """The message is where the two lessons a pathspec does NOT teach live."""
    work, _store, env = primary
    d = _decision(_run_with_env(_commit_payload(work, "git commit -m x"), env))
    reason = d["permissionDecisionReason"]
    assert "WORKING TREE version" in reason
    assert "STRANDS THE DELETE" in reason
    assert "0249b0c98" in reason


def test_a_bare_amend_on_your_own_tip_is_still_denied_by_rule_5(primary):
    """Rule 3 and Rule 5 are independent gates and an amend must clear BOTH.

    Rule 3 asks "is HEAD yours to rewrite". Rule 5 asks "are you naming what
    you are committing". An amend re-commits the whole index, so a peer's
    staged file rides an amend exactly as it rides a fresh bare commit --
    passing Rule 3 says nothing about that. Leaving amend exempt would have
    left the nexus-bbriq defect reachable through the one commit form the
    hook already had an opinion about.
    """
    work, store, env = primary
    sha = _commit(work, "mine")
    _run_recorder({**_bash("git commit -q -m mine", cwd=str(work)), "session_id": "sess-A"}, env)
    assert (store / "sess-A").read_text().split() == [sha]
    d = _decision(_run_with_env(_amend_payload(work), env))
    assert d["permissionDecision"] == "deny", d
    assert "whole index" in d["permissionDecisionReason"]


def test_a_cd_moves_which_repo_is_judged_and_a_solo_repo_is_exempt(primary, tmp_path):
    """A PreToolUse hook is handed the SESSION's cwd, not the directory the
    command runs in, so `cd elsewhere && git commit` was judged against the
    session's checkout. Found 2026-09-18 by the rule refusing its own author.

    Two properties in one test, because each is the other's control. The
    `cd` is HONOURED, so the verdict comes from the scratch repo rather than
    the session's checkout; and a solo repo with no linked worktree is
    EXEMPT, because rule 5 is about a checkout several sessions share and
    `_is_primary_checkout` alone would cover every repo on the machine
    (Sam, 2026-09-18).

    Asserted together so neither can pass for the wrong reason: if the cd
    were ignored, the first case would inherit the shared checkout's deny;
    if the rule had simply stopped working, the second case would not still
    deny.
    """
    work, _store, env = primary
    scratch = tmp_path / "scratch-repo"
    scratch.mkdir()
    _git("init", "-q", "--initial-branch=main", cwd=scratch)
    cmd = f"cd {scratch} && git commit -m x"
    d = _decision(_run_with_env({**_bash(cmd, cwd=str(work)), "session_id": "s"}, env))
    assert d["permissionDecision"] == "allow", d

    # Non-vacuity: the SAME command, judged against the shared checkout, is
    # denied. So the allow above comes from the cd being honoured, not from
    # the rule having quietly stopped working.
    d2 = _decision(_run_with_env({**_bash("git commit -m x", cwd=str(work)), "session_id": "s"}, env))
    assert d2["permissionDecision"] == "deny", d2


def test_a_cd_AFTER_the_commit_does_not_excuse_it(primary, tmp_path):
    """Only a `cd` BEFORE the commit moves where it runs. Otherwise
    `git commit -m x && cd /tmp` would be a one-token bypass."""
    work, _store, env = primary
    scratch = tmp_path / "scratch-after"
    scratch.mkdir()
    _git("init", "-q", "--initial-branch=main", cwd=scratch)
    cmd = f"git commit -m x && cd {scratch}"
    d = _decision(_run_with_env({**_bash(cmd, cwd=str(work)), "session_id": "s"}, env))
    assert d["permissionDecision"] == "deny", d


def test_a_commit_mentioned_inside_a_heredoc_is_not_a_commit(primary):
    """REGRESSION, the second false positive in five minutes (2026-09-18).

    shlex has no idea the lines between `<<'PY'` and `PY` are a Python
    program, so a script that merely MENTIONS a git command reads as one.
    Rule 5 is far more exposed than rules 1-4 because `git commit` is
    ordinary prose in test and doc text.
    """
    work, _store, env = primary
    cmd = (
        "python3 - <<'PY'\n"
        "from pathlib import Path\n"
        "Path('t.py').write_text('git commit -m x')\n"
        "PY"
    )
    d = _decision(_run_with_env({**_bash(cmd, cwd=str(work)), "session_id": "s"}, env))
    assert d["permissionDecision"] == "allow", d


def test_a_real_commit_AFTER_a_heredoc_is_still_caught(primary):
    """Stripping heredoc bodies must not blind the rule to the shell around
    them -- otherwise a heredoc anywhere in the command is the bypass."""
    work, _store, env = primary
    cmd = (
        "cat > note.txt <<'EOF'\n"
        "some text mentioning git commit -m nothing\n"
        "EOF\n"
        "git commit -m x"
    )
    d = _decision(_run_with_env({**_bash(cmd, cwd=str(work)), "session_id": "s"}, env))
    assert d["permissionDecision"] == "deny", d


def test_a_primary_checkout_with_no_linked_worktree_is_exempt(repo_on, tmp_path):
    """Rule 5 is scoped to SHARED checkouts, and a linked worktree is the
    signal chosen for that (Sam, 2026-09-18).

    Deliberately not using the `primary` fixture, which now adds a worktree:
    this is the same repo shape WITHOUT one, so the pair brackets the
    boundary exactly.
    """
    work = repo_on("develop")
    env = {"NX_SESSION_COMMITS_DIR": str(tmp_path / "sc")}
    d = _decision(_run_with_env({**_bash("git commit -m x", cwd=str(work)), "session_id": "s"}, env))
    assert d["permissionDecision"] == "allow", d

    _git("worktree", "add", "-q", "--detach", str(tmp_path / "wt-now"), "HEAD", cwd=work)
    d2 = _decision(_run_with_env({**_bash("git commit -m x", cwd=str(work)), "session_id": "s"}, env))
    assert d2["permissionDecision"] == "deny", (
        "adding a linked worktree must bring the same repo under the rule"
    )


def test_a_multi_line_commit_message_with_a_pathspec_is_allowed(primary):
    """REGRESSION, reported by nexus-01 2026-09-18 and blocking every session.

    This repo writes long multi-line commit messages by convention. An
    earlier fix rewrote newlines to `;` in the RAW command before splitting,
    so the message's own newlines became segment boundaries: the verb landed
    in one piece and the `-- <paths>` in another, and a correctly scoped
    commit was refused. The only ways through were `-F <file>` or burning the
    audited escape on a false positive.
    """
    work, _store, env = primary
    msg = "feat(x): a subject line\n\nA body paragraph; with a semicolon.\nAnd | a pipe.\n"
    cmd = f'git commit -m "{msg}" -- f.txt'
    d = _decision(_run_with_env({**_bash(cmd, cwd=str(work)), "session_id": "s"}, env))
    assert d["permissionDecision"] == "allow", d


def test_a_commit_inside_a_command_substitution_is_caught(primary):
    """`x=$(git commit -m x)` never reached a segment boundary, so it was
    silently allowed -- the bead's own defect wearing a subshell."""
    work, _store, env = primary
    for cmd in (
        'x=$(git commit -m sneaky)',
        'echo "$(git commit -m sneaky)"',
        '( git commit -m sneaky )',
    ):
        d = _decision(_run_with_env({**_bash(cmd, cwd=str(work)), "session_id": "s"}, env))
        assert d["permissionDecision"] == "deny", f"{cmd}: {d}"


def test_a_directory_pathspec_is_not_a_scoped_commit(primary):
    """REGRESSION. Measured 2026-09-18: with `docs/rdr/mine.md` and a peer's
    `docs/rdr/rdr-212-peer.md` both staged, `git commit -m x -- docs/rdr/`
    committed BOTH. A directory pathspec is `-a` scoped to a subtree, and
    `-a` is refused here by name."""
    work, _store, env = primary
    (work / "docs").mkdir(exist_ok=True)
    for cmd in (
        "git commit -m x -- .",
        "git commit -m x -- docs/",
        "git commit -m x -- docs",
        "git commit -m x -- 'src/*.py'",
        "git commit -m x -- f.txt docs/",
    ):
        d = _decision(_run_with_env({**_bash(cmd, cwd=str(work)), "session_id": "s"}, env))
        assert d["permissionDecision"] == "deny", f"{cmd}: {d}"


def test_a_stale_merge_head_does_not_disable_the_rule(primary):
    """REGRESSION. The exemption keyed on MERGE_HEAD's mere existence, and
    that file persists with no TTL until the operation is committed or
    aborted. An ordinary `git merge --no-commit` -- inspect before
    committing -- would disable the rule for every session sharing the
    checkout, indefinitely. It now requires an unmerged index entry, which
    is the only reason a partial commit is impossible."""
    work, _store, env = primary
    _git("checkout", "-q", "-b", "side2", cwd=work)
    (work / "g.txt").write_text("side")
    _git("add", "g.txt", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "side", cwd=work)
    _git("checkout", "-q", "develop", cwd=work)
    # A clean --no-commit merge: MERGE_HEAD exists, nothing is unmerged.
    subprocess.run(["git", "merge", "--no-commit", "--no-ff", "side2"],
                   cwd=work, capture_output=True)
    assert (work / ".git" / "MERGE_HEAD").exists(), "fixture must leave MERGE_HEAD present"
    unmerged = subprocess.run(
        ["git", "diff", "--cached", "--diff-filter=U", "--name-only"],
        cwd=work, capture_output=True, text=True,
    ).stdout.strip()
    assert unmerged == "", "fixture must have NO unmerged entries (a clean --no-commit merge)"
    d = _decision(_run_with_env({**_bash("git commit -m x", cwd=str(work)), "session_id": "s"}, env))
    assert d["permissionDecision"] == "deny", d


def test_a_scoped_amend_really_scopes_on_a_multi_file_commit(primary):
    """Non-vacuity for the amend cases above, which each amend the only file
    their commit touched — so they cannot distinguish "the pathspec scoped
    the amend" from "there was nothing to scope".

    Two files in the commit, a foreign third staged, amend naming ONE: the
    unnamed file's blob must be unchanged from the pre-amend tip and the
    foreign file must stay out. This asserts git's behaviour, which is what
    rule 5's whole justification for covering amend rests on.
    """
    work, store, env = primary
    (work / "one.txt").write_text("one")
    (work / "two.txt").write_text("two")
    _git("add", "one.txt", "two.txt", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pair", cwd=work)
    def _out(*args: str) -> str:
        # This module's own `_git` returns None (it is a fire-and-forget
        # helper); the stdout-returning one lives in the push-script tests.
        return subprocess.run(
            ["git", *args], cwd=work, check=True, capture_output=True, text=True,
        ).stdout.strip()

    before_two = _out("rev-parse", "HEAD:two.txt")

    (work / "one.txt").write_text("one-changed")
    (work / "peer.txt").write_text("a peer's staged file")
    _git("add", "one.txt", "peer.txt", cwd=work)
    _git("-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--amend", "--no-edit", "--", "one.txt", cwd=work)

    assert _out("rev-parse", "HEAD:two.txt") == before_two, (
        "a scoped amend must not disturb a file it did not name"
    )
    assert _out("show", "HEAD:one.txt") == "one-changed"
    tracked = _out("ls-tree", "--name-only", "HEAD").split()
    assert "peer.txt" not in tracked, "the foreign staged file rode the scoped amend"
    assert "peer.txt" in _out("diff", "--cached", "--name-only").split()
