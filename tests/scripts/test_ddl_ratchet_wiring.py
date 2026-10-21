# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ekp7w / nexus-2rhif: the DDL and bytes-written ratchet is called where its verdict can reach CI.

``scripts/ddl_ratchet.py`` sat unwired from 2026-10-19 until this lint: nothing in ``.github`` called it,
so every ceiling was a number nobody compared. These checks parse the workflows and fail when the call
goes missing, loses the flag that makes it correct, or drifts from the shard matrix; the mutation cases
prove each is caught, not merely that today's files are consistent.

* ``ci.yml``: the ``test`` shards write their report to a per-job directory, run ``check --advisory``
  and upload it (both steps ``continue-on-error`` exactly while ``[python_unit] measured_on`` is absent,
  so a missing script, an exception or a failed artifact upload cannot fail the leg and with it
  ``pytest-gate`` before the gate is armed); ``pytest-gate`` downloads every shard's report and runs ``fan-in`` with ``--shards``
  equal to the matrix length, after the verdict step. A shard's mix is not the run's, so only the fan-in
  gates, and it gates only once armed: the fan-in is ``--advisory`` (and its checkout, download and run
  steps ``continue-on-error``) exactly while ``[python_unit] measured_on`` is absent from
  ``tests/ddl_ceiling.toml``, the same rule as the Java steps below. Until real Linux ceilings exist the
  gate must not be able to turn develop red.
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


def _armed(ceilings_text: str) -> bool:
    marker = tomllib.loads(ceilings_text).get("python_unit", {}).get("measured_on")
    return isinstance(marker, str) and bool(marker.strip())


def python_wiring_problems(ci_text: str, ceilings_text: str) -> list[str]:
    doc = yaml.safe_load(ci_text)
    armed = _armed(ceilings_text)
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
    if shard and "3.12" not in str(shard[0].get("if", "")):
        problems.append("test: the per-shard check is not limited to the 3.12 leg (the report it reads is the 3.12 leg's)")
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
    # Until the gate is armed, nothing in the per-shard path may fail the leg: a missing python3 or script,
    # an unexpected exception, a missing report or a transient upload failure would red pytest-gate before
    # any ceiling was measured. Once armed the fan-in is the gate and a swallowed failure would hide a hole.
    for step_ in (*shard, *uploads):
        where = step_.get("name", "?")
        if armed and step_.get("continue-on-error"):
            problems.append(f"test: {where!r} is continue-on-error but the gate is armed: a failure here would pass")
        if not armed and not step_.get("continue-on-error"):
            problems.append(f"test: {where!r} is not continue-on-error while the gate is advisory: "
                            "a missing script, a missing report or a failed upload would red the leg and pytest-gate")

    gate = _run_steps(doc, "pytest-gate", "ddl_ratchet.py fan-in")
    if len(gate) != 1:
        problems.append(f"pytest-gate: expected one fan-in step, found {len(gate)}")
    else:
        step = gate[0]
        run = step["run"]
        advisory = "--advisory" in run
        if armed and advisory:
            problems.append("pytest-gate: the ceilings are measured (measured_on is set) but the fan-in is still --advisory: the gate is switched off")
        if not armed and not advisory:
            problems.append("pytest-gate: the fan-in is enforced but [python_unit] measured_on is absent: the ceilings were not measured on CI, "
                            "so it could turn develop red on an estimate (add --advisory, or measure and arm in one commit)")
        m = re.search(r"--shards\s+(\d+)", run)
        if not m or int(m.group(1)) != n:
            problems.append(f"pytest-gate: fan-in --shards is {m.group(1) if m else 'missing'} but the matrix has {n} shards")
        runs_on = str(doc["jobs"]["test"].get("runs-on", ""))
        m_on = re.fullmatch(r"\$\{\{\s*(.+?)\s*&&\s*'ubuntu-latest'\s*\|\|\s*'lab-[\w-]+'\s*\}\}", runs_on)
        m_ba = re.search(r"\$\{\{\s*(.+?)\s*&&\s*'--bytes-advisory'\s*\|\|\s*''\s*\}\}", run)
        if not m_on:
            problems.append(f"test: runs-on is not the '<hosted> && ubuntu-latest || lab-*' shape the fan-in mirrors: {runs_on!r}")
        elif not m_ba or m_ba.group(1) != m_on.group(1):
            problems.append("pytest-gate: the fan-in's --bytes-advisory condition is not the test job's hosted-runner condition, "
                            "so a hosted run could be gated on WAL measured on the pool (nexus-ekp7w)")
        if "--suite python_unit" not in run or "ddl-reports/*/nx-ddl-report.json" not in run:
            problems.append("pytest-gate: the fan-in does not read every downloaded shard report")
        verdict = [i for i, s in enumerate(_steps(doc, "pytest-gate")) if 'echo "pytest-gate: PASS"' in str(s.get("run", ""))]
        position = _steps(doc, "pytest-gate").index(step)
        if len(verdict) != 1:
            problems.append(f"pytest-gate: expected one verdict step (it echoes pytest-gate: PASS), found {len(verdict)}")
        elif position < verdict[0]:
            problems.append("pytest-gate: the fan-in runs before the verdict step, so it would judge a matrix that has not passed")
        for s_ in (step, *_gate_inputs(doc)):
            cond = str(s_.get("if", ""))
            where = s_.get("name", "?")
            if "code == 'true'" not in cond:
                problems.append(f"pytest-gate: {where!r} is not limited to runs whose shards ran (code == 'true')")
            if re.search(r"\b(always|failure|cancelled)\(\)", cond):
                problems.append(f"pytest-gate: {where!r} can run after a failed verdict ({cond!r}); it must keep the implicit success()")
            if armed and s_.get("continue-on-error"):
                problems.append(f"pytest-gate: {where!r} is continue-on-error but the gate is armed: its failure would pass")
            if not armed and not s_.get("continue-on-error"):
                problems.append(f"pytest-gate: {where!r} is not continue-on-error while the gate is advisory: a wiring slip would red the job")
    downloads = [s for s in _steps(doc, "pytest-gate") if str(s.get("uses", "")).startswith("actions/download-artifact@")]
    if len(downloads) != 1 or downloads[0].get("with", {}).get("pattern") != "ddl-report-shard-*":
        problems.append("pytest-gate: it does not download ddl-report-shard-*")
    if not any(str(s.get("uses", "")).startswith("actions/checkout@") for s in _steps(doc, "pytest-gate")):
        problems.append("pytest-gate: no checkout, so the ratchet script and its ceilings are not there")
    return problems


