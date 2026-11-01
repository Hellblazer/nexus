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
    r = subagent_start.run({"agent_type": agent_type, "session_id": "s-t1", "prompt": "x", "cwd": "/tmp"})
    return json.loads(r.stdout or "{}")["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("agent_type", ["conexus:code-review-expert", "conexus:substantive-critic",
                                        "conexus:developer", "general-purpose", "Explore"])
def test_every_agent_type_gets_the_t1_contract_first(agent_type, monkeypatch) -> None:
    ctx = _ctx(agent_type, monkeypatch)
    assert ctx.startswith("## T1: this session's working layer")
    assert "Your scratch IS your parent session's T1" in ctx
    assert "independent reviewer, read nothing else in T1" in ctx


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
    assert "[abcd0014]" in ctx and "[abcd0015]" not in ctx
    assert "... (5 more: scratch list)" in ctx


def test_an_empty_t1_adds_no_index(monkeypatch) -> None:
    assert "## T1 index" not in _ctx("conexus:developer", monkeypatch)


class _Store:
    """The HttpMemoryStore surface scan() calls."""

    def __init__(self, namespaces: list[str]) -> None:
        self._ns = namespaces

    def get_projects_with_prefix(self, _prefix: str) -> list[dict[str, Any]]:
        return [{"project": p, "last_updated": "2026-11-01T00:00:00Z"} for p in self._ns]

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


def test_the_session_start_context_leads_with_the_t1_contract(monkeypatch) -> None:
    monkeypatch.setattr(session_context, "which", lambda _c: False)
    monkeypatch.setattr(session_context, "run_command", lambda *_a, **_k: None)
    out = session_context.run(None).stdout or ""
    assert out.startswith("## T1: the working layer this session shares with its subagents")
    assert "name its id in each brief" in out
