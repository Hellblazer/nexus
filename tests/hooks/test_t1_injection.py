# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-mtheb: the T1 contract leads the SessionStart and SubagentStart
injections, the write-back line sends interim findings to T1, the T1 index
never carries entry contents, and machine-written T2 namespaces stay out.

T1 inter-agent use was designed five times and faded each time; a survey of
nine sessions on 2026-11-01 found 0 T1 writes in ~45 dispatches. In the hooks,
reviewer agents got no T1 guidance at all, the only write-back line sent
findings to T2 or T3, a machine namespace took the first T2 slot, and every
agent was handed every sibling's T1 contents.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from nexus.hooks import session_context, subagent_start, t2_prefix_scan
from tests._module_seam import setattr_in


def _ctx(agent_type: str, monkeypatch, scratch_list: str = "No scratch entries.") -> str:
    monkeypatch.setattr(subagent_start, "_t2_memory_section", lambda _root: "## T2 Memory\n(stub)\n\n")
    monkeypatch.setattr(subagent_start, "_linked_rdrs_section", lambda *_a: "")
    monkeypatch.setattr(subagent_start, "_knowledge_map_section", lambda _root: "")
    monkeypatch.setattr(subagent_start, "_run_captured", lambda *_a, **_k: scratch_list)
    setattr_in(monkeypatch, subagent_start, "shutil.which", lambda _c: "/usr/bin/nx")
    # agent_id is in every real SubagentStart payload; without it the claimant line
    # is never emitted and the order bar below tests a shape no dispatch has.
    r = subagent_start.run({
        "agent_type": agent_type, "agent_id": "a1b2c3d4e5f6a7b8c", "session_id": "s-t1",
        "prompt": "x", "cwd": "/tmp",
    })
    return json.loads(r.stdout or "{}")["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("agent_type", ["conexus:code-review-expert", "conexus:substantive-critic",
                                        "conexus:developer", "general-purpose", "Explore"])
def test_every_agent_type_gets_the_t1_contract_first(agent_type, monkeypatch) -> None:
    ctx = _ctx(agent_type, monkeypatch)
    assert ctx.startswith("## T1: this session's working layer")
    assert "Your scratch IS your parent session's T1" in ctx
    assert "independent reviewer, read nothing else in T1" in ctx
    # the claimant line follows the contract; it must not open the injection
    assert ctx.index("## T1:") < ctx.index("Claimant id: a1b2c3d4e5f6a7b8c")


def test_the_contract_states_what_t1_survives_and_asks_for_the_role_tag(monkeypatch) -> None:
    ctx = _ctx("conexus:developer", monkeypatch)
    assert "T1 survives compaction; a /clear, /resume or fresh restart starts a new scope" in ctx
    assert "swept 24 h after they were written" in ctx
    assert "flagged entries land in T2 at session end" in ctx
    assert "survives resume" not in ctx
    assert 'agent="<your role>"' in ctx


def test_t2_is_for_a_record_the_brief_or_the_agent_definition_requires(monkeypatch) -> None:
    ctx = _ctx("conexus:developer", monkeypatch)
    assert "T2 memory_put only for a record your brief or your agent definition requires" in ctx
    assert "(gate records stay mandatory)" in ctx


def test_the_tier_line_does_not_send_an_agent_to_its_siblings(monkeypatch) -> None:
    ctx = _ctx("conexus:developer", monkeypatch)
    assert "check before duplicating sibling work" not in ctx
    assert "T1 scratch           shared with siblings    <- read what your brief names" in ctx


def test_the_write_back_line_sends_interim_findings_to_t1(monkeypatch) -> None:
    ctx = _ctx("conexus:developer", monkeypatch)
    [line] = [ln for ln in ctx.splitlines() if ln.startswith("WRITE-BACK")]
    assert "interim findings to T1" in line
    assert "store_put (T3) or memory_put (T2) before returning" not in ctx


def test_the_t1_index_shows_ids_and_tags_never_contents(monkeypatch) -> None:
    listing = "\n".join(
        f"[abcd{i:04d}] nexus-x,review-r1  flagged=False\n  secret finding number {i} from a sibling critic"
        for i in range(20)
    )
    ctx = _ctx("conexus:substantive-critic", monkeypatch, scratch_list=listing)
    assert "## T1 index (ids and tags; read only what your brief names)" in ctx
    assert "secret finding" not in ctx
    assert "[abcd0000] nexus-x,review-r1" in ctx
    assert "[abcd0007]" in ctx and "[abcd0008]" not in ctx
    assert "... (12 more)" in ctx
    assert "scratch list)" not in ctx  # the hint pointed at the command that dumps contents


def test_an_empty_t1_adds_no_index(monkeypatch) -> None:
    assert "## T1 index" not in _ctx("conexus:developer", monkeypatch)


def test_text_with_no_entry_line_adds_no_index(monkeypatch) -> None:
    """nx scratch list answers with an error or a notice, not "[id] tags": the
    `if not index` guard keeps that text out of the section."""
    ctx = _ctx("conexus:developer", monkeypatch, scratch_list="Error: T1 server not found\nrun nx doctor")
    assert "## T1 index" not in ctx
    assert "T1 server not found" not in ctx


