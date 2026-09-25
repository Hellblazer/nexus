# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-219 Phase 2 Step 1c (nexus-wauo1.12): tests/e2e/release-sandbox.sh's
`tmux` mode must launch Claude Code with the harness's own automation token
(`tests/e2e/lib/claude_credentials.py run --`), never by copying the
operator's cached `tests/e2e/.claude-auth/.credentials.json` into the
sandbox HOME.

THE DEFECT THIS CLOSES. Before this fix, `tmux)` mode required
`$REPO_ROOT/tests/e2e/.claude-auth/.credentials.json` to exist (a snapshot
of the operator's own interactive login, cached by `auth-login.sh`) and
copied it verbatim into `$SANDBOX/.claude/.credentials.json`. RDR-219 rule
1 forbids a harness from using the operator's interactive login at all —
the harness must read its own automation identity
(`nexus-automation-oauth-token`) instead, and never write a credential
file to disk.

THE FIX. The private tmux SERVER that hosts the Claude Code pane is
started under `claude_credentials.py run --`, which execs it with
`CLAUDE_CODE_OAUTH_TOKEN` in its own environment. A tmux session inherits
its environment from the SERVER that owns it, not from the command that
later asks for the session (the RDR-219 tmux transport rule), so starting
the server this way is what gets the token to the `claude` process a
later `send-keys "claude"` launches inside it. The server also moves to a
private socket (`tmux -L`) — the same private-server discipline
`tests/cc-validation/runner.sh` already uses via `tests/e2e/lib.sh`'s
`_tmux` wrapper — so this harness never joins (or is silently rejected
from) whatever default tmux server the user's own login shell may
already be running.

Structural, grep-based checks over the tracked script text — same
lint-shape convention as `test_release_sandbox_live_tail_traps.py` in
this directory. A real proof (an actual tmux server launched under `run
--` and a live Claude Code session inside it) is out of scope for an
automated test — see the bead's PROOF instruction for the manual run.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "release-sandbox.sh"
DOC = REPO_ROOT / "tests" / "e2e" / "release-sandbox.md"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text()


def _tmux_block(text: str) -> str:
    start = text.index("\n    tmux)")
    end = text.index("\nesac", start)
    return text[start:end]


def test_no_credentials_json_write_or_read(script_text: str) -> None:
    """Neither `.credentials.json` nor the `.claude-auth` snapshot
    directory is referenced anywhere in the script -- the write, the
    read-back check, and the usage-text mention are all gone."""
    assert ".credentials.json" not in script_text
    assert ".claude-auth" not in script_text


def test_tmux_mode_wraps_the_private_server_start_with_cred_tool_run(
    script_text: str,
) -> None:
    block = _tmux_block(script_text)
    # The one tmux invocation that CREATES the private server (`new-session`)
    # must be wrapped in `_cred_tool run --` (or the bare `claude_credentials.py
    # run --` it wraps) -- this is what puts CLAUDE_CODE_OAUTH_TOKEN into the
    # server's own environment (the tmux transport rule).
    new_session_lines = [
        ln for ln in block.splitlines() if "new-session" in ln and "has-session" not in ln
    ]
    assert new_session_lines, "no `new-session` line found in the tmux) block"
    assert any(
        re.search(r"\b(_cred_tool|claude_credentials\.py)\s+run\b.*--.*\btmux\b", ln)
        or re.search(r"\b(_cred_tool|claude_credentials\.py)\s+run\b", ln)
        for ln in new_session_lines
    ), f"new-session line(s) not wrapped in `run --`: {new_session_lines}"


def test_tmux_mode_uses_a_private_socket_not_the_default_server(script_text: str) -> None:
    block = _tmux_block(script_text)
    assert "NX_TMUX_SOCKET" in block, "tmux) block never sets a private NX_TMUX_SOCKET"
    # The server-creating new-session call must name the socket explicitly
    # (`-L "$NX_TMUX_SOCKET"`) since it is invoked as a bare argv under
    # `_cred_tool run --`, which cannot see the `_tmux` shell-function
    # wrapper.
    assert re.search(r"tmux\s+-L\s+[\"']?\$NX_TMUX_SOCKET", block), (
        "no `tmux -L \"$NX_TMUX_SOCKET\" new-session ...` in the tmux) block"
    )


def test_cred_tool_wrapper_is_defined(script_text: str) -> None:
    assert re.search(r"_cred_tool\(\)\s*\{", script_text), (
        "no `_cred_tool()` wrapper defined -- expected a thin wrapper around "
        "tests/e2e/lib/claude_credentials.py, as tests/cc-validation/runner.sh has"
    )
    assert "tests/e2e/lib/claude_credentials.py" in script_text


def test_usage_help_no_longer_names_auth_login_prerequisite(script_text: str) -> None:
    # The usage/help text (formerly line 589) described a
    # `.credentials.json` / `auth-login.sh` prerequisite for `tmux` mode --
    # that prerequisite is gone.
    help_start = script_text.index('_print_help()')
    help_end = script_text.index("\n}\n", help_start)
    help_text = script_text[help_start:help_end]
    assert "auth-login.sh" not in help_text
    assert ".credentials.json" not in help_text


def test_release_sandbox_md_no_longer_names_the_credentials_json_prerequisite(
    doc_text: str,
) -> None:
    assert ".claude-auth" not in doc_text
    assert ".credentials.json" not in doc_text


def test_release_sandbox_md_documents_the_manual_run_launch(doc_text: str) -> None:
    """The bead's LAUNCH SHAPE instruction: a human using the persistent
    sandbox afterwards launches claude with `claude_credentials.py run --
    claude`; the doc must say so."""
    assert "claude_credentials.py run" in doc_text


def test_shellcheck_finds_no_new_findings_in_the_tmux_block(script_text: str) -> None:
    """Scoped, not whole-file: release-sandbox.sh already carries pre-
    existing SC2015/SC2001 findings elsewhere (unrelated to this bead) that
    a whole-file `shellcheck -x ... == 0` assertion would wrongly attribute
    to this change. Run shellcheck against the tmux) block alone, wrapped
    in a standalone case statement so it parses."""
    block = _tmux_block(script_text)
    probe_src = "#!/usr/bin/env bash\nset -euo pipefail\ncase \"$MODE\" in\n" + block + "\nesac\n"
    proc = subprocess.run(
        ["shellcheck", "-x", "-s", "bash", "-"],
        input=probe_src,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
