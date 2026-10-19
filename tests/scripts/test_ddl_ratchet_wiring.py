# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ekp7w / nexus-2rhif: the DDL and bytes-written ratchet is called where its verdict can reach CI.

``scripts/ddl_ratchet.py`` sat unwired from 2026-10-19 until this lint: nothing in ``.github`` called it,
so every ceiling was a number nobody compared. These checks parse the workflows and fail when the call
goes missing, loses the flag that makes it correct, or drifts from the shard matrix; the mutation cases
prove each is caught, not merely that today's files are consistent.

* ``ci.yml``: the ``test`` shards write their report to a per-job directory, run ``check --advisory``
  and upload it; ``pytest-gate`` downloads every shard's report and runs ``fan-in`` (never advisory) with
  ``--shards`` equal to the matrix length. A shard's mix is not the run's, so only the fan-in gates.
* ``service-ci.yml``: each Java step names its run (``-Dnexus.ddl.run=<id>-<suite>``) and its ratchet
  step reads exactly that run, so a persistent pool workspace cannot mix runs. A Java step is advisory
  exactly while its ceilings in ``tests/ddl_ceiling.toml`` are still ``"measure"``: it must not stay
  advisory once they are numbers, and must not be enforced before.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parents[2]
CI_YML = REPO / ".github" / "workflows" / "ci.yml"
SERVICE_CI_YML = REPO / ".github" / "workflows" / "service-ci.yml"
CEILINGS = REPO / "tests" / "ddl_ceiling.toml"
POM = REPO / "service" / "pom.xml"

_RUN_ID = '${NX_TEST_RUN_ID}'


def _steps(doc: dict, job: str) -> list[dict]:
    return doc["jobs"][job]["steps"]


def _run_steps(doc: dict, job: str, needle: str) -> list[dict]:
    return [s for s in _steps(doc, job) if needle in str(s.get("run", ""))]


def python_wiring_problems(ci_text: str) -> list[str]:
    doc = yaml.safe_load(ci_text)
    problems: list[str] = []
    n = len(doc["jobs"]["test"]["strategy"]["matrix"]["shard"])

    runner = [s for s in _steps(doc, "test") if str(s.get("name", "")).startswith("Run tests")]
    if not runner or "NX_DDL_REPORT_DIR=" not in str(runner[0].get("run", "")):
        problems.append("test: the 'Run tests' step does not export NX_DDL_REPORT_DIR, so the report lands where no step looks")

    shard = _run_steps(doc, "test", "ddl_ratchet.py check --suite python_unit")
    if len(shard) != 1:
        problems.append(f"test: expected one per-shard ratchet step, found {len(shard)}")
    elif "--advisory" not in shard[0]["run"]:
        problems.append("test: the per-shard check is not --advisory; a shard's mix flaps its own ceiling (nexus-2rhif)")
    elif "nx-ddl-report.json" not in shard[0]["run"]:
        problems.append("test: the per-shard check does not read nx-ddl-report.json")
    uploads = [s for s in _steps(doc, "test") if str(s.get("uses", "")).startswith("actions/upload-artifact@")
               and str(s.get("with", {}).get("name", "")).startswith("ddl-report-shard-")]
    if len(uploads) != 1:
        problems.append(f"test: expected one ddl-report-shard-* upload, found {len(uploads)}")
    else:
        up = uploads[0]
        if up["with"]["name"] != "ddl-report-shard-${{ matrix.shard }}":
            problems.append("test: the report artifact is not named per shard (one name would collide across shards)")
        if up["with"].get("if-no-files-found") != "error":
            problems.append("test: a shard with no report must fail its upload (if-no-files-found: error)")
        if "3.12" not in str(up.get("if", "")):
            problems.append("test: the upload is not limited to the 3.12 leg, so a 3.13 leg would add a seventh report")

    gate = _run_steps(doc, "pytest-gate", "ddl_ratchet.py fan-in")
    if len(gate) != 1:
        problems.append(f"pytest-gate: expected one fan-in step, found {len(gate)}")
    else:
        step = gate[0]
        run = step["run"]
        if "--advisory" in run or step.get("continue-on-error"):
            problems.append("pytest-gate: the fan-in is the gate and cannot be advisory or continue-on-error")
        m = re.search(r"--shards\s+(\d+)", run)
        if not m or int(m.group(1)) != n:
            problems.append(f"pytest-gate: fan-in --shards is {m.group(1) if m else 'missing'} but the matrix has {n} shards")
        if "--suite python_unit" not in run or "ddl-reports/*/nx-ddl-report.json" not in run:
            problems.append("pytest-gate: the fan-in does not read every downloaded shard report")
        if "code == 'true'" not in str(step.get("if", "")):
            problems.append("pytest-gate: the fan-in is not limited to runs whose shards ran (code == 'true')")
    downloads = [s for s in _steps(doc, "pytest-gate") if str(s.get("uses", "")).startswith("actions/download-artifact@")]
    if len(downloads) != 1 or downloads[0].get("with", {}).get("pattern") != "ddl-report-shard-*":
        problems.append("pytest-gate: it does not download ddl-report-shard-*")
    if not any(str(s.get("uses", "")).startswith("actions/checkout@") for s in _steps(doc, "pytest-gate")):
        problems.append("pytest-gate: no checkout, so the ratchet script and its ceilings are not there")
    return problems


