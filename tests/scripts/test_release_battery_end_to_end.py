# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-0kmat: the REAL ``release-battery.sh``, end to end, with stubbed legs.

``test_candidate_engine.py`` runs the battery's extracted functions. What it cannot see is the
top-level glue between them: that the environment reaches ``battery_verdict``, that the exit
status leaves the script, that the report loop treats a state as non-red, that a failed candidate
resolution stops the legs. Round 2's review (T2 nexus/review-0kmat-round2-verify, N2) showed four
mutations of that glue survived the whole file. Here the real script runs in a throwaway repo
whose legs are stub scripts at the paths the battery names, so a mutation of the glue changes what
the script prints and returns.

Each test names the mutation it was shown red under.
"""
from __future__ import annotations

import hashlib
import json
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
#: engine leg -> which artifacts-manifest engine it serves, and the control it sends itself.
_ENGINE_LEGS = {
    "mvv": "jar", "smoke": "jar", "shakedown": "jar", "dtok": "jar",
    "lsg": "jar", "shakeout": "native", "candmig": "native",
}
_CONTROLS = {"lsg": 1}  # local-service-gate.sh sends the engine one deliberate ownerless write
_GROUP_AND_ALONE = ("shakedown", "lsg", "pkgup", "candmig", "mvv", "dtok", "smoke", "upshakeout",
                    "genflip", "pluginls", "hookskew", "janitor", "pins", "shakeout")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _engine_lines(leg: str, art: Path, *, mode: str = "enforce", controls: int | None = None) -> str:
    sha = _sha(art)
    controls = _CONTROLS.get(leg, 0) if controls is None else controls
    refused = controls if mode == "enforce" else 0
    would = controls if mode == "log-only" else 0
    return (
        f"ENGINE IDENTITY [{leg}]: candidate=yes kind=jar artifact={art} sha256={sha} "
        f"release_version=0.1.142 build_ref=x ownerless_write_mode={mode}\n"
        f"ENGINE OWNERLESS REFUSALS [{leg}]: candidate=yes sha256={sha} refused_total={refused} "
        f"would_refuse_total={would} log_lines={controls} log=storage_service_jar.log "
        f"controls={controls} mode={mode}\n"
    )


class _Battery:
    """A throwaway repo holding the real battery script, the real candidate_engine.py and stub legs."""

    def __init__(self, tmp_path: Path, *, manifest_rc: int = 0) -> None:
        self.root = tmp_path / "repo"
        self.out = tmp_path / "legout"
        self.ran = tmp_path / "ran"
        for d in (self.out, self.ran):
            d.mkdir()
        self.jar = tmp_path / "cand" / "engine.jar"
        self.native = tmp_path / "cand" / "nexus-service"
        self.jar.parent.mkdir()
        self.jar.write_bytes(b"jar-bytes")
        self.native.write_bytes(b"native-bytes")
        self.native.chmod(0o755)
        e2e = self.root / "tests" / "e2e"
        (e2e / "lib").mkdir(parents=True)
        shutil.copy(E2E / "release-battery.sh", e2e / "release-battery.sh")
        shutil.copy(E2E / "lib" / "candidate_engine.py", e2e / "lib" / "candidate_engine.py")
        # The battery sources this at start and refuses without a python >= 3.10 (nexus-u67ow).
        shutil.copy(E2E / "lib" / "python.sh", e2e / "lib" / "python.sh")
        # The real artifact_manifest.py recomputes this checkout's tree identity; a stub verifies or refuses.
        (e2e / "lib" / "artifact_manifest.py").write_text(
            "import sys\n" + ("print('{}')\n" if manifest_rc == 0 else
                              f"sys.stderr.write('tree identity mismatch (stub)\\n'); sys.exit({manifest_rc})\n")
        )
        (self.root / "src" / "nexus").mkdir(parents=True)
        (self.root / "src" / "nexus" / "engine_version.py").write_text(
            "REQUIRED_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 142)\n")
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"artifacts": {
            "jar": {"path": "jar/svc.jar", "sha256": _sha(self.jar)},
            "native": {"path": "native/nexus-service", "sha256": _sha(self.native)},
        }}))
        self._stub("tests/e2e/migration-rehearsal/build-artifacts.sh", (
            f'mkdir -p "$1/jar" "$1/native"; cp "{self.jar}" "$1/jar/svc.jar"; '
            f'cp "{self.native}" "$1/native/nexus-service"; cp "{manifest}" "$1/manifest.json"; '
            f'touch "{self.ran}/artifacts"; echo "ARTIFACTS BUILT"\n'))
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

    def serve_candidate(self, *, mode: str = "enforce") -> None:
        """Every engine leg prints the identity and refusals lines of the engine it serves."""
        for leg, which in _ENGINE_LEGS.items():
            art = self.jar if which == "jar" else self.native
            self.set_leg(leg, _engine_lines(leg, art, mode=mode) + _PASS_LINE[leg] + "\n", 0)

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


def test_a_clean_cut_run_with_every_leg_reading_its_own_control_passes(battery: _Battery) -> None:
    """The control case for everything below: stub legs that print what the real gates print, lsg's
    refusals line carrying its one deliberate ownerless write (controls=1 refused_total=1)."""
    battery.serve_candidate()
    r = battery.run("--cut")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert r.stdout.rstrip().splitlines()[-1] == "RELEASE BATTERY PASSED", r.stdout
    assert battery.ran_legs() >= set(_GROUP_AND_ALONE), "every leg ran"
    # (an assertion that the stub's own file says controls=1 used to sit here and could not fail: the
    # test wrote it. What reads the control is the battery, and the two tests below hold it to it.)


def test_a_cut_run_whose_lsg_reads_zero_for_its_own_control_is_red(battery: _Battery) -> None:
    """The positive-control half: lsg declares one control but its engine counted none."""
    battery.serve_candidate()
    art = battery.jar
    text = _engine_lines("lsg", art).replace("refused_total=1", "refused_total=0").replace("log_lines=1", "log_lines=0")
    battery.set_leg("lsg", text + _PASS_LINE["lsg"] + "\n", 0)
    r = battery.run("--cut")
    assert r.returncode == 1 and "RELEASE BATTERY FAILED: 1 red leg(s)" in r.stdout, (r.stdout, r.stderr)
    assert "VACUOUS in cut mode" in _row(r.stdout, "lsg")


def test_a_cut_run_whose_lsg_dropped_its_control_is_red_though_its_reading_is_clean(battery: _Battery) -> None:
    """nexus-0kmat critique S1. lsg declares controls=0 and reads 0/0/0: every per-line check agrees,
    and nothing in the battery has shown the counter can see a refusal. The battery must hold lsg, and only
    lsg, to declaring at least one control. Mutation (drop ``$cargs`` from cut_mode_vacuity, or the lsg arm of
    cut_leg_controls_args): the run ends RELEASE BATTERY PASSED."""
    battery.serve_candidate()
    battery.set_leg("lsg", _engine_lines("lsg", battery.jar, controls=0) + _PASS_LINE["lsg"] + "\n", 0)
    r = battery.run("--cut")
    assert r.returncode == 1 and "RELEASE BATTERY FAILED: 1 red leg(s)" in r.stdout, (r.stdout, r.stderr)
    assert "VACUOUS in cut mode" in _row(r.stdout, "lsg") and "controls>=1" in _row(r.stdout, "lsg")


def test_a_cut_run_whose_other_leg_bumps_its_own_declared_control_is_red(battery: _Battery) -> None:
    """Round 3 review M1 / N1l. dtok declares controls=1 and reads refused_total=1 log_lines=1: internally
    consistent, so the per-line check passes, and it is how a stray writer's refusal turns green. The
    battery's own table says every engine leg but lsg declares 0. Mutation (default arm of
    cut_leg_controls_args empty): the run ends RELEASE BATTERY PASSED."""
    battery.serve_candidate()
    battery.set_leg("dtok", _engine_lines("dtok", battery.jar, controls=1) + _PASS_LINE["dtok"] + "\n", 0)
    r = battery.run("--cut")
    assert r.returncode == 1 and "RELEASE BATTERY FAILED: 1 red leg(s)" in r.stdout, (r.stdout, r.stderr)
    assert "VACUOUS in cut mode" in _row(r.stdout, "dtok") and "controls<=0" in _row(r.stdout, "dtok")


def test_a_cut_run_refuses_the_knob_that_drops_lsgs_positive_control_before_any_leg_runs(battery: _Battery) -> None:
    """nexus-0kmat critique S1, the env path through the real script. NEXUS_GATE_NO_VECTOR_SMOKE=1 would
    drop lsg's deliberate ownerless write (and with it the control); cut mode refuses it up front, exit 2,
    no leg started. Mutation (delete the refusal): the legs run and the run ends on lsg's own anchor, so this
    test also pins WHERE it is refused. A non-cut run is untouched."""
    battery.serve_candidate()
    r = battery.run("--cut", env={"NEXUS_GATE_NO_VECTOR_SMOKE": "1"})
    assert r.returncode == 2 and "NEXUS_GATE_NO_VECTOR_SMOKE" in r.stderr, (r.stdout, r.stderr)
    assert battery.ran_legs() == set(), f"legs ran before the refusal: {battery.ran_legs()}"
    plain = battery.run(env={"NEXUS_GATE_NO_VECTOR_SMOKE": "1"})
    assert plain.returncode == 0, (plain.stdout, plain.stderr)


def test_the_mandatory_pins_leg_is_a_leg_and_its_red_is_a_red(battery: _Battery) -> None:
    """nexus-z0o2p.41. The GitHub-backed pins left lsg and run as battery leg `pins`; a red there ends the
    battery FAILED. Mutation (drop the define_leg): the stub never runs and the row is missing."""
    battery.serve_candidate()
    battery.set_leg("pins", "[pins] skipped over the budget\nMANDATORY PINS GATE FAILED\n", 1)
    r = battery.run("--cut")
    assert r.returncode == 1 and "pins" in battery.ran_legs(), (r.stdout, r.stderr)
    assert _row(r.stdout, "pins").startswith("FAILED")


def test_the_none_escape_in_cut_mode_ends_partial_never_final(battery: _Battery) -> None:
    """M11. NX_CANDIDATE_EXPECT_OWNERLESS_MODE=none drops the mode assert, so the battery must say so
    and end PARTIAL. Mutation (delete the CUT_NON_ENFORCE assignment, or the line that reads it in
    battery_verdict): the last line reads a bare RELEASE BATTERY PASSED and no banner is printed."""
    battery.serve_candidate()
    r = battery.run("--cut", env={"NX_CANDIDATE_EXPECT_OWNERLESS_MODE": "none"})
    assert r.returncode == 0, (r.stdout, r.stderr)
    last = r.stdout.rstrip().splitlines()[-1]
    assert "PARTIAL" in last and "NX_CANDIDATE_EXPECT_OWNERLESS_MODE=none" in last and "not a release verdict" in last, last
    assert "CUT MODE WARNING" in r.stderr


def test_a_red_leg_makes_the_battery_exit_nonzero(battery: _Battery) -> None:
    """M12. Mutation (``exit $?`` -> ``exit 0`` after battery_verdict): the verdict line says FAILED
    and the process exits 0, which a CI step or a wrapper reads as a pass."""
    battery.serve_candidate()
    battery.set_leg("hookskew", "HOOK-CLI SKEW GATE FAILED: a hook blocked\n", 1)
    r = battery.run("--cut")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "RELEASE BATTERY FAILED: 1 red leg(s)" in r.stdout
    assert _row(r.stdout, "hookskew").startswith("FAILED")


def test_an_acknowledged_engine_lag_is_named_counted_and_not_red(battery: _Battery) -> None:
    """M10. Outside cut mode a red whose failing step carries EngineOlderThanClientError reads
    EXPECTED-LAG when the ack names this engine. The report loop must treat that state as non-red.
    Mutation (drop EXPECTED-LAG from the loop's non-red case): the row still reads EXPECTED-LAG but
    the battery counts it red and ends FAILED."""
    battery.set_leg("mvv", ("EngineOlderThanClientError: asked for metadata_merge but the response did "
                            "not echo it. The engine is older than this client.\n"
                            "FRESH-INSTALL MVV FAILED: nx index\n"), 1)
    r = battery.run("--expected-engine-lag", "nexus-x@0.1.142")
    assert _row(r.stdout, "mvv").startswith("EXPECTED-LAG"), r.stdout
    assert r.returncode == 0, (r.stdout, r.stderr)
    last = r.stdout.rstrip().splitlines()[-1]
    assert last.startswith("RELEASE BATTERY PASSED (PARTIAL:") and "1 leg(s) EXPECTED-LAG(nexus-x)" in last, last
    # Without the ack the same red is a red.
    plain = battery.run()
    assert plain.returncode == 1 and _row(plain.stdout, "mvv").startswith("FAILED")


def test_an_unresolvable_candidate_stops_every_engine_leg(tmp_path: Path) -> None:
    """M13. When the artifacts manifest does not verify, cut_resolve_candidate fails and the battery
    must not start the gate group: every leg after it would provision the pinned published engine.
    Mutation (drop ``LEG0_ABORT=1`` from the ``||`` handler): the verdict still reds, but the legs
    run, which this asserts through the stubs' own markers."""
    bat = _Battery(tmp_path, manifest_rc=1)
    bat.serve_candidate()
    r = bat.run("--cut")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "CUT MODE: no candidate engine: the artifacts manifest does not verify" in r.stdout + r.stderr
    assert "RELEASE BATTERY FAILED" in r.stdout
    assert bat.ran_legs() <= {"artifacts", "preflight"}, f"engine legs ran: {bat.ran_legs()}"
    assert _row(r.stdout, "mvv").startswith("NOT RUN (cut-mode abort)")
