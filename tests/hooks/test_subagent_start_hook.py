"""Tests for the SubagentStart hook's session_id export (nexus-7o1zh),
``nexus.hooks.subagent_start``.

RDR-215 bead nexus-q02nx.18 ported this hook from
``conexus/hooks/scripts/subagent-start.sh``; bead .21 re-declared its
``hooks.json`` entry to the ``hook_subagent_start`` mcp_tool, so the bash
script no longer runs in production and this file drives the Python
module only (nexus-q02nx.21).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import nexus.hooks.subagent_start as _subagent_start_mod
from tests.db._fake_t2_server import FakeT2HandlerBase, fake_http_server

STDIN_PAYLOAD = json.dumps({
    "session_id": "test-session",
    "hook_event_name": "SubagentStart",
    "task": "general research task",
    "prompt": "look into something",
})


#: Drives the ported module in a CHILD PROCESS, exactly as
#: test_subagent_stop_hook.py's own driver does — a real process with a real
#: environment, since several tests here vary env (and cwd) per call and
#: ``os.environ``/the process cwd are process-global.
_PY_DRIVER = """
import json, sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import subagent_start

raw = sys.stdin.read()
try:
    payload = json.loads(raw) if raw.strip() else None
except Exception:
    payload = None
if not isinstance(payload, dict):
    payload = None
# nexus-zptvf: GUARD STDOUT while the verb runs. This verb spawns
# through bounded_subprocess.run_bounded, which emits a structlog
# warning on TIMEOUT -- and structlog's unconfigured default
# PrintLoggerFactory writes to STDOUT, the channel this driver
# json.loads() below. Production is safe by a DIFFERENT route than
# the nx-hook verbs: hooks.json wires this one as an mcp_tool against
# nx-mcp, whose own configure_logging("mcp") runs before serving, so
# there is no entry.main fd guard here to inherit. A standing
# reviewer reproduced the corruption in this exact driver.
real_stdout = sys.stdout
sys.stdout = sys.stderr
try:
    result = never_fail(lambda: subagent_start.run(payload), "subagent_start")
finally:
    sys.stdout = real_stdout
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""


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
        [sys.executable, "-c", _PY_DRIVER],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
        cwd=cwd,
    )


