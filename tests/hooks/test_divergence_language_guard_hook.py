"""Tests for the divergence-language-guard hook's session_id export
(nexus-7o1zh), ``nexus.hooks.divergence_language_guard``.

RDR-215 bead nexus-q02nx.19 ported this hook from
``conexus/hooks/scripts/divergence-language-guard.sh``; bead .21
re-declared its ``hooks.json`` entry to the
``hook_divergence_language_guard`` mcp_tool, so the bash script no longer
runs in production and this file drives the Python module only
(nexus-q02nx.21).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _make_payload(session_id: str | None, file_path: str) -> str:
    body: dict[str, object] = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": file_path},
    }
    if session_id is not None:
        body["session_id"] = session_id
    return json.dumps(body)


#: A child process, not an in-process call: these tests vary PATH and the
#: environment per case, and ``os.environ`` is process-global.
_PY_DRIVER = """
import json, sys
from nexus._hook_runtime._io import never_fail
from nexus.hooks import divergence_language_guard as _hook

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
    result = never_fail(lambda: _hook.run(payload), "divergence_language_guard")
finally:
    sys.stdout = real_stdout
if result.stdout is not None:
    sys.stdout.write(result.stdout + "\\n")
sys.exit(result.exit_code)
"""


def _run_hook(
    stdin: str,
    *,
    env_overrides: dict[str, str] | None = None,
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
    )


class TestSessionIdExport:
    """nexus-7o1zh: this hook runs detached from any live nx-mcp process and
    cannot rely on env-var inheritance from a parent Claude session. It must
    extract ``session_id`` from its own stdin JSON payload and export it as
    ``NX_SESSION_ID`` before invoking ``nx scratch put`` (the hit-logging
    section), so the CLI resolves the CORRECT session's T1 data instead of
    falling through to the machine-wide (and possibly
    clobbered-by-a-sibling-session) ``current_session`` flat file
    (nexus-36q84's collision, same class)."""

    @staticmethod
    def _make_fake_nx(tmp_path: Path) -> Path:
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        nx_script = fake_bin / "nx"
        nx_script.write_text(
            "#!/bin/bash\n"
            'echo "NX_SESSION_ID=${NX_SESSION_ID:-<unset>}" >> "$NX_CALL_LOG"\n'
            "exit 0\n"
        )
        nx_script.chmod(0o755)
        return fake_bin

    @staticmethod
    def _make_post_mortem_file(tmp_path: Path) -> Path:
        """A file under a docs/rdr/post-mortem/ path (required by the hook's
        fast-no-op guard) whose content matches the divergence-language
        pattern bank (also required, to reach the nx scratch put call)."""
        pm_dir = tmp_path / "docs" / "rdr" / "post-mortem"
        pm_dir.mkdir(parents=True)
        pm_file = pm_dir / "test-incident.md"
        pm_file.write_text("This fix was deferred to a follow-up RDR.\n")
        return pm_file

    def test_exports_session_id_from_stdin_payload(self, tmp_path) -> None:
        fake_bin = self._make_fake_nx(tmp_path)
        log_file = tmp_path / "nx_calls.log"
        pm_file = self._make_post_mortem_file(tmp_path)

        result = _run_hook(
            _make_payload("test-session", str(pm_file)),
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
        pm_file = self._make_post_mortem_file(tmp_path)

        result = _run_hook(
            _make_payload(None, str(pm_file)),
            env_overrides={
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "NX_CALL_LOG": str(log_file),
                "NX_SESSION_ID": "pre-existing-ambient-value",
            },
        )

        assert result.returncode == 0
        log_contents = log_file.read_text() if log_file.exists() else ""
        assert "NX_SESSION_ID=pre-existing-ambient-value" in log_contents, log_contents
