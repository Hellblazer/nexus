# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-219 Phase 2 Step 1, P2.1g (nexus-wauo1.16): run_ladder.py's credential
path moves from `--cred-cmd`/`--cred-file` (which wrote
`home/.claude/.credentials.json` per run) to the environment pass-through:
the caller launches the ladder under `claude_credentials.py run [--remote
HOST] --`, which sets `CLAUDE_CODE_OAUTH_TOKEN` in the ladder's own process
environment; the ladder reads it from there and forwards it into every
tmux-launched `claude` process's scrubbed (`env -i`) environment, refusing
to start at all when the variable is absent.

Two kinds of check, matching the sibling P2.1a/P2.1b migrations:

1. A real subprocess-level positive control (`test_main_refuses_...`):
   invokes the actual script with `CLAUDE_CODE_OAUTH_TOKEN` unset and no
   credential flags, and asserts the refusal names the token, not
   `--cred-cmd`/`--cred-file`. This differentiates old behaviour (refuses
   citing the deleted flags) from new (refuses citing the env var) without
   ever launching tmux or `claude` -- the argparse-time refusal fires
   before any run starts, so this is fast, free and deterministic.
2. Structural (source-text) checks for the harder-to-exercise pieces: the
   deleted flags and `.credentials.json` write are gone, the scrubbed
   `env -i` environment built for each tmux-launched `claude` explicitly
   carries `CLAUDE_CODE_OAUTH_TOKEN`, and a pre-existing tmux server on the
   ladder's own socket is torn down before the run loop starts (plan-audit
   residual: a server left over from an earlier invocation keeps its own
   stale environment).
