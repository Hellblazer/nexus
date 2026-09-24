# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wiring for the RDR-208 local-mode MVV (tests/e2e/rdr208-mvv; beads
nexus-galkv.19, nexus-kdxyv).

The journey itself runs in a container against real Claude Code sessions
and is not part of the suite; these checks keep it runnable: every script
parses, the image copies only what run.sh stages and installs what the
journey drives (claude, tmux), run.sh takes the credential from the shared
picker, the sessions are launched with the development-channel flag and the
staged plugin, every tmux call goes through the private-socket wrapper, and
the symbol run.sh derives step 6's expectation from is the live mechanism
(`fork` in the plugin's SessionStart matcher, paired with the wheel's
handoff sources), so a rename cannot silently flip the expectation to
"pre-fix behaviour" and let the MVV pass on a regression.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_ROOT = Path(__file__).resolve().parents[1]
_DIR = _ROOT / "tests" / "e2e" / "rdr208-mvv"
_HOOKS_JSON = _ROOT / "conexus" / "hooks" / "hooks.json"

#: The session-start verb, HYPHENATED. Locating the block by this token
#: rather than by a whole command string survives both spellings the hook
#: can carry -- the shell form (`"command": "nx hook session-start"`) and
#: the exec form RDR-215 moves to (`"command": "nx-hook", "args":
#: ["session-start"]`) -- and does not collide with the neighbouring
#: `session-context` verb (formerly `session_start_hook.py`, spelled with
#: an UNDERSCORE, deleted at nexus-z9cz2).
_SESSION_START_VERB = "session-start"


def _session_start_matcher(hooks_json: dict) -> str:
    """The matcher of the SessionStart block that runs the session-start
    verb. Raises rather than returning a default: a rewrite that leaves no
    such block must fail this pin loudly, not silently pass a matcher that
    gates nothing."""
    blocks = [
        b for b in hooks_json["hooks"]["SessionStart"]
        if _SESSION_START_VERB in json.dumps(b.get("hooks", []))
    ]
    assert len(blocks) == 1, (
        f"expected exactly one SessionStart block running {_SESSION_START_VERB!r}, "
        f"found {len(blocks)}"
    )
    return blocks[0]["matcher"]


