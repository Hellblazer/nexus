# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-219 Phase 2 Step 1, P2.1e (nexus-wauo1.14): `tests/e2e/hook-surface-
shakeout` migrates from the shared `pick`/`check` credential picker (which
copies the OPERATOR's own interactive login into a staged file and mounts
it read-only into the container) to `claude_credentials.py run --`, the
harness's own automation token (RDR-219), passed as a `docker run -e`
environment variable with no `=value` so the value never lands on argv or
in a file the container mounts.

THE TWO SITES (bead SITES line):
  - `run.sh`: the `pick`/fallback/staged-file block (former lines 57-68 and
    81-83) and the `-v ...:/creds/.credentials.json:ro` mount (former line
    296) on the `docker run` invocation.
  - `shakeout_in_container.sh`: the `cp /creds/.credentials.json ...` copy
    into the container's own `.claude` directory (former line 47), and the
    `claude` launch (former line 114), which must inherit
    `CLAUDE_CODE_OAUTH_TOKEN` through the tmux private server (RDR-219: a
    tmux session takes its environment from the tmux SERVER, not from the
    command that asks for the session -- the private server here is
    created fresh by this script's own first `tmux -L` call, so it
    captures whatever environment the container's entrypoint process
    carries at that moment).

Each assertion here is a MINIMAL structural fact about the two shell
scripts, checked by grep-shaped regex against the tracked source (not by
running a real Claude Code session -- that is the bead's separate,
billed PROOF run). A test counts only if it fails against the
pre-migration file; every assertion below was run against the checked-in
file BEFORE the migration edit and failed there (see the bead's git
history / T2 nexus_rdr/219-continuation-p2-1e for the red/green record).
"""
from __future__ import annotations

import pathlib
import re
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
HOOK_SHAKEOUT = REPO_ROOT / "tests" / "e2e" / "hook-surface-shakeout"
RUN_SH = HOOK_SHAKEOUT / "run.sh"
IN_CONTAINER_SH = HOOK_SHAKEOUT / "shakeout_in_container.sh"


def _text(path: pathlib.Path) -> str:
    return path.read_text()


# --- run.sh: the pick/check/staged-file credential block is gone ----------


def test_run_sh_no_longer_calls_pick() -> None:
    text = _text(RUN_SH)
    assert not re.search(r'"\$CRED_TOOL"\s+pick\b', text), (
        "run.sh must not call the shared picker's `pick` mode -- RDR-219 "
        "replaces it with the harness's own automation token via `run --`"
    )


def test_run_sh_no_longer_calls_check() -> None:
    text = _text(RUN_SH)
    assert not re.search(r'"\$CRED_TOOL"\s+check\b', text), (
        "run.sh must not call the shared picker's `check` mode -- the "
        "~/.claude/.credentials.json fallback (the operator's own "
        "interactive login) is forbidden to a harness under RDR-219 rule 1"
    )


def test_run_sh_no_longer_reads_operator_credentials_file() -> None:
    text = _text(RUN_SH)
    assert "$HOME/.claude/.credentials.json" not in text, (
        "run.sh must not read the operator's own interactive-login "
        "credential file (RDR-219 rule 1)"
    )


def test_run_sh_no_longer_stages_a_credentials_file() -> None:
    text = _text(RUN_SH)
    assert "STAGE/.claude-credentials.json" not in text, (
        "run.sh must not write a staged .claude-credentials.json copy -- "
        "RDR-219 removes the whole class of copied-credential files"
    )


def test_run_sh_no_longer_mounts_a_credentials_file_into_the_container() -> None:
    text = _text(RUN_SH)
    assert "/creds/.credentials.json" not in text, (
        "run.sh must not bind-mount a credential file into the container; "
        "the automation token now travels as a docker run -e environment "
        "variable, never a file"
    )


def test_run_sh_wraps_the_docker_run_with_claude_credentials_run() -> None:
    text = _text(RUN_SH)
    # The exact A2 launch shape (T2 nexus_rdr/219-research-10): the docker
    # invocation's own argv[0] must be `docker` so
    # `_ensure_docker_flags` in claude_credentials.py recognizes it and
    # forces in `-e CLAUDE_CODE_OAUTH_TOKEN` (no value) and `--rm`.
    assert re.search(r'"\$CRED_TOOL"\s+run\s+--\s+docker run\b', text), (
        "run.sh's docker invocation must be wrapped as "
        '`python3 "$CRED_TOOL" run -- docker run ...` so the automation '
        "token reaches the container as an environment variable"
    )


def test_run_sh_gates_on_automation_token_status_not_a_credential_file() -> None:
    text = _text(RUN_SH)
    assert re.search(r'"\$CRED_TOOL"\s+status\b', text), (
        "run.sh must gate on the harness's own automation token status "
        "before staging/building, exactly as the other migrated sites do "
        "(T2 219-continuation-p2-1b)"
    )


def test_run_sh_bash_syntax_is_valid() -> None:
    proc = subprocess.run(["bash", "-n", str(RUN_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_run_sh_help_text_line_range_matches_the_header_it_slices() -> None:
    """`-h|--help` prints the header comment via a hardcoded `sed -n
    'START,ENDp'` line range. Growing the header comment (as this
    migration's AUTH block did, +2 lines) without moving END silently
    truncates the printed help text mid-sentence -- found by hand while
    migrating this file (RDR-219 P2.1e): the range originally ended
    EXACTLY on the `set -euo pipefail` line (its own convention, kept
    on principle rather than fixed here), so that is the invariant
    pinned: whatever ENDs the range, it must land on that same line, not
    short of it."""
    text = _text(RUN_SH)
    match = re.search(r"sed -n '2,(\d+)p' \"\$0\"", text)
    assert match, "run.sh's --help handler must still be the '2,ENDp' sed shape"
    end = int(match.group(1))
    lines = text.splitlines()
    set_line_no = next(
        i + 1 for i, line in enumerate(lines) if line.strip() == "set -euo pipefail"
    )
    assert end == set_line_no, (
        f"--help range ends at line {end}, but 'set -euo pipefail' is at line "
        f"{set_line_no} -- the header comment grew/shrank without the sed range "
        "moving with it, so --help now truncates or over-prints"
    )


# --- shakeout_in_container.sh: no file copy, token flows via env ----------


def test_in_container_no_longer_copies_a_mounted_credentials_file() -> None:
    text = _text(IN_CONTAINER_SH)
    assert "/creds/.credentials.json" not in text, (
        "shakeout_in_container.sh must not copy a mounted credential file "
        "-- CLAUDE_CODE_OAUTH_TOKEN now arrives as an inherited "
        "environment variable, set by docker run -e at the container's "
        "own entrypoint process"
    )


def test_in_container_keeps_the_onboarding_claude_json_seed() -> None:
    text = _text(IN_CONTAINER_SH)
    assert "hasCompletedOnboarding" in text, (
        "the onboarding-only .claude.json seed (unrelated to the "
        "credential transport) must survive the migration"
    )


def test_in_container_checks_the_token_is_present_before_launch() -> None:
    text = _text(IN_CONTAINER_SH)
    # Presence-only: never printing the value (the token rule). Accepts any
    # of the ordinary POSIX presence-test shapes on the bare variable name.
    assert re.search(r"CLAUDE_CODE_OAUTH_TOKEN[:+-]", text), (
        "shakeout_in_container.sh must assert CLAUDE_CODE_OAUTH_TOKEN "
        "reached the container's environment before launching claude -- "
        "presence only, never its value"
    )


#: The token rule's forbidden print shapes (never the value, only presence
#: tests are allowed -- `${VAR:-}`, `${VAR:+...}`, `[ -n "${VAR-}" ]`).
_TOKEN_PRINT_RE = re.compile(
    r"""
    echo\s+[^\n]*\$\{?CLAUDE_CODE_OAUTH_TOKEN(?![:}+-])   # echo $VAR / echo "$VAR"
    | printenv\s+[^\n]*CLAUDE_CODE_OAUTH_TOKEN
    | \bset\b[^\n]*\|[^\n]*CLAUDE_CODE_OAUTH_TOKEN
    """,
    re.VERBOSE,
)


def test_in_container_never_prints_the_token_value() -> None:
    text = _text(IN_CONTAINER_SH)
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        assert not _TOKEN_PRINT_RE.search(line), line


def test_in_container_bash_syntax_is_valid() -> None:
    proc = subprocess.run(["bash", "-n", str(IN_CONTAINER_SH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
