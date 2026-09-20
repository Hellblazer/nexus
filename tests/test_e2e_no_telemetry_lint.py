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


_DOCKER_RUN = re.compile(r"^\s*docker run\b.*$", re.M)


def _spliced(text: str) -> str:
    """Join backslash continuations so a multi-line `docker run` reads as one
    logical line. Without this the flag looks absent on every wrapped
    invocation, which is every real one."""
    return re.sub(r"\\\n\s*", " ", text)


def _forwards_the_flag(line: str, whole: str) -> bool:
    if "NX_NO_TELEMETRY" in line:
        return True
    # ...or via an env array the script builds up, e.g. run_env+=(-e "...").
    for arr in re.findall(r"\$\{(\w+)\[@\]\}", line):
        if re.search(rf"{arr}\+?=\(.*NX_NO_TELEMETRY", whole, re.S):
            return True
    return False


def test_every_e2e_container_forwards_the_opt_out() -> None:
    """EVERY container, with no exemption list (nexus-6doho).

    GENERALISED from a test that named ``migration-rehearsal/run.sh`` and only
    it, so every other container launcher sat outside the lint's domain — and
    two of them, hook-surface-shakeout and rdr208-mvv, shipped pinging
    production for exactly that reason. conexus counted 18 fresh install_ids
    across two release nights, then two more overnight: throwaway installs the
    active-install metric read as users.

    ``-e`` is the only channel into a container. ``docker run`` does not
    inherit the host environment, so an ``export`` in the launching script is
    discarded at the boundary — which is why this cannot be folded into the
    exporting-sandbox check above.

    NO EXEMPTIONS, DELIBERATELY. The natural rule is "every container that
    runs a nexus CLIENT", since only a client mints an install_id. That rule
    needs a judgement per invocation about what runs inside an image, made
    correctly every time by everyone — and the first draft of this test got
    exactly that judgement wrong on migration-rehearsal's native-build
    container. Setting an env var a container ignores costs nothing, so the
    uniform rule is both cheaper and impossible to misapply. An exemption list
    would also be one more thing that goes stale and silently widens the blind
    spot this test exists to close.
    """
    offenders: list[str] = []
    for sh in sorted(E2E.rglob("*.sh")):
        whole = sh.read_text(errors="replace")
        for line in _DOCKER_RUN.findall(_spliced(whole)):
            if not _forwards_the_flag(line, whole):
                offenders.append(
                    f"  {sh.relative_to(E2E)}: {line.strip()[:90]}")
    assert not offenders, (
        "docker run invocations under tests/e2e that do not forward "
        "NX_NO_TELEMETRY=1:\n" + "\n".join(offenders)
        + "\n\nA container that installs or runs a nexus client mints a fresh "
        "install_id on its virgin HOME and pings production. Pass "
        "`-e NX_NO_TELEMETRY=1`; an exported host var does NOT cross the "
        "container boundary. Pass it even where you believe no client runs — "
        "the flag is free and the judgement is not."
    )


def test_the_check_would_notice_a_missing_flag() -> None:
    """Non-vacuity. The scan must fail on a flagless invocation, and it must
    not be fooled by a wrapped one — the continuation splice is the part most
    likely to rot, and without it every real multi-line invocation reads as
    non-compliant or (worse, after a regex tweak) as compliant."""
    assert not _forwards_the_flag("docker run --rm img", "")
    assert _forwards_the_flag("docker run --rm -e NX_NO_TELEMETRY=1 img", "")
    wrapped = 'docker run --rm \\\n    -e NX_NO_TELEMETRY=1 \\\n    img\n'
    found = _DOCKER_RUN.findall(_spliced(wrapped))
    assert found and _forwards_the_flag(found[0], wrapped), (
        "the continuation splice is broken; every wrapped docker run would be "
        "judged on its first line alone"
    )
    assert _forwards_the_flag('docker run "${run_env[@]}" img',
                              'run_env+=(-e "NX_NO_TELEMETRY=1")')
