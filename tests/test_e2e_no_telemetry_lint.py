"""nexus-tb01a — every E2E sandbox opts out of the anonymous install ping.

Within an hour of engine-service-v0.1.107 deploying, the active-install read
counted a release-battery rehearsal install as a user. A throwaway install
spawned by the harness must never ping: every ``env -i`` allowlist under
``tests/e2e`` carries ``NX_NO_TELEMETRY=1``, every non-scrubbing sandbox
script exports it, and the migration-rehearsal container forwards it (``-e``
is the only channel into that container; an exported host var is discarded).
"""

from __future__ import annotations

import pathlib
import re

import pytest

pytestmark = pytest.mark.lint

REPO = pathlib.Path(__file__).resolve().parent.parent
E2E = REPO / "tests" / "e2e"

#: Sandbox scripts that build their environment without ``env -i`` and must
#: export the opt-out instead.
EXPORTING_SANDBOXES = (
    "release-sandbox.sh",
    "upgrade-shakeout.sh",
    "local-service-gate.sh",
    "sandbox.sh",
    "local-index-memory-gate.sh",
)

_ENV_I = re.compile(r"^\s*env -i \\\n((?:.*\\\n)*)", re.M)


def _env_i_blocks(text: str) -> list[str]:
    return [m.group(0) for m in _ENV_I.finditer(text)]


def test_every_env_i_allowlist_under_e2e_opts_out() -> None:
    scripts = sorted(E2E.rglob("*.sh"))
    assert scripts, "no E2E scripts found; the scan is broken"
    seen = 0
    bad: list[str] = []
    for path in scripts:
        for block in _env_i_blocks(path.read_text()):
            seen += 1
            if "NX_NO_TELEMETRY=1" not in block:
                bad.append(f"{path.relative_to(REPO)}: env -i block without NX_NO_TELEMETRY=1")
    assert seen >= 9, f"only {seen} env -i blocks found; the scan is broken"
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("name", EXPORTING_SANDBOXES)
def test_non_scrubbing_sandbox_exports_the_opt_out(name: str) -> None:
    text = (E2E / name).read_text()
    assert re.search(r"^export NX_NO_TELEMETRY=1$", text, re.M), f"{name} must export NX_NO_TELEMETRY=1"


def test_container_rehearsal_forwards_the_opt_out() -> None:
    text = (E2E / "migration-rehearsal" / "run.sh").read_text()
    assert 'run_env+=(-e "NX_NO_TELEMETRY=1")' in text, "run.sh must forward NX_NO_TELEMETRY=1 into the container"
