# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-abcqb: the tenant-saving ratchet is called where its verdict can reach CI.

``tests/_storage_touch_fan_in.py`` sums ``provisioned`` over every shard's
``nx-storage-touch-report.json`` and checks the run's total against
``tests/storage_touch_ceiling.toml``. These checks parse ``ci.yml`` and fail when the wiring
rots: the ``test`` shards write the report beside the DDL report and the one per-shard artifact
carries both; ``pytest-gate`` checks out the fan-in and its ceilings and runs it over every
downloaded shard with ``--shards`` equal to the matrix length, after the verdict step and only
when the shards ran. The fan-in step is ``continue-on-error`` exactly while ``[python_unit]
measured_on`` is absent from the ceiling file (the DDL ratchet's arming rule): a ceiling measured
off CI must not be able to turn develop red, and an armed one must not be swallowed. The mutation
cases prove each check is caught, not merely that today's file is consistent.
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
CEILINGS = REPO / "tests" / "storage_touch_ceiling.toml"

_FAN_IN = "-m tests._storage_touch_fan_in"
_REPORT = "nx-storage-touch-report.json"
_CHECKOUT_PATHS = ("tests/__init__.py", "tests/_storage_touch_fan_in.py", "tests/storage_touch_ceiling.toml")


def _steps(doc: dict, job: str) -> list[dict]:
    return doc["jobs"][job]["steps"]


def _armed(ceilings_text: str) -> bool:
    marker = tomllib.loads(ceilings_text).get("python_unit", {}).get("measured_on")
    return isinstance(marker, str) and bool(marker.strip())


def wiring_problems(ci_text: str, ceilings_text: str) -> list[str]:
    doc = yaml.safe_load(ci_text)
    armed = _armed(ceilings_text)
    problems: list[str] = []
    n = len(doc["jobs"]["test"]["strategy"]["matrix"]["shard"])

    runner = [s for s in _steps(doc, "test") if str(s.get("name", "")).startswith("Run tests")]
    if not runner or 'NX_STORAGE_TOUCH_REPORT_DIR="$NX_DDL_REPORT_DIR"' not in str(runner[0].get("run", "")):
        problems.append("test: the 'Run tests' step does not point NX_STORAGE_TOUCH_REPORT_DIR at the DDL report directory")
    if not runner or not re.search(r"^\s*export NX_STORAGE_TOUCH_SWEEP=1\s*$", str(runner[0].get("run", "")), re.M):
        problems.append("test: the 'Run tests' step does not export NX_STORAGE_TOUCH_SWEEP=1, so the shards' whole-run "
                        "checks are advice only (nexus-ey246)")
    uploads = [s for s in _steps(doc, "test") if str(s.get("with", {}).get("name", "")).startswith("ddl-report-shard-")]
    if len(uploads) != 1 or _REPORT not in str(uploads[0]["with"].get("path", "")):
        problems.append(f"test: the per-shard report artifact does not carry {_REPORT}")

    gate = [s for s in _steps(doc, "pytest-gate") if _FAN_IN in str(s.get("run", ""))]
    if len(gate) != 1:
        problems.append(f"pytest-gate: expected one storage-touch fan-in step, found {len(gate)}")
        return problems
    step = gate[0]
    run = str(step["run"])
    if not re.search(r"\bddl-reports\b", run) or "--suite python_unit" not in run:
        problems.append("pytest-gate: the fan-in does not read the downloaded shard reports for --suite python_unit")
    m = re.search(r"--shards\s+(\d+)", run)
    if not m or int(m.group(1)) != n:
        problems.append(f"pytest-gate: fan-in --shards is {m.group(1) if m else 'missing'} but the matrix has {n} shards")
    cond = str(step.get("if", ""))
    if "code == 'true'" not in cond:
        problems.append("pytest-gate: the fan-in is not limited to runs whose shards ran (code == 'true')")
    if re.search(r"\b(always|failure|cancelled)\(\)", cond):
        problems.append(f"pytest-gate: the fan-in can run after a failed verdict ({cond!r})")
    steps = _steps(doc, "pytest-gate")
    verdict = [i for i, s in enumerate(steps) if 'echo "pytest-gate: PASS"' in str(s.get("run", ""))]
    if len(verdict) != 1 or steps.index(step) < verdict[0]:
        problems.append("pytest-gate: the fan-in does not run after the one verdict step")
    if armed and step.get("continue-on-error"):
        problems.append("pytest-gate: [python_unit] measured_on is set but the fan-in is continue-on-error: the gate is switched off")
    if not armed and not step.get("continue-on-error"):
        problems.append("pytest-gate: the fan-in is enforced but [python_unit] measured_on is absent: the ceiling was not "
                        "measured on CI, so it could turn develop red on a chas number")
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    sparse = "\n".join(str(s.get("with", {}).get("sparse-checkout", "")) for s in checkouts)
    for path in _CHECKOUT_PATHS:
        if path not in sparse:
            problems.append(f"pytest-gate: the checkout does not bring {path}, so the fan-in cannot run")
    return problems


def test_ci_wires_the_storage_touch_fan_in() -> None:
    assert wiring_problems(CI_YML.read_text(encoding="utf-8"), CEILINGS.read_text(encoding="utf-8")) == []


def test_an_armed_ceiling_is_a_number() -> None:
    section = tomllib.loads(CEILINGS.read_text(encoding="utf-8"))["python_unit"]
    if _armed(CEILINGS.read_text(encoding="utf-8")):
        assert isinstance(section["provisioned_tests"], int), section


@pytest.mark.parametrize(
    ("old", "new", "expect"),
    [
        ('export NX_STORAGE_TOUCH_REPORT_DIR="$NX_DDL_REPORT_DIR"', "echo no-dir", "NX_STORAGE_TOUCH_REPORT_DIR"),
        ("          export NX_STORAGE_TOUCH_SWEEP=1\n", "          # export NX_STORAGE_TOUCH_SWEEP=1\n",
         "does not export NX_STORAGE_TOUCH_SWEEP=1"),
        ("${{ runner.temp }}/ddl-report/nx-storage-touch-report.json", "${{ runner.temp }}/ddl-report/other.json",
         "does not carry nx-storage-touch-report.json"),
        ("python3 -m tests._storage_touch_fan_in", "python3 -m tests._other", "found 0"),
        ("tests._storage_touch_fan_in ddl-reports --suite python_unit --shards 6",
         "tests._storage_touch_fan_in ddl-reports --suite python_unit --shards 5", "but the matrix has"),
        ("tests._storage_touch_fan_in ddl-reports --suite python_unit",
         "tests._storage_touch_fan_in elsewhere --suite python_unit", "does not read the downloaded"),
        ("            tests/_storage_touch_fan_in.py\n", "", "does not bring tests/_storage_touch_fan_in.py"),
        ("      - name: Tenant-saving ratchet over the whole run (storage-touch fan-in)\n"
         "        if: needs.changes.outputs.code == 'true'\n",
         "      - name: Tenant-saving ratchet over the whole run (storage-touch fan-in)\n"
         "        if: always()\n", "not limited to runs whose shards ran"),
        ("      - name: Tenant-saving ratchet over the whole run (storage-touch fan-in)\n"
         "        if: needs.changes.outputs.code == 'true'\n",
         "      - name: Tenant-saving ratchet over the whole run (storage-touch fan-in)\n"
         "        if: always() && needs.changes.outputs.code == 'true'\n", "after a failed verdict"),
    ],
)
def test_each_rot_is_caught(old: str, new: str, expect: str) -> None:
    text = CI_YML.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    problems = wiring_problems(text.replace(old, new), CEILINGS.read_text(encoding="utf-8"))
    assert any(expect in p for p in problems), problems


def test_arming_and_continue_on_error_move_together() -> None:
    text = CI_YML.read_text(encoding="utf-8")
    ceilings = CEILINGS.read_text(encoding="utf-8")
    armed = _armed(ceilings)
    flipped = (ceilings.replace("measured_on", "# measured_on") if armed
               else ceilings.replace("[python_unit]\n", '[python_unit]\nmeasured_on = "test"\n', 1))
    assert _armed(flipped) is not armed
    assert any("continue-on-error" in p or "measured_on" in p for p in wiring_problems(text, flipped))


def test_a_fan_in_moved_before_the_verdict_is_caught() -> None:
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    steps = doc["jobs"]["pytest-gate"]["steps"]
    fan_in = next(s for s in steps if _FAN_IN in str(s.get("run", "")))
    steps.remove(fan_in)
    steps.insert(0, fan_in)
    problems = wiring_problems(yaml.safe_dump(doc), CEILINGS.read_text(encoding="utf-8"))
    assert any("after the one verdict step" in p for p in problems), problems
