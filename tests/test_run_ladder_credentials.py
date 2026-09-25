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

import pathlib
import subprocess
import sys

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = (
    REPO_ROOT / "tests" / "cc-validation" / "connection-race-ladder" / "run_ladder.py"
)


def _source() -> str:
    return SCRIPT.read_text()


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


def test_env_i_scrub_forwards_the_token() -> None:
    """run_one()'s per-run `env -i` scrub (which strips everything except a
    short allowlist before launching `claude` in tmux) must explicitly
    carry CLAUDE_CODE_OAUTH_TOKEN through, or the scrub itself would strip
    the very credential the harness now depends on."""
    text = _source()
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text, (
        "expected the ladder to reference CLAUDE_CODE_OAUTH_TOKEN somewhere "
        "(reading it from os.environ and forwarding it through env -i)"
    )
    # The forwarding must land in the same env -i allowlist ("keep") that
    # already carries PATH/HOME/etc for the tmux-launched claude process,
    # not merely be read and discarded.
    keep_idx = text.index("keep = {")
    keep_block_end = text.index("\n\n", keep_idx)
    keep_block = text[keep_idx:keep_block_end]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in keep_block, (
        f"CLAUDE_CODE_OAUTH_TOKEN must be added to run_one()'s `keep` env-i "
        f"allowlist, not just read elsewhere; keep block was:\n{keep_block}"
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