"""
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys
import types

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = (
    REPO_ROOT / "tests" / "cc-validation" / "connection-race-ladder" / "run_ladder.py"
)


def _source() -> str:
    return SCRIPT.read_text()


def _load_run_ladder() -> types.ModuleType:
    """Import run_ladder.py by path -- it is a standalone script under
    tests/cc-validation/, not part of the nexus package, so it needs
    importlib rather than a plain `import`."""
    spec = importlib.util.spec_from_file_location("run_ladder_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_TOKEN = "sk-ant-oatFAKE00000000000000000000000000000000TESTONLY"  # nosec: not a real credential


def test_script_exists() -> None:
    assert SCRIPT.is_file(), f"expected {SCRIPT} -- did it move?"


def test_main_refuses_without_token_and_names_it(tmp_path: pathlib.Path) -> None:
    """Positive control, real subprocess. `CLAUDE_CODE_OAUTH_TOKEN` unset,
    no `--cred-*` flags on argv (they no longer exist to pass). Must exit
    non-zero before touching tmux or claude, and the stderr must name
    CLAUDE_CODE_OAUTH_TOKEN -- not --cred-cmd/--cred-file, which is what
    the pre-migration script says here instead."""
    env = {k: v for k, v in __import__("os").environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--out", str(tmp_path / "out"), "--python", sys.executable],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode != 0, (
        f"expected a refusal with no CLAUDE_CODE_OAUTH_TOKEN set; got rc=0, "
        f"stdout={proc.stdout!r}"
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" in proc.stderr, (
        f"refusal must name the missing environment variable; stderr={proc.stderr!r}"
    )
    assert "--cred-cmd" not in proc.stderr and "--cred-file" not in proc.stderr, (
        f"refusal still cites the deleted credential flags; stderr={proc.stderr!r}"
    )


def test_no_cred_cmd_or_cred_file_flags() -> None:
    text = _source()
    assert "--cred-cmd" not in text, "the ladder must no longer accept --cred-cmd"
    assert "--cred-file" not in text, "the ladder must no longer accept --cred-file"
    assert "cred_cmd" not in text, "no leftover cred_cmd attribute reference"
    assert "cred_file" not in text, "no leftover cred_file attribute reference"


def test_no_oauth_seed_flag() -> None:
    """T2 nexus_rdr/219-research-14: the oauthAccount seed is not needed --
    a bare hasCompletedOnboarding stub authenticates from the token alone
    on every launch shape tested (A1/A2/A3/A4). Both sibling P2.1
    migrations (runner.sh, e2e/run.sh) dropped it outright rather than
    keep an inert flag; the ladder follows the same record."""
    text = _source()
    assert "--oauth-seed" not in text, "the ladder must no longer accept --oauth-seed"
    assert "oauth_seed" not in text, "no leftover oauth_seed attribute reference"
    assert "oauthAccount" not in text, "no leftover oauthAccount seed-merging logic"


def test_no_credentials_json_write() -> None:
    text = _source()
    assert ".credentials.json" not in text, (
        "the ladder must never write a home/.claude/.credentials.json file -- "
        "RDR-219 rule: the token lives only in the process environment"
    )


def _run_one_with_fake_subprocess(monkeypatch, tmp_path: pathlib.Path):
    """Runs run_one() for real, but with every subprocess.run() call
    intercepted: nothing actually launches tmux or claude. Returns
    (module, list-of-recorded-calls). Each recorded call is
    {"cmd": [...], "kwargs": {...}} exactly as run_ladder.py invoked
    subprocess.run, so a test can inspect every argv AND every env= it
    ever builds -- this is the only way to prove an absence (the token
    is nowhere) rather than merely a presence (a keyword appears in
    source text), which is what the retired
    test_env_i_scrub_forwards_the_token could only ever pin."""
    mod = _load_run_ladder()
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", FAKE_TOKEN)
    # No real wall-clock cost: the readiness loop's capture-pane fake
    # answers "ready" on its first call, and turn_timeout=0 means the
    # Stop-wait loop body never runs; time.sleep is stubbed out entirely
    # so the hardcoded end-of-run sleeps (0.3s/1s/3s) cost nothing either.
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    calls: list[dict] = []

    def fake_run(cmd, *a, **kw):
        calls.append({"cmd": list(cmd), "kwargs": dict(kw)})
        stdout = "Bypass permissions on\n" if isinstance(cmd, list) and "capture-pane" in cmd else ""
        return types.SimpleNamespace(stdout=stdout, returncode=0, args=cmd)

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    args = types.SimpleNamespace(
        out=str(tmp_path / "out"), python=sys.executable, hook_python="python3",
        claude="fake-claude-binary-not-executed", sock="test-veh77-sock",
        ready_regex=r"[Bb]ypass permissions on", turn_timeout=0.0, barrier=False,
    )
    mod.run_one(args, "t", 0.0, "0", "bash", 0)
    return mod, calls


def test_token_never_appears_in_any_process_argv(monkeypatch, tmp_path: pathlib.Path) -> None:
    """The token must never be an element of any argv run_ladder.py hands
    to subprocess.run -- not a literal `env -i CLAUDE_CODE_OAUTH_TOKEN=...`
    argument to a transient `env` process, and not a tmux client argv like
    `-e CLAUDE_CODE_OAUTH_TOKEN=...`. This fails against the pre-fix
    shape, which built `env -i {envs} {claude} ...` as ONE shell string
    (with the token's literal value inside `envs`) and handed that whole
    string to `tmux new-session` as an argv element -- ps-visible for the
    life of the pane's `$SHELL -c` process."""
    _mod, calls = _run_one_with_fake_subprocess(monkeypatch, tmp_path)
    assert calls, "run_one() made no subprocess.run calls at all -- fixture is broken"
    for call in calls:
        for arg in call["cmd"]:
            assert FAKE_TOKEN not in str(arg), (
                f"the fake token leaked into an argv element: {arg!r} "
                f"(full call: {call['cmd']!r})"
            )


def test_claude_still_receives_the_token_via_the_server_env(
    monkeypatch, tmp_path: pathlib.Path
) -> None:
    """The tmux `new-session` call that actually spawns the private-socket
    server must carry the token through subprocess's `env=` (execve, not
    argv) -- that is the one channel that reaches the tmux SERVER's own
    environment, which every pane (and so `claude`) inherits. Fails
    against the pre-fix shape, which never passed `env=` to that call at
    all (the token only ever reached `claude` by being embedded, as text,
    in the pane's shell command)."""
    _mod, calls = _run_one_with_fake_subprocess(monkeypatch, tmp_path)
    new_session_calls = [c for c in calls if "new-session" in c["cmd"]]
    assert len(new_session_calls) == 1, (
        f"expected exactly one tmux new-session call, got {len(new_session_calls)}: "
        f"{new_session_calls!r}"
    )
    env = new_session_calls[0]["kwargs"].get("env")
    assert env is not None, (
        "the new-session call was given no env= at all -- the server would "
        "inherit whatever this Python process's own environment happens to "
        "be, not the deliberately built allowlist"
    )
    assert env.get("CLAUDE_CODE_OAUTH_TOKEN") == FAKE_TOKEN, (
        f"the new-session call's env= must carry CLAUDE_CODE_OAUTH_TOKEN "
        f"(that's how claude, launched inside the resulting server's panes, "
        f"gets the credential now that env -i is gone); env keys were "
        f"{sorted(env.keys())!r}"
    )
    # And the pane command string itself (also an argv element of this
    # same call) must NOT be how the token travels -- confirms (a) and (b)
    # are the same mechanism switch, not two unrelated fixes.
    pane_cmd = new_session_calls[0]["cmd"][-1]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in pane_cmd, (
        f"the pane command string still names CLAUDE_CODE_OAUTH_TOKEN "
        f"(expected the token to travel only via env=, never via the "
        f"shell command text); pane_cmd={pane_cmd!r}"
    )


def test_stale_tmux_server_is_reset_before_the_run_loop() -> None:
    """Plan-audit round-1 residual (bead notes): run_ladder.py's own tmux
    socket (--sock veh77-ladder) can carry a server left running from an
    earlier invocation, with that earlier invocation's own environment.
    Kill (or refuse) it before the first run of THIS invocation."""
    text = _source()
    assert "kill-server" in text, (
        "expected a tmux kill-server call resetting any pre-existing server "
        "on args.sock before the run loop starts"
    )


def test_docstring_no_longer_describes_cred_cmd_cred_file() -> None:
    text = _source()
    doc_end = text.index('"""', text.index('"""') + 3) + 3
    docstring = text[:doc_end]
    assert "--cred-cmd" not in docstring
    assert "--cred-file" not in docstring
    assert "--oauth-seed" not in docstring