def java_wiring_problems(service_ci_text: str, ceilings_text: str, pom_text: str) -> list[str]:
    doc = yaml.safe_load(service_ci_text)
    ceilings = tomllib.loads(ceilings_text)
    problems: list[str] = []
    job = "java-test-and-drift-guard"
    for suite in ("main", "integration"):
        mvn = _run_steps(doc, job, f"-Dnexus.ddl.suite={suite} ")
        if len(mvn) != 1:
            problems.append(f"{job}: expected one mvn step for suite {suite}, found {len(mvn)}")
            continue
        run_id = f'"-Dnexus.ddl.run={_RUN_ID}-{suite}"'
        if run_id not in mvn[0]["run"]:
            problems.append(f"{suite}: the mvn step does not name its run ({run_id})")
        check = _run_steps(doc, job, f"ddl_ratchet.py check --suite java_{suite} ")
        if len(check) != 1:
            problems.append(f"{suite}: expected one ratchet step, found {len(check)}")
            continue
        run = check[0]["run"]
        if f'--run "{_RUN_ID}-{suite}"' not in run:
            problems.append(f"{suite}: the ratchet step does not read the run mvn was given (--run \"{_RUN_ID}-{suite}\")")
        if "--report service/target/ddl-counts" not in run:
            problems.append(f"{suite}: the ratchet step does not read service/target/ddl-counts")
        unset = all(ceilings[f"java_{suite}"][k] == "measure" for k in ceilings[f"java_{suite}"] if not k.endswith("tolerance"))
        advisory = "--advisory" in run
        if unset and not advisory:
            problems.append(f"{suite}: the ceilings are still 'measure' and the step is enforced: it would exit 11 every run")
        if not unset and advisory:
            problems.append(f"{suite}: the ceilings are numbers but the step is still --advisory: the gate is switched off")
        order = [i for i, s in enumerate(_steps(doc, job)) if s in (mvn[0], check[0])]
        if order != sorted(order) or order[0] + 1 != order[1]:
            problems.append(f"{suite}: the ratchet step does not directly follow its mvn step")
    if "<nexus.ddl.run>${nexus.ddl.run}</nexus.ddl.run>" not in pom_text:
        problems.append("service/pom.xml: the fork's run id is not the overridable nexus.ddl.run property")
    return problems


# ── today's files ────────────────────────────────────────────────────────────


def test_ci_yml_wires_the_python_ratchet() -> None:
    assert python_wiring_problems(CI_YML.read_text()) == []


def test_service_ci_yml_wires_the_java_ratchet() -> None:
    assert java_wiring_problems(SERVICE_CI_YML.read_text(), CEILINGS.read_text(), POM.read_text()) == []


