# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.1 fix round A, critique B1: the completion check's state is not ``boot_work``, so the two idle rules that
count every non-parked ``boot_work`` entry as work (the bundle swap's ``engine_busy_reason`` and the 3h harness's
``boot_work_settled``) never see it.

The fixtures are the engine's own ``/v1/status`` bodies (reduced to ``boot_work`` and ``rdr229_completion``: the rest is the
process's own counters) in the three states the review named
(``service/.../CompletionStatusFieldIntegrationTest`` renders them through ``StatusHandler`` and fails when they differ from
these files; ``-Dnexus.rdr229.writeFixtures=true`` rewrites them):

* ``all_stamps_done_gate3_waiting``: every stamp move done and validated, gate 3 waiting on P3.4 (it waits in every release
  until P3.4, so on every install);
* ``stamps_blocked``: an install whose bundle lacks pg_background: the stamp moves are open and the move reporter says
  ``blocked:``, which the idle rules discount;
* ``superseded_by_check_missing``: the superseded_by CHECK is held back by D2's data.

An older nx reads ``boot_work`` the same way, which is why the engine does not publish the entries at all, and why the
control case below (the old entry added back) is busy: it proves the cases would fail if the entry came back.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from nexus.daemon import pg_bundle_refresh

_REPO = Path(__file__).parent.parent.parent
_FIXTURES = _REPO / "tests" / "fixtures" / "rdr229_completion_status"
_STATES = ["all_stamps_done_gate3_waiting", "stamps_blocked", "superseded_by_check_missing"]

_spec = importlib.util.spec_from_file_location("upgrade_boot_writes", _REPO / "scripts" / "upgrade_boot_writes.py")
_harness = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("upgrade_boot_writes", _harness)
_spec.loader.exec_module(_harness)

_OLD_ENTRY = {
    "id": "rdr229-completion",
    "what": "gate 1 holds; gate 2 holds; gate 3 waits: this release builds no id-predicate index (P3.4)",
    "started_at": 1.0, "done": 2, "total": 3, "unit": "gates", "safe_to_stop": True, "on_stop": "x", "step": None,
}


def _fixture(name: str) -> dict:
    path = _FIXTURES / f"{name}.json"
    assert path.is_file(), (
        f"{path} is the engine's body: run CompletionStatusFieldIntegrationTest with -Dnexus.rdr229.writeFixtures=true")
    return json.loads(path.read_text())


def _busy(monkeypatch, tmp_path, status: dict) -> str | None:
    monkeypatch.setattr(pg_bundle_refresh, "_engine_status", lambda: status)
    monkeypatch.setattr(pg_bundle_refresh, "_service_up", lambda _cfg: True)
    monkeypatch.setattr(pg_bundle_refresh, "_progress_rows", lambda _cfg: 0)
    return pg_bundle_refresh.engine_busy_reason(tmp_path)


@pytest.mark.parametrize("state", _STATES)
def test_the_bundle_swap_proceeds_on_the_engines_real_status(state, monkeypatch, tmp_path):
    status = _fixture(state)
    assert "rdr229_completion" in status, "non-vacuity: the body carries the new field"
    assert not [e for e in status["boot_work"] if str(e.get("id", "")).startswith("rdr229")]
    assert _busy(monkeypatch, tmp_path, status) is None


@pytest.mark.parametrize("state", _STATES)
def test_the_boot_write_harness_reaches_its_idle_rule_on_the_engines_real_status(state):
    status = _fixture(state)
    assert _harness.boot_work_settled(status)
    assert _harness.status_says_idle(200, status)
    assert _harness.failed_moves(status) == []


@pytest.mark.parametrize("state", _STATES)
def test_control_the_old_entry_would_have_deferred_the_swap_and_blocked_the_harness(state, monkeypatch, tmp_path):
    status = json.loads(json.dumps(_fixture(state)))
    status["boot_work"].append(_OLD_ENTRY)
    assert _busy(monkeypatch, tmp_path, status) is not None
    assert not _harness.boot_work_settled(status)


def test_the_blocked_state_carries_a_parked_move_the_idle_rules_discount():
    status = _fixture("stamps_blocked")
    parked = _harness.parked_moves(status)
    assert len(parked) == 1 and parked[0]["id"].startswith("data-move:rdr229-stamp-")
