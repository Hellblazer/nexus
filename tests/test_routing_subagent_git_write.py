# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-184 Gap-4 mechanization (nexus-s88vq):
subagent_git_write_requires_orchestrator.

Subagents (PreToolUse payloads carrying ``agent_id`` — the documented
subagent-origin marker) are denied ``git commit`` / ``git add`` in the
PRIMARY checkout. The main conversation (no ``agent_id``), read-only git,
linked-worktree agents, and ``# routing-allow:`` escapes all pass.
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import runpy
import subprocess
import sys
import time

import pytest

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
HOOK_SCRIPT = (
    PROJECT_ROOT
    / "conexus"
    / "hooks"
    / "scripts"
    / "routing"
    / "subagent_git_write_requires_orchestrator.py"
)

AGENT_ID = "aworker-x-6f59dab8bbb14864"


def _run(payload: dict, env_extra: dict[str, str] | None = None):
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True, text=True, timeout=20, env=env,
    )


def _decision(proc):
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _bash(cmd: str, *, agent: bool = True, cwd: str | None = None) -> dict:
    payload: dict = {"tool_name": "Bash", "tool_input": {"command": cmd}}
    if agent:
        payload["agent_id"] = AGENT_ID
        payload["agent_type"] = "worker-x"
    if cwd is not None:
        payload["cwd"] = cwd
    return payload


@pytest.fixture(autouse=True)
def _isolate_log(tmp_path, monkeypatch):
    monkeypatch.setenv("NX_ROUTING_LOG_PATH", str(tmp_path / "log.jsonl"))


@pytest.fixture()
def shared_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A PRIMARY git checkout (git-dir == git-common-dir)."""
    repo = tmp_path / "shared"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


@pytest.fixture()
def linked_worktree(shared_repo: pathlib.Path, tmp_path: pathlib.Path) -> pathlib.Path:
    (shared_repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=shared_repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
        cwd=shared_repo, check=True,
    )
    wt = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "wt-branch", str(wt)],
        cwd=shared_repo, check=True,
    )
    return wt


def test_script_exists():
    assert HOOK_SCRIPT.exists()


def test_registered_in_hooks_json():
    hooks = json.loads(
        (PROJECT_ROOT / "conexus" / "hooks" / "hooks.json").read_text()
    )
    commands = [
        h["command"]
        for entry in hooks["hooks"]["PreToolUse"]
        for h in entry.get("hooks", [])
    ]
    assert any("subagent_git_write_requires_orchestrator.py" in c for c in commands)


def test_registered_in_registry_yaml():
    text = (
        PROJECT_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "registry.yaml"
    ).read_text()
    assert "subagent_git_write_requires_orchestrator:" in text


class TestDeny:
    def test_subagent_commit_in_shared_tree_denied(self, shared_repo):
        out = _decision(_run(_bash("git commit -m msg", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"
        assert "orchestrator" in out["permissionDecisionReason"].lower()

    def test_subagent_add_in_shared_tree_denied(self, shared_repo):
        out = _decision(_run(_bash("git add src/file.py", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"

    def test_compound_command_denied(self, shared_repo):
        out = _decision(
            _run(_bash("uv run pytest && git add x.py && git commit -m done", cwd=str(shared_repo)))
        )
        assert out["permissionDecision"] == "deny"

    def test_global_flag_form_denied(self, shared_repo):
        out = _decision(
            _run(_bash(f"git -C {shared_repo} commit -m msg", cwd=str(shared_repo)))
        )
        assert out["permissionDecision"] == "deny"


class TestAllow:
    def test_main_conversation_commit_allowed(self, shared_repo):
        out = _decision(_run(_bash("git commit -m msg", agent=False, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow"

    def test_subagent_readonly_git_allowed(self, shared_repo):
        for cmd in ("git status", "git diff", "git log --oneline", "git show HEAD"):
            out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
            assert out["permissionDecision"] == "allow", cmd

    def test_subagent_nongit_allowed(self, shared_repo):
        out = _decision(_run(_bash("ls -la && echo commit", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow"

    def test_commit_substring_not_subcommand_denied_since_round4(self, shared_repo):
        """PRE-nexus-3c92m-round-4 this allowed (the structured parser saw
        `--grep=commit` was an argument, not a subcommand). Round 4 replaced
        that structured decision with a structure-agnostic proximity scan
        (module docstring): `git` near the literal word `commit` -- ANYWHERE,
        including inside a `--grep` pattern value -- now denies. Accepted
        false positive per the round-4 design (false positives are cheap;
        false negatives destroy data)."""
        out = _decision(_run(_bash("git log --grep=commit", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"

    def test_linked_worktree_commit_allowed(self, linked_worktree):
        """Worktree-isolated agents own their tree — their local commits are
        the documented harvest choreography, never blocked. Still true under
        round 4: a POSITIVELY PROVEN linked worktree is the one exemption
        left in the primary rule."""
        out = _decision(_run(_bash("git commit -m wt", cwd=str(linked_worktree))))
        assert out["permissionDecision"] == "allow"

    def test_non_repo_cwd_now_fails_CLOSED_since_round4(self, tmp_path):
        """PRE-round-4 this allowed: the old design fail-OPENED hygiene verbs
        (add/commit) when the worktree state was undeterminable (nexus-ays2l
        item 3), reasoning that `add` mutates only the index and destroys
        nothing. Round 4 retires that split entirely (module docstring): the
        ONLY exemption from the primary rule left is a POSITIVELY PROVEN
        linked worktree; "I could not prove this tree is safe" no longer
        earns a pass for any write verb, hygiene or destructive."""
        out = _decision(_run(_bash("git commit -m msg", cwd=str(tmp_path / "norepo"))))
        assert out["permissionDecision"] == "deny"

    def test_escape_token_allows_and_logs(self, shared_repo, tmp_path):
        out = _decision(
            _run(_bash("git commit -m msg # routing-allow: orchestrator sanctioned", cwd=str(shared_repo)))
        )
        assert out["permissionDecision"] == "allow"
        log = (tmp_path / "log.jsonl").read_text()
        assert '"outcome": "escape"' in log or '"escape"' in log

    def test_junk_stdin_fails_open(self):
        proc = subprocess.run(
            [sys.executable, str(HOOK_SCRIPT)],
            input="not json", capture_output=True, text=True, timeout=20,
            env={**os.environ},
        )
        assert proc.returncode == 0


# ── nexus-ays2l: the WORKING-TREE-DESTROYING verbs ──────────────────────────
#
# The original verb set was {"commit", "add"} — strictly narrower than the set
# of git verbs that can destroy an orchestrator's uncommitted work. `git add`
# mutates only the INDEX and destroys nothing; `git checkout -- <path>` and
# `git restore <path>` and `git stash` mutate the WORKING TREE and delete
# uncommitted edits outright. The guard blocked the harmless-but-untidy verbs
# and permitted the destructive ones.
#
# Damage signature that produced the bead (2026-07-24): three silent reversions
# of src/nexus/upgrade_finish.py over ~10 minutes with two subagents live,
# sibling files edited in the same window untouched, NO stash entry and NO
# reflog entry — the trace `git checkout -- <path>` leaves and `git stash`
# does not. Attribution was never proven; what IS established is that the
# guard would not have stopped any subagent that ran those verbs.


_DESTRUCTIVE_INVOCATIONS = [
    "git checkout -- src/nexus/upgrade_finish.py",
    "git checkout HEAD -- src/nexus/upgrade_finish.py",
    "git restore src/nexus/upgrade_finish.py",
    "git stash",
    "git stash push -m wip",
    "git clean -fd",
    "git reset --hard HEAD",
    "git rm -f src/nexus/upgrade_finish.py",
    # nexus-3c92m: `switch` moves HEAD exactly like `checkout <branch>`
    # (already covered above) but was the one verb missing from the set.
    "git switch main",
    "git switch -c newbranch",
    "git switch --detach HEAD",
]


@pytest.mark.parametrize("cmd", _DESTRUCTIVE_INVOCATIONS)
def test_destructive_verbs_denied_in_shared_tree(cmd, shared_repo):
    out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
    assert out["permissionDecision"] == "deny", f"{cmd} was permitted: {out}"
    assert "uncommitted" in out["permissionDecisionReason"].lower()


@pytest.mark.parametrize("cmd", [
    "git show HEAD:src/nexus/upgrade_finish.py",
    "git status",
    "git diff",
    "git log --oneline -5",
])
def test_read_only_inspection_still_allowed(cmd, shared_repo):
    """Reviewers must keep working. The bead's stated preference: allowlist the
    read-only invocations rather than blanket-denying the verb."""
    out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
    assert out["permissionDecision"] == "allow", f"{cmd} was blocked: {out}"


@pytest.mark.parametrize("cmd", ["git stash list", "git stash show -p"])
def test_stash_readonly_forms_now_denied_since_round4(cmd, shared_repo):
    """PRE-round-4 these allowed via `_READ_ONLY_FORMS` (a read-only-spelling
    allowlist consulted by the STRUCTURED parser that used to decide).
    Round 4's primary rule has no such refinement -- `stash` is
    unconditionally in the write-verb list and the primary rule does not
    inspect what follows it. `_matched_write_subcommands` (secondary) still
    knows `stash list`/`stash show` are reads, but per the module docstring
    the secondary parser never overrides the primary verdict. Accepted
    regression: false positives are cheap under this design (a reviewer who
    needs `git stash list` hands it to the orchestrator)."""
    out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
    assert out["permissionDecision"] == "deny", f"{cmd} was permitted: {out}"


@pytest.mark.parametrize("cmd", _DESTRUCTIVE_INVOCATIONS)
def test_destructive_verbs_allowed_in_linked_worktree(cmd, linked_worktree):
    """A worktree-isolated agent owns its tree — including destroying it."""
    out = _decision(_run(_bash(cmd, cwd=str(linked_worktree))))
    assert out["permissionDecision"] == "allow", f"{cmd} was blocked: {out}"


def test_main_conversation_unaffected_by_the_widening(shared_repo):
    """The rule targets subagents. The orchestrator resets its own tree."""
    out = _decision(_run(_bash("git checkout -- x.py", agent=False, cwd=str(shared_repo))))
    assert out["permissionDecision"] == "allow"


def test_routing_allow_escape_still_works(shared_repo):
    out = _decision(_run(_bash(
        "git checkout -- x.py  # routing-allow: orchestrator asked me to revert this",
        cwd=str(shared_repo),
    )))
    assert out["permissionDecision"] == "allow"


# ── Fail mode WAS split by what is at stake (Hal ruling 2026-07-25, item 3),
# retired by nexus-3c92m round 4: the primary rule's ONLY exemption is a
# POSITIVELY PROVEN linked worktree; an undeterminable worktree state now
# fails closed UNIFORMLY, for hygiene verbs (add/commit) exactly the same as
# destructive ones. See the module docstring's round-4 section. ──────────────


def test_destructive_verb_fails_CLOSED_when_worktree_undeterminable(tmp_path):
    """A non-repo cwd makes `git rev-parse` fail, so worktree state is
    undeterminable. Destroyers deny anyway: 'I could not tell whether this tree
    is shared' is not a licence to destroy one."""
    not_a_repo = tmp_path / "bare"
    not_a_repo.mkdir()
    out = _decision(_run(_bash("git checkout -- x.py", cwd=str(not_a_repo))))
    assert out["permissionDecision"] == "deny", out
    reason = out["permissionDecisionReason"].lower()
    assert "could not be determined" in reason and "fail closed" in reason


