# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-3ceda.7: the probe battery's own logic.

capture and ask need the local engine and ``claude``; these cases pin what decides a verdict: each
probe's ``fixed`` rule (both ways), the registered-tool gate, the comparability refusals, the pass
rule at its boundary, and the exact McNemar p.
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

#: Tools whose valid call writes, sends or runs work. A probe on one must say what refuses it.
WRITERS = {"mailbox_send", "memory_put", "store_put", "tuple_out", "tuple_ack", "tuple_in", "tuple_subscribe",
           "procedure_run", "nx_answer", "nx_tidy", "memory_delete", "scratch_manage"}

#: For every probe: a proposal its rule must accept, and one it must refuse.
CASES: dict[str, tuple[tuple[str, dict], tuple[str, dict]]] = {
    "search-where-word": (("search", {"query": "attention"}), ("search", {"query": "x", "where": "attention"})),
    "search-where-title": (("search", {"query": "x", "where": "title=Horizontal"}), ("search", {"query": "x", "where": "Horizontal"})),
    "mailbox-message-arg": (("mailbox_send", {"to": "s", "body": "hello"}), ("mailbox_send", {"to": "s", "message": "hello"})),
    "memory-put-ttl-string": (("memory_put", {"title": "t"}), ("memory_put", {"title": "t", "ttl": "30"})),
    "memory-get-no-project": (("memory_get", {"project": "nexus", "title": "push-train"}), ("memory_get", {"title": "push-train"})),
    "memory-put-no-title": (("memory_put", {"content": "c", "title": "t"}), ("memory_put", {"content": "c"})),
    "tuple-subscribe-no-subspace": (("tuple_subscribe", {"subspace": "mailbox/tamsin"}), ("tuple_subscribe", {})),
    "tuple-rd-unregistered": (("tuple_registry", {}), ("tuple_rd", {"subspace": "ledger/probe-v0174"})),
    "tuple-ack-missing": (("tuple_ack", {"claim_id": "c-123", "claimant": "tamsin"}), ("tuple_ack", {"claim_id": "c-123"})),
    "tuple-out-no-keys": (("tuple_out", {"subspace": "board/probe", "keys": {"k": "v"}}), ("tuple_out", {"subspace": "board/probe"})),
    "tuple-rd-no-subspace": (("tuple_rd", {"subspace": "board/ci"}), ("tuple_rd", {})),
    "store-put-placeholder": (("store_put", {"collection": "distributed-systems"}), ("store_put", {"collection": "knowledge"})),
    "store-get-many-no-ids": (("store_get_many", {"ids": ["abc", "def"]}), ("store_get_many", {"ids": "abc"})),
    "scratch-unknown-action": (("scratch", {"action": "get", "entry_id": "abc"}), ("scratch", {"action": "read", "entry_id": "abc"})),
    "scratch-get-no-id": (("scratch", {"action": "get", "entry_id": "abc"}), ("scratch", {"action": "get"})),
    "metadata-scoped-operator": (("search_metadata_scoped", {"where": "topic=consensus"}), ("search_metadata_scoped", {"where": "year>=2020"})),
    "aspect-scoped-unknown-field": (("search_aspect_scoped", {"field": "experimental_datasets"}), ("search_aspect_scoped", {"field": "colour"})),
    "store-get-empty-id": (("store_get", {"doc_id": "push-train"}), ("store_get", {"doc_id": ""})),
    "mailbox-name-with-spaces": (("tuple_list", {}), ("mailbox_send", {"to": "Seminar seat: Bullock", "body": "x"})),
    "query-catalog-where-operator": (("query", {"question": "c", "author": "Lamport"}), ("query", {"question": "c", "author": "Lamport", "where": "year>=2020"})),
}
TOOLS = {t for (good, bad) in CASES.values() for t in (good[0], bad[0])} | {"search", "query"}


def test_twenty_probes_each_with_cases_and_writer_probes_say_what_refuses_them() -> None:
    ids = [p.id for p in aep.PROBES]
    assert len(ids) == 20 and len(set(ids)) == 20 and set(ids) == set(CASES)
    for p in aep.PROBES:
        assert p.expect, p.id
        if p.tool in WRITERS:
            assert p.refused_by, f"{p.id} calls a writing tool and does not say what refuses it"
    assert not {p.tool for p in aep.PROBES} & {"procedure_run", "nx_answer", "nx_tidy", "memory_delete"}


@pytest.mark.parametrize("probe_id", sorted(CASES))
def test_each_rule_accepts_its_fix_and_refuses_the_failed_call(probe_id: str) -> None:
    probe = next(p for p in aep.PROBES if p.id == probe_id)
    good, bad = CASES[probe_id]
    assert aep.score(probe, {"tool": good[0], "arguments": good[1]}, TOOLS) is True
    assert aep.score(probe, {"tool": bad[0], "arguments": bad[1]}, TOOLS) is False
    assert aep.score(probe, {"tool": probe.tool, "arguments": probe.args}, TOOLS) is False


