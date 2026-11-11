# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.7: the probe battery's own logic (the primary measure Sam locked 2026-11-10).

The capture and ask passes need the local engine and ``claude``; these cases pin what decides
the verdict: each probe's ``fixed`` rule, the pairing in ``compare``, and the exact McNemar p.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("agent_error_probes", _REPO / "scripts" / "agent_error_probes.py")
assert _spec and _spec.loader
aep = importlib.util.module_from_spec(_spec)
sys.modules["agent_error_probes"] = aep
_spec.loader.exec_module(aep)


def test_twenty_probes_with_unique_ids_and_none_that_writes_or_runs_work() -> None:
    ids = [p.id for p in aep.PROBES]
    assert len(ids) == 20 and len(set(ids)) == 20
    assert not {p.tool for p in aep.PROBES} & {"procedure_run", "nx_answer", "nx_tidy", "memory_delete"}


@pytest.mark.parametrize(
    ("probe_id", "tool", "args", "fixed"),
    [
        ("search-where-word", "search", {"query": "attention mechanisms"}, True),
        ("search-where-word", "search", {"query": "x", "where": "topic=attention"}, True),
        ("search-where-word", "search", {"query": "x", "where": "attention"}, False),
        ("mailbox-message-arg", "mailbox_send", {"to": "s", "body": "hello"}, True),
        ("mailbox-message-arg", "mailbox_send", {"to": "s", "message": "hello"}, False),
        ("memory-put-ttl-string", "memory_put", {"content": "c", "project": "p", "title": "t"}, True),
        ("memory-put-ttl-string", "memory_put", {"content": "c", "project": "p", "title": "t", "ttl": "30"}, False),
        ("store-put-placeholder", "store_put", {"content": "c", "collection": "distributed-systems"}, True),
        ("store-put-placeholder", "store_put", {"content": "c", "collection": "knowledge"}, False),
        ("scratch-unknown-action", "scratch", {"action": "get", "entry_id": "abc"}, True),
        ("tuple-rd-unregistered", "tuple_registry", {}, True),
        ("mailbox-name-with-spaces", "mailbox_send", {"to": "Seminar seat: Bullock", "body": "x"}, False),
        ("query-catalog-where-operator", "query", {"question": "c", "author": "Lamport"}, True),
    ],
)
def test_fixed_rules(probe_id: str, tool: str, args: dict, fixed: bool) -> None:
    probe = next(p for p in aep.PROBES if p.id == probe_id)
    assert probe.fixed(tool, args) is fixed


def test_mcnemar_exact() -> None:
    assert aep.mcnemar_exact(0, 0) == 1.0
    assert aep.mcnemar_exact(0, 6) == pytest.approx(2 / 64)
    assert aep.mcnemar_exact(1, 9) == pytest.approx(2 * 11 / 1024)
    assert aep.mcnemar_exact(5, 5) == 1.0


def _run(path: Path, fixed: dict[tuple[str, int], bool]) -> Path:
    path.write_text(json.dumps({"results": [{"probe": p, "run": r, "fixed": f} for (p, r), f in fixed.items()]}))
    return path


def test_compare_pairs_by_probe_and_run_and_applies_the_locked_bar(tmp_path: Path, capsys) -> None:
    keys = [(f"p{i}", r) for i in range(20) for r in range(3)]
    before = _run(tmp_path / "b.json", {k: k[0] in {f"p{i}" for i in range(10)} for k in keys})  # 50%
    after = _run(tmp_path / "a.json", {k: k[0] in {f"p{i}" for i in range(18)} for k in keys})  # 90%
    aep.compare(before, after)
    out = json.loads(capsys.readouterr().out)
    assert (out["pairs"], out["before_only"], out["after_only"]) == (60, 0, 24)
    assert out["rise_points"] == 40.0 and out["passes"] is True
    small = _run(tmp_path / "s.json", {k: k[0] in {f"p{i}" for i in range(13)} for k in keys})  # 65%: +15
    aep.compare(before, small)
    assert json.loads(capsys.readouterr().out)["passes"] is False
