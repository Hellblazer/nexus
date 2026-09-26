# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-219 amendment Phase 3b Step 3 (nexus-wauo1.40): a GRANT mode of
`tests/e2e/migration-rehearsal/run.sh --fullstack` that proves the nx-mcp
dispatch grant end to end -- an operator tool, an nx-mcp-spawned aspect
worker, a tool-granting nested dispatch, and no leak of either token name
to a Bash-tool child.

SCOPE (from the critique of nexus-wauo1.39, recorded on the bead): today's
--fullstack leg does not stage `tests/e2e/lib` into the image, never runs
`claude` through `claude_mcp_grant.sh`, always pre-starts the RDR-173
aspect worker (which proof 2 must skip), and drives no operator_summarize /
nx_plan_audit / nx_enrich_beads call and no leak check. This module pins the
structural wiring `--fullstack --grant` needs; the actual runs (billed,
real) are out of scope for an automated test -- see the bead's PROOF
instruction.

Structural, grep-based checks over the tracked script text -- same
lint-shape convention as test_migration_rehearsal_automation_token.py in
this directory.
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
GRANT_LAUNCHER = REPO_ROOT / "tests" / "e2e" / "lib" / "claude_mcp_grant.sh"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


@pytest.fixture(scope="module")
def dockerfile_fullstack_text() -> str:
    return DOCKERFILE_FULLSTACK.read_text()


@pytest.fixture(scope="module")
def rehearse_fullstack_text() -> str:
    return REHEARSE_FULLSTACK.read_text()


def test_grant_launcher_exists() -> None:
    assert GRANT_LAUNCHER.is_file(), "claude_mcp_grant.sh (nexus-wauo1.39) missing"


# ─────────────────────────────────────────────────────────────────────────
# run.sh: the --grant flag itself
# ─────────────────────────────────────────────────────────────────────────


def test_grant_flag_is_parsed(script_text: str) -> None:
    assert re.search(r'--grant\)\s*GRANT=1', script_text), (
        "no '--grant) GRANT=1' case in run.sh's argument parser"
    )


def test_grant_flag_refused_without_fullstack(script_text: str) -> None:
    """--grant is a --fullstack sub-flag; standalone (or combined with any
    other leg) it must be refused before the first mutation, the same
    convention every other leg's combination guard uses."""
    assert re.search(
        r'\[\s*"\$GRANT"\s*=\s*1\s*\]\s*&&\s*\[\s*"\$FULLSTACK"\s*!=\s*1\s*\].*exit 2',
        script_text,
    ), "no guard refusing --grant without --fullstack"


def test_grant_default_is_zero(script_text: str) -> None:
    assert re.search(r'^GRANT=0\s*$', script_text, re.MULTILINE), (
        "GRANT variable not declared/defaulted to 0"
    )


# ─────────────────────────────────────────────────────────────────────────
# run.sh: staging tests/e2e/lib into the fullstack image (like --package-upgrade)
# ─────────────────────────────────────────────────────────────────────────


def _fullstack_staging_block(text: str) -> str:
    start = text.index('elif [ "$FULLSTACK" = 1 ] || [ "$SHAKEOUT_E2E" = 1 ]')
    end = text.index("elif ", start + 1)
    return text[start:end]


def test_fullstack_staging_copies_lib(script_text: str) -> None:
    block = _fullstack_staging_block(script_text)
    assert re.search(r'cp\s+-R\s+"\$HERE/lib"\s+"\$STAGE/lib"', block), (
        f"the FULLSTACK/SHAKEOUT_E2E staging branch does not stage tests/e2e/lib "
        f"into $STAGE, unlike --package-upgrade:\n{block}"
    )


def test_dockerfile_fullstack_copies_lib(dockerfile_fullstack_text: str) -> None:
    assert re.search(r'COPY\s+lib/\s+/home/nexus/lib/', dockerfile_fullstack_text), (
        "Dockerfile.fullstack has no COPY of lib/ into the image"
    )


# ─────────────────────────────────────────────────────────────────────────
# run.sh: the grant flag reaches the container
# ─────────────────────────────────────────────────────────────────────────


def test_grant_forwarded_into_container_env(script_text: str) -> None:
    assert re.search(r'-e\s+"NX_FULLSTACK_GRANT=\$GRANT"', script_text), (
        "GRANT is never forwarded into the container's run_env as "
        "NX_FULLSTACK_GRANT"
    )


# ─────────────────────────────────────────────────────────────────────────
# rehearse_fullstack.sh: the in-container grant-mode behavior
# ─────────────────────────────────────────────────────────────────────────


def test_rehearse_fullstack_reads_grant_mode_env(rehearse_fullstack_text: str) -> None:
    assert "NX_FULLSTACK_GRANT" in rehearse_fullstack_text, (
        "rehearse_fullstack.sh never reads NX_FULLSTACK_GRANT"
    )


def test_rehearse_fullstack_sources_the_launcher(rehearse_fullstack_text: str) -> None:
    assert "claude_mcp_grant.sh" in rehearse_fullstack_text, (
        "rehearse_fullstack.sh never sources claude_mcp_grant.sh"
    )
    assert re.search(r'\bclaude_mcp_grant\s+nx-mcp\s+--', rehearse_fullstack_text), (
        "rehearse_fullstack.sh never calls claude_mcp_grant nx-mcp -- ..."
    )


