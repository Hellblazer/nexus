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
REHEARSE_SHAKEOUT_E2E = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "rehearse_shakeout_e2e.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def dockerfile_fullstack_text() -> str:
    return DOCKERFILE_FULLSTACK.read_text()


@pytest.fixture(scope="module")
def rehearse_fullstack_text() -> str:
    return REHEARSE_FULLSTACK.read_text()


@pytest.fixture(scope="module")
def rehearse_shakeout_e2e_text() -> str:
    return REHEARSE_SHAKEOUT_E2E.read_text()


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


# ===========================================================================
# nexus-wauo1.15 follow-up: the aspect-worker daemon must be pre-started by
# the HARNESS (bash, which has CLAUDE_CODE_OAUTH_TOKEN from docker -e), not
# left to nx-mcp's own spawn-if-absent call -- Claude Code strips
# CLAUDE_CODE_OAUTH_TOKEN from every subprocess it spawns itself (Bash tool
# AND MCP stdio servers), even against an explicit named `.mcp.json` `env`
# block (empirically reproduced with a throwaway diagnostic MCP server; see
# T2 nexus_rdr/219-continuation-p2-1f for the full reproduction). A real
# proof (an actual --fullstack run with document_aspects > 0) is out of
# scope for an automated test -- see the bead's PROOF instruction.
# ===========================================================================

_MCP_WORKLOAD_MARKER = "--mcp-config"
_PRESTART_MARKER = "ensure_aspect_worker_daemon"
_FINDING_MARKERS = ("nexus-wauo1.15", "strips CLAUDE_CODE_OAUTH_TOKEN")


def _mcp_json_body(text: str) -> str:
    start = text.index("cat > /home/nexus/mcp.json <<'MCPJSON'\n") + len(
        "cat > /home/nexus/mcp.json <<'MCPJSON'\n"
    )
    end = text.index("\nMCPJSON", start)
    return text[start:end]


@pytest.mark.parametrize(
    "fixture_name",
    ["rehearse_fullstack_text", "rehearse_shakeout_e2e_text"],
)
def test_worker_prestart_appears_before_the_mcp_workload_call(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    text: str = request.getfixturevalue(fixture_name)
    prestart_idx = text.index(_PRESTART_MARKER)
    workload_idx = text.index(_MCP_WORKLOAD_MARKER)
    assert prestart_idx < workload_idx, (
        f"{fixture_name}: the aspect-worker daemon pre-start "
        f"({_PRESTART_MARKER!r} at {prestart_idx}) must appear BEFORE the "
        f"MCP workload's claude -p call ({_MCP_WORKLOAD_MARKER!r} at "
        f"{workload_idx}) -- otherwise nx-mcp's own store_put can race the "
        f"harness's pre-start and spawn its own (env-stripped) daemon first."
    )


@pytest.mark.parametrize(
    "fixture_name",
    ["rehearse_fullstack_text", "rehearse_shakeout_e2e_text"],
)
def test_mcp_json_carries_no_env_block(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    """A prior round of this fix tried an explicit `.mcp.json` `env` block
    naming CLAUDE_CODE_OAUTH_TOKEN by exact key -- proven NOT to work (Claude
    Code strips the name regardless) and reverted. This pins that reversion:
    no `"env"` key should reappear in either mcp.json heredoc, since it would
    misleadingly suggest the token reaches the MCP server that way."""
    text: str = request.getfixturevalue(fixture_name)
    body = _mcp_json_body(text)
    assert '"env"' not in body, (
        f"{fixture_name}: mcp.json heredoc carries an \"env\" key again -- "
        f"this does not deliver CLAUDE_CODE_OAUTH_TOKEN (Claude Code strips "
        f"it from spawned MCP servers regardless) and misleadingly suggests "
        f"it does: {body!r}"
    )


@pytest.mark.parametrize(
    "fixture_name",
    ["rehearse_fullstack_text", "rehearse_shakeout_e2e_text"],
)
def test_prestart_comment_names_the_empirical_finding(
    fixture_name: str, request: pytest.FixtureRequest
) -> None:
    text: str = request.getfixturevalue(fixture_name)
    for marker in _FINDING_MARKERS:
        assert marker in text, (
            f"{fixture_name}: expected a comment naming {marker!r} near the "
            f"aspect-worker pre-start, documenting why it exists"
        )


def test_rehearse_fullstack_prestart_block_shellchecks_clean(
    rehearse_fullstack_text: str,
) -> None:
    """Scoped, not whole-file -- same convention as the docker-run block
    check above. Extracts the inserted pre-start snippet (from its own
    comment header to the following `ok`/`bad` liveness assertion) and
    shellchecks it in isolation."""
    start = rehearse_fullstack_text.index("# 0. Pre-start")
    end_marker = "the real proof)\"; fi"
    end = rehearse_fullstack_text.index(end_marker) + len(end_marker)
    block = rehearse_fullstack_text[start:end]
    probe_src = (
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        'ok() { :; }; bad() { :; }; note() { :; }\n'
        + block
        + "\n"
    )
    proc = subprocess.run(
        ["shellcheck", "-x", "-s", "bash", "-"],
        input=probe_src,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