class TestSubagentStartHook:
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
        assert "Background: SubagentHandback (or SendMessage)" in ctx  # nexus-4xo3k: the hand-back is the report
        assert "Foreground: final message IS the hand-back" in ctx
        assert "| Inbox |" in ctx
        assert "Re-check inbox right before composing any hand-back" in ctx
        assert "| Git |" in ctx
        assert "NEVER git add/commit" in ctx
        assert "orchestrator commits pathspec-limited" in ctx

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

    def test_module_does_no_network_io(self) -> None:
        """RDR-205: "It does no network I/O." A crude but effective source
        scan -- no curl/wget/socket/urllib/requests token anywhere in the
        module. The actual tuple write lives in the separate hook
        (``hook_subagent_start_tuple`` / ``nexus.hooks.tuple_projection``),
        never here. Re-pointed at the Python module (nexus-q02nx.21): the
        bash script this used to scan no longer runs in production.
        """
        src = Path(_subagent_start_mod.__file__).read_text()
        for forbidden in ("curl ", "wget ", "urllib", "socket.", "requests."):
            assert forbidden not in src, (
                f"nexus.hooks.subagent_start must perform no network I/O; found {forbidden!r}"
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


def _make_t2_memory_handler(
    token: str, seen_prefixes: list[str], entries: list[dict[str, str]],
) -> type[FakeT2HandlerBase]:
    """A fake T2 engine serving exactly the two GET routes
    ``nexus.hooks.t2_prefix_scan.scan`` calls (via ``HttpMemoryStore``),
    recording every ``prefix`` value ``/v1/memory/projects`` was called
    with in *seen_prefixes*."""

    class _Handler(FakeT2HandlerBase):
        TOKEN = token

        def do_GET(self) -> None:
            if not self._check_auth():
                return
            path = urlparse(self.path).path
            params = self._params()
            if path == "/v1/memory/projects":
                prefix = params.get("prefix", "")
                seen_prefixes.append(prefix)
                self._send(200, [{"project": prefix, "last_updated": "2026-01-01T00:00:00Z"}])
            elif path == "/v1/memory/all":
                self._send(200, entries)
            else:
                self._send(404, {"error": "not found"})

    return _Handler


def _service_env_overrides(url: str, token: str) -> dict[str, str]:
    """``NX_SERVICE_HOST``/``PORT``/``TOKEN`` env overrides pointing a
    subprocess's ``resolve_service_endpoint()`` at *url*, the fake T2
    engine.

    ``NX_SERVICE_URL`` is blanked deliberately: it is leg 1 of
    ``resolve_service_endpoint()``'s resolution order and this suite's
    own autouse ``t2_service_env``/``_pin_t2_substrate`` fixture chain
    sets it (ambient, real) to the suite's OWN hermetic engine substrate
    for every test regardless of marker -- without clearing it here, that
    real ambient endpoint wins over the host/port leg below and the
    subprocess talks to the wrong (real) engine instead of this test's
    fake one.
    """
    host_port = url.split("://", 1)[1]
    host, port = host_port.rsplit(":", 1)
    return {
        "NX_SERVICE_URL": "",
        "NX_SERVICE_HOST": host,
        "NX_SERVICE_PORT": port,
        "NX_SERVICE_TOKEN": token,
    }


class TestT2MemorySectionInProcess:
    """RDR-215 bead nexus-b5ugt: the "## T2 Memory" section is produced
    IN-PROCESS (``nexus.hooks.t2_prefix_scan.scan``, called from
    ``_t2_memory_section``) against the real T2 HTTP client, never by
    shelling out to a ``$CLAUDE_PLUGIN_ROOT``-resolved subprocess.

    Non-vacuity: this test FAILS against the pre-fix ``_t2_memory_section``
    (which built ``f"{plugin_root}/hooks/scripts/t2_prefix_scan.py"`` from
    ``os.environ.get("CLAUDE_PLUGIN_ROOT", "")``). This test never sets
    ``CLAUDE_PLUGIN_ROOT`` — exactly the live production shape, since
    ``conexus/.mcp.json`` sets it to the LITERAL, unexpanded string
    ``"${CLAUDE_PLUGIN_ROOT}"`` rather than a real path — so the old code
    resolved an unreachable script path, silently produced no T2 output,
    and this assertion failed regardless of how reachable the fake engine
    below was. Verified: reverting only ``_t2_memory_section`` (`git show
    HEAD -- src/nexus/hooks/subagent_start.py`'s pre-fix version) and
    rerunning this test fails on ``assert "## T2 Memory" in ctx``.
    """

    def test_t2_memory_section_renders_from_real_engine(self, tmp_path) -> None:
        seen_prefixes: list[str] = []
        handler_cls = _make_t2_memory_handler(
            "test-bearer-t2section", seen_prefixes,
            [{"title": "T2-SCAN-MARKER", "content": "marker body line"}],
        )

        repo = tmp_path / "in-process-project"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "--allow-empty", "-m", "init"],
            check=True,
        )

        with fake_http_server(handler_cls) as url:
            result = _run_hook(
                env_overrides=_service_env_overrides(url, "test-bearer-t2section"),
                cwd=str(repo),
            )

        assert result.returncode == 0, result.stderr
        assert seen_prefixes == ["in-process-project"], seen_prefixes
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "## T2 Memory" in ctx
        assert "T2-SCAN-MARKER" in ctx


