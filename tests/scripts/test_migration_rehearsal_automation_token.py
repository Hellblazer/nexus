# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-219 Phase 2 Step 1f (nexus-wauo1.15): tests/e2e/migration-rehearsal/
run.sh's `--fullstack` and `--shakeout-e2e` legs must launch the in-
container `claude -p` with the harness's OWN automation token
(`tests/e2e/lib/claude_credentials.py run --`), never a copy of the
operator's interactive login.

THE DEFECT THIS CLOSES. Both legs fetched the operator's Claude Code OAuth
credential with the shared `pick`/`check` picker (nexus-galkv.19), fell back
to reading `~/.claude/.credentials.json` -- the operator's own interactive
login, which RDR-219 rule 1 forbids a harness to use -- staged the raw JSON
to `$STAGE/.claude-credentials.json` on disk, and bind-mounted it read-only
into the container at `/home/nexus/.claude/.credentials.json`.

THE FIX. `claude_credentials.py status` gates the run (never prints token
material); the `docker run` invocation itself is wrapped in
`claude_credentials.py run --`, which execs it with `CLAUDE_CODE_OAUTH_TOKEN`
forced into the docker client's own environment as a bare `-e` flag (A2
launch shape, T2 `nexus_rdr/219-research-10`) -- so the value reaches the
container without ever touching argv, a mounted file, or disk at all.

Structural, grep-based checks over the tracked script text -- same
lint-shape convention as `test_release_sandbox_automation_token.py` in this
directory. A real proof (an actual `--fullstack`/`--shakeout-e2e` run,
billed) is out of scope for an automated test -- see the bead's PROOF
instruction for the manual run.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "run.sh"
DOCKERFILE_FULLSTACK = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "Dockerfile.fullstack"
REHEARSE_FULLSTACK = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "rehearse_fullstack.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def dockerfile_fullstack_text() -> str:
    return DOCKERFILE_FULLSTACK.read_text()


@pytest.fixture(scope="module")
def rehearse_fullstack_text() -> str:
    return REHEARSE_FULLSTACK.read_text()


#: Anchors the real launch if/elif chain (the tail of the script, right
#: after the "NOT `exec`" EXIT-trap comment) rather than the several
#: earlier `[ "$FULLSTACK" = 1 ] && { ... }` combination guards -- `"if ["`
#: is also a substring of `"elif ["`, so a bare `text.index('if [ "$FULLSTACK"...')`
#: matches the WRONG, much earlier occurrence (an `elif` on a different
#: if/elif chain entirely) without this anchor.
_LAUNCH_CHAIN_ANCHOR = "# NOT `exec`"


def _block(text: str, start_marker: str, end_marker: str) -> str:
    chain_start = text.index(_LAUNCH_CHAIN_ANCHOR)
    start = text.index(start_marker, chain_start)
    end = text.index(end_marker, start)
    return text[start:end]


def _fullstack_block(text: str) -> str:
    return _block(text, 'if [ "$FULLSTACK" = 1 ]', 'elif [ "$SHAKEOUT_E2E" = 1 ]')


def _shakeout_e2e_block(text: str) -> str:
    return _block(text, 'elif [ "$SHAKEOUT_E2E" = 1 ]', 'elif [ "$HOLE_PUNCH" = 1 ]')


def test_no_credentials_json_write_or_mount_anywhere_in_run_sh(script_text: str) -> None:
    """The staged file, its mount target, and the ~/.claude fallback are all
    gone from the whole script -- not just narrowed to the two blocks."""
    assert ".claude-credentials.json" not in script_text
    assert "/home/nexus/.claude/.credentials.json" not in script_text
    assert "$HOME/.claude/.credentials.json" not in script_text


def test_no_pick_or_check_calls_remain(script_text: str) -> None:
    assert re.search(r'"\$CRED_TOOL"\s+pick\b', script_text) is None
    assert re.search(r'"\$CRED_TOOL"\s+check\b', script_text) is None


@pytest.mark.parametrize("block_fn", [_fullstack_block, _shakeout_e2e_block])
def test_block_gates_on_status(script_text: str, block_fn) -> None:
    block = block_fn(script_text)
    assert re.search(r'"\$CRED_TOOL"\s+status\b', block), (
        f"no `$CRED_TOOL status` gate found in block:\n{block}"
    )


@pytest.mark.parametrize("block_fn", [_fullstack_block, _shakeout_e2e_block])
def test_block_wraps_docker_run_with_cred_tool_run(script_text: str, block_fn) -> None:
    block = block_fn(script_text)
    docker_run_lines = [
        ln
        for ln in block.splitlines()
        if re.search(r"\bdocker run\b", ln) and not ln.lstrip().startswith("#")
    ]
    assert docker_run_lines, f"no `docker run` line found in block:\n{block}"
    assert all(
        re.search(r'"\$CRED_TOOL"\s+run\s+--\s+docker run\b', ln) for ln in docker_run_lines
    ), f"docker run line(s) not wrapped in `$CRED_TOOL run -- docker run`: {docker_run_lines}"


def test_fullstack_block_still_uses_run_env_and_image(script_text: str) -> None:
    """The migration -- credential transport only -- must not have dropped
    the pre-existing docker argv this leg depends on."""
    block = _fullstack_block(script_text)
    assert '"${run_env[@]}"' in block
    assert '"$IMAGE"' in block


def test_shakeout_e2e_block_still_overrides_the_entrypoint(script_text: str) -> None:
    block = _shakeout_e2e_block(script_text)
    assert "--entrypoint /bin/bash" in block
    assert "/home/nexus/rehearse_shakeout_e2e.sh" in block


def test_dockerfile_fullstack_auth_comment_no_longer_describes_a_mount(
    dockerfile_fullstack_text: str,
) -> None:
    """The old comment promised a read-only bind mount of the operator's
    credentials file -- that mechanism is gone; the comment must not still
    claim it."""
    assert ".claude/.credentials.json" not in dockerfile_fullstack_text
    assert "mounted read-only" not in dockerfile_fullstack_text


def test_rehearse_fullstack_auth_comment_no_longer_describes_a_mount(
    rehearse_fullstack_text: str,
) -> None:
    assert ".claude/.credentials.json" not in rehearse_fullstack_text
    assert "mounted read-only" not in rehearse_fullstack_text


def test_shellcheck_finds_no_new_findings_in_the_fullstack_block(script_text: str) -> None:
    """Scoped, not whole-file: run.sh already carries pre-existing findings
    elsewhere that a whole-file assertion would wrongly attribute to this
    change. Wrap the extracted if/elif fragment in a standalone if/fi so it
    parses on its own."""
    block = _fullstack_block(script_text)
    probe_src = (
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'CRED_TOOL=cred.py; IMAGE=img; run_env=(); FULLSTACK=1\n'
        + block
        + "\nfi\n"
    )
    proc = subprocess.run(
        ["shellcheck", "-x", "-s", "bash", "-"],
        input=probe_src,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
