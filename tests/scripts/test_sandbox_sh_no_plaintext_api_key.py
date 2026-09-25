# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-219 Phase 2 Step 2 (nexus-wauo1.17), mechanism 2: tests/e2e/sandbox.sh
must never write the ANTHROPIC_API_KEY *value* into the generated
`$SANDBOX/activate` file.

THE DEFECT THIS CLOSES. `sandbox.sh` used to `printf` the caller's own
`$ANTHROPIC_API_KEY` into `activate` as a literal `export
ANTHROPIC_API_KEY="<value>"` line -- a plaintext credential copy left on
disk indefinitely (RDR-219 Research Findings mechanism 2). The key stays in
the launching process's own environment; a harness that needs it passes it
through, never writes it to a file.

Structural check: the source no longer contains the printf line that
interpolates `${ANTHROPIC_API_KEY...}` into an `export` statement (kill
control on a synthetic reproduction of the pre-fix shape, same convention
as test_release_sandbox_automation_token.py in this directory).

Behavioral check: sandbox.sh is cheap to run standalone (filesystem-only --
no tmux, no docker, no `uv tool install`), so this also runs it for real
against an isolated `NEXUS_SANDBOX_HOME` with a fake, non-secret
ANTHROPIC_API_KEY value and asserts the REAL generated `activate` file
never contains that value.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tests" / "e2e" / "sandbox.sh"

#: The exact pre-fix shape this test's kill control reproduces.
_PRE_FIX_LINE = 'printf \'export ANTHROPIC_API_KEY="%s"\\n\' "${ANTHROPIC_API_KEY:-}"'

#: A live line that interpolates the ANTHROPIC_API_KEY *value* into an
#: `export` statement -- matches both the pre-fix single-quoted printf
#: format string shape and any double-quoted / echo-based equivalent.
_VALUE_WRITE_RE = re.compile(
    r"""(printf|echo)[^\n]*ANTHROPIC_API_KEY=\\?["'][^\n]*\$\{?ANTHROPIC_API_KEY"""
)


def _violations(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if _VALUE_WRITE_RE.search(ln)]


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT.read_text()


def test_detector_flags_the_pre_fix_shape() -> None:
    """Kill control on a synthetic fixture -- never on the real repo file,
    so this can never pass vacuously because the tree happens to already be
    clean."""
    hits = _violations(_PRE_FIX_LINE + "\n")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_no_plaintext_api_key_value_write_in_source(script_text: str) -> None:
    hits = _violations(script_text)
    assert not hits, (
        "tests/e2e/sandbox.sh writes the ANTHROPIC_API_KEY value into a "
        f"generated file: {hits}"
    )


def test_real_run_generates_activate_with_no_key_value(tmp_path: Path) -> None:
    fake_key = "sk-ant-api-fake-test-value-not-a-real-secret"
    sandbox_home = tmp_path / "sandbox-home"
    env = dict(os.environ)
    env["NEXUS_SANDBOX_HOME"] = str(sandbox_home)
    env["ANTHROPIC_API_KEY"] = fake_key
    # Isolate from a real .env at the repo root, which sandbox.sh sources
    # if present -- this run must prove sandbox.sh's OWN behavior, not
    # whatever happens to be in the developer's .env.
    env.pop("VOYAGE_API_KEY", None)
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"sandbox.sh failed: {proc.stderr}"
    activate = sandbox_home / "activate"
    assert activate.is_file(), f"sandbox.sh did not create {activate}"
    activate_text = activate.read_text()
    assert fake_key not in activate_text, (
        f"the fake ANTHROPIC_API_KEY value leaked into the generated activate "
        f"file: {activate_text}"
    )
    assert "ANTHROPIC_API_KEY" not in activate_text, (
        "activate must not mention ANTHROPIC_API_KEY at all -- the key stays "
        f"in the launching process's own environment: {activate_text}"
    )
