# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every migration-rehearsal rehearse*.sh refuses to run outside its
container (nexus-oqh4s round 2).

THE CLASS. The 2026-09-12 incident (see
``test_no_git_config_global_writes_lint.py``) was one symptom of a wider
defect: every ``rehearse*.sh`` header under
``tests/e2e/migration-rehearsal/`` says "runs INSIDE the container", but
nothing enforced it. A bare host invocation does not just rewrite git
identity -- it runs ``nx init --service``, provisions a real Postgres,
installs the native service binary, and sources ``pg_credentials``, all
under the operator's REAL ``$HOME/.config/nexus``. This is a strictly
worse blast radius than the git-identity incident, and
``rehearse_package_upgrade.sh``'s own pre-existing quarantine check (guards
against a binary already present) only counts a FAIL via ``bad()`` --
it never exits, so a host run proceeds regardless.

THE FIX. ``lib/require_container.sh`` is sourced at the top of every
``rehearse*.sh`` (right after its ``set -...`` line, before anything else
executes) and refuses with exit 2 unless one of three markers is present:
the explicit ``NX_REHEARSAL_IN_CONTAINER=1`` env var (set via ``ENV`` in
every Dockerfile under this directory that COPYs or ENTRYPOINTs one of
these scripts), or the generic ``/.dockerenv`` / ``/run/.containerenv``
container markers.

SCOPE NOTE. The task that raised this named "the nine" rehearse*.sh
scripts implicated in the git-config sweep. Two more scripts in the same
directory (``rehearse_fullstack.sh``, ``rehearse_shakeout_e2e.sh``) carry
the identical "runs INSIDE the container" header and the identical
``$HOME/.config/nexus``-writing risk shape (confirmed:
``rehearse_fullstack.sh``'s own Phase A comment says "PG provisioned
in-box by `nx init --service`"), so this lint covers the whole
``rehearse*.sh`` family (eleven scripts today), not only the nine the
original incident touched -- guarding nine of eleven siblings with
identical headers would leave a two-tier guarantee.

This lint checks two things mechanically (every rehearse*.sh sources the
guard; every Dockerfile that runs one sets the marker) plus two functional
kill controls that exercise the guard's actual bash logic in a subprocess,
so a change to its detection condition that silently stops refusing
cannot pass by text-matching alone.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
REHEARSAL_DIR = REPO_ROOT / "tests" / "e2e" / "migration-rehearsal"
GUARD_LIB = REHEARSAL_DIR / "lib" / "require_container.sh"
_GUARD_SOURCE_RE = re.compile(r"source\s+.*lib/require_container\.sh")
_BASH = "/bin/bash"


def _rehearse_scripts() -> list[pathlib.Path]:
    return sorted(REHEARSAL_DIR.glob("rehearse*.sh"))


def _dockerfiles_running_rehearse_scripts() -> dict[pathlib.Path, set[str]]:
    """Map each Dockerfile in REHEARSAL_DIR to the rehearse*.sh basenames it
    COPYs or names in ENTRYPOINT/CMD -- i.e. the Dockerfiles that actually
    run one of these scripts and so must arm the container marker."""
    mapping: dict[pathlib.Path, set[str]] = {}
    script_names = {p.name for p in _rehearse_scripts()}
    for dockerfile in sorted(REHEARSAL_DIR.glob("Dockerfile*")):
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        hits = {name for name in script_names if name in text}
        if hits:
            mapping[dockerfile] = hits
    return mapping


def test_rehearse_script_scan_is_non_vacuous() -> None:
    """A broken glob (wrong extension, wrong directory) must fail loud
    rather than silently checking zero scripts."""
    scripts = _rehearse_scripts()
    assert len(scripts) >= 9, (
        f"only found {len(scripts)} rehearse*.sh under {REHEARSAL_DIR} -- "
        "the glob may be broken rather than the family genuinely shrinking "
        "below the nine the 2026-09-12 incident sweep found"
    )


def test_guard_lib_exists() -> None:
    assert GUARD_LIB.is_file(), f"{GUARD_LIB} missing"


def test_every_rehearse_script_sources_the_container_guard() -> None:
    missing = [
        path.name
        for path in _rehearse_scripts()
        if not _GUARD_SOURCE_RE.search(
            path.read_text(encoding="utf-8", errors="replace")
        )
    ]
    assert not missing, (
        "rehearse*.sh script(s) do not source lib/require_container.sh -- "
        "a run on a bare host would write into the operator's real "
        "$HOME/.config/nexus (pg credentials, service binary) with no "
        "refusal:\n" + "\n".join(missing)
    )


def test_dockerfile_scan_is_non_vacuous() -> None:
    mapping = _dockerfiles_running_rehearse_scripts()
    assert len(mapping) >= 7, (
        f"only found {len(mapping)} Dockerfiles referencing a rehearse*.sh "
        f"script under {REHEARSAL_DIR} -- the scan may be broken"
    )


def test_every_dockerfile_running_a_rehearse_script_sets_the_marker() -> None:
    mapping = _dockerfiles_running_rehearse_scripts()
    missing = []
    for dockerfile, scripts in mapping.items():
        text = dockerfile.read_text(encoding="utf-8", errors="replace")
        if "NX_REHEARSAL_IN_CONTAINER=1" not in text:
            missing.append(f"{dockerfile.name} (runs {', '.join(sorted(scripts))})")
    assert not missing, (
        "Dockerfile(s) run a rehearse*.sh script but never set "
        "NX_REHEARSAL_IN_CONTAINER=1 in an ENV instruction -- the script's "
        "own container guard would refuse inside this image, redding every "
        "release-battery leg that builds it:\n" + "\n".join(missing)
    )


@pytest.mark.skipif(
    os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"),
    reason="this test box is itself a container; the guard would legitimately pass",
)
def test_guard_refuses_on_a_bare_host() -> None:
    """Kill control: sourcing the real guard lib with the marker stripped
    must refuse with exit 2 and never reach a line after the source --
    proves the refusal is real bash behavior, not just a comment or a
    string this lint pattern-matched."""
    env = {k: v for k, v in os.environ.items() if k != "NX_REHEARSAL_IN_CONTAINER"}
    result = subprocess.run(
        [_BASH, "-c", f'source "{GUARD_LIB}"; echo SHOULD_NOT_REACH_HERE'],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2, (
        f"guard did not refuse on a bare host: rc={result.returncode} "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "SHOULD_NOT_REACH_HERE" not in result.stdout
    assert "REFUSING" in result.stderr


def test_guard_passes_with_the_marker_set() -> None:
    """Kill control, the other direction: the exact env every rehearsal
    Dockerfile sets must let the guard through."""
    env = dict(os.environ)
    env["NX_REHEARSAL_IN_CONTAINER"] = "1"
    result = subprocess.run(
        [_BASH, "-c", f'source "{GUARD_LIB}"; echo REACHED'],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "REACHED" in result.stdout
