# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-219 Phase 2 Step 1 (nexus-wauo1.10): `tests/cc-validation/runner.sh`
launches its tmux server -- and so every pane under it, per the Phase 0
spike's transport rule ("a tmux session takes its environment from the tmux
SERVER, not from the command that asks for the session") -- under
`tests/e2e/lib/claude_credentials.py run --`, never with the operator's own
`Claude Code-credentials` keychain item copied into
`$TEST_HOME/.claude/.credentials.json`.

Before this change, `runner.sh` provisioned a credential FILE:
`provision_credentials()` read `_cred_tool pick` (the shared picker over the
operator's interactive-login keychain item), wrote the result to
`$TEST_HOME/.claude/.credentials.json`, refreshed a persistent snapshot at
`tests/e2e/.claude-auth/.credentials.json`, and fell back to that snapshot
when the keychain read failed -- three of RDR-219's "gaps to close" (Gap 1:
a copy left on disk; Gap 3: the harness identity is the operator's own
login) in one function. This test pins the post-migration shape: no
`.credentials.json` write, no read of the operator's `Claude
Code-credentials` service or its interactive-login fallback snapshot, and
the tmux server-starting call wrapped in `claude_credentials.py run --`.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNNER = REPO_ROOT / "tests" / "cc-validation" / "runner.sh"


def _text() -> str:
    return RUNNER.read_text(encoding="utf-8")


def _noncomment_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def test_runner_exists() -> None:
    assert RUNNER.is_file(), f"expected {RUNNER}"


def test_tmux_server_start_is_wrapped_in_credentials_run() -> None:
    """The call that creates the tmux SERVER (`new-session`, the first
    tmux invocation against the harness's private socket) must run under
    `claude_credentials.py run --`, so CLAUDE_CODE_OAUTH_TOKEN reaches the
    server's own environment before any pane exists."""
    text = _text()
    new_session_lines = [
        line for line in _noncomment_lines(text) if "new-session" in line and "tmux" in line
    ]
    assert new_session_lines, "expected a tmux new-session call in runner.sh"
    assert len(new_session_lines) == 1, (
        f"expected exactly one tmux new-session call, found {len(new_session_lines)}: "
        f"{new_session_lines}"
    )
    (line,) = new_session_lines
    assert "claude_credentials.py" in line or "_cred_tool" in line, (
        "tmux new-session (the call that starts the private tmux SERVER) must run "
        f"under `claude_credentials.py run --` (RDR-219 transport rule) -- got: {line!r}"
    )
    assert re.search(r"\brun\b.*--.*new-session", line) or re.search(
        r"_cred_tool run -- .*new-session", line
    ), f"expected `run -- ... new-session`, got: {line!r}"


def test_no_credentials_json_write() -> None:
    """No write to a `.credentials.json` path anywhere in the harness --
    RDR-219 rule 2: the token travels in the environment, never a file."""
    hits = [line for line in _noncomment_lines(_text()) if ".credentials.json" in line]
    assert not hits, f"runner.sh must not read or write a .credentials.json file: {hits}"


def test_no_interactive_login_keychain_read() -> None:
    """`_cred_tool pick`/`check` (the interactive-login picker) and the
    literal service name are gone -- the harness now authenticates from the
    dedicated automation token only (RDR-219 rule 1)."""
    text = _text()
    hits = [
        line
        for line in _noncomment_lines(text)
        if re.search(r"_cred_tool\s+(pick|check)\b", line) or "Claude Code-credentials" in line
    ]
    assert not hits, f"runner.sh must not read the operator's interactive login: {hits}"


def test_no_provision_credentials_function() -> None:
    assert "provision_credentials" not in _text(), (
        "provision_credentials() (the file-provisioning gate) should be deleted, not "
        "merely unused -- RDR-219 Migration of the 26 sites"
    )


def test_no_auth_dir_snapshot_fallback() -> None:
    """The persistent snapshot fallback (`$AUTH_DIR/.credentials.json`) is
    gone: Phase 0 (T2 nexus_rdr/219-research-9..13) verified no launch shape
    needs a file fallback for this harness."""
    hits = [line for line in _noncomment_lines(_text()) if "AUTH_DIR" in line]
    assert not hits, f"runner.sh must not reference the snapshot fallback dir: {hits}"


def test_claude_run_uses_the_helper_not_a_bare_claude_invocation_wrapper() -> None:
    """Sanity: the shared helper module is actually referenced somewhere in
    the file (the `run --` wrapping above), so this isn't pinning an
    accidentally-vacuous absence."""
    assert "claude_credentials.py" in _text()
