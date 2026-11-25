# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-clfse.5: the stack's lab entry conditions list every check that otherwise runs only in CI.

``scripts/lab-entry-conditions.toml`` names, for each check that only a CI workflow runs (Service CI's
integration-ran assert, the native trip-wire's embedded-resources check, the pipefail lint in the lint bucket,
and the other post-suite asserts), the exact lab command a lane runs before it stacks, or the reason the check
is left out. Before this list, four defects reached develop and were caught only by the post-push CI run
(2026-11-24, 9c8bc0d04b and 05c2704080).

These tests parse ``.github/workflows/ci.yml`` and ``service-ci.yml`` and fail when the list and CI disagree:

* a listed check whose step is gone from CI (or no longer runs its script),
* a step that runs a repo script the list does not mention, as a check or as an exclusion with a reason,
* an exclusion whose script CI no longer runs (a stale reason is how the next gap hides),
* a lab command that breaks the lab-host rules (a bare ``./mvnw``, a pytest run not sent through
  ``lab-remote``, ``lab-6c18g``) or names a flag, script or test file that does not exist.

The sweep must find the named checks and a floor of script references; a pass over nothing is a failure.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "scripts" / "lab-entry-conditions.toml"
WORKFLOWS = ("ci.yml", "service-ci.yml")

# The checks the bead names; the registry must carry at least these.
REQUIRED_IDS = {
    "integration-group-ran",
    "native-embedded-resources",
    "pipefail-lint",
    "upgrade-boot-writes-leg-a",
}

# Scripts a CI run block can invoke. tests/e2e/*.sh are gates CI calls directly.
SCRIPT_REF = re.compile(r"(?<![\w/])(?:scripts|tests/e2e)/[\w./-]+\.(?:py|sh)")
# Floor on the references the sweep finds in the two workflows (30 on 2026-11-25). A parse that finds a
# handful has read the wrong keys.
SCRIPT_REF_FLOOR = 25

LAB_CLASSES = {"lab-s", "lab-m", "lab-l", "lab-xl", "lab-4c8g"}


def _registry() -> dict:
    with REGISTRY.open("rb") as fh:
        return tomllib.load(fh)


def _steps(workflow: str) -> list[tuple[str, str, str]]:
    """(job id, step name, run text) for every step of one workflow."""
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text())
    return [
        (job_id, str(step.get("name", "")), str(step.get("run") or ""))
        for job_id, job in doc["jobs"].items()
        for step in job.get("steps", [])
    ]


def _ci_script_refs() -> dict[str, set[str]]:
    """script path -> the workflows whose run blocks invoke it."""
    refs: dict[str, set[str]] = {}
    for wf in WORKFLOWS:
        for _job, _name, run in _steps(wf):
            for ref in SCRIPT_REF.findall(run):
                refs.setdefault(ref, set()).add(wf)
    return refs


def _all_commands(reg: dict) -> list[tuple[str, str]]:
    out = []
    for kind in ("prerequisite", "check"):
        for entry in reg.get(kind, []):
            out.extend((f"{kind}:{entry['id']}", cmd) for cmd in entry["command"])
    return out


def test_registry_has_the_named_checks_and_unique_ids() -> None:
    reg = _registry()
    ids = [c["id"] for c in reg["check"]] + [p["id"] for p in reg["prerequisite"]]
    assert len(ids) == len(set(ids)), "duplicate ids"
    missing = REQUIRED_IDS - {c["id"] for c in reg["check"]}
    assert not missing, f"the registry lost named checks: {sorted(missing)}"
    for c in reg["check"]:
        assert c["twin"] in {"exact", "partial", "conditional"}, c["id"]
        assert c["command"], f"{c['id']} has no lab command"
        if c["twin"] != "exact":
            assert c.get("gap", "").strip(), f"{c['id']} is a {c['twin']} twin and must say what it does not cover"
        for pre in c.get("after", []):
            assert pre in {p["id"] for p in reg["prerequisite"]}, f"{c['id']} runs after unknown {pre}"


