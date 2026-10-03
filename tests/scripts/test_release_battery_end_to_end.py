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
    "preflight": "PREFLIGHT PASSED",
    "lsg": "LOCAL-SERVICE GATE PASSED",
    "pkgup": "PACKAGE-UPGRADE CONVERGENCE MVV PASSED",
    "mvv": "FRESH-INSTALL MVV PASSED",
}
_GROUP = ("lsg", "pkgup", "mvv")


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
        for rel, key in (
            ("tests/e2e/release-preflight.sh", "preflight"),
            ("tests/e2e/local-service-gate.sh", "lsg"),
            ("tests/e2e/fresh-install-mvv.sh", "mvv"),
            ("tests/e2e/migration-rehearsal/run.sh", "pkgup"),
        ):
            self._stub(rel, f'k={key}\n{self._leg_body()}')
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
             "commit", "-q", "-m", "fixture"], cwd=self.root, check=True)
        for leg, line in _PASS_LINE.items():
            self.set_leg(leg, line + "\n", 0)

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


def test_a_clean_run_passes_and_runs_the_preflight_and_every_leg(battery: _Battery) -> None:
    """The control case for everything below: stub legs print their pass lines and the battery ends
    PASSED. The battery is exactly the preflight plus the three legs."""
    r = battery.run()
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.rstrip().splitlines()[-1] == "RELEASE BATTERY PASSED", r.stdout
    assert battery.ran_legs() == {"preflight", *_GROUP}, "every leg ran, and nothing else"


def test_only_names_a_real_leg_and_skips_the_rest(battery: _Battery) -> None:
    """Mutation (drop the ``--only`` skip): the unnamed legs run anyway and the verdict loses its
    PARTIAL marker."""
    r = battery.run("--only", "mvv")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert battery.ran_legs() == {"preflight", "mvv"}
    assert _row(r.stdout, "lsg").startswith("SKIPPED")
    assert "PARTIAL" in r.stdout.rstrip().splitlines()[-1], r.stdout


def test_a_red_preflight_is_a_red_and_does_not_stop_the_legs(battery: _Battery) -> None:
    """The preflight reports every red and the battery carries on: its red ends the battery FAILED
    while the three legs still ran. Mutation (abort the battery on a preflight red): the legs never
    run."""
    battery.set_leg("preflight", "PREFLIGHT FAILED -- fix ALL of the above\n", 1)
    r = battery.run()
    assert r.returncode == 1 and battery.ran_legs() == {"preflight", *_GROUP}, (r.stdout, r.stderr)
    assert _row(r.stdout, "preflight").startswith("FAILED")


def test_a_red_leg_makes_the_battery_exit_nonzero(battery: _Battery) -> None:
    """M12. Mutation (``exit $?`` -> ``exit 0`` after battery_verdict): the verdict line says FAILED
    and the process exits 0, which a CI step or a wrapper reads as a pass."""
    battery.set_leg("pkgup", "PACKAGE-UPGRADE CONVERGENCE MVV FAILED: a hop did not converge\n", 1)
    r = battery.run()
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "RELEASE BATTERY FAILED: 1 red leg(s)" in r.stdout
    assert _row(r.stdout, "pkgup").startswith("FAILED")


def test_a_leg_that_exits_zero_without_its_verdict_line_is_missing_not_passed(battery: _Battery) -> None:
    """nexus-f2g8u. Mutation (treat rc 0 as a pass): a leg that printed nothing reads PASSED."""
    battery.set_leg("lsg", "no verdict here\n", 0)
    r = battery.run()
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _row(r.stdout, "lsg").startswith("MISSING")
