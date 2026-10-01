# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-z0o2p.41: the GitHub-backed ``mandatory_regression_pin`` tests live outside the
local-service gate, and they still run, at a zero skip budget, where gh auth exists.

The gate's fenced HOME never mirrors ``~/.config/gh``, so under it these pins skip and conftest's zero
budget reddened the gate on every run; the gate also carries the cut battery's only positive control.
Sam's decision: move the pins out of the gate's selection and run them in a leg with a real HOME. The
two halves are pinned separately, because a move that only does the first half silently deletes the
pins: (1) the gate's selection holds none of them; (2) ``tests/e2e/mandatory-pins-gate.sh`` runs them
and fails when they did not run.

Each test names the mutation it was shown red under.
"""
from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / "tests" / "e2e"
LIB_SH = E2E / "lib" / "mandatory_pins.sh"
CHECK = E2E / "lib" / "mandatory_pins_check.py"
GATE = E2E / "mandatory-pins-gate.sh"
LSG = E2E / "local-service-gate.sh"
NIGHTLY = REPO_ROOT / ".github" / "workflows" / "local-service-gate-nightly.yml"


def _lib_value(name: str) -> str:
    r = subprocess.run(
        ["bash", "-c", f'. "{LIB_SH}"; printf %s "${name}"'], capture_output=True, text=True, check=True, timeout=30,
    )
    return r.stdout


def _pin_test_ids() -> set[str]:
    """``path::name`` of every test carrying ``mandatory_regression_pin``, read from the source with
    ast so no 60-second collection is needed to know what the marker covers."""
    found: set[str] = set()
    for path in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
        if path == Path(__file__).resolve():
            continue
        text = path.read_text(errors="ignore")
        if "mandatory_regression_pin" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                if any("mandatory_regression_pin" in ast.unparse(d) for d in node.decorator_list):
                    found.add(f"{path.relative_to(REPO_ROOT)}::{node.name}")
    return found


def test_the_declared_pin_count_is_the_real_one() -> None:
    """The count both gates hold is the number of pins in the tree. Mutation (add a pin without bumping
    MANDATORY_PIN_EXPECTED, or bump it without one): red."""
    ids = _pin_test_ids()
    assert len(ids) == int(_lib_value("MANDATORY_PIN_EXPECTED")), sorted(ids)


@pytest.mark.lint
def test_the_local_service_gates_selection_contains_no_mandatory_pin() -> None:
    """The gate's REAL pytest selection, collected, holds none of the pins; the pins exist (non-vacuity of
    this test); and the selection the gate used BEFORE this change did hold them (the control that shows
    the assertion can fail). Mutation (drop ``and not mandatory_regression_pin`` from
    LSG_PYTEST_MARK_EXPR): red, naming the four pins."""
    pins = _pin_test_ids()
    assert pins, "no mandatory_regression_pin test found: this test would pass on nothing"
    expr = _lib_value("LSG_PYTEST_MARK_EXPR")
    old_expr = "integration and not lived_in and not cloud_mode"
    assert expr.startswith(old_expr), expr

    def collect(mark: str) -> set[str]:
        r = subprocess.run(
            ["uv", "run", "pytest", "-o", "addopts=", "-m", mark, "--collect-only", "-q"],
            capture_output=True, text=True, timeout=600, cwd=REPO_ROOT,
            env={**os.environ, "NX_TEST_T2_SUBSTRATE": "none"},
        )
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        return {ln.strip() for ln in r.stdout.splitlines() if "::" in ln and not ln.startswith(" ")}

    selected = collect(expr)
    assert len(selected) > 100, "the gate's selection collected almost nothing; this test would pass on nothing"
    leaked = sorted(i for i in selected if i.split("[")[0] in pins)
    assert leaked == [], f"the local-service gate's selection still runs {leaked}: they skip under its fenced HOME"
    # The control: the old expression DID select every pin, so the assertion above can fail.
    before = collect(old_expr)
    assert pins <= {i.split("[")[0] for i in before}, "the control selection no longer holds the pins; the test proves nothing"


def test_lsg_runs_its_pytest_with_the_shared_expression_and_holds_the_carve_out_to_a_count() -> None:
    """The gate must not hand-write its own expression (a second copy is how a pin comes back), and must
    bound the carve-out the way lived_in and cloud_mode are bounded. Mutation (hard-code the old expression
    in the pytest line, or delete the count guard): red."""
    text = LSG.read_text()
    assert 'tests/e2e/lib/mandatory_pins.sh' in text
    pytest_lines = [ln for ln in text.splitlines() if re.search(r'\buv run pytest -m "\$LSG_PYTEST_MARK_EXPR"', ln)]
    assert len(pytest_lines) == 1, pytest_lines
    assert 'MANDATORY_PIN_COUNT' in text and '-ne "$MANDATORY_PIN_EXPECTED"' in text
    assert not re.search(r'pytest -m "integration and not lived_in and not cloud_mode"', text)


def test_the_fence_still_never_mirrors_gh_and_the_budget_default_is_still_zero() -> None:
    """The decision keeps both: no token passes into the fence, the budget is not raised for the gate.
    Mutation (drop .config/gh from FENCE_ALWAYS_SHADOWS, or set the gate's budget): red."""
    fence = (E2E / "lib" / "fence_home.sh").read_text()
    shadows = re.search(r"FENCE_ALWAYS_SHADOWS=\((.*?)\n\)", fence, re.S)
    assert shadows and '".config/gh"' in shadows.group(1)
    assert re.search(r"^\s*[^#\n]*NX_MANDATORY_PIN_SKIP_BUDGET", LSG.read_text(), re.M) is None, \
        "the gate must not set or raise the pin skip budget"
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text()
    assert re.search(r'env_var="NX_MANDATORY_PIN_SKIP_BUDGET",\s*default_budget=0', conftest)


def test_the_nightly_runs_the_pins_in_their_own_step_and_not_in_the_gates() -> None:
    """The nightly keeps the pins: a step runs the pins gate with the job token, and the lsg step carries
    neither the token nor a raised budget. Mutation (leave the budget on the lsg step, or drop the pins
    step): red."""
    wf = yaml.safe_load(NIGHTLY.read_text())
    steps = wf["jobs"]["gate"]["steps"]
    lsg = [s for s in steps if "tests/e2e/local-service-gate.sh" in str(s.get("run", ""))]
    pins = [s for s in steps if "tests/e2e/mandatory-pins-gate.sh" in str(s.get("run", ""))]
    assert len(lsg) == 1 and len(pins) == 1, (lsg, pins)
    for var in ("NX_MANDATORY_PIN_SKIP_BUDGET", "GITHUB_TOKEN", "GH_TOKEN"):
        assert var not in (lsg[0].get("env") or {}), var
    assert "GITHUB_TOKEN" in pins[0]["env"] and pins[0]["env"]["NX_MANDATORY_PIN_SKIP_BUDGET"] == "1"
    assert steps.index(pins[0]) > steps.index(lsg[0])


# ── the pins gate itself, against a fake `uv` ───────────────────────────────

_JUNIT = """<?xml version="1.0"?><testsuites><testsuite>{cases}</testsuite></testsuites>"""


def _junit(*, ran: int = 0, skipped: int = 0, failed: int = 0) -> str:
    cases = [f'<testcase classname="t" name="ran{i}"/>' for i in range(ran)]
    cases += [f'<testcase classname="t" name="skip{i}"><skipped message="no gh auth"/></testcase>' for i in range(skipped)]
    cases += [f'<testcase classname="t" name="fail{i}"><failure message="x"/></testcase>' for i in range(failed)]
    return _JUNIT.format(cases="".join(cases))


@pytest.fixture()
def fake_uv(tmp_path: Path):
    """A `uv` that answers the gate's two pytest calls: the collection and the run."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text(textwrap.dedent("""\
        #!/bin/bash
        case "$*" in
          *--collect-only*)
            i=0; while [ "$i" -lt "${FAKE_COLLECTED:-4}" ]; do echo "tests/x.py::test_$i"; i=$((i+1)); done; exit 0 ;;
        esac
        for a in "$@"; do case "$a" in --junit-xml=*) cp "$FAKE_JUNIT" "${a#--junit-xml=}" ;; esac; done
        echo "$*" >> "$FAKE_LOG"
        exit "${FAKE_RC:-0}"
        """))
    uv.chmod(0o755)

    def run(junit: str, *, collected: int = 4, rc: int = 0, budget: str | None = None) -> subprocess.CompletedProcess[str]:
        (tmp_path / "pins.xml").write_text(junit)
        env = {
            "PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
            "FAKE_JUNIT": str(tmp_path / "pins.xml"), "FAKE_LOG": str(tmp_path / "uv.log"),
            "FAKE_COLLECTED": str(collected), "FAKE_RC": str(rc),
        }
        if budget is not None:
            env["NX_MANDATORY_PIN_SKIP_BUDGET"] = budget
        return subprocess.run(["bash", str(GATE)], capture_output=True, text=True, timeout=120, env=env, cwd=tmp_path)

    run.log = tmp_path / "uv.log"  # type: ignore[attr-defined]
    return run


def test_the_pins_gate_passes_when_every_pin_ran(fake_uv) -> None:
    r = fake_uv(_junit(ran=4))
    assert r.returncode == 0 and r.stdout.rstrip().endswith("MANDATORY PINS GATE PASSED"), (r.stdout, r.stderr)
    assert "integration and mandatory_regression_pin" in fake_uv.log.read_text()


def test_the_pins_gate_runs_at_a_zero_budget_unless_told_otherwise(fake_uv) -> None:
    """A pin that skipped proved nothing. Mutation (default the budget above zero, or drop the junit read):
    the one-skip run reads PASSED."""
    r = fake_uv(_junit(ran=3, skipped=1))
    assert r.returncode == 1 and r.stdout.rstrip().endswith("MANDATORY PINS GATE FAILED"), (r.stdout, r.stderr)
    assert "skipped over the budget" in r.stdout + r.stderr


def test_the_pins_gate_honours_an_explicit_budget_and_only_that_one(fake_uv) -> None:
    """CI's job token cannot read branch protection: the nightly step sets the budget to 1 for that one pin."""
    assert fake_uv(_junit(ran=3, skipped=1), budget="1").returncode == 0
    assert fake_uv(_junit(ran=2, skipped=2), budget="1").returncode == 1


