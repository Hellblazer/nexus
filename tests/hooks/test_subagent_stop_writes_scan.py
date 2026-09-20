# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unlanded-write scan tripwire (nexus-piqm5 Layer 1).

The bead's own bar: any fix "must be falsifiable by breaking the store. A
check that passes when persistence is unavailable is the same defect one
level up." These tests hold the scan to it.

THE FIXTURE IS BUILT FROM PRODUCTION CODE, NOT FROM A COPIED STRING. The
failure text a broken store actually produces comes from
``nexus.mcp.core._mcp_tool_error``; ``test_falsifier_*`` calls that function
with a real ``SESSION_UNAUTHORIZED_MARKER`` exception -- the literal
2026-08-25 outage condition -- and feeds its output into the transcript. If
someone changes the returned prefix, the scan stops matching AND these tests
go red together. A hardcoded ``"Error: ..."`` fixture would keep passing
while the scan silently matched nothing in production, which is the exact
class of inert guard this bead exists to eliminate.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nexus.hooks import subagent_stop_scans as scans


def run_scan(path: Path) -> str:
    """The scan's own contract: CLEAN / UNLANDED <n> <tools> / SCANERROR.

    Ported from the plugin's bash writes-scan script (RDR-215 bead
    nexus-q02nx.12); the script itself is deleted at bead .21 once
    ``hooks.json`` no longer runs it (bead .21 also re-points the wired
    entry). ``nexus.hooks.subagent_stop.writes_verdict`` (the
    hook's own production wrapper) additionally collapses SCANERROR and a
    missing/unreadable transcript to CLEAN before use -- exercised
    directly in :func:`test_hook_consults_the_scan_and_only_acts_on_unlanded`
    below rather than here, since that collapse is the HOOK's contract,
    not the scan's.
    """
    try:
        count, tools = scans.writes_scan(str(path))
    except Exception:  # noqa: BLE001 — mirrors the scan's own bare except
        return "SCANERROR"
    return f"UNLANDED {count} {','.join(tools)}" if count else "CLEAN"


def _tool_use(tid: str, name: str, tool_input: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": tid, "name": name,
             "input": tool_input if tool_input is not None else {}},
        ]},
    }


def _tool_result(tid: str, content, is_error: bool | None = None) -> dict:
    block: dict = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        block["is_error"] = is_error
    return {"type": "user", "message": {"content": [block]}}


def write_transcript(tmp_path: Path, entries: list[dict], name="t.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return p


def real_store_failure_text() -> str:
    """The string a genuinely broken T1 hands back, from production code."""
    from nexus.db.http_scratch_store import SESSION_UNAUTHORIZED_MARKER
    from nexus.mcp.core import _mcp_tool_error

    return _mcp_tool_error("memory_put", RuntimeError(SESSION_UNAUTHORIZED_MARKER))


# ── non-vacuity: break the store, the scan MUST trip ────────────────────────

def test_falsifier_real_401_failure_is_detected(tmp_path):
    """The 2026-08-25 condition, reproduced from production code."""
    failure = real_store_failure_text()
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__memory_put",
                  {"project": "nexus", "title": "findings", "content": "..."}),
        _tool_result("a1", failure),
    ])
    verdict = run_scan(t)
    assert verdict.startswith("UNLANDED "), verdict
    assert "memory_put" in verdict


def test_falsifier_precondition_the_real_text_is_error_prefixed(tmp_path):
    """Guard the guard: if this drifts, the scan above is inert, not passing.

    The scan keys on an "Error:" prefix. Assert production still produces
    one, so a change to _mcp_tool_error cannot leave the scan quietly
    matching nothing while its own test keeps passing on a stale fixture.
    """
    assert real_store_failure_text().lstrip().startswith("Error:")


def test_falsifier_is_error_flag_alone_is_enough(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__store_put", {"content": "x"}),
        _tool_result("a1", "anything at all", is_error=True),
    ])
    assert run_scan(t).startswith("UNLANDED ")


# ── the other direction: it must not fire on healthy transcripts ────────────

def test_successful_writes_are_clean(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__memory_put", {"title": "x"}),
        _tool_result("a1", "Stored: [23582] nexus/x"),
        _tool_use("a2", "mcp__plugin_conexus_nexus__store_put", {"content": "y"}),
        _tool_result("a2", "Stored document abc123 in knowledge"),
    ])
    assert run_scan(t) == "CLEAN"


def test_read_only_agent_is_clean(tmp_path):
    """An agent that never writes cannot have unlanded writes."""
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__search", {"query": "x"}),
        _tool_result("a1", "Error: search blew up"),
        _tool_use("a2", "Read", {"file_path": "/etc/hosts"}),
        _tool_result("a2", "Error: nope"),
    ])
    assert run_scan(t) == "CLEAN"


def test_failed_read_on_a_write_capable_tool_is_not_a_write(tmp_path):
    """`scratch` is a write only for action=put; a failed search is a read."""
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__scratch",
                  {"action": "search", "query": "x"}),
        _tool_result("a1", "Error: T1 unreachable"),
    ])
    assert run_scan(t) == "CLEAN"


def test_scratch_put_failure_is_a_write(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__scratch",
                  {"action": "put", "content": "findings"}),
        _tool_result("a1", "Error: T1 unreachable"),
    ])
    verdict = run_scan(t)
    assert verdict.startswith("UNLANDED "), verdict
    assert "scratch" in verdict