def test_a_content_line_holding_a_carriage_return_is_not_an_index_line(monkeypatch) -> None:
    """splitlines() breaks on \\r, so a content line "  note\\r[deadbeef] leaked"
    surfaced as an index line carrying sibling contents."""
    listing = "[abcd0001] nexus-x,r1  flagged=False\n  note\r[deadbeef] leaked sibling finding"
    ctx = _ctx("conexus:developer", monkeypatch, scratch_list=listing)
    assert "[abcd0001] nexus-x,r1" in ctx
    assert "leaked sibling finding" not in ctx
    assert "[deadbeef]" not in ctx


def test_an_index_line_is_clamped_so_tags_cannot_outgrow_the_section(monkeypatch) -> None:
    tags = ",".join(f"tag{i:03d}" for i in range(100))
    ctx = _ctx("conexus:developer", monkeypatch, scratch_list=f"[abcd0001] {tags}  flagged=False")
    [line] = [ln for ln in ctx.splitlines() if ln.startswith("[abcd0001]")]
    assert len(line.encode("utf-8")) == subagent_start._T1_INDEX_LINE_MAX
    assert line.endswith("…")


class _Store:
    """The HttpMemoryStore surface scan() calls."""

    def __init__(self, namespaces: list[str], last_updated: dict[str, str] | None = None) -> None:
        self._ns = namespaces
        self._ts = last_updated or {}

    def get_projects_with_prefix(self, _prefix: str) -> list[dict[str, Any]]:
        return [
            {"project": p, "last_updated": self._ts.get(p, "2026-11-01T00:00:00Z")} for p in self._ns
        ]

    def list_entries(self, project: str | None = None, **_k: Any) -> list[dict[str, Any]]:
        return [{"title": f"{project}-note", "project": project}]

    def get(self, project: str | None = None, title: str | None = None, **_k: Any) -> dict[str, Any]:
        return {"title": title, "content": "body"}

    def close(self) -> None:
        pass


def test_machine_written_namespaces_never_reach_an_injection(monkeypatch) -> None:
    store = _Store(["nexus_taxonomy_discover_health", "nexus", "nexus_rdr"])
    monkeypatch.setattr(t2_prefix_scan, "HttpMemoryStore", lambda **_k: store)
    out = t2_prefix_scan.scan("nexus")
    assert "taxonomy_discover_health" not in out
    assert "nexus-note" in out and "nexus_rdr-note" in out


def test_a_project_with_only_machine_namespaces_renders_nothing(monkeypatch) -> None:
    store = _Store(["nexus_taxonomy_discover_health"])
    monkeypatch.setattr(t2_prefix_scan, "HttpMemoryStore", lambda **_k: store)
    assert t2_prefix_scan.scan("nexus") == ""


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_fresh_machine_namespace_over_a_stale_human_one_gives_no_stale_warning(monkeypatch) -> None:
    """The warning asks whether the T2 substrate is live. The machine stamp is
    newest (DESC order puts it first), the human namespace is past the window:
    the substrate is live, so no warning; the filter is for display only."""
    store = _Store(
        ["nexus_taxonomy_discover_health", "nexus"],
        {"nexus_taxonomy_discover_health": _days_ago(0), "nexus": _days_ago(60)},
    )
    monkeypatch.setattr(t2_prefix_scan, "HttpMemoryStore", lambda **_k: store)
    out = t2_prefix_scan.scan("nexus")
    assert "nexus-note" in out
    assert "WARNING" not in out


def test_a_stale_substrate_still_warns(monkeypatch) -> None:
    """Non-vacuity for the test above: with nothing recent anywhere, it warns."""
    store = _Store(
        ["nexus_taxonomy_discover_health", "nexus"],
        {"nexus_taxonomy_discover_health": _days_ago(60), "nexus": _days_ago(90)},
    )
    monkeypatch.setattr(t2_prefix_scan, "HttpMemoryStore", lambda **_k: store)
    assert "WARNING: T2 memory freshest entry is 60d old" in t2_prefix_scan.scan("nexus")


def test_the_session_start_context_states_survival_and_the_claude_p_caveat(monkeypatch) -> None:
    monkeypatch.setattr(session_context, "which", lambda _c: False)
    monkeypatch.setattr(session_context, "run_command", lambda *_a, **_k: None)
    out = session_context.run(None).stdout or ""
    assert "T1 survives compaction; a `/clear`, `/resume` or fresh restart starts a new scope" in out
    assert "swept 24 h after they were written" in out
    assert "survives resume" not in out
    assert "A `claude -p` child has its own T1 unless started with `share_t1=True`" in out


def test_the_session_start_context_leads_with_the_t1_contract(monkeypatch) -> None:
    monkeypatch.setattr(session_context, "which", lambda _c: False)
    monkeypatch.setattr(session_context, "run_command", lambda *_a, **_k: None)
    out = session_context.run(None).stdout or ""
    assert out.startswith("## T1: the working layer this session shares with its subagents")
    assert "name its id in each brief" in out


def test_an_index_line_is_clamped_by_bytes_not_characters(monkeypatch) -> None:
    """A non-ASCII tag is up to 4 bytes a character; the budget counts bytes."""
    listing = "[abcd0001] " + "\u00e9" * 200 + "  flagged=False\n  content"
    ctx = _ctx("conexus:developer", monkeypatch, scratch_list=listing)
    [line] = [ln for ln in ctx.splitlines() if ln.startswith("[abcd0001]")]
    assert len(line.encode("utf-8")) <= 100
    assert line.endswith("\u2026")