class TestWorktreeProjectResolution:
    """nexus-cnzei.2 (S6): `--show-toplevel` resolves to the WORKTREE root
    for a worktree-isolated dispatch, so its basename was the worktree's
    own directory name, never the project's -- the T2 scan below always
    ran with the wrong project name, and the Knowledge Map cache lookup
    (keyed on the MAIN repo's sha1'd path) always missed, falling through
    to an unrelated global cache. `--git-common-dir` resolves to the same
    shared .git directory from either the primary checkout or any linked
    worktree, so its parent directory names the actual project the same
    way from both.

    RDR-215 bead nexus-b5ugt: the T2 scan is now in-process
    (``nexus.hooks.t2_prefix_scan.scan``), so the interception point is a
    fake T2 engine reached via ``NX_SERVICE_HOST``/``PORT``/``TOKEN`` env,
    not a stubbed ``$CLAUDE_PLUGIN_ROOT/hooks/scripts/t2_prefix_scan.py``
    script (that interception point no longer exists on this call path)."""

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

        seen_prefixes: list[str] = []
        handler_cls = _make_t2_memory_handler(
            "test-bearer-worktree", seen_prefixes,
            [{"title": "T2-SCAN-MARKER", "content": "marker body line"}],
        )

        with fake_http_server(handler_cls) as url:
            result = _run_hook(
                env_overrides=_service_env_overrides(url, "test-bearer-worktree"),
                cwd=str(worktree_dir),
            )
        assert result.returncode == 0, result.stderr
        assert seen_prefixes == ["the-real-project"], (
            f"the T2 scan was called with prefix={seen_prefixes!r}, "
            f"expected the MAIN repo's name, not the worktree dir's"
        )
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "T2-SCAN-MARKER" in ctx


class TestAgentTypeClassification:
    """nexus-cnzei.6 (injection audit S1): the real harness payload carries
    agent_id/agent_type/session_id/prompt_id and never task/prompt, so a
    classification keyed on TASK_TEXT alone never fires in production. These
    tests use a payload shaped like a REAL dispatch — no ``task``/``prompt``
    keys at all — to prove the agent_type-keyed classification actually
    fires without them, unlike STDIN_PAYLOAD above (which fabricates a
    task/prompt the harness never sends and would mask this exact defect)."""

    _REAL_SHAPE_NO_TASK_OR_PROMPT = {
        "session_id": "test-session",
        "hook_event_name": "SubagentStart",
        "prompt_id": "abc123",
    }

    def _payload(self, agent_type: str) -> str:
        return json.dumps({**self._REAL_SHAPE_NO_TASK_OR_PROMPT, "agent_type": agent_type})

    def test_code_review_agent_type_skips_storage_docs_with_no_task_text(self) -> None:
        result = _run_hook(stdin=self._payload("code-review-expert"))
        assert result.returncode == 0, result.stderr
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "## nx storage" not in ctx
        assert "## Analytical operators" not in ctx

    def test_explore_agent_type_skips_storage_docs_with_no_task_text(self) -> None:
        result = _run_hook(stdin=self._payload("Explore"))
        assert result.returncode == 0, result.stderr
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "## nx storage" not in ctx
        assert "## Analytical operators" not in ctx

    def test_unclassified_agent_type_keeps_storage_docs_with_no_task_text(self) -> None:
        """A general-purpose dispatch (no task/prompt, agent_type matching
        neither classification) still gets the full storage/operators
        content — this is the negative case proving the classification is
        selective, not merely always-on."""
        result = _run_hook(stdin=self._payload("general-purpose"))
        assert result.returncode == 0, result.stderr
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "## nx storage" in ctx
        assert "## Analytical operators" in ctx

    def test_task_text_fallback_still_classifies_when_present(self) -> None:
        """The old TASK_TEXT path stays live as a fallback: a payload that
        DOES carry task/prompt (the shape tests, not the harness, produce)
        still classifies correctly, so this is a strict addition, not a
        replacement that could regress an existing caller."""
        payload = json.dumps({
            "session_id": "test-session",
            "hook_event_name": "SubagentStart",
            "task": "run a lint style check",
            "prompt": "",
        })
        result = _run_hook(stdin=payload)
        assert result.returncode == 0, result.stderr
        ctx = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        assert "## nx storage" not in ctx
