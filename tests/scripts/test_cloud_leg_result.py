# SPDX-License-Identifier: AGPL-3.0-or-later
"""The step record keeps a step's marked result lines, bounded and scrubbed (cloud_leg_result). Kept apart from
test_cloud_leg.py, which is at its size cap; the fixtures are that module's."""
from __future__ import annotations

import json
from pathlib import Path

from tests.scripts.test_cloud_leg import SECRET, _fake_sandbox, _res, _step, cl


def test_run_slot_records_a_bounded_scrubbed_result_line_and_nothing_else_of_stdout(tmp_path: Path) -> None:
    """A step's stdout is not kept; its `result: ` lines are, scrubbed and cut to 200 characters, as one JSON string."""
    nx_body = (f'#!/bin/bash\necho "chatter {SECRET}"\necho "result: first {SECRET} reason r1"\n'
               f'echo "result: second {"x" * 300}"\nprintf "#tap-ready\\n200\\tGET\\t/v1/x\\th\\n" >> "$NX_CLOUDLEG_TAP"\nexit 0\n')
    sb = _fake_sandbox(tmp_path, nx_body=nx_body)
    plan = {"run": "r0123456", "label": "cloudleg-r0123456", "steps": [_step()], "floor": 1}
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w", slot="cloud")
    got = res[0]["result"]
    assert got.startswith("first *** reason r1 | second x") and len(got) == 200
    assert SECRET not in json.dumps(res) and "chatter" not in json.dumps(res)
    assert json.loads(json.dumps(res))[0]["result"] == got


def test_a_step_that_prints_no_result_line_records_an_empty_result(tmp_path: Path) -> None:
    sb = _fake_sandbox(tmp_path, nx_body='#!/bin/bash\necho "plain output"\nexit 0\n')
    plan = {"run": "r0123456", "label": "cloudleg-r0123456", "steps": [_step()], "floor": 1}
    res = cl.run_slot(plan, runner=str(sb / "run"), sandbox=sb, host="h", workdir=tmp_path / "w", slot="cloud")
    assert res[0]["result"] == ""


def test_compare_reads_records_with_and_without_a_result_key() -> None:
    """The comparator never judges on `result`: a record from before the key existed gets the same verdict."""
    plan = {"run": "r0123456", "label": "cloudleg-r0123456", "steps": [_step()], "floor": 1, "skipped": [], "failures": []}
    old = [_res("probe", "local")]
    new = [old[0] | {"result": "legal rename answered 409 reason rename_not_in_registry"}]
    a, b = cl.compare(plan, old, new, environ={}), cl.compare(plan, old, old, environ={})
    assert [(r["step"], r["verdict"]) for r in a.rows] == [(r["step"], r["verdict"]) for r in b.rows] and a.failed == b.failed == 0


def _plan() -> dict:
    return {"run": "r0123456", "label": "cloudleg-r0123456", "steps": [_step("probe")], "floor": 1, "skipped": [], "failures": []}


def test_render_shows_each_steps_result_under_its_row_so_a_green_report_keeps_it() -> None:
    local = _res("probe", "local") | {"result": "legal rename answered 200 gate3_latched=False"}
    cloud = _res("probe", "cloud") | {"result": "legal rename answered 200 gate3_latched=True"}
    text = cl.compare(_plan(), [local], [cloud], environ={}).render()
    lines = text.splitlines()
    row = next(i for i, ln in enumerate(lines) if ln.startswith("probe"))
    assert lines[row + 1] == ("    result: local: legal rename answered 200 gate3_latched=False"
                              " || cloud: legal rename answered 200 gate3_latched=True")
    same = _res("probe", "local") | {"result": "persist_split answered 400 error 'x'"}
    text = cl.compare(_plan(), [same], [same | {"slot": "cloud"}], environ={}).render()
    assert "    result: persist_split answered 400 error 'x'" in text.splitlines()


def test_render_prints_no_result_line_for_a_step_that_recorded_none() -> None:
    text = cl.compare(_plan(), [_res("probe", "local")], [_res("probe", "cloud")], environ={}).render()
    assert "result:" not in text