def test_the_pins_gate_fails_when_all_pins_skipped_even_if_pytest_exits_zero(fake_uv) -> None:
    """The vacuous run: pytest exits 0 on an all-skipped selection. Mutation (drop the non-vacuity read):
    PASSED on four skips at a budget of four."""
    r = fake_uv(_junit(skipped=4), budget="4")
    assert r.returncode == 1 and "no pin ran" in r.stdout + r.stderr


def test_the_pins_gate_fails_on_a_wrong_pin_count_before_and_after_the_run(fake_uv) -> None:
    """Collected count is held exactly (a new pin must bump the constant), and so is the reported count."""
    r = fake_uv(_junit(ran=4), collected=3)
    assert r.returncode == 1 and "expected exactly 4" in r.stdout + r.stderr
    assert not fake_uv.log.exists(), "the run must not start after the count check failed"
    r = fake_uv(_junit(ran=3))
    assert r.returncode == 1 and "reported 3 pin test(s), expected exactly 4" in r.stdout + r.stderr


def test_the_pins_gate_fails_when_pytest_fails_or_a_pin_fails(fake_uv) -> None:
    assert fake_uv(_junit(ran=4), rc=1).returncode == 1
    r = fake_uv(_junit(ran=3, failed=1))
    assert r.returncode == 1, (r.stdout, r.stderr)


def test_the_check_script_exits_two_on_bad_usage(tmp_path: Path) -> None:
    r = subprocess.run(["python3", str(CHECK), "nope"], capture_output=True, text=True)
    assert r.returncode == 2
    r = subprocess.run(["python3", str(CHECK), str(tmp_path / "missing.xml"), "4", "0"], capture_output=True, text=True)
    assert r.returncode == 1 and "cannot read" in r.stdout


def test_the_pins_gate_is_executable_and_the_battery_names_it() -> None:
    assert os.access(GATE, os.X_OK)
    assert "tests/e2e/mandatory-pins-gate.sh" in (E2E / "release-battery.sh").read_text()
    assert shutil.which("bash")
