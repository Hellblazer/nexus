# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-clfse.5: the stack's lab entry conditions list every check that otherwise runs only in CI.

``scripts/lab-entry-conditions.toml`` names, for each check that only a CI workflow runs (Service CI's
integration-ran assert, the native trip-wire's embedded-resources check, the CA-3 bundle smoke, the pipefail lint in
the lint bucket, and the other post-suite asserts), the lab command a lane runs before it stacks, or the reason the
check is left out. Before this list, four defects reached develop and were caught only by the post-push CI run
(2026-11-24, 9c8bc0d04b and 05c2704080).

These tests parse ``.github/workflows/ci.yml``, ``service-ci.yml`` and ``.github/actions/*`` and fail when the list
and CI disagree:

* a listed check whose step is gone from CI, or whose invocation (arguments included) no longer reads as the list says;
* a step that runs a repo script (``scripts/``, ``tests/e2e/``, a ``tests/`` file or ``python -m tests.x``) the list
  names neither as a check nor as an exclusion with a reason, and an exclusion that points at a step that does not
  run the script;
* a path-gate claim the real ``dorny/paths-filter`` globs contradict (a "path-gated" exclusion or a check whose
  trigger the list states);
* a lab command that breaks the lab-host rules (a bare ``./mvnw``, a pytest run outside ``lab-remote`` or
  ``lab-run``, a status file under an ignored directory, an unset ``$PREV``, no ``uv sync`` before ``uv run``,
  ``lab-6c18g`` or an unmarked ``lab-l``) or names a flag, script or test file that does not exist.

SWEEP LIMITS, stated so the list is not trusted past them. The sweep is keyed on repo scripts and test files: a CI
step that runs none (``uvx ruff``, an inline pytest, a count floor) is covered only when ``[[script_less]]`` names it,
and a new one can appear unlisted. Only ``ci.yml``, ``service-ci.yml`` and the composite actions are swept; the
release, engine-release, nightly and Windows workflows are gates of their own, run at the cut.
"""
from __future__ import annotations

import fnmatch
import glob
import re
import subprocess
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
    "ca3-liquibase-bundle-smoke",
}

# A repo script or test file a CI run block invokes: scripts/x, ./scripts/x, ../scripts/x and
# $GITHUB_WORKSPACE/scripts/x (anything not preceded by a word character, and not the tail of tests/scripts/x),
# tests/e2e/x, a tests/ python file, and `python -m tests.mod` / `-m scripts.mod`.
SCRIPT_REF = re.compile(
    r"(?<![\w])(?<!tests/)(?:scripts|tests/e2e)/[\w./-]+\.(?:py|sh)"
    r"|(?<![\w/])tests/[\w./-]+\.py"
    r"|(?<=-m )(?:tests|scripts)\.[\w.]+"
)
# Floor on the references the sweep finds (37 on 2026-11-25). A parse that finds a handful has read the wrong keys.
SCRIPT_REF_FLOOR = 30

LAB_CLASSES = {"lab-s", "lab-m", "lab-4c8g"}
FULL_SUITE_CLASSES = {"lab-l", "lab-xl"}


def _registry() -> dict:
    with REGISTRY.open("rb") as fh:
        return tomllib.load(fh)


def _norm(run: str) -> str:
    """A run block as one line: comment lines dropped, backslash continuations joined, whitespace collapsed."""
    lines = [ln for ln in run.splitlines() if not ln.strip().startswith("#")]
    return re.sub(r"\s+", " ", re.sub(r"\\\n", " ", "\n".join(lines))).strip()


def _workflow_doc(workflow: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text())


def _steps(workflow: str) -> list[tuple[str, str, str]]:
    """(job id, step name, normalised run text) for every step of one workflow."""
    return [
        (job_id, str(step.get("name", "")), _norm(str(step.get("run") or "")))
        for job_id, job in _workflow_doc(workflow)["jobs"].items()
        for step in job.get("steps", [])
    ]


def _action_steps() -> list[tuple[str, str, str]]:
    out = []
    for path in sorted(glob.glob(str(ROOT / ".github" / "actions" / "*" / "action.yml"))):
        doc = yaml.safe_load(Path(path).read_text())
        for step in doc.get("runs", {}).get("steps", []):
            out.append((Path(path).parent.name, str(step.get("name", "")), _norm(str(step.get("run") or ""))))
    return out


def _ci_script_refs() -> dict[str, set[str]]:
    """script or test path -> the workflows (or actions) whose run blocks invoke it."""
    refs: dict[str, set[str]] = {}
    sources = [(wf, _steps(wf)) for wf in WORKFLOWS] + [("actions", _action_steps())]
    for label, steps in sources:
        for _job, _name, run in steps:
            for m in SCRIPT_REF.finditer(run):
                ref = m.group(0)
                if "/" not in ref:  # python -m tests.mod -> tests/mod.py
                    ref = ref.replace(".", "/") + ".py"
                refs.setdefault(ref, set()).add(label)
    return refs


def _step_run(workflow: str, job: str, step: str) -> list[str]:
    return [run for j, name, run in _steps(workflow) if j == job and name == step]


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """picomatch-style glob as dorny/paths-filter reads it: ``**`` crosses directories, ``*`` does not."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z")


def _filter_globs(workflow: str, job: str, name: str) -> list[str]:
    """The globs of the named dorny/paths-filter filter in one job."""
    for step in _workflow_doc(workflow)["jobs"][job].get("steps", []):
        if str(step.get("uses", "")).startswith("dorny/paths-filter"):
            filters = yaml.safe_load(step["with"]["filters"])
            if name in filters:
                return list(filters[name])
    pytest.fail(f"{workflow} job {job} has no paths-filter filter named {name!r}")


def _matches(globs: list[str], path: str) -> bool:
    return any(_glob_regex(g).match(path) for g in globs)


def _entries(reg: dict) -> list[tuple[str, dict]]:
    return [("prerequisite", e) for e in reg["prerequisite"]] + [("check", e) for e in reg["check"]]


def _all_commands(reg: dict) -> list[tuple[str, str, dict]]:
    return [(f"{kind}:{e['id']}", cmd, e) for kind, e in _entries(reg) for cmd in e["command"]]


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


def test_every_ci_step_the_list_names_is_still_there_and_runs_what_it_says() -> None:
    """Checks and prerequisites that carry a CI mapping: the step exists once, its run text still holds the
    pinned invocation, and a script the entry names is still run by that step."""
    reg = _registry()
    mapped = [(k, e) for k, e in _entries(reg) if e.get("workflow")]
    assert len([1 for k, _ in mapped if k == "check"]) >= 8, "non-vacuity: fewer CI checks than the bead's list"
    for _kind, e in mapped:
        match = _step_run(e["workflow"], e["job"], e["step"])
        assert len(match) == 1, f"{e['id']}: CI has {len(match)} step(s) '{e['step']}' in job {e['job']} of {e['workflow']}"
        assert _norm(e["run_contains"]) in match[0], (
            f"{e['id']}: the CI step '{e['step']}' no longer runs {e['run_contains']!r}; it runs {match[0]!r}"
        )
        if e.get("script"):
            assert e["script"] in match[0], f"{e['id']}: step '{e['step']}' no longer runs {e['script']}"


def test_a_lab_command_that_mirrors_a_ci_line_carries_it_whole() -> None:
    """`lab_runs_ci_line`: the pinned CI invocation (arguments included) is one of the lab command's lines, so a
    changed argument in CI (a report path, a flag) turns the CI-step test red and the lab line cannot silently differ."""
    reg = _registry()
    flagged = [e for _k, e in _entries(reg) if e.get("lab_runs_ci_line")]
    assert len(flagged) >= 4, "non-vacuity: the post-suite asserts must mirror CI's own lines"
    for e in flagged:
        assert any(_norm(e["run_contains"]) in _norm(cmd) for cmd in e["command"]), (
            f"{e['id']}: no lab command line carries CI's invocation {e['run_contains']!r}"
        )


def test_ci_flags_are_in_both_the_ci_step_and_the_lab_command() -> None:
    """`ci_flags`: the Maven selectors (-Dgroups, -Dtest.excluded.groups, -Dnexus.ddl.suite) the lab command must
    pass exactly as CI does. A flag CI drops or changes fails here; so does a lab command that omits one."""
    reg = _registry()
    flagged = [e for _k, e in _entries(reg) if e.get("ci_flags")]
    assert len(flagged) >= 2, "non-vacuity: both Maven prerequisites pin their CI flags"
    for e in flagged:
        (run,) = _step_run(e["workflow"], e["job"], e["step"])
        lab = " ".join(e["command"])
        for flag in e["ci_flags"]:
            assert flag in run.split(), f"{e['id']}: CI's '{e['step']}' no longer passes {flag}"
            assert flag in lab.split(), f"{e['id']}: the lab command does not pass {flag}"


def test_ci_env_pins_match_the_ci_job_env_and_the_lab_command() -> None:
    reg = _registry()
    pinned = [e for _k, e in _entries(reg) if e.get("ci_env")]
    assert pinned, "non-vacuity: the bundle build pins its CI versions"
    for e in pinned:
        env = _workflow_doc(e["workflow"])["jobs"][e["job"]]["env"]
        matrix_images = {t["image"] for t in _workflow_doc(e["workflow"])["jobs"][e["job"]]["strategy"]["matrix"]["target"]}
        lab = " ".join(e["command"])
        for name, value in e["ci_env"].items():
            if name == "MANYLINUX_IMAGE":
                assert value in matrix_images, f"{e['id']}: CI's CA-3 matrix has no image {value}"
            else:
                assert str(env[name]) == value, f"{e['id']}: CI pins {name}={env[name]}, the list says {value}"
            assert f"{name}={value}" in lab, f"{e['id']}: the lab command does not set {name}={value}"


def test_every_script_ci_invokes_is_a_check_or_an_excluded_entry() -> None:
    reg = _registry()
    refs = _ci_script_refs()
    assert len(refs) >= SCRIPT_REF_FLOOR, f"swept only {len(refs)} script references: the parse is off"
    listed = {e["script"] for _k, e in _entries(reg) if e.get("script")}
    listed |= {x for _k, e in _entries(reg) for x in e.get("also_covers", [])}
    excluded = {e["script"] for e in reg["excluded"]} | {x for e in reg["excluded"] for x in e.get("also_covers", [])}
    assert not (listed & excluded), f"in both lists: {sorted(listed & excluded)}"
    unlisted = sorted(set(refs) - listed - excluded)
    assert not unlisted, (
        "CI runs these scripts and scripts/lab-entry-conditions.toml names neither a lab twin nor an "
        f"exclusion with a reason for them: {unlisted}"
    )


def test_exclusions_carry_a_reason_name_their_ci_step_and_are_not_stale() -> None:
    reg = _registry()
    refs = _ci_script_refs()
    assert len(reg["excluded"]) >= 10, "non-vacuity: no exclusions listed"
    for e in reg["excluded"]:
        assert e["reason"].strip(), f"{e['script']} is excluded without a reason"
        for path in [e["script"], *e.get("also_covers", [])]:
            assert path in refs, f"{path} is excluded but no CI step runs it any more: delete the entry"
            if "." not in path.replace(".py", "").replace(".sh", ""):  # a real path, not a module name
                assert (ROOT / path).is_file(), f"{path} does not exist"
        # the reason names the step that runs the script: a reason written about the wrong job is how a gap hides
        run = _step_run(e["ci_workflow"], e["ci_job"], e["ci_step"])
        assert len(run) == 1, f"{e['script']}: CI has {len(run)} step(s) '{e['ci_step']}' in job {e['ci_job']}"
        assert e["script"] in run[0], f"{e['script']}: step '{e['ci_step']}' of job {e['ci_job']} does not run it"
    for _k, c in _entries(reg):
        if c.get("script"):
            assert c["script"] in refs, f"{c['id']}: CI no longer runs {c['script']}"
            assert (ROOT / c["script"]).is_file(), f"{c['script']} does not exist"


def test_path_gate_claims_agree_with_the_real_paths_filter_globs() -> None:
    """An entry that says a step is path-gated names the filter (`gated_by`, in `gate_job`) and the paths a lane does
    and does not touch (`lane_paths_hit`, `lane_paths_miss`); the real dorny/paths-filter globs decide."""
    reg = _registry()
    gated = [e for e in reg["excluded"] if e.get("gated_by")] + [
        e for _k, e in _entries(reg) if e.get("gated_by")
    ]
    assert len(gated) >= 5, "non-vacuity: the path-gated entries must carry their filter"
    hits = 0
    for e in gated:
        workflow = e.get("ci_workflow") or e["workflow"]
        globs = _filter_globs(workflow, e["gate_job"], e["gated_by"])
        assert e.get("lane_paths_hit") or e.get("lane_paths_miss"), f"{e.get('id', e.get('script'))}: gated_by without lane paths"
        for path in e.get("lane_paths_hit", []):
            hits += 1
            assert _matches(globs, path), f"{e.get('id', e.get('script'))}: filter {e['gated_by']} does not match {path}"
        for path in e.get("lane_paths_miss", []):
            assert not _matches(globs, path), (
                f"{e.get('id', e.get('script'))}: filter {e['gated_by']} DOES match {path}, so a lane touching it "
                "triggers the step and the exclusion reason is false"
            )
    assert hits >= 3, "non-vacuity: no entry states a path that reaches its gate"


def test_the_ca3_bundle_gate_is_reached_by_a_changeset_lane() -> None:
    """The fact the first round got wrong: ca3_core includes the Liquibase changelog tree, so every changeset lane
    runs the bundle build, the CA-3 test and the Liquibase-against-bundle smoke."""
    globs = _filter_globs("ci.yml", "ca3-pgvector-bundle", "ca3_core")
    assert _matches(globs, "service/src/main/resources/db/changelog/db.changelog-master.xml")
    reg = _registry()
    smoke = next(c for c in reg["check"] if c["id"] == "ca3-liquibase-bundle-smoke")
    assert smoke["gated_by"] == "ca3_core" and smoke["lane_paths_hit"], "the smoke entry must state its trigger"


def test_script_less_ci_steps_the_list_names_still_exist() -> None:
    reg = _registry()
    assert len(reg["script_less"]) >= 4, "non-vacuity: the script-less CI-only steps are listed"
    for s in reg["script_less"]:
        assert s["reason"].strip(), s["step"]
        (run,) = _step_run(s["workflow"], s["job"], s["step"])
        assert _norm(s["run_contains"]) in run, f"step '{s['step']}' no longer runs {s['run_contains']!r}"


def test_lab_commands_follow_the_lab_host_rules() -> None:
    """AGENTS.md rules 12-13 and the stack checklist's item 0/1/2 forms."""
    reg = _registry()
    cmds = _all_commands(reg)
    assert len(cmds) >= 12, "non-vacuity: too few lab commands to be the stack's entry conditions"
    for where, cmd, entry in cmds:
        assert "lab-6c18g" not in cmd, f"{where}: lab-6c18g is CI's class, never a hand run: {cmd}"
        classes = re.findall(r"(?:-c |--lab-run )(lab-[\w-]+)", cmd)
        for cls in classes:
            if cls in FULL_SUITE_CLASSES:
                assert entry.get("full_suite"), f"{where}: {cls} is for a full suite; mark the entry full_suite or use lab-m: {cmd}"
            else:
                assert cls in LAB_CLASSES, f"{where}: unknown lab class {cls}: {cmd}"
        if re.search(r"(?<![\w-])(\./)?mvnw?\b", cmd) and "mvnw-leased.sh" not in cmd:
            pytest.fail(f"{where}: an engine build goes through scripts/mvnw-leased.sh --lab-run, got: {cmd}")
        if "mvnw-leased.sh" in cmd:
            assert "--lab-run" in cmd, f"{where}: mvnw-leased.sh on a pool host takes --lab-run <class>: {cmd}"
            assert "lab-remote" not in cmd, f"{where}: engine builds never go through lab-remote: {cmd}"
        if "build-gate-jar.sh" in cmd and "lab-remote" not in cmd:
            assert "--lab-run" in cmd, f"{where}: build-gate-jar.sh on a pool host takes --lab-run <class>: {cmd}"
        if re.search(r"\buv run pytest\b", cmd):
            assert "lab-remote" in cmd or "lab-run" in cmd, f"{where}: a pytest hand run goes through lab-remote or lab-run: {cmd}"
            assert "watched_pytest.py" in cmd, f"{where}: a remote pytest run goes through watched_pytest.py: {cmd}"


def test_status_files_are_root_level_ignored_names_and_lab_remote_fetches_them() -> None:
    """A `--status` path under a directory (output/ is git-ignored and absent from a lab-remote copy, and
    watched_pytest does not create it) fails the run before pytest starts. The root-level `*status*.json` form is
    the repo's convention (.gitignore, docs/agents/test-running.md rule 1); `lab-remote` must `--fetch` it back."""
    reg = _registry()
    seen = 0
    for where, cmd, _e in _all_commands(reg):
        for path in re.findall(r"--status\s+(\S+)", cmd):
            seen += 1
            assert "/" not in path, f"{where}: --status {path} is under a directory the lab-remote copy does not have"
            assert re.fullmatch(r"[\w.-]*status[\w.-]*\.json", path), f"{where}: --status {path} is not a *status*.json name"
            ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False).returncode == 0
            assert ignored, f"{where}: {path} is not git-ignored, so a commit built from git status could take it"
            if "lab-remote" in cmd:
                assert f"--fetch {path}" in cmd, f"{where}: lab-remote runs --status {path} without --fetch {path}"
    assert seen >= 2, "non-vacuity: no --status path found"


def test_a_command_sequence_sets_its_variables_and_syncs_before_uv_run() -> None:
    """Leg A as first written could not run: $PREV was never set, and nothing ran `uv sync` before `uv run`."""
    reg = _registry()
    leg_checked = 0
    for _kind, e in _entries(reg):
        lines = e["command"]
        for i, cmd in enumerate(lines):
            for var in set(re.findall(r"\$\{?([A-Z][A-Z0-9_]*)\}?", cmd)) - {"PWD", "HOME", "GITHUB_WORKSPACE"}:
                assert any(re.match(rf"\s*(?:export\s+)?(?:[A-Z_]+=\S+\s+)*{var}=", prev) for prev in lines[:i]) or any(
                    f"{var}=" in prev for prev in lines[:i]
                ), f"{e['id']}: ${var} is used by '{cmd}' but no earlier command sets it"
            if re.search(r"\buv run\b", cmd) and "lab-remote" not in cmd:
                leg_checked += 1
                assert any(re.match(r"uv sync\b", prev) for prev in lines[:i]), (
                    f"{e['id']}: '{cmd}' runs uv run on the host with no earlier `uv sync`"
                )
    assert leg_checked >= 2, "non-vacuity: no host-side uv run found"


def test_lab_commands_name_things_that_exist() -> None:
    """Every repo path, flag and Maven test class a lab command names is real."""
    reg = _registry()
    for where, cmd, _e in _all_commands(reg):
        for path in re.findall(r"(?<![\w/])(?:scripts|tests)/[\w./-]+\.(?:py|sh)", cmd):
            assert (ROOT / path).is_file(), f"{where}: names {path}, which does not exist"
        for cls in re.findall(r"-Dtest=([\w,]+)", cmd):
            for name in cls.split(","):
                hits = list((ROOT / "service" / "src" / "test").rglob(f"{name}.java"))
                assert hits, f"{where}: -Dtest={name} matches no test class"
        # --flags of a repo script the command runs: each must appear in that script's source.
        for script in re.findall(r"(?<![\w/])scripts/[\w./-]+\.(?:py|sh)", cmd):
            text = (ROOT / script).read_text()
            segment = cmd[cmd.index(script):]
            # the script's own options end at `--` (what follows is another program's command line) or the next command
            segment = re.split(r"\s(?:&&|\|\||;|\||--)\s|\sscripts/", segment, maxsplit=1)[0]
            for flag in re.findall(r"(?<=\s)(--[a-z][\w-]*)", segment):
                assert flag in text, f"{where}: {script} has no {flag} option"


def test_a_lint_marked_twin_runs_a_file_the_lint_bucket_selects() -> None:
    """`pytest -m lint <file>` runs nothing unless the file carries the lint mark."""
    reg = _registry()
    seen = 0
    for where, cmd, _e in _all_commands(reg):
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
    reg = _registry()
    twin = next(c for c in reg["check"] if c["id"] == "pipefail-lint")
    assert twin["twin"] == "partial", "the twin runs one file of the lint bucket, not the bucket"


def test_the_leg_a_twin_matches_the_engine_release_skill_and_the_real_cache_switch() -> None:
    reg = _registry()
    leg = next(c for c in reg["check"] if c["id"] == "upgrade-boot-writes-leg-a")
    assert not leg.get("workflow"), "leg A is a release-skill gate, not a CI step"
    text = "\n".join(leg["command"])
    assert "NX_GATE_JAR_CACHE=off" in text, "leg A on a stack tip builds the gate jar with the cache off"
    assert re.search(r"^PREV=\d+\.\d+\.\d+$", leg["command"][0]), "leg A names its previous client release first"
    assert "NX_GATE_JAR_CACHE" in (ROOT / "scripts/build-gate-jar.sh").read_text(), (
        "NX_GATE_JAR_CACHE is not the switch scripts/build-gate-jar.sh reads"
    )
    skill = (ROOT / ".claude/skills/engine-release/SKILL.md").read_text()
    for token in ("scripts/upgrade_boot_writes.py", "--ceiling-boot 67108864", "--ceiling-reboot 67108864", "--prev-client", "uv sync -q"):
        assert token in text, f"leg A twin lost {token}"
        assert token in skill, f"the engine-release skill's Step 3h no longer carries {token}"


def test_the_pointer_in_test_running_names_the_registry_and_its_limits() -> None:
    doc = (ROOT / "docs/agents/test-running.md").read_text()
    assert "scripts/lab-entry-conditions.toml" in doc
    assert "cannot appear without" not in doc, "the sweep is script-keyed; the pointer must not claim more"
