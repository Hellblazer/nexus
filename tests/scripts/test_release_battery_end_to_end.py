# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The REAL ``release-battery.sh``, end to end, with stubbed legs.

The real script runs in a throwaway repo whose legs are stub scripts at the paths the battery
names, so a mutation of the top-level glue (the environment reaching ``battery_verdict``, the exit
status leaving the script, the report loop's treatment of a state) changes what the script prints
and returns.

Each test names the mutation it was shown red under.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / "tests" / "e2e"

#: leg -> the verdict line the stub prints when it passes (the battery's own verdict regexes).
_PASS_LINE = {
    "preflight": "PINS PREFLIGHT PASSED",
    "shakedown": "SHAKEDOWN PASSED",
    "lsg": "LOCAL-SERVICE GATE PASSED",
    "pkgup": "PACKAGE-UPGRADE CONVERGENCE MVV PASSED",
    "candmig": "CANDIDATE-MIGRATION REHEARSAL PASSED",
    "mvv": "FRESH-INSTALL MVV PASSED",
    "dtok": "DATA-TOKEN CLI GATE PASSED",
    "smoke": "SMOKE PASSED",
    "upshakeout": "UPGRADE-SHAKEOUT PASSED",
    "genflip": "GEN-FLIP LIVE-HOLDER PASSED",
    "pluginls": "PLUGIN-LOCKSTEP GATE PASSED",
    "hookskew": "HOOK-CLI SKEW GATE PASSED",
    "janitor": "CREDENTIAL JANITOR PASSED",
    "pins": "MANDATORY PINS GATE PASSED",
    "shakeout": "CANDIDATE SHAKEOUT PASSED",
}
_GROUP_AND_ALONE = ("shakedown", "lsg", "pkgup", "candmig", "mvv", "dtok", "smoke", "upshakeout",
                    "genflip", "pluginls", "hookskew", "janitor", "pins", "shakeout")


class _Battery:
    """A throwaway repo holding the real battery script and stub legs."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.out = tmp_path / "legout"
        self.ran = tmp_path / "ran"
        for d in (self.out, self.ran):
            d.mkdir()
        e2e = self.root / "tests" / "e2e"
        (e2e / "lib").mkdir(parents=True)
        shutil.copy(E2E / "release-battery.sh", e2e / "release-battery.sh")
        # The battery sources this at start and refuses without a python >= 3.10 (nexus-u67ow).
        shutil.copy(E2E / "lib" / "python.sh", e2e / "lib" / "python.sh")
        (self.root / "src" / "nexus").mkdir(parents=True)
        (self.root / "src" / "nexus" / "engine_version.py").write_text(
            "REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 142)\n")
        self._stub("tests/e2e/migration-rehearsal/build-artifacts.sh", (
            f'mkdir -p "$1"; touch "{self.ran}/artifacts"; echo "ARTIFACTS BUILT"\n'))
        for rel, key in (
            ("scripts/pins-preflight.sh", "preflight"),
            ("tests/e2e/local-service-gate.sh", "lsg"),
            ("tests/e2e/fresh-install-mvv.sh", "mvv"),
            ("tests/e2e/data-token-cli-gate.sh", "dtok"),
            ("tests/e2e/upgrade-shakeout.sh", "upshakeout"),
            ("tests/e2e/gen-flip-live-holder.sh", "genflip"),
            ("tests/e2e/plugin-lockstep-gate.sh", "pluginls"),
            ("tests/e2e/hook-cli-skew/run.sh", "hookskew"),
            ("tests/e2e/mandatory-pins-gate.sh", "pins"),
        ):
            self._stub(rel, f'k={key}\n{self._leg_body()}')
        self._stub("tests/e2e/release-sandbox.sh", f'k="$1"\n{self._leg_body()}')
        self._stub("tests/e2e/migration-rehearsal/run.sh", (
            'case "$*" in *--package-upgrade*) k=pkgup;; *--candidate-migration*) k=candmig;; '
            f'*--shakeout*) k=shakeout;; esac\n{self._leg_body()}'))
        janitor = self.root / "scripts" / "credential_janitor.py"
        janitor.parent.mkdir(parents=True, exist_ok=True)
        janitor.write_text(
            "import pathlib, sys\n"
            f"pathlib.Path('{self.ran}/janitor').touch()\n"
            f"sys.stdout.write(pathlib.Path('{self.out}/janitor.out').read_text())\n"
            f"sys.exit(int(pathlib.Path('{self.out}/janitor.rc').read_text()))\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
             "commit", "-q", "-m", "fixture"], cwd=self.root, check=True)
        for leg in ("artifacts", *_PASS_LINE):
            self.set_leg(leg, _PASS_LINE.get(leg, "ARTIFACTS BUILT") + "\n", 0)

    def _leg_body(self) -> str:
        return f'touch "{self.ran}/$k"; cat "{self.out}/$k.out"; exit "$(cat "{self.out}/$k.rc")"\n'

    def _stub(self, rel: str, body: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/bash\n" + body)
        path.chmod(0o755)

    def set_leg(self, leg: str, text: str, rc: int) -> None:
        (self.out / f"{leg}.out").write_text(text)
        (self.out / f"{leg}.rc").write_text(str(rc))

    def run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(
            ["bash", str(self.root / "tests" / "e2e" / "release-battery.sh"), "--max-parallel", "20", *args],
            capture_output=True, text=True, timeout=300, cwd=self.root,
            env={"PATH": os.environ["PATH"], "HOME": str(self.root.parent), **(env or {})},
        )
        work = re.search(r"RELEASE BATTERY: work=(\S+)", r.stdout)
        if work:  # the battery leaves its work dir (logs) behind by design
            shutil.rmtree(work.group(1), ignore_errors=True)
        return r

    def ran_legs(self) -> set[str]:
        return {p.name for p in self.ran.iterdir()}


@pytest.fixture()
def battery(tmp_path: Path) -> _Battery:
    return _Battery(tmp_path)


def _row(out: str, leg: str) -> str:
    m = re.search(rf"^{leg}\s+(.*)$", out, re.M)
    assert m, f"no report row for {leg}:\n{out}"
    return m.group(1)


def test_a_clean_run_passes_and_runs_every_leg_but_dtok(battery: _Battery) -> None:
    """The control case for everything below: stub legs print their pass lines, the battery ends
    PASSED, and dtok (named in --only to run) is skipped."""
    r = battery.run()
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.rstrip().splitlines()[-1] == "RELEASE BATTERY PASSED", r.stdout
    assert battery.ran_legs() >= set(_GROUP_AND_ALONE) - {"dtok"}, "every leg ran"
    assert "dtok" not in battery.ran_legs()
    assert _row(r.stdout, "dtok").startswith("SKIPPED")


def test_dtok_runs_when_named_in_only(battery: _Battery) -> None:
    """Mutation (drop the dtok skip's --only exemption): the named leg never starts."""
    r = battery.run("--only", "dtok")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "dtok" in battery.ran_legs()
    assert _row(r.stdout, "dtok").startswith("PASSED")


def test_the_mandatory_pins_leg_is_a_leg_and_its_red_is_a_red(battery: _Battery) -> None:
    """nexus-z0o2p.41. The GitHub-backed pins left lsg and run as battery leg `pins`; a red there ends the
    battery FAILED. Mutation (drop the define_leg): the stub never runs and the row is missing."""
    battery.set_leg("pins", "[pins] skipped over the budget\nMANDATORY PINS GATE FAILED\n", 1)
    r = battery.run()
    assert r.returncode == 1 and "pins" in battery.ran_legs(), (r.stdout, r.stderr)
    assert _row(r.stdout, "pins").startswith("FAILED")


def test_a_red_leg_makes_the_battery_exit_nonzero(battery: _Battery) -> None:
    """M12. Mutation (``exit $?`` -> ``exit 0`` after battery_verdict): the verdict line says FAILED
    and the process exits 0, which a CI step or a wrapper reads as a pass."""
    battery.set_leg("hookskew", "HOOK-CLI SKEW GATE FAILED: a hook blocked\n", 1)
    r = battery.run()
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "RELEASE BATTERY FAILED: 1 red leg(s)" in r.stdout
    assert _row(r.stdout, "hookskew").startswith("FAILED")
