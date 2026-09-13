"""Tests for the SubagentStart hook script's session_id export (nexus-7o1zh)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "subagent-start.sh"
)

STDIN_PAYLOAD = json.dumps({
    "session_id": "test-session",
    "hook_event_name": "SubagentStart",
    "task": "general research task",
    "prompt": "look into something",
})


def _run_hook(
    *,
    env_overrides: dict[str, str] | None = None,
    stdin: str = STDIN_PAYLOAD,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": os.environ.get("PATH", ""),
        **(env_overrides or {}),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
        cwd=cwd,
    )


class TestSubagentStartHook:
    def test_script_exists_and_is_executable(self) -> None:
        assert SCRIPT.exists(), f"Script not found: {SCRIPT}"
        assert os.access(SCRIPT, os.X_OK), f"Script not executable: {SCRIPT}"

    def test_exits_zero(self) -> None:
        result = _run_hook()
        assert result.returncode == 0

    def test_emits_json_envelope(self) -> None:
        result = _run_hook()
        payload = json.loads(result.stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SubagentStart"

    def test_orchestration_directive_rows_injected(self) -> None:
        """RDR-184 P1.3 (nexus-ccs9v.8): the THREE orchestration directive
        rows — Completion (Gap 1), Inbox (Gap 2), Git (Gap 4) — ride the
        live injection path into every subagent's initial context.

        nexus-cnzei.2 (C4): the Completion row is now scoped by
        background/foreground, not a single unconditional instruction.
        The SubagentStart payload carries no background flag (audit
        finding), so the wording is a conditional the agent evaluates
        itself rather than a blanket "always SendMessage before idling"
        that a foreground agent's own contract (final message IS the
        hand-back) contradicts."""
        result = _run_hook()
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "| Completion |" in ctx
        assert "Background: SendMessage" in ctx
        assert "Foreground: final message IS the hand-back" in ctx
        assert "| Inbox |" in ctx
        assert "Re-check inbox right before composing any hand-back" in ctx
        assert "| Git |" in ctx
        assert "NEVER git add/commit" in ctx
        assert "orchestrator commits pathspec-limited" in ctx

    def test_heredoc_bodies_respect_deadlock_ceiling(self) -> None:
        """The file's own rule: heredoc bodies stay under 500 bytes (bash
        5.3.x pipe deadlock). Guard the NEW ORCH heredoc mechanically;
        pre-existing PHASE_GATE (540 bytes) is grandfathered until its
        owner trims it."""
        import re

        src = SCRIPT.read_text()
        m = re.search(r"cat <<'ORCH'\n(.*?)\nORCH\n", src, re.S)
        assert m is not None, "ORCH heredoc missing from subagent-start.sh"
        assert len(m.group(1).encode()) < 500


class TestClaimantIdInjection:
    """RDR-205 "Identity and addressing" (bead nexus-em75s.11): this is the
    ONE line subagent-start.sh adds beyond its existing injection -- the
    harness's own opaque per-instance agent_id, plus the tuple-space
    mailbox address derived from it (``mailbox/<agent_id>``). No hook
    mints anything, and this script does no network I/O at all: the
    async SubagentStart/SubagentStop entries beside it write the actual
    ledger tuples independently, keyed on this same id.
    """

    def test_claimant_id_and_mailbox_line_injected(self) -> None:
        payload = json.dumps({
            "session_id": "test-session",
            "hook_event_name": "SubagentStart",
            "agent_id": "aworker1234567890abcdef",
            "task": "general research task",
            "prompt": "look into something",
        })
        result = _run_hook(stdin=payload)
        assert result.returncode == 0, result.stderr
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Claimant id: aworker1234567890abcdef" in ctx
        assert "mailbox/aworker1234567890abcdef" in ctx

    def test_no_claimant_line_when_agent_id_absent(self) -> None:
        """The original STDIN_PAYLOAD fixture carries no agent_id -- the
        injected line must not appear with nothing to fill it."""
        result = _run_hook()
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Claimant id:" not in ctx

    def test_script_does_no_network_io(self) -> None:
        """RDR-205: "It does no network I/O." A crude but effective source
        scan -- no curl/wget/socket/urllib/requests token anywhere in the
        script. The actual tuple write lives in the separate async hook
        entries (subagent-start-tuple-async.sh), never here.
        """
        src = SCRIPT.read_text()
        for forbidden in ("curl ", "wget ", "urllib", "socket.", "requests."):
            assert forbidden not in src, (
                f"subagent-start.sh must perform no network I/O; found {forbidden!r}"
            )


class TestSessionIdExport:
    """nexus-7o1zh: this hook runs detached from any live nx-mcp process and
    cannot rely on env-var inheritance from a parent Claude session. It must
    extract ``session_id`` from its own stdin JSON payload and export it as
    ``NX_SESSION_ID`` before invoking ``nx scratch list`` (the "Inject
    current T1 scratch entries" section), so the CLI resolves the CORRECT
    session's T1 data instead of falling through to the machine-wide (and
    possibly clobbered-by-a-sibling-session) ``current_session`` flat file
    (nexus-36q84's collision, same class)."""

    @staticmethod
    def _make_fake_nx(tmp_path: Path) -> Path:
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        nx_script = fake_bin / "nx"
        nx_script.write_text(
            "#!/bin/bash\n"
            'echo "NX_SESSION_ID=${NX_SESSION_ID:-<unset>}" >> "$NX_CALL_LOG"\n'
            'echo "no scratch entries"\n'
            "exit 0\n"
        )
        nx_script.chmod(0o755)
        return fake_bin

    def test_exports_session_id_from_stdin_payload(self, tmp_path) -> None:
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"

        result = _run_hook(
            env_overrides={
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "NX_CALL_LOG": str(log_file),
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=test-session" in log_contents, log_contents

    def test_missing_session_id_in_payload_preserves_ambient_env(
        self, tmp_path
    ) -> None:
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"

        payload = json.dumps({
            "hook_event_name": "SubagentStart",
            "task": "no session_id field",
        })

        result = _run_hook(
            stdin=payload,
            env_overrides={
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "NX_CALL_LOG": str(log_file),
                "NX_SESSION_ID": "pre-existing-ambient-value",
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=pre-existing-ambient-value" in log_contents, log_contents


class TestNoMachineWideActiveBeadLine:
    """nexus-cnzei.2 (S7): the old "Active Bead: ..." line named the
    first `bd list --status=in_progress` row MACHINE-WIDE -- with several
    sessions or worktree agents live, that is almost always a peer's
    bead, not this dispatch's. Dropped outright."""

    def test_active_bead_line_never_injected_even_with_a_real_in_progress_bead(
        self, tmp_path,
    ) -> None:
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        bd_script = fake_bin / "bd"
        bd_script.write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == "list" ]]; then\n'
            '  echo "in_progress nexus-somepeer Some peer bead"\n'
            "  exit 0\n"
            "fi\n"
            "exit 0\n"
        )
        bd_script.chmod(0o755)

        result = _run_hook(
            env_overrides={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        )
        assert result.returncode == 0
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "Active Bead" not in ctx
        assert "nexus-somepeer" not in ctx


class TestWorktreeProjectResolution:
    """nexus-cnzei.2 (S6): `--show-toplevel` resolves to the WORKTREE root
    for a worktree-isolated dispatch, so its basename was the worktree's
    own directory name, never the project's -- the T2 scan below always
    ran with the wrong project name, and the Knowledge Map cache lookup
    (keyed on the MAIN repo's sha1'd path) always missed, falling through
    to an unrelated global cache. `--git-common-dir` resolves to the same
    shared .git directory from either the primary checkout or any linked
    worktree, so its parent directory names the actual project the same
    way from both."""

    def test_t2_scan_uses_main_repo_name_not_worktree_dir_name(
        self, tmp_path,
    ) -> None:
        main_repo = tmp_path / "the-real-project"
        main_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(main_repo)], check=True)
        subprocess.run(
            ["git", "-C", str(main_repo), "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "--allow-empty", "-m", "init"],
            check=True,
        )
        worktree_dir = tmp_path / "totally-different-worktree-name"
        subprocess.run(
            ["git", "-C", str(main_repo), "worktree", "add", "-q", str(worktree_dir), "-b", "wt-branch"],
            check=True,
        )

        # Stub CLAUDE_PLUGIN_ROOT/hooks/scripts/t2_prefix_scan.py: record the
        # PROJECT argument it was called with, print a marker so the "## T2
        # Memory" section actually renders.
        plugin_root = tmp_path / "plugin_root"
        scan_dir = plugin_root / "hooks" / "scripts"
        scan_dir.mkdir(parents=True)
        call_log = tmp_path / "scan_calls.log"
        (scan_dir / "t2_prefix_scan.py").write_text(
            "import sys\n"
            f"open({str(call_log)!r}, 'a').write(sys.argv[1] + chr(10))\n"
            "print('T2-SCAN-MARKER')\n"
        )

        result = _run_hook(
            env_overrides={"CLAUDE_PLUGIN_ROOT": str(plugin_root)},
            cwd=str(worktree_dir),
        )
        assert result.returncode == 0, result.stderr
        logged = call_log.read_text().strip() if call_log.exists() else ""
        assert logged == "the-real-project", (
            f"t2_prefix_scan.py was called with PROJECT={logged!r}, "
            f"expected the MAIN repo's name, not the worktree dir's"
        )
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "T2-SCAN-MARKER" in ctx
