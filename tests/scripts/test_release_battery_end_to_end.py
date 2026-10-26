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
    "dual": "DUAL-EMBEDDER GATE PASSED",
    "resolve": "CONSTRAINTS RESOLVE GATE PASSED",
}
_GROUP = ("lsg", "pkgup", "mvv", "dual", "resolve")

#: nexus-rbmja.3: the two legs only ``--plugin-only`` defines (the default battery has neither).
_PASS_LINE.update({
    "skew": "HOOK-CLI SKEW GATE PASSED",
    "lockstep": "PLUGIN LOCKSTEP TESTS PASSED",
})
_PLUGIN_GROUP = ("skew", "lockstep")
_TAG = "v7.86.3"
_ELIGIBLE = "PLUGIN-ONLY ELIGIBILITY PASSED"
#: What a skipped leg claims, and no more (nexus-rbmja.5): the diff from the tag moves nothing the
#: wheel or engine are built from but the version strings; tests/ and the plugin trees do differ.
_SKIP_STATUS = f"SKIPPED(plugin-only: wheel and engine identical to {_TAG} but for the version)"


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
            ("tests/e2e/dual-embedder-gate.sh", "dual"),
            ("tests/e2e/constraints-resolve-gate.sh", "resolve"),
            ("tests/e2e/hook-cli-skew/run.sh", "skew"),
            ("tests/e2e/plugin-lockstep-tests.sh", "lockstep"),
        ):
            self._stub(rel, f'k={key}\n{self._leg_body()}')
        # The eligibility gate (nexus-rbmja.1) is a python script the battery runs with the resolved
        # interpreter; the stub records its argv, touches ``ran/elig`` and answers from ``legout``.
        eligibility = self.root / "scripts" / "check_plugin_only_eligibility.py"
        eligibility.parent.mkdir(parents=True)
        eligibility.write_text(
            "import pathlib, sys\n"
            f"out = pathlib.Path({str(self.out)!r}); ran = pathlib.Path({str(self.ran)!r})\n"
            "(ran / 'elig').touch()\n"
            "(out / 'elig.args').write_text(' '.join(sys.argv[1:]))\n"
            "sys.stdout.write((out / 'elig.out').read_text())\n"
            "sys.exit(int((out / 'elig.rc').read_text()))\n")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
             "commit", "-q", "-m", "fixture"], cwd=self.root, check=True)
        for leg, line in _PASS_LINE.items():
            self.set_leg(leg, line + "\n", 0)
        self.set_leg("elig", f"{_ELIGIBLE}\n", 0)

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
    PASSED. The battery is exactly the preflight plus the four legs."""
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
    while the four legs still ran. Mutation (abort the battery on a preflight red): the legs never
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


def test_a_red_dual_embedder_gate_fails_the_battery_and_a_green_one_is_required_to_pass(battery: _Battery) -> None:
    """nexus-oc2bk (RDR-210 P5.2). The dual-embedder gate is a battery leg: a gate that fails (an absent
    Voyage key included, which the gate reports as a failure and never a skip) makes the battery FAILED,
    and ``--only dual`` runs it alone. Mutation (drop the leg from the battery): the first run never
    runs it and the second names an unknown leg."""
    battery.set_leg("dual", "dual-embedder gate: FAIL no Voyage API key\n", 1)
    r = battery.run()
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "RELEASE BATTERY FAILED: 1 red leg(s)" in r.stdout
    assert _row(r.stdout, "dual").startswith("FAILED") and "dual" in battery.ran_legs()
    battery.set_leg("dual", _PASS_LINE["dual"] + "\n", 0)
    r = battery.run("--only", "dual")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _row(r.stdout, "dual").startswith("PASSED") and _row(r.stdout, "lsg").startswith("SKIPPED"), r.stdout


def test_a_leg_that_exits_zero_without_its_verdict_line_is_missing_not_passed(battery: _Battery) -> None:
    """nexus-f2g8u. Mutation (treat rc 0 as a pass): a leg that printed nothing reads PASSED."""
    battery.set_leg("lsg", "no verdict here\n", 0)
    r = battery.run()
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _row(r.stdout, "lsg").startswith("MISSING")


# ── nexus-rbmja.3: --plugin-only vX.Y.Z ─────────────────────────────────────────────────────────────
_SKIPPED_LEGS = _GROUP  # lsg, pkgup, mvv, dual, resolve: shown SKIPPED, never omitted


def _plugin(battery: _Battery, *args: str) -> subprocess.CompletedProcess[str]:
    return battery.run("--plugin-only", _TAG, *args)


@pytest.mark.parametrize(
    ("text", "rc"),
    [
        ("INELIGIBLE: src/nexus/x.py [M] outside the plugin channel\nPLUGIN-ONLY ELIGIBILITY FAILED\n", 1),
        ("PLUGIN-ONLY ELIGIBILITY UNVERIFIED\n", 2),
        # exit 0 with no verdict line is not ELIGIBLE (a gate that says nothing proves nothing)
        ("", 0),
        ("some other text\n", 0),
    ],
    ids=["ineligible", "unverified", "silent-exit-0", "no-verdict-line"],
)
def test_plugin_only_refuses_before_any_leg_unless_eligible(battery: _Battery, text: str, rc: int) -> None:
    """(a) and the silent-pass edge. Mutation (drop the eligibility refusal): the battery runs the
    preflight, skew and lockstep legs on a tree the gate refused, and exits 0."""
    battery.set_leg("elig", text, rc)
    r = _plugin(battery)
    assert r.returncode == 2, (r.stdout, r.stderr)
    last = [ln for ln in text.splitlines() if ln.strip()][-1:] or [f"(no verdict line; eligibility exit {rc})"]
    assert f"BATTERY REFUSED: plugin-only mode: {last[0]}" in r.stderr, r.stderr
    assert battery.ran_legs() == {"elig"}, "no leg may start on a refused tree"
    assert "RELEASE BATTERY PASSED" not in r.stdout


def test_plugin_only_asks_the_gate_about_the_named_tag(battery: _Battery) -> None:
    """Mutation (pass the wrong tag or none): the gate diffs from the wrong base."""
    _plugin(battery)
    assert (battery.out / "elig.args").read_text() == f"--base {_TAG}"


@pytest.mark.parametrize("spelling", [("--plugin-only", "7.86.3"), ("--plugin-only", "main"), ("--plugin-only",)])
def test_plugin_only_wants_a_client_tag(battery: _Battery, spelling: tuple[str, ...]) -> None:
    """The argument is the last client tag, vX.Y.Z; anything else is a usage error before the gate."""
    r = battery.run(*spelling)
    assert r.returncode == 2 and battery.ran_legs() == set(), (r.stdout, r.stderr)


def test_plugin_only_skips_the_heavy_legs_by_design_and_runs_skew_and_lockstep(battery: _Battery) -> None:
    """(b) Mutation (run the five legs anyway, or omit them from the table): the ran set grows or the
    SKIPPED rows vanish. Mutation (bare closing line): a plugin-only run reads as a full release verdict."""
    r = _plugin(battery)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert battery.ran_legs() == {"elig", "preflight", *_PLUGIN_GROUP}, "only preflight, skew, lockstep run"
    for leg in _SKIPPED_LEGS:
        assert _row(r.stdout, leg).startswith(_SKIP_STATUS), r.stdout
    for leg in ("preflight", *_PLUGIN_GROUP):
        assert _row(r.stdout, leg).startswith("PASSED"), r.stdout
    closing = r.stdout.rstrip().splitlines()[-1]
    assert closing == f"RELEASE BATTERY PASSED (PLUGIN-ONLY vs {_TAG}: 5 leg(s) skipped by design)", closing


@pytest.mark.parametrize("leg", _PLUGIN_GROUP)
def test_a_red_plugin_only_leg_fails_the_battery(battery: _Battery, leg: str) -> None:
    """(c) Mutation (count only the default legs as red): a red skew or lockstep leg exits 0."""
    battery.set_leg(leg, f"{_PASS_LINE[leg].replace('PASSED', 'FAILED')}: boom\n", 1)
    r = _plugin(battery)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "RELEASE BATTERY FAILED: 1 red leg(s)" in r.stdout
    assert _row(r.stdout, leg).startswith("FAILED")
    assert "PASSED (PLUGIN-ONLY" not in r.stdout


@pytest.mark.parametrize("leg", _PLUGIN_GROUP)
def test_a_plugin_only_leg_that_exits_zero_without_its_verdict_line_is_missing(battery: _Battery, leg: str) -> None:
    """(d) nexus-f2g8u on the new legs. Mutation (give skew or lockstep a verdict regex that matches
    anything): a leg that printed nothing reads PASSED."""
    battery.set_leg(leg, "nothing to see\n", 0)
    r = _plugin(battery)
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _row(r.stdout, leg).startswith("MISSING")


def test_plan_lists_the_plugin_only_statuses(battery: _Battery) -> None:
    """(e) Mutation (skip the status assignment, or define the legs after the plan prints)."""
    r = _plugin(battery, "--plan")
    assert r.returncode == 0, (r.stdout, r.stderr)
    plan = {m.group(1): m.group(3) for m in re.finditer(r"^PLAN (\S+) (\S+) (.*)$", r.stdout, re.M)}
    assert list(plan) == ["preflight", *_GROUP, *_PLUGIN_GROUP], plan
    assert plan["preflight"] == "PENDING"
    for leg in _SKIPPED_LEGS:
        assert plan[leg] == _SKIP_STATUS, plan
    for leg in _PLUGIN_GROUP:
        assert plan[leg] == "PENDING", plan
    assert battery.ran_legs() == {"elig"}, "--plan runs the gate and no leg"


def test_the_default_battery_has_no_skew_or_lockstep_leg(battery: _Battery) -> None:
    """(f) Mutation (define skew and lockstep unconditionally): the default battery grows two legs and
    the full-release leg set changes."""
    r = battery.run()
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert battery.ran_legs() == {"preflight", *_GROUP}, "no eligibility gate, skew or lockstep in the default battery"
    assert not re.search(r"^(skew|lockstep)\s", r.stdout, re.M), r.stdout
    assert r.stdout.rstrip().splitlines()[-1] == "RELEASE BATTERY PASSED"
    plan = battery.run("--plan").stdout
    assert "skew" not in plan and "lockstep" not in plan and "SKIPPED" not in plan, plan
    r = battery.run("--only", "skew")
    assert r.returncode == 2 and "unknown leg 'skew'" in r.stderr, (r.stdout, r.stderr)


def test_only_composes_with_plugin_only_and_the_verdict_names_both(battery: _Battery) -> None:
    """Mutation (let --only re-enable a plugin-skipped leg): ``--only mvv`` would run the heavy leg
    the mode exists to skip."""
    r = _plugin(battery, "--only", "skew")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert battery.ran_legs() == {"elig", "preflight", "skew"}
    assert _row(r.stdout, "lockstep").startswith("SKIPPED(--only)")
    closing = r.stdout.rstrip().splitlines()[-1]
    assert closing.startswith("RELEASE BATTERY PASSED (PARTIAL:") and "PLUGIN-ONLY" in closing, closing
    r = _plugin(battery, "--only", "mvv")
    assert "mvv" not in battery.ran_legs() and _row(r.stdout, "mvv").startswith("SKIPPED(plugin-only"), r.stdout


# ── nexus-rbmja.5: the phase-1 review findings ────────────────────────────────────────────────────────


def test_the_skipped_legs_claim_only_what_the_gate_proved(battery: _Battery) -> None:
    """Mutation (restore "inputs identical to"): tests/, the plugin trees and the version strings differ
    from the tag, so that wording is false; the table must say wheel and engine, but for the version."""
    r = _plugin(battery)
    assert "inputs identical" not in r.stdout, r.stdout
    for leg in _SKIPPED_LEGS:
        assert _row(r.stdout, leg).startswith(_SKIP_STATUS), r.stdout


def test_plugin_only_refuses_a_nonzero_gate_exit_even_with_a_passed_last_line(battery: _Battery) -> None:
    """The gate is ELIGIBLE only on exit 0 AND its verdict line. Mutation (judge by the last line
    alone): a gate that printed PASSED and exited 1 runs the battery."""
    battery.set_leg("elig", f"{_ELIGIBLE}\n", 1)
    r = _plugin(battery)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert f"BATTERY REFUSED: plugin-only mode: {_ELIGIBLE}" in r.stderr
    assert battery.ran_legs() == {"elig"}


def test_the_equals_spelling_of_plugin_only_is_the_same_mode(battery: _Battery) -> None:
    """Mutation (drop the ``--plugin-only=*`` arm): the spelling is an unknown argument, exit 2."""
    r = battery.run(f"--plugin-only={_TAG}")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (battery.out / "elig.args").read_text() == f"--base {_TAG}"
    assert battery.ran_legs() == {"elig", "preflight", *_PLUGIN_GROUP}
    assert r.stdout.rstrip().splitlines()[-1].startswith(f"RELEASE BATTERY PASSED (PLUGIN-ONLY vs {_TAG}")
    assert battery.run("--plugin-only=7.86.3").returncode == 2


def test_the_banner_names_the_mode_and_the_base_and_only_in_that_mode(battery: _Battery) -> None:
    """Mutation (drop the banner suffix): a log opens without saying it is a plugin-only run."""
    r = _plugin(battery)
    banner = next(ln for ln in r.stdout.splitlines() if ln.startswith("RELEASE BATTERY: work="))
    assert banner.endswith(f"mode=plugin-only base={_TAG}"), banner
    banner = next(ln for ln in battery.run().stdout.splitlines() if ln.startswith("RELEASE BATTERY: work="))
    assert "mode=" not in banner, banner


def test_the_gate_output_is_echoed_into_the_log_on_a_pass(battery: _Battery) -> None:
    """The examined-count line is the evidence of what was checked; it must reach the log. Mutation
    (drop the ``printf`` of the gate's output after a pass)."""
    battery.set_leg("elig", f"examined {_TAG}..HEAD: 3 changed paths (3 channel)\n{_ELIGIBLE}\n", 0)
    r = _plugin(battery)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert f"examined {_TAG}..HEAD: 3 changed paths (3 channel)" in r.stdout


def test_the_gate_output_reaches_stderr_on_a_refusal(battery: _Battery) -> None:
    """Mutation (drop the echo before the refusal): the operator sees a verdict and no offenders."""
    battery.set_leg("elig", "INELIGIBLE: src/nexus/x.py [M] outside the plugin channel\nPLUGIN-ONLY ELIGIBILITY FAILED: 1 problem(s)\n", 1)
    r = _plugin(battery)
    assert r.returncode == 2
    assert "INELIGIBLE: src/nexus/x.py [M] outside the plugin channel" in r.stderr


def _dirty(battery: _Battery, how: str) -> None:
    if how == "modified":
        (battery.root / "scripts" / "check_plugin_only_eligibility.py").write_text("# edited\n")
    else:
        (battery.root / "untracked.txt").write_text("x\n")


@pytest.mark.parametrize("how", ["modified", "untracked"])
def test_plugin_only_refuses_a_dirty_tree_before_the_gate_or_any_leg(battery: _Battery, how: str) -> None:
    """Eligibility reads committed HEAD and the legs run the working tree. Mutation (drop the
    porcelain check): the gate passes on HEAD and the legs run uncommitted bytes."""
    _dirty(battery, how)
    r = _plugin(battery)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "BATTERY REFUSED: plugin-only mode: the working tree has changes or untracked paths" in r.stderr
    assert ("check_plugin_only_eligibility.py" if how == "modified" else "untracked.txt") in r.stderr
    assert battery.ran_legs() == set(), "neither the gate nor any leg may start on a dirty tree"


def test_a_dirty_tree_does_not_stop_the_full_battery(battery: _Battery) -> None:
    """The refusal is a plugin-only rule: the full battery has its own dirty-tree banner and a
    developer's sweep must still run. Mutation (move the check out of the plugin-only branch)."""
    _dirty(battery, "untracked")
    r = battery.run()
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert battery.ran_legs() == {"preflight", *_GROUP}


def test_skip_preflight_does_not_compose_with_plugin_only(battery: _Battery) -> None:
    """The preflight is the only full-tree gate the mode keeps; without it the closing line would
    read like a full-preflight run. Mutation (drop the refusal)."""
    r = _plugin(battery, "--skip-preflight")
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "--skip-preflight does not compose with --plugin-only" in r.stderr
    assert battery.ran_legs() == set()
    r = battery.run("--skip-preflight", "--plugin-only", _TAG)  # either order
    assert r.returncode == 2 and battery.ran_legs() == set(), (r.stdout, r.stderr)
    r = battery.run("--skip-preflight", "--only", "mvv")  # the full battery still allows it
    assert r.returncode == 0, (r.stdout, r.stderr)