def _gate_inputs(doc: dict) -> list[dict]:
    """The checkout and the download that feed the fan-in in pytest-gate."""
    return [s for s in _steps(doc, "pytest-gate")
            if str(s.get("uses", "")).startswith(("actions/checkout@", "actions/download-artifact@"))]


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
    assert python_wiring_problems(CI_YML.read_text(), CEILINGS.read_text()) == []


def test_service_ci_yml_wires_the_java_ratchet() -> None:
    assert java_wiring_problems(SERVICE_CI_YML.read_text(), CEILINGS.read_text(), POM.read_text()) == []


# ── mutations: each way the wiring can rot is caught ─────────────────────────


def _mutate(text: str, old: str, new: str) -> str:
    assert text.count(old) >= 1, f"mutation anchor missing: {old!r}"
    return text.replace(old, new, 1)


_CHECK_HEAD = ("- name: DDL/bytes ratchet, this shard (advisory; the gate is the fan-in in pytest-gate)\n"
               "        if: success() && matrix.python-version == '3.12'")
_UPLOAD_IF = ("- name: Upload this shard's DDL report (read by the fan-in in pytest-gate)\n"
              "        if: success() && matrix.python-version == '3.12'")
_FAN_IN_IF = "- name: DDL/bytes ratchet over the whole run (fan-in)\n        if: needs.changes.outputs.code == 'true'"
_DOWNLOAD_IF = "- name: Fetch every shard's DDL report\n        if: needs.changes.outputs.code == 'true'"


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ("python3 scripts/ddl_ratchet.py fan-in", "python3 scripts/ddl_ratchet.py frobnicate", "expected one fan-in step"),
        ("--suite python_unit --shards 6 ", "--suite python_unit --shards 5 ", "--shards is 5 but the matrix has 6"),
        ('--report "$RUNNER_TEMP/ddl-report/nx-ddl-report.json" --advisory', '--report "$RUNNER_TEMP/ddl-report/nx-ddl-report.json"', "is not --advisory"),
        ('export NX_DDL_REPORT_DIR="$RUNNER_TEMP/ddl-report"', 'echo no-report-dir', "does not export NX_DDL_REPORT_DIR"),
        ("name: ddl-report-shard-${{ matrix.shard }}", "name: ddl-report", "expected one ddl-report-shard-* upload"),
        ("          if-no-files-found: error\n          # A re-run", "          if-no-files-found: warn\n          # A re-run", "must fail its upload"),
        ("pattern: ddl-report-shard-*", "pattern: something-else", "does not download ddl-report-shard-*"),
        (_CHECK_HEAD, "- name: DDL/bytes ratchet, this shard (advisory; the gate is the fan-in in pytest-gate)\n        if: success()",
         "per-shard check is not limited to the 3.12 leg"),
        (_UPLOAD_IF, "- name: Upload this shard's DDL report (read by the fan-in in pytest-gate)\n        if: success()",
         "upload is not limited to the 3.12 leg"),
        (_FAN_IN_IF, "- name: DDL/bytes ratchet over the whole run (fan-in)\n        if: success()", "is not limited to runs whose shards ran"),
        (_DOWNLOAD_IF, "- name: Fetch every shard's DDL report\n        if: success()", "is not limited to runs whose shards ran"),
        (_FAN_IN_IF, "- name: DDL/bytes ratchet over the whole run (fan-in)\n        if: always() && needs.changes.outputs.code == 'true'",
         "can run after a failed verdict"),
        (_FAN_IN_IF, _FAN_IN_IF + "\n        continue-on-error: true", "is continue-on-error but the gate is armed"),
        (_DOWNLOAD_IF, _DOWNLOAD_IF + "\n        continue-on-error: true", "is continue-on-error but the gate is armed"),
        (_CHECK_HEAD, _CHECK_HEAD + "\n        continue-on-error: true", "is continue-on-error but the gate is armed"),
        (_UPLOAD_IF, _UPLOAD_IF + "\n        continue-on-error: true", "is continue-on-error but the gate is armed"),
        ("--suite python_unit --shards 6 --report", "--suite python_unit --shards 6 --advisory --report", "the gate is switched off"),
        ("&& '--bytes-advisory' || ''", "&& '' || ''", "--bytes-advisory condition is not the test job's"),
        ("vars.NX_CI_POOL == 'off' || (github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository)) && '--bytes-advisory'",
         "vars.NX_CI_POOL == 'off') && '--bytes-advisory'", "--bytes-advisory condition is not the test job's"),
    ],
    ids=["fan-in gone", "shards drift", "shard check enforced", "no report dir", "artifact not a shard report",
         "missing report passes", "download pattern", "shard check on the 3.13 leg", "upload on the 3.13 leg",
         "fan-in runs on a skipped matrix", "download runs on a skipped matrix", "fan-in runs after a red verdict",
         "armed fan-in swallows a red", "armed download swallows a red",
         "armed shard check swallows a red", "armed report upload swallows a red", "armed fan-in back to advisory",
         "hosted run gated on WAL", "bytes-advisory condition drifts from runs-on"],
)
def test_the_python_wiring_lint_catches_each_rot(old: str, new: str, expect: str) -> None:
    problems = python_wiring_problems(_mutate(CI_YML.read_text(), old, new), CEILINGS.read_text())
    assert any(expect in p for p in problems), problems


