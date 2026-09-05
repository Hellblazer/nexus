# SPDX-License-Identifier: AGPL-3.0-or-later
"""``--package-upgrade``'s ``docker run -e`` allowlist must forward
``UV_HTTP_TIMEOUT`` into the container (nexus-0j6gy).

THE DEFECT: Stage 1 of the package-upgrade rehearsal (``pip install
conexus==$PREV_RELEASE``) runs INSIDE the container, and pulls
hundred-MB transitive wheels (onnxruntime, nvidia-cufft via
mineru->torch). ``docker run``'s explicit ``run_env`` array is the ONLY
channel into the container — env vars set on the HOST have no effect
inside it. Before the fix, ``UV_HTTP_TIMEOUT`` was absent from that
array, so uv always used its 30s default regardless of what the
operator exported, and the resulting timeout's own failure text named
the exact variable to raise — actively misdirecting, since raising it
on the host did nothing. Observed twice, 2026-08-24 (bead comment).

The fix is one line (``run_env+=(-e "UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT:-300}")``)
inside the ``$PACKAGE_UPGRADE`` block of
``tests/e2e/migration-rehearsal/run.sh``. Nothing OTHER than a static
read of run.sh's own ``-e`` allowlist can catch a regression here: the
container journey is Docker-only and explicitly out of scope for the
fast/unit loop, so this is the only test that would go RED if the
forwarding line were ever removed or moved outside the
``$PACKAGE_UPGRADE`` conditional.

Non-vacuity: the extraction regex is anchored on the literal
``$PACKAGE_UPGRADE`` conditional and asserted to find something a KNOWN
sibling var (``PREV_RELEASE``) is also inside — a regex that silently
stopped matching (e.g. after the block was reshaped) fails on that
anchor assertion first, rather than passing vacuously on an empty
capture.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SH = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal" / "run.sh"

#: The ``$PACKAGE_UPGRADE`` conditional in run.sh's ``run_env`` assembly.
#: Non-greedy up to the first ``fi`` — the block itself has no nested
#: if/fi (only a bare ``[ -n ... ] &&`` guard), so this is exact, not a
#: heuristic. Anchored at LINE START (``^if``, MULTILINE) rather than a
#: bare substring search: an unanchored ``if \[...`` also matches inside
#: ``elif [ "$PACKAGE_UPGRADE" = 1 ]; then`` (the docker-run dispatch
#: further down in the same file), which silently captured that WRONG,
#: much larger span first — caught only by re-deriving this test's own
#: extraction against the real file rather than trusting it by inspection.
_PACKAGE_UPGRADE_BLOCK_RE = re.compile(
    r'^if \[ "\$PACKAGE_UPGRADE" = 1 \]; then\n(.*?)\n^fi$', re.DOTALL | re.MULTILINE,
)


def _package_upgrade_run_env_block() -> str:
    text = RUN_SH.read_text()
    match = _PACKAGE_UPGRADE_BLOCK_RE.search(text)
    assert match is not None, (
        "the `$PACKAGE_UPGRADE` run_env block was not found in run.sh by "
        "this lint's extraction regex -- the block was reshaped and this "
        "test needs updating, NOT silently skipped"
    )
    return match.group(1)


def test_the_harness_exists() -> None:
    assert RUN_SH.is_file(), f"harness moved: {RUN_SH}"


def test_extraction_anchor_is_non_vacuous() -> None:
    """The block must contain a KNOWN long-standing forwarded var
    (``PREV_RELEASE``) -- proves the regex above is actually finding the
    real block, not matching nothing and returning an empty capture that
    would vacuously pass the real assertion below."""
    block = _package_upgrade_run_env_block()
    assert "PREV_RELEASE=" in block, (
        "the extracted $PACKAGE_UPGRADE block is missing a var this "
        "harness has always forwarded -- the extraction regex is matching "
        "the wrong span"
    )


def test_package_upgrade_forwards_uv_http_timeout() -> None:
    """nexus-0j6gy: without this line, Stage 1's pip install inside the
    container is silently capped at uv's 30s default no matter what the
    operator exports on the host, and fails on any release whose
    transitive deps include a large wheel (onnxruntime, cufft via
    mineru->torch)."""
    block = _package_upgrade_run_env_block()
    assert "UV_HTTP_TIMEOUT=" in block, (
        "UV_HTTP_TIMEOUT is not forwarded into the container inside the "
        "$PACKAGE_UPGRADE run_env block -- raising it on the HOST has no "
        "effect (docker run's -e allowlist is the only channel in); see "
        "nexus-0j6gy"
    )


def test_uv_http_timeout_has_a_conservative_default() -> None:
    """The forwarded value must fall back to a generous default when the
    operator has not set it -- an unset passthrough (``-e
    "UV_HTTP_TIMEOUT="`` with no ``${VAR:-default}``) would forward an
    EMPTY string into the container, which is worse than not forwarding
    at all: uv would see ``UV_HTTP_TIMEOUT=""`` instead of falling back to
    its own 30s default."""
    block = _package_upgrade_run_env_block()
    match = re.search(r'UV_HTTP_TIMEOUT=\$\{UV_HTTP_TIMEOUT:-(\d+)\}', block)
    assert match is not None, (
        "UV_HTTP_TIMEOUT is forwarded but not with a ${UV_HTTP_TIMEOUT:-N} "
        "bash default-expansion -- an operator who has not set it would "
        "forward an empty value into the container"
    )
    assert int(match.group(1)) >= 300, (
        "the default UV_HTTP_TIMEOUT is too low to survive the observed "
        "hundred-MB transitive wheels (onnxruntime, nvidia-cufft); "
        "nexus-0j6gy's verified fix used 300s"
    )