# ── mutations: each way the wiring can rot is caught ─────────────────────────


def _mutate(text: str, old: str, new: str) -> str:
    assert text.count(old) >= 1, f"mutation anchor missing: {old!r}"
    return text.replace(old, new, 1)


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("python3 scripts/ddl_ratchet.py fan-in", "python3 scripts/ddl_ratchet.py frobnicate", "expected one fan-in step"),
        ("--suite python_unit --shards 6 ", "--suite python_unit --shards 5 ", "--shards is 5 but the matrix has 6"),
        ("--suite python_unit --shards 6 ", "--suite python_unit --shards 6 --advisory ", "cannot be advisory"),
        ('--report "$RUNNER_TEMP/ddl-report/nx-ddl-report.json" --advisory', '--report "$RUNNER_TEMP/ddl-report/nx-ddl-report.json"', "is not --advisory"),
        ('export NX_DDL_REPORT_DIR="$RUNNER_TEMP/ddl-report"', 'echo no-report-dir', "does not export NX_DDL_REPORT_DIR"),
        ("name: ddl-report-shard-${{ matrix.shard }}", "name: ddl-report", "expected one ddl-report-shard-* upload"),
        ("          if-no-files-found: error\n          # A re-run", "          if-no-files-found: warn\n          # A re-run", "must fail its upload"),
        ("pattern: ddl-report-shard-*", "pattern: something-else", "does not download ddl-report-shard-*"),
    ],
    ids=["fan-in gone", "shards drift", "fan-in advisory", "shard check enforced", "no report dir", "artifact not a shard report",
         "missing report passes", "download pattern"],
)
def test_the_python_wiring_lint_catches_each_rot(old: str, new: str, expect: str) -> None:
    problems = python_wiring_problems(_mutate(CI_YML.read_text(), old, new))
    assert any(expect in p for p in problems), problems


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ('"-Dnexus.ddl.run=${NX_TEST_RUN_ID}-main"', "", "mvn step does not name its run"),
        ('--run "${NX_TEST_RUN_ID}-integration"', "", "does not read the run mvn was given"),
        ("--suite java_main --report service/target/ddl-counts", "--suite java_main --report service/target", "does not read service/target/ddl-counts"),
    ],
    ids=["mvn run unnamed", "check reads any run", "check reads the wrong directory"],
)
def test_the_java_wiring_lint_catches_each_rot(old: str, new: str, expect: str) -> None:
    problems = java_wiring_problems(_mutate(SERVICE_CI_YML.read_text(), old, new), CEILINGS.read_text(), POM.read_text())
    assert any(expect in p for p in problems), problems


def test_a_java_step_is_advisory_exactly_while_its_ceilings_are_unset() -> None:
    text = SERVICE_CI_YML.read_text()
    unset = "[java_main]\nbytes_tolerance = 0.05\nddl_tolerance = 0.0\n" + "".join(
        f'{k} = "measure"\n' for k in ("migration_wal_bytes_per_fork", "migration_ddl_per_fork", "other_wal_bytes", "other_ddl")
    )
    unset += unset.replace("java_main", "java_integration")
    set_ = unset.replace('"measure"', "7")
    enforced = _mutate(text, '--run "${NX_TEST_RUN_ID}-main" --advisory', '--run "${NX_TEST_RUN_ID}-main"')
    assert any("would exit 11" in p for p in java_wiring_problems(enforced, unset, POM.read_text()))
    assert any("gate is switched off" in p for p in java_wiring_problems(text, set_, POM.read_text()))


def test_the_pom_run_id_is_overridable() -> None:
    mutated = POM.read_text().replace("<nexus.ddl.run>${nexus.ddl.run}</nexus.ddl.run>", "<nexus.ddl.run>${maven.build.timestamp}</nexus.ddl.run>", 1)
    problems = java_wiring_problems(SERVICE_CI_YML.read_text(), CEILINGS.read_text(), mutated)
    assert any("overridable nexus.ddl.run" in p for p in problems), problems