def test_every_ci_check_is_still_a_ci_step() -> None:
    """The listed CI step exists in the named job and still runs what the entry says it runs."""
    reg = _registry()
    ci_checks = [c for c in reg["check"] if c.get("workflow")]
    assert len(ci_checks) >= 5, "non-vacuity: the registry names fewer CI checks than the bead's list"
    for c in ci_checks:
        match = [
            run
            for job, name, run in _steps(c["workflow"])
            if job == c["job"] and name == c["step"]
        ]
        assert len(match) == 1, f"{c['id']}: CI has {len(match)} step(s) '{c['step']}' in job {c['job']} of {c['workflow']}"
        assert c["run_contains"] in match[0], (
            f"{c['id']}: the CI step '{c['step']}' no longer runs {c['run_contains']!r}"
        )


def test_every_script_ci_invokes_is_a_check_or_an_excluded_entry() -> None:
    reg = _registry()
    refs = _ci_script_refs()
    assert len(refs) >= SCRIPT_REF_FLOOR, f"swept only {len(refs)} script references: the parse is off"
    listed = {c["script"] for c in reg["check"] if c.get("script")}
    excluded = {e["script"] for e in reg["excluded"]}
    assert not (listed & excluded), f"in both lists: {sorted(listed & excluded)}"
    unlisted = sorted(set(refs) - listed - excluded)
    assert not unlisted, (
        "CI runs these scripts and scripts/lab-entry-conditions.toml names neither a lab twin nor an "
        f"exclusion with a reason for them: {unlisted}"
    )


def test_exclusions_carry_a_reason_and_are_not_stale() -> None:
    reg = _registry()
    refs = _ci_script_refs()
    assert reg["excluded"], "non-vacuity: no exclusions listed"
    for e in reg["excluded"]:
        assert e["reason"].strip(), f"{e['script']} is excluded without a reason"
        assert e["script"] in refs, f"{e['script']} is excluded but no CI step runs it any more: delete the entry"
        assert (ROOT / e["script"]).is_file(), f"{e['script']} does not exist"
    for c in reg["check"]:
        if c.get("script"):
            assert c["script"] in refs, f"{c['id']}: CI no longer runs {c['script']}"
            assert (ROOT / c["script"]).is_file(), f"{c['script']} does not exist"


def test_lab_commands_follow_the_lab_host_rules() -> None:
    """AGENTS.md rules 12-13 and the stack checklist's item 0/1/2 forms."""
    reg = _registry()
    cmds = _all_commands(reg)
    assert len(cmds) >= 8, "non-vacuity: too few lab commands to be the stack's entry conditions"
    for where, cmd in cmds:
        assert "lab-6c18g" not in cmd, f"{where}: lab-6c18g is CI's class, never a hand run: {cmd}"
        for cls in re.findall(r"-c (lab-[\w-]+)", cmd):
            assert cls in LAB_CLASSES, f"{where}: unknown lab class {cls}"
        if re.search(r"(?<![\w-])(\./)?mvnw?\b", cmd) and "mvnw-leased.sh" not in cmd:
            pytest.fail(f"{where}: an engine build goes through scripts/mvnw-leased.sh --lab-run, got: {cmd}")
        if "mvnw-leased.sh" in cmd:
            assert "--lab-run" in cmd, f"{where}: mvnw-leased.sh on a pool host takes --lab-run <class>: {cmd}"
            assert "lab-remote" not in cmd, f"{where}: engine builds never go through lab-remote: {cmd}"
        if "build-gate-jar.sh" in cmd and "lab-remote" not in cmd:
            assert "--lab-run" in cmd, f"{where}: build-gate-jar.sh on a pool host takes --lab-run <class>: {cmd}"
        if re.search(r"\buv run pytest\b", cmd):
            assert "lab-remote" in cmd, f"{where}: a pytest hand run goes through lab-remote: {cmd}"
            assert "watched_pytest.py" in cmd, f"{where}: a remote pytest run goes through watched_pytest.py: {cmd}"


