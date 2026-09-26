# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 Phase 2 Step 1 (bead nexus-wauo1.13): tests/e2e/rdr208-mvv migrated
to `tests/e2e/lib/claude_credentials.py run --` instead of picking a fresh
credential and mounting it into the container.

BEFORE this bead: `run.sh` called `"$CRED_TOOL" pick` (falling back to
`~/.claude/.credentials.json` via `"$CRED_TOOL" check`), wrote the result to
a staged `.claude-credentials.json`, and mounted that file read-only at
`/home/nexus/.claude/.credentials.json` -- the operator's own interactive
login, which RDR-219 rule 1 forbids a harness to use. It also seeded the
container's `~/.claude.json` with the `oauthAccount` block copied from
`tests/e2e/.claude-auth/claude.json`.

AFTER: the harness's own automation identity (keychain item
`nexus-automation-oauth-token`) is passed as an environment variable --
`python3 "$CRED_TOOL" run -- docker run -e CLAUDE_CODE_OAUTH_TOKEN ...` with
no `=value`, so docker copies the value from ITS OWN environment (which
`run` already set) into the container's. No credential file exists at any
point (T2 nexus_rdr/219-research-10, launch shape A2). The `oauthAccount`
seed is dropped too (T2 nexus_rdr/219-research-14: a bare
`hasCompletedOnboarding` authenticates from the token alone in this launch
shape).

This is a structural, grep-based test over the tracked shell text -- same
technique as `tests/test_cc_validation_runner_automation_token.py`
(nexus-wauo1.10, the first Phase 2 harness migration). It is RED against the
pre-migration script (5df9837e1) and GREEN after.
"""
from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_RUN_SH = _ROOT / "tests" / "e2e" / "rdr208-mvv" / "run.sh"
_MVV_SH = _ROOT / "tests" / "e2e" / "rdr208-mvv" / "mvv_in_container.sh"


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


# ─────────────────────────── run.sh: the launch itself ───────────────────────


def test_run_sh_launches_docker_through_the_shared_run_wrapper() -> None:
    run_sh = _text(_RUN_SH)
    assert re.search(r'"\$CRED_TOOL"\s+run\s+--\s+docker run\b', run_sh), (
        "expected the container launch to go through "
        "`python3 \"$CRED_TOOL\" run -- docker run ...` (RDR-219 launch shape A2)"
    )


def test_run_sh_names_the_env_flag_with_no_literal_value() -> None:
    """`-e CLAUDE_CODE_OAUTH_TOKEN` (docker copies the value from its own
    process environment, which `run` already set) -- never `=<value>`,
    which would put the token on the docker client's own argv."""
    run_sh = _text(_RUN_SH)
    assert "-e CLAUDE_CODE_OAUTH_TOKEN" in run_sh
    assert "CLAUDE_CODE_OAUTH_TOKEN=" not in run_sh


def test_run_sh_no_longer_picks_or_checks_a_credential() -> None:
    run_sh = _text(_RUN_SH)
    assert '"$CRED_TOOL" pick' not in run_sh
    assert '"$CRED_TOOL" check' not in run_sh
    assert "FRESHCREDS" not in run_sh


def test_run_sh_never_reads_the_operators_interactive_login() -> None:
    """RDR-219 rule 1: a harness never falls back to the operator's own
    `~/.claude/.credentials.json` -- that file is the interactive login,
    not the harness's automation identity."""
    run_sh = _text(_RUN_SH)
    assert "find-generic-password" not in run_sh
    assert ".claude/.credentials.json" not in run_sh


def test_run_sh_writes_and_mounts_no_credential_file() -> None:
    run_sh = _text(_RUN_SH)
    assert ".claude-credentials.json" not in run_sh