def test_rehearse_fullstack_skips_prestart_in_grant_mode(rehearse_fullstack_text: str) -> None:
    """Proof 2's non-vacuity requirement: the pre-start must be
    conditioned on grant mode being OFF, and grant mode must assert the
    registry lease is ABSENT before the first store_put -- not merely that
    document_aspects ends up > 0, which the pre-started worker alone would
    also satisfy."""
    prestart_idx = rehearse_fullstack_text.index("ensure_aspect_worker_daemon")
    # The pre-start call must sit behind a grant-mode-off guard.
    preceding = rehearse_fullstack_text[:prestart_idx]
    last_grant_check = preceding.rfind("GRANT_MODE")
    assert last_grant_check != -1 and last_grant_check < prestart_idx, (
        "the aspect-worker pre-start is not gated on GRANT_MODE"
    )
    assert "registry.discover" in rehearse_fullstack_text
    assert re.search(r'no worker lease|lease.*absent|ABSENT', rehearse_fullstack_text), (
        "no assertion that the worker lease is absent before the first "
        "store_put in grant mode"
    )


def test_rehearse_fullstack_asserts_anthropic_api_key_absent(rehearse_fullstack_text: str) -> None:
    assert "ANTHROPIC_API_KEY" in rehearse_fullstack_text, (
        "rehearse_fullstack.sh never checks ANTHROPIC_API_KEY"
    )


def test_rehearse_fullstack_calls_operator_summarize(rehearse_fullstack_text: str) -> None:
    assert "mcp__nexus__operator_summarize" in rehearse_fullstack_text


def test_rehearse_fullstack_calls_a_tool_granting_dispatch(rehearse_fullstack_text: str) -> None:
    assert (
        "mcp__nexus__nx_enrich_beads" in rehearse_fullstack_text
        or "mcp__nexus__nx_plan_audit" in rehearse_fullstack_text
    ), "no nx_enrich_beads/nx_plan_audit call in rehearse_fullstack.sh"


def test_rehearse_fullstack_no_leak_diagnostic(rehearse_fullstack_text: str) -> None:
    """A real Bash-tool child must be granted and run a counting-only
    diagnostic over BOTH token names -- never a value dump."""
    assert re.search(r'grep -c\s+NX_HARNESS_CLAUDE_OAUTH_TOKEN', rehearse_fullstack_text), (
        "no counting diagnostic for NX_HARNESS_CLAUDE_OAUTH_TOKEN"
    )
    assert re.search(r'grep -c\s+CLAUDE_CODE_OAUTH_TOKEN', rehearse_fullstack_text), (
        "no counting diagnostic for CLAUDE_CODE_OAUTH_TOKEN"
    )
    # Must actually grant a Bash tool for this to check a REAL child, not
    # merely the absence of one.
    assert re.search(r'--allowedTools[^\n]*\bBash\b', rehearse_fullstack_text), (
        "no --allowedTools ... Bash grant found for the no-leak diagnostic"
    )


def test_rehearse_fullstack_argv_check(rehearse_fullstack_text: str) -> None:
    assert re.search(r"ps -axww -o args \| grep -c", rehearse_fullstack_text), (
        "no live argv check (ps -axww -o args | grep -c ...) in rehearse_fullstack.sh"
    )


def test_rehearse_fullstack_prestart_block_shellchecks_clean(
    rehearse_fullstack_text: str,
) -> None:
    """Scoped shellcheck over the grant-mode additions, same convention as
    the existing prestart-block check in
    test_migration_rehearsal_automation_token.py."""
    start = rehearse_fullstack_text.index("# 0. Pre-start")
    # nexus-wauo1.40: the inner if/else/fi (the ok/bad liveness assertion, on
    # one line) is nested inside an OUTER `if [ "$GRANT_MODE" != 1 ]; then`
    # block -- the extraction needs BOTH closing `fi`s, one on the assertion
    # line and one on the line right after it, or the probe is unbalanced.
    end_marker = 'registry lease not found after pre-start"; fi\nfi'
    end = rehearse_fullstack_text.index(end_marker) + len(end_marker)
    block = rehearse_fullstack_text[start:end]
    probe_src = (
        "#!/usr/bin/env bash\nset -uo pipefail\n"
        'ok() { :; }; bad() { :; }; note() { :; }; NXENV_PY=python3; GRANT_MODE=0\n'
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


def test_default_fullstack_workload_prompt_unchanged_when_not_grant(
    rehearse_fullstack_text: str,
) -> None:
    """The default (non-grant) fullstack run must not gain the grant-only
    tool calls in its own workload prompt/allowedTools -- those exercise
    the nx-mcp dispatch grant and would fail without it."""
    idx_op = rehearse_fullstack_text.find("mcp__nexus__operator_summarize")
    idx_grant_mode = rehearse_fullstack_text.find("GRANT_MODE")
    assert idx_op != -1 and idx_grant_mode != -1 and idx_grant_mode < idx_op, (
        "operator_summarize call is not gated behind a GRANT_MODE check "
        "appearing earlier in the file"
    )