def test_lab_commands_name_things_that_exist() -> None:
    """Every repo path, flag and Maven test class a lab command names is real."""
    reg = _registry()
    for where, cmd in _all_commands(reg):
        for path in re.findall(r"(?:scripts|tests)/[\w./-]+\.(?:py|sh)", cmd):
            assert (ROOT / path).is_file(), f"{where}: names {path}, which does not exist"
        for cls in re.findall(r"-Dtest=([\w,]+)", cmd):
            for name in cls.split(","):
                hits = list((ROOT / "service" / "src" / "test").rglob(f"{name}.java"))
                assert hits, f"{where}: -Dtest={name} matches no test class"
        # --flags of a repo script the command runs: each must appear in that script's source.
        scripts = re.findall(r"(?<![\w/])scripts/[\w./-]+\.(?:py|sh)", cmd)
        for script in scripts:
            text = (ROOT / script).read_text()
            segment = cmd[cmd.index(script):]
            segment = re.split(r"\s(?:&&|\|\||;|\|)\s|\sscripts/", segment, maxsplit=1)[0]
            for flag in re.findall(r"(?<=\s)(--[a-z][\w-]*)", segment):
                assert flag in text, f"{where}: {script} has no {flag} option"


def test_a_lint_marked_twin_runs_a_file_the_lint_bucket_selects() -> None:
    """`pytest -m lint <file>` runs nothing unless the file carries the lint mark."""
    reg = _registry()
    seen = 0
    for where, cmd in _all_commands(reg):
        if "-m lint" not in cmd:
            continue
        for path in re.findall(r"tests/[\w./-]+\.py", cmd):
            seen += 1
            assert re.search(r"mark\.lint", (ROOT / path).read_text()), f"{where}: {path} is not lint-marked"
    assert seen >= 1, "non-vacuity: no lint-marked twin found"


def test_ci_runs_the_pipefail_lint_through_the_lint_bucket() -> None:
    """The pipefail lint is CI-only because the default addopts deselect `lint`; pin that CI still selects it."""
    runs = [run for job, _name, run in _steps("ci.yml") if job == "test-lint"]
    assert any("pytest -m lint" in r for r in runs), "ci.yml's test-lint job no longer runs `pytest -m lint`"
    assert "pytest.mark.lint" in (ROOT / "tests/test_pipefail_early_exit_consumer_lint.py").read_text()


def test_the_leg_a_twin_matches_the_engine_release_skill_and_the_real_cache_switch() -> None:
    reg = _registry()
    leg = next(c for c in reg["check"] if c["id"] == "upgrade-boot-writes-leg-a")
    assert not leg.get("workflow"), "leg A is a release-skill gate, not a CI step"
    text = "\n".join(leg["command"])
    assert "NX_GATE_JAR_CACHE=off" in text, "leg A on a stack tip builds the gate jar with the cache off"
    assert "NX_GATE_JAR_CACHE" in (ROOT / "scripts/build-gate-jar.sh").read_text(), (
        "NX_GATE_JAR_CACHE is not the switch scripts/build-gate-jar.sh reads"
    )
    skill = (ROOT / ".claude/skills/engine-release/SKILL.md").read_text()
    for token in ("scripts/upgrade_boot_writes.py", "--ceiling-boot 67108864", "--ceiling-reboot 67108864", "--prev-client"):
        assert token in text, f"leg A twin lost {token}"
        assert token in skill, f"the engine-release skill's Step 3h no longer carries {token}"


def test_the_pointer_in_test_running_names_the_registry() -> None:
    doc = (ROOT / "docs/agents/test-running.md").read_text()
    assert "scripts/lab-entry-conditions.toml" in doc