def test_index_verbs_now_ALSO_fail_CLOSED_when_worktree_undeterminable_since_round4(tmp_path):
    """PRE-round-4 this allowed: `add`/`commit` were a HYGIENE bucket that
    fail-OPENED on an undeterminable worktree (a flaky `git rev-parse` must
    never wedge agent work over mere tidiness, and `add` destroys nothing).
    Round 4 retires that split (module docstring): the primary rule's only
    exemption is a POSITIVELY PROVEN linked worktree, full stop -- an
    undeterminable state no longer earns a pass for ANY write verb."""
    not_a_repo = tmp_path / "bare2"
    not_a_repo.mkdir()
    for cmd in ("git add -A", "git commit -m x"):
        out = _decision(_run(_bash(cmd, cwd=str(not_a_repo))))
        assert out["permissionDecision"] == "deny", f"{cmd}: {out}"


def test_bare_stash_and_stash_list_both_denied_since_round4(shared_repo):
    """PRE-round-4: `git stash list` read (allowlisted read-only spelling);
    `git stash` STASHED (denied). Round 4's primary rule has no read-only-
    spelling refinement for `stash` -- both now deny. See
    `test_stash_readonly_forms_now_denied_since_round4` for the dedicated
    parametrized coverage of the `list`/`show` forms; this test keeps the
    original two-command shape as a regression lock."""
    assert _decision(_run(_bash("git stash", cwd=str(shared_repo))))["permissionDecision"] == "deny"
    assert _decision(_run(_bash("git stash list", cwd=str(shared_repo))))["permissionDecision"] == "deny"


def test_destructive_verb_hidden_in_a_compound_command_is_caught(shared_repo):
    """Segment splitting must see past `&&` — the realistic shape is a cleanup
    tail on an otherwise innocuous command."""
    out = _decision(_run(_bash(
        "pytest -q && git checkout -- src/nexus/upgrade_finish.py", cwd=str(shared_repo),
    )))
    assert out["permissionDecision"] == "deny", out


def test_unbalanced_quote_commit_is_still_denied(shared_repo):
    """nexus-2e874: a stray quote in any argument used to make shlex reject
    the segment and the guard silently skipped it -- a subagent `git commit`
    (or stash) became invisible. The degraded whitespace fallback keeps the
    `git <subcommand>` anchor visible."""
    out = _decision(_run(_bash('git commit -m "unterminated', cwd=str(shared_repo))))
    assert out["permissionDecision"] == "deny", out


def test_unbalanced_quote_nongit_segment_now_denied_since_round4(shared_repo):
    """PRE-round-4 this allowed: the structured parser anchored on a
    segment's FIRST token, and `echo`'s segment didn't start with `git`, so
    a quoted-string mention of "git commit" never counted. Round 4's primary
    rule doesn't care about segment position AT ALL -- it's a pure proximity
    scan over the raw text (module docstring: "quoting... creates no
    exemption"), so `git commit` appearing anywhere, including inside an
    unterminated echo string, now denies. Accepted false positive."""
    out = _decision(_run(_bash('echo "later run git commit -m x', cwd=str(shared_repo))))
    assert out["permissionDecision"] == "deny", out


def test_quote_inside_the_subcommand_is_still_denied(shared_repo):
    """Review Important-1 (nexus-2e874): quote INSIDE the verb -- the
    quote-removed degraded variant must still anchor `git commit`."""
    out = _decision(_run(_bash('git com"mit -m msg', cwd=str(shared_repo))))
    assert out["permissionDecision"] == "deny", out


# ── nexus-3c92m: incident follow-up (2026-08-20) ─────────────────────────────
#
# A code-review-expert subagent falsified nexus-pfuns' fixes with
# `git checkout -- <file>` on three uncommitted production files in the
# shared tree (T2 nexus/lessons-coordination-2026-08-20). `switch` (tested
# below) was a genuine minor gap -- `git switch` moves HEAD exactly like
# `git checkout <branch>` (already denied) but was never added to the
# destructive set -- but it was NOT what let the incident through.
#
# CORRECTED ROOT CAUSE (superseding an earlier, REFUTED hypothesis that the
# live guard was simply an older pinned plugin release): the coordinator
# confirmed from the live transcript that all three wiping Bash calls ran
# against TODAY's plugin and got `permissionDecision=allow`. The actual bug:
# `_matched_write_subcommands` split segments on `&&`/`||`/`;`/`|`/`then`/
# `do` but never on a bare NEWLINE, and `_git_subcommand` only looks at a
# segment's FIRST token. The real incident shape was a multi-line script
# joined by nothing but newlines -- `cd dir` / `echo ...` / a `python3 -
# <<'EOF' ... EOF` heredoc / then `git checkout -- <path>` on a LATER line --
# so the entire multi-line blob was one segment whose first token was `cd`,
# and the destructive verb many tokens later was never inspected. A second,
# independent incident (`git add -N` / `git reset -- <pathspec>`, sibling
# c00dw) traced to the SAME cause -- confirmed by replaying the pre-fix
# splitter offline: the single-line forms of both were already denied
# (add/reset match unconditionally, independent of arguments); only the
# multi-line/heredoc-adjacent forms slipped through. Not a separate
# verb-matching gap.
#
# These tests lock in: the exact incident shape; `$(...)`/backtick
# substitution; a destructive verb after a bare `#` comment line
# (newline-joined, no `;`); a heredoc body fed to a real shell (scanned) vs.
# fed to `python3` and merely mentioning "git checkout" as text (must stay
# ALLOWED -- the false-positive case); and the add -N/reset -- pathspec
# forms, single-line and multi-line.


