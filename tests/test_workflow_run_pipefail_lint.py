# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Every piping ``run:`` block in a workflow must set pipefail.

GitHub's default shell for a ``run:`` block on Linux is ``bash -e {0}``.
``pipefail`` is NOT part of that: it is added only by an explicit
``shell: bash`` (which GitHub expands to ``bash --noprofile --norc -eo
pipefail {0}``) or by the step setting it itself. So a step of the shape

    run: <command> | tee out.txt

exits with ``tee``'s status and CANNOT FAIL, no matter what ``<command>``
does. Reproduced:

    bash -e -c 'false | tee /dev/null'          -> exit 0
    bash -eo pipefail -c 'false | tee /dev/null' -> exit 1

Found 2026-08-23 on ``ci.yml``'s ``Run lint-marked tests`` step, which was
the SOLE CI enforcement point for the whole ``-m lint`` bucket (~1050
tests): ``pyproject.toml``'s addopts deselect ``lint`` from every other
leg, and the required ``pytest-gate`` check reads
``needs.test-lint.result``. The bucket was green at the time, so nothing
was masked -- the mechanism was the defect. Among the guards it silently
un-enforced were the MCP wire-shape censuses, whose whole job is to stop a
new tool re-opening a shipped regression.

Why no existing lint caught it: ``tests/test_pipefail_early_exit_consumer_
lint.py`` is a dedicated pipefail-hazard lint, but it selects files with
``git ls-files "*.sh"`` and never reads ``.github/workflows/*.yml`` -- by
construction it could not see the one pipe that disarmed its own CI leg.
This module closes that gap for workflow YAML specifically.

Heredoc bodies are excluded: a ``run:`` block may embed Python or another
language whose source legitimately contains ``|`` (``release.yml``'s
CHANGELOG extractor has ``(?=\\n## \\[|\\Z)`` inside a Python heredoc).
Those are not shell pipes.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: Sanity floors. A sweep that finds nothing to check is a failure, not a
#: pass (the house vacuous-gate doctrine). These are well below today's
#: real counts (107 run blocks, 18 of them piping) so ordinary edits do
#: not trip them, but deleting the workflows or breaking the parser does.
_MIN_RUN_BLOCKS = 50
_MIN_PIPING_BLOCKS = 8

_HEREDOC_START = re.compile(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?")
_PIPEFAIL = re.compile(r"set\s+-[A-Za-z]*o[A-Za-z]*\s+pipefail|set\s+-o\s+pipefail")


def _shell_lines(run: str) -> list[str]:
    """``run``'s lines with comments and heredoc BODIES removed."""
    out: list[str] = []
    terminator: str | None = None
    for line in run.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _HEREDOC_START.search(line)
        if match:
            terminator = match.group(1)
            # The line introducing the heredoc is still shell; keep it.
        out.append(stripped)
    return out


def _pipes_in(run: str) -> str | None:
    """The first line carrying a real shell pipe, or ``None``.

    ``||`` is not a pipe. Neither is ``|`` inside a heredoc body.
    """
    for line in _shell_lines(run):
        if "|" in line.replace("||", ""):
            return line
    return None


def _iter_steps():
    """Yield (workflow_path, job_name, step, guarded_by_declared_shell)."""
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        wf = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        wf_shell = "shell" in ((wf.get("defaults") or {}).get("run") or {})
        for job_name, job in (wf.get("jobs") or {}).items():
            job_shell = "shell" in ((job.get("defaults") or {}).get("run") or {})
            for step in job.get("steps") or []:
                if isinstance(step.get("run"), str):
                    yield path, job_name, step, (wf_shell or job_shell or "shell" in step)


def test_the_sweep_is_not_vacuous() -> None:
    """Guards the parser itself: a sweep that inspects nothing passes
    everything."""
    steps = list(_iter_steps())
    assert len(steps) >= _MIN_RUN_BLOCKS, (
        f"only {len(steps)} run: blocks found across {WORKFLOW_DIR} -- the "
        "YAML parse or the glob is broken, and this module is proving nothing"
    )
    piping = [s for *_, s, _ in [(p, j, st, g) for p, j, st, g in steps] if _pipes_in(s["run"])]
    assert len(piping) >= _MIN_PIPING_BLOCKS, (
        f"only {len(piping)} piping run: blocks detected -- the pipe detector "
        "has stopped detecting, so the assertion below cannot fail"
    )


def test_every_piping_run_block_sets_pipefail() -> None:
    offenders: list[str] = []
    for path, job_name, step, shell_declared in _iter_steps():
        run = step["run"]
        piped = _pipes_in(run)
        if piped is None or shell_declared or _PIPEFAIL.search(run):
            continue
        offenders.append(
            f"{path.name} :: job {job_name} :: step "
            f"{step.get('name', '<unnamed>')!r}\n      {piped}"
        )
    assert not offenders, (
        "these run: blocks pipe under GitHub's default `bash -e {0}`, which "
        "does NOT set pipefail, so the step exits with the LAST command's "
        "status and cannot fail:\n    "
        + "\n    ".join(offenders)
        + "\n  Fix: add `set -euo pipefail` as the first line of a `run: |` "
        "block (see ci.yml's shard-matrix step), or declare `shell: bash`."
    )


def test_detector_flags_a_bare_pipe_and_clears_a_guarded_one() -> None:
    """Falsification control for the detector itself."""
    assert _pipes_in("pytest -q | tee out.txt") is not None
    assert _pipes_in("a || b") is None, "|| is not a pipe"
    assert _pipes_in("# commented | pipe") is None, "comments are not shell"
    heredoc = "python - <<'PY'\nprint('a|b')\nPY\n"
    assert _pipes_in(heredoc) is None, "heredoc bodies are not shell"
    assert _PIPEFAIL.search("set -euo pipefail\npytest | tee o") is not None
    assert _PIPEFAIL.search("set -eu\npytest | tee o") is None