@pytest.mark.parametrize("name", ["mvv_in_container.sh", "run.sh"])
def test_the_script_parses(name: str) -> None:
    proc = subprocess.run(["bash", "-n", str(_DIR / name)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_the_stand_in_session_server_is_gone() -> None:
    assert not (_DIR / "session_server.sh").exists(), "the bash-as-claude stand-in was retired with nexus-kdxyv"


def test_the_image_copies_only_what_run_sh_stages() -> None:
    run_sh = (_DIR / "run.sh").read_text(encoding="utf-8")
    staged = {"wheel/", "plugin/", "settings.json"} | set(re.findall(r'"\$HERE/([\w.]+)"', run_sh))
    copied: set[str] = set()
    for line in (_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            parts = [p for p in line.split()[1:] if not p.startswith("--")]
            copied.update(parts[:-1])
    assert copied, "no COPY lines found"
    assert copied <= staged, sorted(copied - staged)


def test_the_image_installs_claude_and_tmux() -> None:
    text = (_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "claude.ai/install.sh" in text
    assert re.search(r"apt-get install[^\n\\]*(\\\n[^\n\\]*)*\btmux\b", text), "tmux is not installed"
    assert "cp /bin/bash /home/nexus/bin/claude" not in text, "the bash-as-claude stand-in is back"


def test_run_sh_takes_the_credential_from_the_shared_picker() -> None:
    run_sh = (_DIR / "run.sh").read_text(encoding="utf-8")
    assert "tests/e2e/lib/claude_credentials.py" in run_sh
    assert '"$CRED_TOOL" pick' in run_sh
    assert "find-generic-password" not in run_sh
    assert "UNVERIFIED" in run_sh and "exit 2" in run_sh


def test_the_sessions_are_launched_with_the_channel_and_the_staged_plugin() -> None:
    mvv = (_DIR / "mvv_in_container.sh").read_text(encoding="utf-8")
    assert "--dangerously-load-development-channels server:nexus" in mvv
    assert "--plugin-dir $PLUGIN" in mvv
    assert "--strict-mcp-config" in mvv
    assert "exec claude " in mvv, "claude must be exec'd so the pane pid is the claude pid"


def test_every_tmux_call_goes_through_the_private_socket_wrapper() -> None:
    mvv = (_DIR / "mvv_in_container.sh").read_text(encoding="utf-8")
    bare = [
        line for line in mvv.splitlines()
        if re.search(r"(^|[^_A-Za-z])tmux ", line)
        and not line.lstrip().startswith("#")
        and "command tmux -L" not in line
    ]
    assert not bare, bare
    assert mvv.count("command tmux -L") == 1


def test_run_sh_stages_the_drain_hooks_sibling_imports() -> None:
    """mailbox_drain.py imports sibling modules at import time, before its own
    never-raise contract can apply, so staging only the hook file itself makes
    the container's UserPromptSubmit hook exit 1 on every prompt with a
    ModuleNotFoundError. Measured 2026-09-18 by running the hook inside the
    built image. run.sh must stage the whole scripts directory.

    7.58.0 wires the plugin script again (plugin-ahead skew, nexus-t9klx), so
    this is the v7.57.0 check restored with it."""
    run_sh = (_DIR / "run.sh").read_text(encoding="utf-8")
    assert 'cp -R "$SRC/hooks/scripts/." "$STAGE/plugin/hooks/scripts/"' in run_sh, (
        "run.sh must stage the whole hooks/scripts directory, not named files"
    )
    drain = (_ROOT / "conexus" / "hooks" / "scripts" / "mailbox_drain.py").read_text(encoding="utf-8")
    siblings = set(re.findall(r"^import (_\w+)", drain, re.M))
    assert siblings, "expected mailbox_drain.py to import sibling modules"
    for name in siblings:
        assert (_ROOT / "conexus" / "hooks" / "scripts" / f"{name}.py").is_file(), name


def test_the_generated_hooks_json_names_a_verb_the_cli_still_has() -> None:
    """run.sh generates the container's trimmed hooks.json with the SHELL
    spelling of the session-start verb, to drive the wheel under test. That
    is this harness's only dependency on the `nx hook` Click surface, and
    this harness is the only consumer of it that nothing else names, so a
    bead retiring the verb would otherwise leave a container whose
    SessionStart hook never runs and a journey that fails far from its
    cause."""
    run_sh = (_DIR / "run.sh").read_text(encoding="utf-8")
    assert '"/home/nexus/nxenv/bin/nx hook session-start"' in run_sh, (
        "the generated hook must name nx by ABSOLUTE path: a hook runs under /bin/sh "
        "with a PATH that does not carry the venv, and a bare `nx` is 'not found' on "
        "every SessionStart (measured 2026-09-18)"
    )
    spelling = re.search(r'"command": "(?:[\w/.-]*/)?(nx hook session-start)"', run_sh)
    assert spelling, "run.sh no longer generates a session-start hook declaration"
    hook_cmd = (_ROOT / "src" / "nexus" / "commands" / "hook.py").read_text(encoding="utf-8")
    assert '@hook_group.command("session-start")' in hook_cmd, (
        f"run.sh generates {spelling.group(1)!r} for the container's hooks.json, but the "
        "nx CLI no longer declares that Click verb; update the generator in the same diff"
    )
    cli = (_ROOT / "src" / "nexus" / "cli.py").read_text(encoding="utf-8")
    assert 'add_command(hook_group, name="hook")' in cli


def test_every_journey_helper_it_calls_is_defined() -> None:
    """`bash -n` parses a script whose helpers do not exist: an undefined
    function is a RUNTIME "command not found", one step at a time, deep into
    a billed run. That happened: a block replacement that rewrote `arm()`
    swallowed `model_send()` with it, the syntax check stayed green, the
    change shipped to origin, and the next run died at step 1 with
    `model_send: command not found` after launching two real sessions.

    So the journey's own vocabulary is pinned here: every name below must be
    DEFINED in the script, and any of them that the script CALLS must resolve.
    A helper that is genuinely retired is removed from this list in the same
    diff that removes it, which is the point at which someone thinks about it.
    """
    text = (_DIR / "mvv_in_container.sh").read_text(encoding="utf-8")
    defined = set(re.findall(r"^(\w+)\(\) \{", text, re.M))
    required = {
        "launch", "stop", "arm", "prompt", "send", "model_send",
        "delivered", "delivered_by_floor", "not_delivered", "discover_name",
        "rendered_count", "rendered_from", "reply_has", "wake_count",
        "transcript_of", "turn_stamp", "wait_for", "check", "snap",
    }
    assert required <= defined, sorted(required - defined)

    # And nothing calls a helper-shaped name this file does not define: scan
    # command positions for the journey's own vocabulary only, so a shell
    # builtin or a real binary is never mistaken for a missing helper.
    called = set(re.findall(r"^\s*(?:\w+=\S+\s+)*(\w+)\s", text, re.M))
    journey_calls = {c for c in called if c in required or c.endswith("_of") or c.startswith("delivered")}
    assert journey_calls <= defined, sorted(journey_calls - defined)


def test_the_step6_expectation_names_the_live_mechanism() -> None:
    """run.sh derives EXPECT_BRANCH_FIX from `fork` in the plugin's
    SessionStart matcher; the tree's plugin carries it, and the wheel's
    handoff sources carry `fork` too (the two halves of the fix)."""
    run_sh = (_DIR / "run.sh").read_text(encoding="utf-8")
    assert re.search(r"grep -qE '\\bfork\\b' <<<\"\$MATCHER\"", run_sh), "run.sh no longer derives step 6's expectation from the matcher"
    matcher = _session_start_matcher(json.loads(_HOOKS_JSON.read_text(encoding="utf-8")))
    assert "fork" in matcher.split("|"), matcher
    from nexus.hooks import _T1_HANDOFF_SOURCES

    assert "fork" in _T1_HANDOFF_SOURCES