class TestNexus3c92mNamedInvocations:
    """The exact command shapes named in the bead, each as its own test."""

    def test_checkout_pathspec_form_denied(self, shared_repo):
        out = _decision(_run(_bash(
            "git checkout -- src/nexus/upgrade_finish.py", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny"

    def test_checkout_bare_branch_form_denied(self, shared_repo):
        """`git checkout <branch>` (no `--`, no pathspec) moves HEAD in the
        shared tree just as destructively as the pathspec form."""
        out = _decision(_run(_bash("git checkout main", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"

    def test_global_C_flag_restore_denied(self, shared_repo):
        out = _decision(_run(_bash(
            f"git -C {shared_repo} restore .", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny"

    def test_reset_hard_bare_denied(self, shared_repo):
        out = _decision(_run(_bash("git reset --hard", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"

    def test_clean_fd_denied(self, shared_repo):
        out = _decision(_run(_bash("git clean -fd", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"

    def test_bare_stash_denied(self, shared_repo):
        out = _decision(_run(_bash("git stash", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny"

    @pytest.mark.parametrize("cmd", [
        "git switch main",
        "git switch -c newbranch",
        "git switch --detach HEAD",
    ])
    def test_switch_denied(self, cmd, shared_repo):
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", f"{cmd}: {out}"

    def test_no_pager_global_flag_checkout_denied(self, shared_repo):
        out = _decision(_run(_bash(
            "git --no-pager checkout -- x.py", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny"

    def test_compound_cd_then_checkout_denied(self, shared_repo):
        """The realistic incident shape: a `cd` into the shared tree ahead of
        the destructive verb, joined by `&&`."""
        out = _decision(_run(_bash(
            f"cd {shared_repo} && git checkout -- f.py", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny"

    @pytest.mark.parametrize("cmd", [
        "git status", "git diff", "git log --oneline", "git show HEAD",
    ])
    def test_read_only_git_allowed(self, cmd, shared_repo):
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", f"{cmd}: {out}"

    def test_stash_list_now_denied_since_round4(self, shared_repo):
        """Was in the read-only-allowed set above pre-round-4; see
        test_stash_readonly_forms_now_denied_since_round4 for the rationale."""
        out = _decision(_run(_bash("git stash list", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_orchestrator_context_not_denied(self, shared_repo):
        """The main conversation (no agent_id) is never subject to this
        guard -- it resets its own tree."""
        out = _decision(_run(_bash(
            "git checkout -- f.py", agent=False, cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "allow"

    def test_deny_message_names_the_rule(self, shared_repo):
        out = _decision(_run(_bash("git checkout -- f.py", cwd=str(shared_repo))))
        reason = out["permissionDecisionReason"].lower()
        assert "orchestrator commits" in reason

    def test_deny_message_suggests_falsification_by_comparison(self, shared_repo):
        out = _decision(_run(_bash("git checkout -- f.py", cwd=str(shared_repo))))
        reason = out["permissionDecisionReason"].lower()
        assert "falsify by comparison" in reason
        assert "git show head:" in reason
        assert "diff" in reason


class TestNexus3c92mNewlineAndHeredocGap:
    """The CORRECTED root cause (see the module comment above this class):
    the segment splitter never split on bare newlines, so a multi-line Bash
    tool command joined by nothing but newlines was ONE segment whose first
    token decided everything. Each test here reproduces a shape that was
    verified ALLOWED (wrongly) before this fix and DENIED after."""

    def test_the_exact_incident_shape(self, shared_repo):
        """cd / echo / a python3 heredoc / then `git checkout` on a LATER
        line, joined by nothing but newlines -- the live-transcript shape."""
        cmd = "\n".join([
            f"cd {shared_repo}",
            'echo "=== Falsify #1 ==="',
            "python3 - <<'EOF'",
            "with open('t3.py') as fh:",
            "    content = fh.read()",
            "print(len(content))",
            "EOF",
            'echo "checking output"',
            "git checkout -- t3.py",
        ])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_destructive_verb_inside_dollar_paren_subshell(self, shared_repo):
        out = _decision(_run(_bash("x=$(git checkout -- t3.py)", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_destructive_verb_inside_backtick_substitution(self, shared_repo):
        cmd = "x=" + chr(96) + "git checkout -- t3.py" + chr(96)
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_destructive_verb_after_semicolon_single_line(self, shared_repo):
        out = _decision(_run(_bash(
            "echo hi; git checkout -- t3.py", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny", out

    def test_destructive_verb_inside_a_for_do_block(self, shared_repo):
        out = _decision(_run(_bash(
            "for f in a b; do git checkout -- $f; done", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny", out

    def test_destructive_verb_inside_a_multiline_if_then_block(self, shared_repo):
        cmd = "\n".join(["if true; then", "  git checkout -- t3.py", "fi"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_destructive_verb_after_a_bare_comment_line(self, shared_repo):
        """A `#` comment line followed by the destructive verb on the next
        line -- newline-joined, no `;` in sight."""
        cmd = "\n".join(["# setup step", "git checkout -- t3.py"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_heredoc_body_fed_to_a_real_shell_is_scanned(self, shared_repo):
        """`bash <<'EOF' ... EOF` -- the heredoc body IS shell code that will
        execute, so it must be scanned like any other segment."""
        cmd = "\n".join(["bash <<'EOF'", "git checkout -- t3.py", "EOF"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_heredoc_body_fed_to_python3_now_denied_since_round4(self, shared_repo):
        """PRE-round-4 this allowed: the SECONDARY (structured) parser
        classified a python3 heredoc body as opaque DATA, not shell code, so
        a body that merely PRINTED the text "git checkout" stayed allowed.
        Round 4's PRIMARY rule does no heredoc-consumer classification at
        all -- it is a pure proximity scan over the raw command text, so
        `git` near `checkout` anywhere, including inside this now-opaque
        heredoc body, denies. This is the accepted false-positive the module
        docstring names explicitly as the design's deliberate cost (a false
        positive costs a rephrase; a false negative destroys data)."""
        cmd = "\n".join([
            "python3 - <<'EOF'",
            "print('as text only: git checkout -- t3.py')",
            "EOF",
        ])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_multiline_with_no_git_at_all_stays_allowed(self, shared_repo):
        cmd = "\n".join([f"cd {shared_repo}", "echo hi", "ls -la"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out


class TestNexus3c92mAddDashNAndResetPathspec:
    """Second live incident (sibling c00dw): `git add -N <path>` then
    `git reset -- <path>` were both allowed. Investigation found these
    single-line forms were ALREADY denied before this fix (add/reset match
    unconditionally, independent of arguments) -- not a separate
    verb-matching gap. Only the multi-line/heredoc-adjacent forms were
    missed, and those are covered by the same newline-splitting fix as the
    checkout incident. These tests lock in both shapes so the class stays
    covered regardless of which explanation is correct in the future."""

    def test_add_intent_to_add_short_flag_single_line_denied(self, shared_repo):
        out = _decision(_run(_bash("git add -N t3.py", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_add_intent_to_add_long_flag_single_line_denied(self, shared_repo):
        out = _decision(_run(_bash("git add --intent-to-add t3.py", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_reset_pathspec_single_line_denied(self, shared_repo):
        out = _decision(_run(_bash("git reset -- t3.py", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_reset_pathspec_no_double_dash_single_line_denied(self, shared_repo):
        out = _decision(_run(_bash("git reset t3.py", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_add_then_reset_newline_joined_no_ampersand_denied(self, shared_repo):
        """Two statements on their own lines, no `&&` between them -- the
        newline-splitting shape, not the already-covered `&&` shape."""
        cmd = "\n".join(["git add -N t3.py", "git reset -- t3.py"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_add_dash_n_on_a_later_line_after_cd_and_echo_denied(self, shared_repo):
        cmd = "\n".join([f"cd {shared_repo}", 'echo "staging"', "git add -N t3.py"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_reset_pathspec_on_a_later_line_after_cd_and_echo_denied(self, shared_repo):
        cmd = "\n".join([f"cd {shared_repo}", 'echo "unstaging"', "git reset -- t3.py"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out


# ── nexus-3c92m review round 2 (2026-08-20): stacked review found 5 new
# bypasses in the round-1 newline/heredoc fix -- both reviewers falsified by
# calling `_matched_write_subcommands()` directly, no git mutation. Fixed:
#
# 1. (critic ship-blocker) `_HEREDOC_OPEN_RE` matched inside the bash
#    HERE-STRING operator `<<<` whenever its RHS started with a letter or
#    underscore -- `cat <<< "hello"` opened a false "heredoc" that swallowed
#    every following line (including a destructive verb) as an unscanned
#    body, since `cat` isn't a shell consumer. Fixed with a negative
#    lookaround rejecting a `<<` that is part of a longer `<` run.
# 2. (code-review CRITICAL) backslash line-continuation (`git \` + newline +
#    `checkout`) split `git` from its own subcommand across two newline
#    segments. Fixed by joining continued lines BEFORE any newline split.
# 3. (code-review CRITICAL) heredoc-consumer classification was exact-string
#    membership, so `/bin/bash <<EOF` (path-qualified) matched nothing.
#    Fixed by matching each head token's basename, plus widening the
#    consumer set to env/sudo/xargs/eval/source.
# 4. (code-review CRITICAL) `<(...)`/`>(...)` process substitution was never
#    extracted at all. Fixed with `_PROCESS_SUB_RE` alongside the existing
#    `$(...)`/backtick extractors.
# 5. (critic SIGNIFICANT) `$(...)`/backtick extraction ran against the RAW
#    command, so substitution-shaped TEXT sitting inside an opaque (e.g.
#    python) heredoc body was still extracted and matched -- contradicting
#    the module's own stated invariant. Fixed by extracting from the
#    heredoc-FILTERED text instead.


class TestNexus3c92mReviewRound2Bypasses:
    """One test per reviewer-confirmed bypass, matching each reviewer's own
    reproduction command."""

    def test_here_string_does_not_falsely_open_a_heredoc(self, shared_repo):
        """`cat <<< "hello"` is a here-string, not a heredoc -- it must not
        swallow the next line (the destructive verb) as an unscanned body."""
        cmd = "\n".join(['cat <<< "hello"', "git checkout -- t3.py"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_bare_here_string_alone_stays_allowed(self, shared_repo):
        """Regression guard: a here-string with no destructive verb anywhere
        must not become deny-by-default just because `<<<` is now excluded
        from heredoc-open matching."""
        out = _decision(_run(_bash('cat <<< "hello world"', cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_backslash_line_continuation_denied(self, shared_repo):
        """`git \\` + newline + `checkout -- t3.py` is ONE logical shell
        line (`git checkout -- t3.py`), not two newline-separated segments."""
        cmd = "git \\\ncheckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    @pytest.mark.parametrize("consumer_line", [
        "/bin/bash <<EOF",
        "env bash <<EOF",
        "sudo bash <<EOF",
        "bash -s <<EOF",
        "xargs sh <<EOF",
        "eval <<EOF",
        "source <<EOF",
    ])
    def test_heredoc_consumer_denied(self, consumer_line, shared_repo):
        cmd = "\n".join([consumer_line, "git checkout -- t3.py", "EOF"])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", f"{consumer_line}: {out}"

    def test_process_substitution_input_denied(self, shared_repo):
        out = _decision(_run(_bash(
            "diff <(git checkout -- t3.py) /dev/null", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny", out

    def test_process_substitution_output_denied(self, shared_repo):
        out = _decision(_run(_bash(
            "echo hi > >(git checkout -- t3.py)", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny", out

    def test_dollar_paren_text_inside_heredoc_now_denied_since_round4(self, shared_repo):
        """PRE-round-4 this allowed: `$(...)`-shaped TEXT inside a python
        (non-shell) heredoc body was data, not code, and the SECONDARY
        parser's substitution extraction was scoped to skip dropped bodies.
        Round 4's PRIMARY rule has no heredoc/substitution awareness at all
        -- it is a pure proximity scan, so `git` near `checkout` inside this
        text denies regardless of the surrounding `$(...)`/heredoc dressing.
        Accepted false positive (module docstring)."""
        cmd = "\n".join([
            "python3 - <<'EOF'",
            "print('as text only: $(git checkout -- t3.py)')",
            "EOF",
        ])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_backtick_text_inside_heredoc_now_denied_since_round4(self, shared_repo):
        """Same round-4 rationale as the `$(...)` case above, for backticks."""
        cmd = "\n".join([
            "python3 - <<'EOF'",
            "print('as text only: " + chr(96) + "git checkout -- t3.py" + chr(96) + "')",
            "EOF",
        ])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_dollar_paren_outside_any_heredoc_still_denies(self, shared_repo):
        """Regression guard: moving substitution extraction onto the
        heredoc-filtered text must not lose the plain (no-heredoc) case."""
        out = _decision(_run(_bash("x=$(git checkout -- t3.py)", cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out


_INCIDENT_FIXTURES = runpy.run_path(
    str(PROJECT_ROOT / "tests" / "fixtures" / "incident_3c92m_commands.py")
)


class TestNexus3c92mVerbatimIncidentCommands:
    """The exact byte-for-byte Bash tool_input commands from the real
    transcript (T2 nexus/lessons-coordination-2026-08-20), pinned per the
    substantive-critic's recommendation rather than a prose reconstruction --
    quoting/delimiter subtleties are this guard's repeat failure class."""

    @pytest.mark.parametrize("fixture_name", [
        "INCIDENT_CMD_1", "INCIDENT_CMD_2", "INCIDENT_CMD_3",
    ])
    def test_verbatim_incident_command_denied(self, fixture_name, shared_repo):
        cmd = _INCIDENT_FIXTURES[fixture_name]
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", f"{fixture_name}: {out}"


# ── nexus-3c92m round 4: structure-agnostic primary rule ─────────────────────
#
# Round 3's stacked re-review found THREE MORE bypasses in the round-1/2
# structural-parsing approach (CRLF-after-continuation, a non-shell heredoc
# whose corrupted terminator swallows a verb placed AFTER the heredoc, and
# piping literal text into a shell) and ruled that three rounds of "add a
# case" is the signal to change the design, not patch it again. Round 4
# replaces the structural parser as the DECIDING mechanism with a
# structure-agnostic proximity regex (`_PRIMARY_DENY_RE`, module docstring)
# that is immune BY CONSTRUCTION to any bypass that merely relocates the
# same literal text via a different shell construct -- these tests exercise
# exactly that class of shape.


class TestNexus3c92mRound4PrimaryRule:
    """The three round-3 bypasses, plus the primary rule's other required
    properties: an expanded read-only allowlist, and a performance bound."""

    def test_crlf_after_continuation_backslash_denied(self, shared_repo):
        """Round 3 finding #1: `\\r` sitting after the continuation
        backslash (before the `\\n`) defeated round 2's trailing-backslash
        counter, which required the backslash to be the literal last
        character. The primary rule normalizes CRLF to LF BEFORE collapsing
        continuations, so this has nowhere to hide."""
        cmd = "git \\\r\ncheckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_heredoc_corrupted_terminator_with_verb_after_denied(self, shared_repo):
        """Round 3 finding #2: a non-shell heredoc's LAST body line ending in
        a single backslash used to corrupt the SECONDARY parser's terminator
        detection (continuation-joining ran on raw text before heredoc-aware
        segmentation), folding everything after -- including a destructive
        verb placed AFTER the heredoc -- into the dropped body. The primary
        rule does no heredoc segmentation at all, so this shape cannot hide
        anything from it regardless of what the secondary parser does."""
        cmd = "\n".join([
            "python3 - <<'EOF'",
            "x = 1 \\",
            "EOF",
            "git checkout -- t3.py",
        ])
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    @pytest.mark.parametrize("cmd", [
        "printf 'git checkout -- t3.py' | sh",
        "echo 'git checkout -- t3.py' | bash",
    ])
    def test_pipe_into_shell_denied(self, cmd, shared_repo):
        """Round 3 finding #3: piping literal text into `sh`/`bash` is the
        same risk as a heredoc (untrusted text becomes executed shell code)
        and the old design's heredoc-consumer classification was the ONLY
        place that risk was reasoned about -- a pipe isn't a heredoc, so it
        got zero handling. The primary rule doesn't reason about consumers
        at all; the literal text is denied wherever it sits."""
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", f"{cmd}: {out}"

    def test_dot_source_of_process_substitution_denied(self, shared_repo):
        """Round 3 finding #4 (lower severity): `. <(echo '...')` dot-sources
        a process substitution's STDOUT, not its literal argument text -- but
        the literal text is still `git checkout` somewhere in the raw
        command, so the primary rule denies regardless of the mechanism."""
        out = _decision(_run(_bash(
            ". <(echo 'git checkout -- t3.py')", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny", out

    def test_quote_split_verb_denied(self, shared_repo):
        """A gap found while re-verifying THIS round against the pre-existing
        suite, not from a review: `git com"mit" -m msg` really executes as
        `git commit -m msg` (a shell removes quote characters and fuses the
        surrounding text into one token). `_normalize_for_primary_scan`
        strips quote characters before scanning specifically to keep this
        case caught by a substring-only design."""
        out = _decision(_run(_bash('git com"mit" -m msg', cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    @pytest.mark.parametrize("cmd", [
        "git blame t3.py", "git rev-parse HEAD", "git ls-files",
    ])
    def test_expanded_read_only_set_allowed(self, cmd, shared_repo):
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", f"{cmd}: {out}"

    def test_100kb_command_scans_well_under_50ms(self):
        """Performance bound the round-4/5 design must meet. Round 5 removed
        the bounded window (a measurable, exceedable structure -- see
        TestNexus3c92mRound5PrimaryRuleFixes), so this now measures the
        UNBOUNDED two-linear-search path (`_primary_match`) directly (no
        subprocess/interpreter-startup overhead, which would dominate and
        hide a real regression) against a 100KB command that is adversarial
        for it: `git ` immediately followed by 100,000 non-matching
        characters, forcing the second (verb) search to walk the entire
        remainder with no early exit."""
        spec = importlib.util.spec_from_file_location("_nx3c92m_guard", str(HOOK_SCRIPT))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        big = "git " + ("x" * 100_000)
        t0 = time.perf_counter()
        guard._primary_match(guard._normalize_for_primary_scan(big))
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50, f"{elapsed_ms:.3f}ms >= 50ms budget"

    def test_100kb_many_git_occurrences_verb_at_end_scans_well_under_50ms(self):
        """A second adversarial shape for the unbounded design: many `git`
        occurrences scattered through ~100KB, with the actual verb only at
        the very end -- stresses whether anchoring on the FIRST `git` (not
        re-scanning from every occurrence) keeps this linear rather than
        quadratic."""
        spec = importlib.util.spec_from_file_location("_nx3c92m_guard2", str(HOOK_SCRIPT))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        big = ("git nonmatch " * 5000) + "checkout"
        t0 = time.perf_counter()
        m = guard._primary_match(guard._normalize_for_primary_scan(big))
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert m is not None, "expected a match (checkout is present after the first git)"
        assert elapsed_ms < 50, f"{elapsed_ms:.3f}ms >= 50ms budget"


# ── nexus-3c92m round 5: two more Criticals in the PRIMARY rule itself ───────
#
# Round 4's re-review (5th review round in a row) found the redesign had
# TWO of its own new bypasses: mid-word backslash escaping (the same
# "shell removes a character and fuses adjacent text" class just closed for
# quotes, left open for backslashes) and the bounded 160-char window itself
# being a measurable, exceedable structure (ordinary git global flags padded
# long enough exceed it). Both closed: `_collapse_escapes` in
# `_normalize_for_primary_scan`, and removing the window entirely in favor
# of `_primary_match`'s two unbounded linear searches.


class TestNexus3c92mRound5PrimaryRuleFixes:
    """The two round-5 gaps, plus the flag-padding shape named explicitly by
    the review, plus a regression guard that escaped-backslash pairs still
    collapse to one literal backslash rather than vanishing or double-firing
    as an escape for the FOLLOWING character."""

    @pytest.mark.parametrize("cmd", [
        "git che" + chr(92) + "ckout -- t3.py",
        "git ad" + chr(92) + "d -N t3.py",
        "git com" + chr(92) + "mit -m msg",
        "git re" + chr(92) + "set --hard",
    ])
    def test_mid_word_backslash_escape_denied(self, cmd, shared_repo):
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", f"{cmd!r}: {out}"

    def test_g_backslash_it_mid_word_denied(self, shared_repo):
        """`g\\it checkout -- f` -- the backslash splits `git` itself, not
        just the verb. Kept as its own test (not parametrized with the
        others above) since the shape is structurally different: the split
        word is `git`, not the verb."""
        cmd = "g" + chr(92) + "it checkout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_escaped_backslash_pair_collapses_to_one_not_double_processed(self, shared_repo):
        """`\\\\` (two literal backslashes) must collapse to ONE backslash,
        not vanish entirely and not be treated as "backslash escapes the
        NEXT backslash as though it were an ordinary escape target" in a way
        that then mis-consumes a THIRD character. Regression guard for the
        `_ESCAPE_RE` alternation ordering (`\\\\\\\\` tried before the
        single-backslash-plus-ordinary-char case)."""
        cmd = "echo " + (chr(92) * 2) + "n"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_flag_padding_beyond_the_old_160_char_window_denied(self, shared_repo):
        """The review's named exploit shape: an ordinary, syntactically
        valid `-c key=value` padded well past the OLD bounded window. Round
        5 removed the window entirely, so distance is no longer a variable
        an attacker can tune."""
        cmd = "git -c user.name=" + ("a" * 200) + " checkout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_flag_padding_many_repeated_c_flags_denied(self, shared_repo):
        cmd = "git " + " ".join(f"-c a.b{i}=v" for i in range(30)) + " checkout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_verb_well_past_the_old_window_boundary_denied(self, shared_repo):
        """300 filler characters between `git` and the verb -- comfortably
        past the old ~160-char ceiling, which is exactly the point."""
        cmd = "git " + ("x" * 300) + " checkout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_ifs_split_checkout_denied_and_was_never_really_a_gap(self, shared_repo):
        """`git${IFS}checkout` -- KNOWN LIMITS previously (incorrectly)
        listed this as unclosed; corrected in round 5 (module docstring):
        IFS-splitting only matters to a TOKENIZING parser, and this design
        has never tokenized, so this always denied under the round-4+
        substring-scan design. Locked in here as a regression test now that
        the doc claim is fixed."""
        out = _decision(_run(_bash(
            "git${IFS}checkout -- t3.py", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny", out

    def test_base64_decode_then_exec_still_allows_documented_residual(self, shared_repo):
        """Verified (not assumed) per the round-5 instruction to check both
        prior claims rather than trust either: base64-encoded payloads carry
        no literal `git`/verb substring in the raw command text, so removing
        the window changes nothing here -- this remains the genuine,
        documented KNOWN LIMITS residual (runtime-assembled / encoded verb).
        """
        import base64
        b64 = base64.b64encode(b"git checkout -- t3.py").decode()
        out = _decision(_run(_bash(
            f"echo {b64} | base64 -d | sh", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "allow", out


# ── nexus-3c92m round 6: expansions spliced INSIDE a token ───────────────────
#
# Round 5's re-review (6th review round in a row) found the round-4/5
# PRIMARY rule had a further-out version of the SAME "shell removes/replaces
# a marker and fuses adjacent text" class already closed for quotes and
# backslash-escapes: real bash confirmed `g${x:-i}t`, `g$(echo i)t`,
# `git st$(echo a)tus`, and `g$'\151't` all execute AS `git`, so
# `git ch${x:-e}ckout -- f` allowed pre-fix. Closed in the normalizer with
# four ordered steps (see `_normalize_for_primary_scan` and
# `_find_spliced_expansion`): ANSI-C `$'...'` decode, parameter-expansion
# default resolution, command-substitution literal resolution, then an
# ADJACENCY RULE for whatever expansion syntax survives all three.
#
# Round 6's FIRST cut of that adjacency rule was itself wrong -- it denied
# ANY subagent Bash command where a surviving expansion sat glued to a word
# character on EITHER side, unconditionally, independent of `git` presence.
# The coordinator caught this in round 7 (a 7th review round): that denied
# completely ordinary, non-git subagent usage like `echo file${i}.txt`,
# `cp "${dir}/a${n}.log" .`, `tar xf pkg${ver}.tgz`. Round 7 (see
# `TestNexus3c92mRound7ScopedAdjacency` below) narrows the rule to check
# WHAT the glued letters spell (an in-order, gap-allowed subsequence of
# `git` or of a simple destructive verb, the latter only when a literal
# `git` is ALSO present elsewhere), not merely THAT something is glued.


class TestNexus3c92mRound6SplicedExpansions:
    """The reviewer's exact reconstruction shapes (both as bare 'git'-only
    proofs-of-mechanism, which correctly ALLOW since no verb is attached,
    and as full exploits with a verb spliced in, which correctly DENY),
    the required nested/opaque/bare-var shapes, and the whole-word
    read-only regression guards."""

    def test_param_default_reconstructs_bare_git_no_verb_allowed(self, shared_repo):
        """`g${x:-i}t` alone reconstructs the bare word `git` with no verb
        attached -- equivalent to running bare `git` (prints usage),
        correctly ALLOWED. The mechanism this proves is validated by
        `test_param_default_spliced_into_verb_denied` below, which DOES
        attach a verb."""
        cmd = "g" + chr(36) + "{x:-i}t"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_command_sub_echo_reconstructs_bare_git_no_verb_allowed(self, shared_repo):
        cmd = "g" + chr(36) + "(echo i)t"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_ansi_c_octal_reconstructs_bare_git_no_verb_allowed(self, shared_repo):
        cmd = "g" + chr(36) + "'" + chr(92) + "151't"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_command_sub_echo_reconstructs_benign_status_allowed(self, shared_repo):
        """`git st$(echo a)tus` reconstructs `git status` -- read-only,
        correctly ALLOWED even though the splicing mechanism fires."""
        cmd = "git st" + chr(36) + "(echo a)tus"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_param_default_spliced_into_verb_denied(self, shared_repo):
        """The actual exploit: `git ch${x:-e}ckout -- f`."""
        cmd = "git ch" + chr(36) + "{x:-e}ckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_command_sub_echo_spliced_into_verb_denied(self, shared_repo):
        cmd = "git ch" + chr(36) + "(echo e)ckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_backtick_echo_spliced_into_verb_denied(self, shared_repo):
        cmd = "git ch" + chr(96) + "echo e" + chr(96) + "ckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_nested_command_sub_glued_denied(self, shared_repo):
        """`$(echo $(echo e))` -- the inner substitution breaks the
        echo-literal regex's no-parens argument class, so the OUTER
        construct stays opaque/unresolved; the adjacency rule still catches
        it since it's glued on both sides regardless of not knowing its
        resolved value."""
        cmd = "git ch" + chr(36) + "(echo " + chr(36) + "(echo e))ckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_glued_opaque_python_c_substitution_denied(self, shared_repo):
        """`$(python -c ...)` is not an echo/printf form -- stays opaque;
        glued to a word char on both sides, denied regardless of its
        (unknowable to this guard) resolved value."""
        cmd = "git ch" + chr(36) + "(python -c 'x')ckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_glued_bare_dollar_var_denied(self, shared_repo):
        """A bare `$VAR` (no braces, no default) can never be resolved to a
        literal by this guard -- glued to `ckout`, denied."""
        cmd = "git ch" + chr(36) + "VARckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_ansi_c_whitespace_producing_still_denies_via_literal_text(self, shared_repo):
        """`$'\\t'` decodes to an actual tab character -- `git` and
        `checkout` remain literal substrings in the normalized text either
        way (round 5's unbounded search doesn't require adjacency), so this
        denies regardless; locks in that ANSI-C whitespace decode doesn't
        error or misbehave."""
        cmd = "git" + chr(36) + "'" + chr(92) + "t'checkout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_whole_word_command_sub_in_quotes_readonly_allowed(self, shared_repo):
        """`git diff -- "$(pwd)/f"` -- `$(pwd)` is bordered by a quote on
        the left and `/` on the right, neither a word character, so it is
        never glued; read-only verb, correctly ALLOWED."""
        cmd = 'git diff -- "' + chr(36) + '(pwd)/f"'
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_whole_word_bare_var_readonly_allowed(self, shared_repo):
        """`git log $REV` -- `$REV` is a standalone token, not glued;
        read-only verb, correctly ALLOWED."""
        cmd = "git log " + chr(36) + "REV"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_whole_word_command_sub_unrelated_to_git_allowed(self, shared_repo):
        """No `git` anywhere in the command at all -- the adjacency rule is
        unconditional (fires independent of `git` presence), but `$(pwd)`
        here is a standalone quoted token, not glued, so this stays
        allowed."""
        cmd = 'echo "' + chr(36) + '(pwd)"'
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_spliced_expansion_deny_message_names_the_rule(self, shared_repo):
        cmd = "git ch" + chr(36) + "VARckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        reason = out["permissionDecisionReason"].lower()
        assert "spliced inside a word" in reason
        assert "orchestrator" in reason

    def test_spliced_expansion_orchestrator_context_not_denied(self, shared_repo):
        """The adjacency rule targets subagents only, same as every other
        gate in this hook."""
        cmd = "git ch" + chr(36) + "VARckout -- t3.py"
        out = _decision(_run(_bash(cmd, agent=False, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_spliced_expansion_routing_allow_escape_works(self, shared_repo):
        cmd = (
            "git ch" + chr(36) + "VARckout -- t3.py"
            "  # routing-allow: orchestrator sanctioned this specific rephrase"
        )
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_spliced_expansion_denied_in_linked_worktree_is_allowed(self, linked_worktree):
        """The worktree exemption applies uniformly to both gates: a
        positively-proven linked worktree is the agent's own tree."""
        cmd = "git ch" + chr(36) + "VARckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(linked_worktree))))
        assert out["permissionDecision"] == "allow", out

    def test_ansi_c_decode_function_correctness(self):
        """Direct unit-level check of `_expand_ansi_c_strings`: octal, hex,
        4-digit unicode, and the `\\n`/`\\t` whitespace escapes all decode to
        their actual characters, not literal backslash-letter pairs."""
        spec = importlib.util.spec_from_file_location("_nx3c92m_guard_ansi", str(HOOK_SCRIPT))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        assert guard._expand_ansi_c_strings("$'" + chr(92) + "151'") == "i"  # octal
        assert guard._expand_ansi_c_strings("$'" + chr(92) + "x69'") == "i"  # hex
        assert guard._expand_ansi_c_strings("$'" + chr(92) + "u0069'") == "i"  # unicode
        assert guard._expand_ansi_c_strings("$'" + chr(92) + "n'") == "\n"
        assert guard._expand_ansi_c_strings("$'" + chr(92) + "t'") == "\t"

    def test_100kb_command_with_expansion_steps_scans_well_under_50ms(self):
        """Performance bound including the round-6/7 expansion-processing
        steps (ANSI-C decode, param-default resolve, command-sub resolve,
        scoped adjacency check) ahead of the primary scan."""
        spec = importlib.util.spec_from_file_location("_nx3c92m_guard3", str(HOOK_SCRIPT))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        big = "git " + ("x" * 100_000)
        t0 = time.perf_counter()
        normalized = guard._normalize_for_primary_scan(big)
        guard._find_spliced_expansion(normalized)
        guard._primary_match(normalized)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50, f"{elapsed_ms:.3f}ms >= 50ms budget"

    def test_100kb_many_dollar_var_occurrences_scans_well_under_50ms(self):
        """Adversarial for the adjacency check specifically: many bare
        `$VAR`-shaped constructs scattered through ~100KB."""
        spec = importlib.util.spec_from_file_location("_nx3c92m_guard4", str(HOOK_SCRIPT))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        big = "git " + (chr(36) + "VAR " ) * 5000 + "checkout"
        t0 = time.perf_counter()
        normalized = guard._normalize_for_primary_scan(big)
        guard._find_spliced_expansion(normalized)
        guard._primary_match(normalized)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50, f"{elapsed_ms:.3f}ms >= 50ms budget"

    def test_100kb_many_touching_letter_expansions_scans_well_under_50ms(self):
        """Round-7-specific adversarial case: THIS is the shape that
        actually exercises `_adjacent_letter_fragments` per-match (the
        `$VAR`-only case above has no letters touching it, so it never
        stressed fragment extraction at all). Found and fixed during this
        round's own verification: a first-cut `_adjacent_letter_fragments`
        using `text[:start]` (an O(start) slice) on EVERY match went
        quadratic -- 2+ SECONDS on this exact shape -- before being rewritten
        as a bounded character walk."""
        spec = importlib.util.spec_from_file_location("_nx3c92m_guard5", str(HOOK_SCRIPT))
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        big = "git " + (
            chr(36) + "{v}ar" + chr(36) + "{i}able "
        ) * 3000 + "checkout"
        t0 = time.perf_counter()
        normalized = guard._normalize_for_primary_scan(big)
        guard._find_spliced_expansion(normalized)
        guard._primary_match(normalized)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50, f"{elapsed_ms:.3f}ms >= 50ms budget"


# ── nexus-3c92m round 7: the adjacency rule, SCOPED ───────────────────────────
#
# The coordinator's own correction of round 6: an unconditional "any glue
# denies" rule was too broad, catching ordinary subagent interpolation with
# no exploit potential. Round 7 checks WHAT the glued letters actually
# spell -- see `_EXPANSION_CONSTRUCT_RE`'s module comment for the full
# two-branch rule -- rather than merely THAT something is glued.


class TestNexus3c92mRound7ScopedAdjacency:
    """The must-ALLOW ordinary-interpolation examples the coordinator named
    explicitly (round 6 would have wrongly denied every one of these), the
    must-DENY exploit shapes, and the message/verdict-naming regression.

    NOTE (round 8): these ALLOW/DENY verdicts are unchanged from round 7,
    but the MECHANISM for the git-present DENY cases changed underneath
    them -- round 7's simple-verb-subsequence-with-git-elsewhere branch was
    DROPPED entirely in round 8 (it could never cover compound/hyphenated
    verbs like `filter-branch`, a genuine regression round 7's own
    re-review found; see TestNexus3c92mRound8CompoundVerbCoverage below).
    Round 8's branch B denies on GLUE ALONE once `git` is present,
    independent of what the glued text spells -- so these tests still pass
    for a broader (not narrower) reason than their original docstrings
    claimed; docstrings updated accordingly rather than left stale."""

    def test_file_interpolation_allowed(self, shared_repo):
        """`echo file${i}.txt` -- no `git` anywhere, so branch A applies:
        joined fragment `file` is not an in-order subsequence of the
        3-letter target `git` (no shared letters)."""
        cmd = "echo file" + chr(36) + "{i}.txt"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_path_interpolation_in_quotes_allowed(self, shared_repo):
        """`cp "${dir}/a${n}.log" .` -- no `git` present (branch A); both
        expansions' joined letter fragments are length 0 or 1 (below the
        length-2 threshold branch A requires: `${dir}` touches no letters
        on either side, `${n}` touches only `a` on the left)."""
        cmd = 'cp "' + chr(36) + '{dir}/a' + chr(36) + '{n}.log" .'
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_package_filename_interpolation_allowed(self, shared_repo):
        """`tar xf pkg${ver}.tgz` -- no `git` present (branch A); joined
        fragment `pkg` is not a subsequence of `git` (no shared letters)."""
        cmd = "tar xf pkg" + chr(36) + "{ver}.tgz"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_command_sub_path_join_allowed(self, shared_repo):
        """`x=$(pwd)/sub` -- no `git` present (branch A); `$(pwd)` touches
        `=` on the left and `/` on the right, neither a letter, so the
        joined fragment is empty."""
        cmd = "x=" + chr(36) + "(pwd)/sub"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_joined_fragment_not_a_verb_subsequence_allowed(self, shared_repo):
        """`echo a${b}c` -- the coordinator's own worked example: no `git`
        anywhere (branch A applies), and joined fragment `ac` is not an
        in-order subsequence of `git` (no `a` in `git` at all)."""
        cmd = "echo a" + chr(36) + "{b}c"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_command_sub_reconstructs_verb_with_git_present_denied(self, shared_repo):
        """`git ch$(cmd)ckout -- f` -- a literal `git` is present, so
        branch B applies: `$(cmd)` sits glued to `ch` on the left and
        `ckout` on the right -- glue alone denies, independent of the
        fact that the glued text happens to spell something checkout-like."""
        cmd = "git ch" + chr(36) + "(cmd)ckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_bare_brace_var_reconstructs_verb_with_git_present_denied(self, shared_repo):
        """`git re${x}set --hard` -- a literal `git` is present, so branch
        B applies: `${x}` sits glued to `re` and `set` -- glue alone
        denies."""
        cmd = "git re" + chr(36) + "{x}set --hard"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_bare_brace_var_reconstructs_git_itself_denied(self, shared_repo):
        """`gi${X}t commit` -- NO literal `git` is present in the raw text
        (`${X}` is unresolved, so the substring `git` never actually
        appears) -- branch A applies: joined fragment `git` (from `gi` +
        `t`) is trivially a subsequence of the literal word `git` itself,
        denied UNCONDITIONALLY."""
        cmd = "gi" + chr(36) + "{X}t commit"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_default_resolution_still_fully_resolves_before_adjacency_runs(self, shared_repo):
        """`g${x:-i}t checkout` -- `${x:-i}` has a `:-` default, so it fully
        resolves to the literal `i` in the normalizer BEFORE the adjacency
        check ever runs, reconstructing `git checkout` directly.

        Round-7 review falsification (T2 nexus/3c92m-code-review-round7):
        disabling ONLY `_resolve_param_defaults` in a scratch copy still
        denied this command -- via the adjacency rule's own `g`...`t`
        bookend subsequence match on the UNRESOLVED text, not via the
        mechanism this test's docstring claims. The old version of this
        test asserted only the AGGREGATE `deny` verdict, which both
        mechanisms produce, so it never actually isolated which one fired.
        Fixed by asserting directly on `_normalize_for_primary_scan`'s
        OUTPUT: it can only equal the literal string `git checkout` if
        `_resolve_param_defaults` genuinely ran and substituted `${x:-i}`
        with `i` -- if that step were disabled, the normalized text would
        still contain `${x:-i}` verbatim and this assertion would fail
        immediately, independent of what the aggregate hook verdict is."""
        spec = importlib.util.spec_from_file_location(
            "_nx3c92m_guard_default_resolution", str(HOOK_SCRIPT),
        )
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        cmd = "g" + chr(36) + "{x:-i}t checkout"
        normalized = guard._normalize_for_primary_scan(cmd)
        assert normalized == "git checkout", (
            f"expected full literal resolution via _resolve_param_defaults, "
            f"got {normalized!r} instead"
        )
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_verb_shaped_fragment_without_any_git_present_allowed(self, shared_repo):
        """`a${x}dd` joins to `add` -- round 7 had a simple-verb-subsequence
        branch this would have exercised (gated on git-elsewhere); round 8
        DROPPED that branch entirely (subsumed by branch B). Under round 8
        this allows for a simpler reason: no `git` anywhere means branch A
        applies, and `add` is not an in-order subsequence of the 3-letter
        target `git` (no shared letters at all)."""
        cmd = "a" + chr(36) + "{x}dd bystander"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_deny_message_names_the_reconstructed_fragment(self, shared_repo):
        cmd = "git re" + chr(36) + "{x}set --hard"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        reason = out["permissionDecisionReason"]
        assert "reset" in reason
        assert "spliced inside a word" in reason.lower()

    def test_verdict_summary_names_the_round(self, tmp_path, shared_repo):
        """The `outcome=deny` routing-log entry (not just the model-facing
        reason) should be attributable to this specific gate for audit."""
        out = _decision(_run(_bash(
            "gi" + chr(36) + "{X}t commit", cwd=str(shared_repo),
        )))
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"].lower()
        assert "round 8" in reason


# ── nexus-3c92m round 8: compound/hyphenated verbs, closed for real ─────────
#
# Round 7's re-review (an 8th review round) found a Critical: round 7's
# `_SIMPLE_VERB_WORDS` scoping (deliberately excluding compound/hyphenated
# verbs to avoid the file/filter-branch and ac/branch false positives) left
# EVERY compound verb -- worktree add/remove/prune, branch -d/-D/-m, tag -d,
# update-ref, symbolic-ref, filter-branch, reflog expire, cherry-pick --
# completely UNPROTECTED against a splice, since no finite word list can
# also cover them without reopening the same false-positive class, and the
# primary contiguous-substring scan can never match a verb whose own
# spelling is broken by an unresolved expansion either. A REGRESSION from
# round 6, which caught all of these on glue alone. Round 8 resolves this
# by splitting the rule on git-presence rather than trying to extend the
# word list: branch A (no `git` anywhere) keeps the round-7 subsequence-of-
# `git` check verbatim; branch B (`git` present anywhere) reinstates round
# 6's unconditional glue check, now scoped to git-containing commands only
# -- see `_find_spliced_expansion`'s module comment for the full rule.


class TestNexus3c92mRound8CompoundVerbCoverage:
    """The eight compound-verb splice shapes from the round-7 review's
    Critical finding, each verified ALLOW pre-fix and required DENY here."""

    @pytest.mark.parametrize("cmd", [
        "git worktree re" + chr(36) + "{x}move w1",
        "git fil" + chr(36) + "{x}ter-branch",
        "git branch -" + chr(36) + "{x}d b",
        "git tag -" + chr(36) + "{x}d t",
        "git update-" + chr(36) + "{x}ref",
        "git symbolic-" + chr(36) + "{x}ref",
        "git reflog " + chr(36) + "{x}expire --all",
        "git cherry-" + chr(36) + "{x}pick abc",
    ])
    def test_compound_verb_splice_denied(self, cmd, shared_repo):
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", f"{cmd}: {out}"

    def test_git_log_with_spliced_filename_denied_documented_false_positive(self, shared_repo):
        """`git log file${i}.txt` -- a read-only, harmless command that
        happens to contain both a literal `git` and a glued expansion.
        Denied by branch B (glue alone, once `git` is present) -- an
        EXPLICITLY ACCEPTED false positive per the coordinator's round-8
        instructions, not a bug."""
        cmd = "git log file" + chr(36) + "{i}.txt"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out
        reason = out["permissionDecisionReason"].lower()
        assert "not allowed in git commands" in reason
        assert "whole-word" in reason

    @pytest.mark.parametrize("cmd", [
        "echo file" + chr(36) + "{i}.txt",
        'cp "' + chr(36) + '{dir}/a' + chr(36) + '{n}.log" .',
    ])
    def test_ordinary_interpolation_no_git_allowed(self, cmd, shared_repo):
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", f"{cmd}: {out}"

    def test_make_int_no_git_present_documents_the_coordinators_own_verified_edge_case(self, shared_repo):
        """`make i${n}t` -- NO `git` anywhere, so branch A applies (kept
        VERBATIM from round 7 per the round-8 instruction to not touch it).
        The round-8 relay listed this as a must-ALLOW case, but the round-7
        reviewer had ALREADY verified (and explicitly judged ACCEPTABLE,
        recommending only a documentation addition, not a behavior change)
        that this exact shape denies under branch A: joined fragment `it`
        (from `i` + `t`) IS a genuine in-order subsequence of the 3-letter
        target `git` (i@1, t@2) -- mathematically unavoidable for ANY
        2-letter fragment drawn from the letters `g`/`i`/`t` in order, and
        branch A is unchanged from round 7 here. Asserting the VERIFIED
        behavior (deny) rather than the apparently-inconsistent relay text,
        flagged explicitly in the round-8 handback for the coordinator to
        correct if a real behavior change was intended -- not something to
        invent unilaterally."""
        cmd = "make i" + chr(36) + "{n}t"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_accepted_proximity_false_positive_documented_in_known_limits(self, shared_repo):
        """Round-7 review's IMPORTANT finding: `_find_spliced_expansion`'s
        branches have no proximity/segment requirement -- ANY literal `git`
        anywhere in the whole raw text gates branch B, even a harmless,
        unrelated `git status` far from the actual splice. Same accepted
        trade-off as `git log --grep=commit` (rounds 4-5)."""
        cmd = "git status && echo add" + chr(36) + "{item} to list"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_dropped_simple_verb_branch_no_longer_exists(self):
        """`_SIMPLE_VERB_WORDS` was removed entirely in round 8 (subsumed
        by branch B) -- regression-lock that it stays gone rather than
        silently reappearing."""
        spec = importlib.util.spec_from_file_location(
            "_nx3c92m_guard_no_simple_verbs", str(HOOK_SCRIPT),
        )
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        assert not hasattr(guard, "_SIMPLE_VERB_WORDS")


# ── nexus-3c92m round 9: chained expansions, closed for real ────────────────
#
# Round 8's re-review (a 9th review round) found a Critical: Branch A was
# defeated by chaining two or more expansion constructs back-to-back with
# NOTHING between them -- `g${a}${b}i${c}${d}t checkout -- t3.py` allowed,
# and with all four variables unset, real bash genuinely executes this as
# `git checkout`. `_adjacent_letter_fragments` computed the touching-letter
# run per INDIVIDUAL construct match, so two directly-adjacent constructs
# each only ever saw ONE neighboring letter (the sibling construct blocked
# the run), never reaching the length->=2 threshold alone even though the
# combined reconstruction spans the target. Closed two ways, kept together
# as defense in depth: (1) `_expansion_construct_runs` groups directly-
# touching constructs into one unit before computing the fragment; (2) the
# ZERO-EXPANSION PASS deletes every remaining opaque construct outright and
# re-runs the primary git+verb scan on that text too -- a literal
# simulation of what real bash does with an unset variable, catching any
# chain length or interleaving pattern with no fragment reasoning at all.


class TestNexus3c92mRound9ChainedExpansions:
    """The exact repro from the round-8 review, chain-length and
    interleaving variants, the required must-ALLOW regression set, and a
    perf check at 1MB (round 8's review noted the shipped perf tests
    stopped at 100KB)."""

    def test_exact_repro_four_construct_chain_denied(self, shared_repo):
        """`g${a}${b}i${c}${d}t checkout -- t3.py` -- the reviewer's minimal
        repro: 2 constructs in the g-i gap, 2 in the i-t gap. Verified
        against real bash: with a/b/c/d all unset, this genuinely executes
        as `git checkout`."""
        cmd = (
            "g" + chr(36) + "{a}" + chr(36) + "{b}i"
            + chr(36) + "{c}" + chr(36) + "{d}t checkout -- t3.py"
        )
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_three_construct_chain_denied(self, shared_repo):
        """`g${a}${b}${c}it checkout` -- three constructs grouped into one
        run, confirming the run-grouping loop isn't hardcoded to exactly 2."""
        cmd = "g" + chr(36) + "{a}" + chr(36) + "{b}" + chr(36) + "{c}it checkout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_chain_inside_a_verb_denied(self, shared_repo):
        """`git ch${a}${b}eckout -- f` -- the chain sits inside the VERB,
        not the word `git` itself, with a literal `git` present (branch B
        would also independently catch this on glue alone)."""
        cmd = "git ch" + chr(36) + "{a}" + chr(36) + "{b}eckout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_chained_command_substitution_forms_denied(self, shared_repo):
        """`g$(true)$(true)it checkout` -- chained `$(...)` forms, not just
        `${...}`; `true` is a portable always-succeeds command, no unset-var
        trick needed for THIS specific construct type."""
        cmd = "g" + chr(36) + "(true)" + chr(36) + "(true)it checkout -- t3.py"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_chained_backtick_forms_denied(self, shared_repo):
        """`g\\`true\\`\\`true\\`it checkout` -- chained backtick forms."""
        cmd = (
            "g" + chr(96) + "true" + chr(96) + chr(96) + "true" + chr(96)
            + "it checkout -- t3.py"
        )
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "deny", out

    def test_separated_constructs_still_allowed(self, shared_repo):
        """`${dir}/a${n}` -- separated by `/`, NOT a chain (a non-word
        character breaks the run) -- must stay allowed, confirming the
        run-grouping fix doesn't over-merge non-adjacent constructs."""
        cmd = 'cp "' + chr(36) + '{dir}/a' + chr(36) + '{n}.log" .'
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_file_interpolation_still_allowed_zero_expansion_has_no_git(self, shared_repo):
        """`echo file${i}.txt` -- the zero-expansion text is literally
        `echo file.txt`, which contains no `git` at all."""
        cmd = "echo file" + chr(36) + "{i}.txt"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_command_sub_path_join_still_allowed(self, shared_repo):
        cmd = "x=" + chr(36) + "(pwd)/sub"
        out = _decision(_run(_bash(cmd, cwd=str(shared_repo))))
        assert out["permissionDecision"] == "allow", out

    def test_zero_expansion_pass_directly(self):
        """Unit-level: `_delete_all_expansions` on the exact repro produces
        the literal, contiguous text `git checkout -- t3.py`."""
        spec = importlib.util.spec_from_file_location(
            "_nx3c92m_guard_zero_expansion", str(HOOK_SCRIPT),
        )
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        cmd = (
            "g" + chr(36) + "{a}" + chr(36) + "{b}i"
            + chr(36) + "{c}" + chr(36) + "{d}t checkout -- t3.py"
        )
        normalized = guard._normalize_for_primary_scan(cmd)
        zero = guard._delete_all_expansions(normalized)
        assert zero == "git checkout -- t3.py", repr(zero)

    def test_expansion_construct_runs_groups_adjacent_matches(self):
        """Unit-level: `_expansion_construct_runs` on the exact repro
        produces exactly 2 runs (the g-i gap pair, the i-t gap pair), not
        4 individual matches."""
        spec = importlib.util.spec_from_file_location(
            "_nx3c92m_guard_runs", str(HOOK_SCRIPT),
        )
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        cmd = (
            "g" + chr(36) + "{a}" + chr(36) + "{b}i"
            + chr(36) + "{c}" + chr(36) + "{d}t checkout -- t3.py"
        )
        runs = guard._expansion_construct_runs(cmd)
        assert len(runs) == 2, runs

    def test_100kb_and_1mb_perf(self):
        """Round-8 review noted the shipped perf tests stopped at 100KB;
        this adds the 1MB check the reviewer spot-checked manually."""
        spec = importlib.util.spec_from_file_location(
            "_nx3c92m_guard_perf_1mb", str(HOOK_SCRIPT),
        )
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)

        def timed(cmd):
            t0 = time.perf_counter()
            normalized = guard._normalize_for_primary_scan(cmd)
            zero = guard._delete_all_expansions(normalized)
            guard._find_spliced_expansion(normalized)
            guard._primary_match(normalized)
            guard._primary_match(zero)
            return (time.perf_counter() - t0) * 1000

        ms_100k = timed("git " + ("x" * 100_000))
        assert ms_100k < 50, f"100KB: {ms_100k:.3f}ms >= 50ms budget"
        ms_1m = timed("git " + ("x" * 1_000_000))
        assert ms_1m < 500, f"1MB: {ms_1m:.3f}ms >= 500ms budget"
        ms_1m_chain = timed("git " + (chr(36) + "{a}") * 50_000 + "checkout")
        assert ms_1m_chain < 500, f"1MB chained: {ms_1m_chain:.3f}ms >= 500ms budget"