def _move_fan_in_before_the_verdict(text: str) -> str:
    """The fan-in block (comment, checkout, download, run) lifted above the verdict step."""
    block_start = text.index("      # nexus-ekp7w / nexus-2rhif: THE DDL AND BYTES-WRITTEN GATE")
    block_end = text.index("\n  ca3-pgvector-bundle:")
    block = text[block_start:block_end]
    text = text[:block_start] + text[block_end:]
    anchor = "      - name: Verify the pytest suite or its legitimate skip"
    assert text.count(anchor) == 1
    return text.replace(anchor, block.rstrip("\n") + "\n\n" + anchor, 1)


def test_the_fan_in_must_follow_the_verdict_step() -> None:
    problems = python_wiring_problems(_move_fan_in_before_the_verdict(CI_YML.read_text()), CEILINGS.read_text())
    assert any("fan-in runs before the verdict step" in p for p in problems), problems


def _unarmed_ceilings() -> str:
    text = CEILINGS.read_text()
    lines = [ln for ln in text.splitlines(keepends=True) if not ln.startswith("measured_on = ")]
    assert len(lines) == len(text.splitlines(keepends=True)) - 1, "expected exactly one measured_on line"
    return "".join(lines)


def test_the_python_fan_in_is_advisory_exactly_while_the_ceilings_are_unmeasured_on_ci() -> None:
    """Armed 2026-10-20 (nexus-ekp7w) from three Linux CI fan-ins: the fan-in enforces and nothing in the
    path swallows a failure. The same wiring on unmeasured ceilings could turn develop red on an estimate,
    and the advisory wiring is still what an unarmed file requires."""
    text, ceilings = CI_YML.read_text(), CEILINGS.read_text()
    assert _armed(ceilings), "the python gate was armed on 2026-10-20; disarming is a deliberate, reviewed commit"
    assert python_wiring_problems(text, ceilings) == []
    unarmed = _unarmed_ceilings()
    problems = python_wiring_problems(text, unarmed)
    assert any("could turn develop red on an estimate" in p for p in problems), problems
    # Every advisory step, the two per-shard ones and the three in pytest-gate (nexus-aca0q.1): a missing
    # continue-on-error on any one of them in an unarmed file must be reported by name.
    for name in ("DDL/bytes ratchet, this shard", "Upload this shard's DDL report",
                 "Check out the ratchet and its ceilings", "Fetch every shard's DDL report",
                 "DDL/bytes ratchet over the whole run (fan-in)"):
        assert any("is not continue-on-error while the gate is advisory" in p and name in p for p in problems), (name, problems)
    advisory_ok = _mutate(text, "--shards 6 --report", "--shards 6 --advisory --report")
    for head in (_CHECK_HEAD, _UPLOAD_IF):
        advisory_ok = _mutate(advisory_ok, head, head + "\n        continue-on-error: true")
    for step in ("- name: Check out the ratchet and its ceilings", _DOWNLOAD_IF.split("\n")[0], _FAN_IN_IF.split("\n")[0]):
        advisory_ok = _mutate(advisory_ok, step + "\n        if: needs.changes.outputs.code == 'true'\n",
                              step + "\n        if: needs.changes.outputs.code == 'true'\n        continue-on-error: true\n")
    assert python_wiring_problems(advisory_ok, unarmed) == []


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