def test_run_sh_drops_the_oauthaccount_seed() -> None:
    """T2 nexus_rdr/219-research-14: not needed in this launch shape. A
    comment explaining the removal (which names the word) is fine; only a
    live, non-comment reference to the old snapshot file or an
    `oauthAccount` code shape is flagged."""
    run_sh = _text(_RUN_SH)
    assert ".claude-auth" not in run_sh
    code_lines = [
        line for line in run_sh.splitlines() if not line.lstrip().startswith("#")
    ]
    assert not any("oauthAccount" in line for line in code_lines), [
        line for line in code_lines if "oauthAccount" in line
    ]


def test_run_sh_still_names_the_shared_credential_tool() -> None:
    run_sh = _text(_RUN_SH)
    assert "tests/e2e/lib/claude_credentials.py" in run_sh


# ─────────────────────── mvv_in_container.sh: the tmux trap ──────────────────


def test_mvv_in_container_never_unsets_the_automation_token() -> None:
    """RDR-219 tmux trap: a tmux session takes its environment from the
    private server, not from the command asking for the session. The
    server is started as a direct child of THIS script's own process (the
    first `T new-session` call, inside `launch()`), so as long as this
    script's own environment -- which `docker run -e
    CLAUDE_CODE_OAUTH_TOKEN` set at container boot -- is never stripped of
    the token before that point, the server (and every session it hosts)
    inherits it automatically. This guards the one way that could
    silently break: adding CLAUDE_CODE_OAUTH_TOKEN to the `unset` line
    that already clears ANTHROPIC_API_KEY and the session-identity
    variables."""
    mvv = _text(_MVV_SH)
    unset_lines = [line for line in mvv.splitlines() if line.strip().startswith("unset ")]
    assert unset_lines, "expected an `unset` line clearing inherited session/API-key variables"
    assert not any("CLAUDE_CODE_OAUTH_TOKEN" in line for line in unset_lines), unset_lines
    assert any("ANTHROPIC_API_KEY" in line for line in unset_lines), (
        "the ANTHROPIC_API_KEY unset must stay -- RDR-219's own precedence "
        "note (an API key outranks the automation token)"
    )


def _function_body(text: str, name: str) -> str:
    """Extract one shell function's body text, from its `name() {` opener to
    the closing `}` at column 0 -- good enough for this file's own
    consistent 4-space-indented style; not a general shell parser."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{.*?\n(.*?)^\}}\n", text, re.M | re.S)
    assert m, f"could not find a `{name}() {{ ... }}` function body"
    return m.group(1)


def test_launch_starts_the_private_tmux_server_before_anything_else_touches_it() -> None:
    """RDR-219 tmux trap: a session takes its environment from the private
    SERVER, not from whatever asked for the session. `launch()`'s own first
    two tmux calls must be `kill-session` (a no-op the first time -- no
    server yet) then `new-session`, because `new-session` is what actually
    spawns the server, as a direct child of this SCRIPT's own process --
    which carries CLAUDE_CODE_OAUTH_TOKEN from `docker run -e
    CLAUDE_CODE_OAUTH_TOKEN` at container boot. If some other tmux
    subcommand ran first inside `launch()`, or ran anywhere before
    `launch()` at the top level, that inheritance chain would not hold."""
    mvv = _text(_MVV_SH)
    body = _function_body(mvv, "launch")
    calls = re.findall(r"\bT ([\w-]+)", body)
    assert calls[:2] == ["kill-session", "new-session"], calls[:2]

    # And nothing at the TOP LEVEL (outside any function body -- this
    # script's own functions are all indented, so a column-0 line is
    # top-level) calls a tmux-touching helper before `launch` does.
    tmux_touching = {
        "launch", "stop", "arm", "prompt", "model_send", "send",
        "delivered", "delivered_by_floor", "pane",
    }
    first_top_level_call = None
    for line in mvv.splitlines():
        if not line or line[0] in " \t#":
            continue
        word = line.split(None, 1)[0] if line.split() else ""
        if word in tmux_touching:
            first_top_level_call = word
            break
    assert first_top_level_call == "launch", (
        f"expected `launch` to be the first top-level call to touch tmux, "
        f"got {first_top_level_call!r}"
    )