def test_counts_are_per_failed_call_and_tools_deduped(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "mcp__plugin_conexus_nexus__memory_put", {"title": "1"}),
        _tool_result("a1", "Error: boom"),
        _tool_use("a2", "mcp__plugin_conexus_nexus__memory_put", {"title": "2"}),
        _tool_result("a2", "Error: boom"),
        _tool_use("a3", "mcp__plugin_conexus_nexus__store_put", {"content": "3"}),
        _tool_result("a3", "Stored document ok"),
    ])
    assert run_scan(t) == "UNLANDED 2 memory_put"


# ── robustness: never crash, never invent evidence from absence ─────────────

def test_junk_lines_are_skipped_not_fatal(tmp_path):
    failure = real_store_failure_text()
    p = tmp_path / "t.jsonl"
    p.write_text(
        "not json at all\n"
        + "{ broken json\n"
        + "\n"
        + json.dumps(_tool_use("a1", "memory_put", {"title": "x"})) + "\n"
        + json.dumps(_tool_result("a1", failure)) + "\n",
        encoding="utf-8",
    )
    assert run_scan(p).startswith("UNLANDED ")


def test_missing_transcript_is_scanerror_not_unlanded(tmp_path):
    assert run_scan(tmp_path / "nope.jsonl") == "SCANERROR"


def test_empty_transcript_is_clean(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("", encoding="utf-8")
    assert run_scan(p) == "CLEAN"


def test_result_content_as_block_list_is_flattened(tmp_path):
    t = write_transcript(tmp_path, [
        _tool_use("a1", "memory_put", {"title": "x"}),
        _tool_result("a1", [{"type": "text", "text": "Error: T2 down"}]),
    ])
    assert run_scan(t).startswith("UNLANDED ")


def test_orphan_result_without_its_tool_use_is_ignored(tmp_path):
    """A result whose tool_use never appeared is not evidence of a write."""
    t = write_transcript(tmp_path, [_tool_result("ghost", "Error: boom")])
    assert run_scan(t) == "CLEAN"


# ── the hook must actually consult the scan ─────────────────────────────────

def test_hook_consults_the_scan_and_only_acts_on_unlanded(tmp_path, monkeypatch):
    """Naming is not wiring: assert ``nexus.hooks.subagent_stop.run`` really
    calls ``writes_verdict`` and gates its block decision on the literal
    UNLANDED verdict. Re-pointed at the Python module (RDR-215 bead
    nexus-q02nx.21) from a bash-body string check on ``subagent-stop.sh``,
    now deleted -- this and the CLEAN companion below are the ONLY
    coverage anywhere that the stop hook actually consults this scan
    rather than merely naming it; ``tests/hooks/test_subagent_stop_hook.py``
    never exercises the unlanded-write branch at all."""
    from nexus.hooks import expectations as exp
    from nexus.hooks import subagent_stop

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("NX_ORCH_STOP_GUARD", "block")

    session_id, agent_id, agent_type = "sess-unlanded", "aunlanded00000001", "worker-x"
    exp.expectations_expect(session_id, agent_type, "background")

    failure = real_store_failure_text()
    t = write_transcript(tmp_path, [
        _tool_use("r1", "SendMessage", {"to": "main", "content": "done"}),
        _tool_use("w1", "mcp__plugin_conexus_nexus__memory_put",
                  {"project": "nexus", "title": "findings"}),
        _tool_result("w1", failure),
    ])
    payload = {
        "session_id": session_id, "agent_id": agent_id, "agent_type": agent_type,
        "agent_transcript_path": str(t), "stop_hook_active": False,
    }
    result = subagent_stop.run(payload)
    assert result.stdout is not None, "an unlanded write on a reporting agent must block"
    decision = json.loads(result.stdout)
    assert decision["decision"] == "block"
    ledger = (
        tmp_path / "state" / "nexus" / "orchestration" / f"{session_id}.expectations"
    ).read_text()
    assert f"\tUNLANDEDWRITE\t{agent_id}\t" in ledger
    assert f"\tBLOCKED\t{agent_id}\tunlanded-write\n" in ledger


def test_hook_never_acts_when_writes_are_clean(tmp_path, monkeypatch):
    """The other half of "only acts on unlanded": fail-open bias means
    anything that is not the literal UNLANDED verdict — including a clean
    transcript's CLEAN — must collapse to no-op rather than reaching the
    block branch. Same scaffolding as the positive case above, all writes
    landing instead of failing."""
    from nexus.hooks import expectations as exp
    from nexus.hooks import subagent_stop

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("NX_ORCH_STOP_GUARD", "block")

    session_id, agent_id, agent_type = "sess-clean", "aclean0000000001", "worker-y"
    exp.expectations_expect(session_id, agent_type, "background")

    t = write_transcript(tmp_path, [
        _tool_use("r1", "SendMessage", {"to": "main", "content": "done"}),
        _tool_use("w1", "mcp__plugin_conexus_nexus__memory_put", {"title": "x"}),
        _tool_result("w1", "Stored: [1] nexus/x"),
    ])
    payload = {
        "session_id": session_id, "agent_id": agent_id, "agent_type": agent_type,
        "agent_transcript_path": str(t), "stop_hook_active": False,
    }
    result = subagent_stop.run(payload)
    assert result.stdout is None
    ledger = tmp_path / "state" / "nexus" / "orchestration" / f"{session_id}.expectations"
    content = ledger.read_text() if ledger.exists() else ""
    assert "UNLANDEDWRITE" not in content
    assert "BLOCKED" not in content


@pytest.mark.parametrize("qualified", [
    "mcp__plugin_conexus_nexus__memory_put",
    "memory_put",
])
def test_tool_name_qualification_is_tolerated(tmp_path, qualified):
    t = write_transcript(tmp_path, [
        _tool_use("a1", qualified, {"title": "x"}),
        _tool_result("a1", "Error: boom"),
    ])
    assert run_scan(t).startswith("UNLANDED ")