@pytest.mark.parametrize("probe_id", sorted(CASES))
def test_a_tool_the_server_does_not_register_is_never_a_fix(probe_id: str) -> None:
    probe = next(p for p in aep.PROBES if p.id == probe_id)
    good = CASES[probe_id][0]
    assert aep.score(probe, {"tool": good[0], "arguments": good[1]}, TOOLS - {good[0]}) is False
    assert aep.score(probe, {"parse": "no call in reply"}, TOOLS) is False


def test_mcnemar_exact() -> None:
    assert aep.mcnemar_exact(0, 0) == 1.0
    assert aep.mcnemar_exact(0, 6) == pytest.approx(2 / 64)
    assert aep.mcnemar_exact(1, 9) == pytest.approx(2 * 11 / 1024)
    assert aep.mcnemar_exact(5, 5) == 1.0


def _run(path: Path, fixed: dict[tuple[str, int], bool], **manifest) -> Path:
    m = {"model": "m", "probe_set": "p", "prompt": "q", "runs": 3, **manifest}
    path.write_text(json.dumps({"manifest": m, "results": [{"probe": p, "run": r, "fixed": f} for (p, r), f in fixed.items()]}))
    return path


_KEYS = [(f"p{i}", r) for i in range(20) for r in range(3)]


def _share(n_probes: int) -> dict:
    return {k: k[0] in {f"p{i}" for i in range(n_probes)} for k in _KEYS}


def _verdict(tmp_path: Path, capsys, before: dict, after: dict) -> dict:
    assert aep.compare(_run(tmp_path / "b.json", before), _run(tmp_path / "a.json", after)) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out) | {"_err": captured.err}


def test_the_bar_passes_at_exactly_its_points_and_needs_probe_level_significance(tmp_path: Path, capsys) -> None:
    out = _verdict(tmp_path, capsys, _share(4), _share(8))  # 20% -> 40%: exactly +20, 4 probes flipped
    assert out["rise_points"] == 20.0 and out["probe_level"]["after_only"] == 4
    assert out["passes"] is False, "4 flipped probes of 20 is p=0.125 at probe level, whatever the run level says"
    out = _verdict(tmp_path, capsys, _share(4), _share(10))  # +30, 6 flipped: p=0.031
    assert out["passes"] is True and out["probe_level"]["mcnemar_p"] < 0.05
    # exactly +20 points by run (12 of 60) over 6 probes, as integers: no float shortfall at the bar
    before = {k: k[0] in {"p0", "p1", "p2", "p3", "p4", "p5", "p6", "p7"} for k in _KEYS}
    after = dict(before) | {(f"p{i}", r): True for i in range(8, 14) for r in range(2)}
    out = _verdict(tmp_path, capsys, before, after)
    assert out["rise_points"] == 20.0 and out["passes"] is True


def test_a_rise_below_the_bar_fails_and_a_ceiling_says_the_bar_is_unreachable(tmp_path: Path, capsys) -> None:
    out = _verdict(tmp_path, capsys, _share(10), _share(13))
    assert out["passes"] is False
    out = _verdict(tmp_path, capsys, _share(17), _share(20))  # 85% before: at most +15
    assert out["bar_reachable"] is False and out["passes"] is False
    assert "cannot pass" in out["_err"]


@pytest.mark.parametrize("field", ["model", "probe_set", "prompt", "runs"])
def test_compare_refuses_runs_that_are_not_comparable(field: str, tmp_path: Path) -> None:
    before = _run(tmp_path / "b.json", _share(5))
    after = _run(tmp_path / "a.json", _share(5), **{field: "other"})
    assert aep.compare(before, after) == 1


def test_compare_refuses_no_pairs(tmp_path: Path) -> None:
    before = _run(tmp_path / "b.json", {("x", 0): True})
    after = _run(tmp_path / "a.json", {("y", 0): True})
    assert aep.compare(before, after) == 1


def test_capture_refuses_anything_but_a_local_engine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import nexus.config  # noqa: PLC0415 — the switch the guard reads

    monkeypatch.setattr(nexus.config, "is_local_mode", lambda: False)
    assert "not in local mode" in aep.local_engine_refusal()
    out = tmp_path / "e.json"
    assert aep.capture(out) == 1 and not out.exists(), "a refused capture writes nothing and calls nothing"
    monkeypatch.setattr(nexus.config, "is_local_mode", lambda: True)
    monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
    assert "not a local engine" in aep.local_engine_refusal()
    monkeypatch.setenv("NX_SERVICE_URL", "http://127.0.0.1:5999")
    assert aep.local_engine_refusal() == ""
