"""RDR process records: the ten defects intrastate's RDR-system comparison
surfaced (nexus-5r0ho; T2 intrastate/rdr-compare-synthesis-2026-09-17 (4/4)
section 8). Each test names the item it pins.
"""

from __future__ import annotations

from pathlib import Path

import nexus.commands.rdr as rdr_mod
from nexus.commands.rdr import rdr
from tests.test_rdr_preamble import (  # noqa: F401 — rdr_env and _rdr_git_template are fixtures
    _FakeT2ResearchClient,
    _rdr_git_template,
    _runner,
    _write_rdr,
    rdr_env,
)

_BODY = "## Problem Statement\n\n#### Gap 1: a\n\ntext\n\n### Approach\n\n1. **A**: one\n\n## Revision History\n\n"


class TestCritiqueRounds:
    """Items 1, 2 and 6: one home for gate critiques, a round-numbered title,
    and a count that a duplicate copy cannot inflate."""

    def test_distinct_rows_collapse_a_same_content_duplicate(self):
        rows = [
            {"title": "207-gate-critique-2026-09-10", "content": "round one text"},
            {"title": "207-gate-critique-2026-09-10b", "content": "round one text"},
            {"title": "207-gate-critique-2026-09-11-r2", "content": "round two text"},
            {"title": "207-gate-critique-2026-09-12-r3", "content": "round one text"},  # re-raised verbatim later: a round
            {"title": "208-gate-critique-2026-09-11", "content": "other rdr"},
        ]
        got = rdr_mod._distinct_critique_rows(rows, "207")
        assert [t for t, _ in got] == [
            "207-gate-critique-2026-09-10", "207-gate-critique-2026-09-11-r2", "207-gate-critique-2026-09-12-r3",
        ]

    def test_gate_names_the_critique_title_with_the_round_number(self, rdr_env, monkeypatch):
        _write_rdr(rdr_env["rdr_dir"], "rdr-207-x.md", {"title": "X", "status": "draft"}, _BODY)
        fake = _FakeT2ResearchClient(entries={
            "207-gate-latest": "outcome: BLOCKED\ndate: 2026-09-10\ncritical_count: 1\n"
                               "critique: nexus_rdr/207-gate-critique-2026-09-10\ncommit: abc1234\n",
            "207-gate-critique-2026-09-10": "## Critical Issues\n\n### Issue: one\n- Location: a\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "207"])
        assert res.exit_code == 0, res.output
        assert "207-gate-critique-" in res.output and "-r2`" in res.output, res.output

    def test_first_gate_names_round_one(self, rdr_env, monkeypatch):
        _write_rdr(rdr_env["rdr_dir"], "rdr-208-x.md", {"title": "X", "status": "draft"}, _BODY)
        fake = _FakeT2ResearchClient()
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "208"])
        assert "208-gate-critique-" in res.output and "-r1`" in res.output, res.output

    def test_a_critique_pointer_outside_the_rdr_project_is_flagged(self, rdr_env, monkeypatch):
        """Item 6: RDR-200's pointer names nexus/critique-rdr-200-gate-...; the
        record of record is {repo}_rdr, and a pointer elsewhere is named."""
        _write_rdr(rdr_env["rdr_dir"], "rdr-200-x.md", {"title": "X", "status": "draft"}, _BODY)
        fake = _FakeT2ResearchClient(entries={
            "200-gate-latest": "outcome: PASSED\ndate: 2026-09-01\ncritical_count: 0\n"
                               "critique: nexus/critique-rdr-200-gate-2026-09-01\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "200"])
        assert "Misplaced critique" in res.output, res.output
        assert "critique-rdr-200-gate-2026-09-01" in res.output


class TestFixCheckDispatches:
    """Item 9: the fix-check record must show three dispatches."""

    def test_a_record_with_one_verdict_line_is_flagged(self):
        lines = rdr_mod._fix_check_pointer_lines(
            "nexus_rdr/207-fix-check-abc1234", "abc1234", is_regate=True,
            record_exists=True, record_content="FIX CHECK: PASS\n",
        )
        assert any("three dispatches" in ln for ln in lines), lines

    def test_a_record_naming_three_dispatches_passes(self):
        content = "dispatches: 3\nFIX CHECK: PASS\nFIX CHECK: PASS\nFIX CHECK: FAIL — 1 BLOCKS-PLANNING rows\n"
        lines = rdr_mod._fix_check_pointer_lines(
            "nexus_rdr/207-fix-check-abc1234", "abc1234", is_regate=True,
            record_exists=True, record_content=content,
        )
        assert lines == []

    def test_three_verdict_lines_without_the_field_pass(self):
        content = "FIX CHECK: PASS\nFIX CHECK: PASS\nFIX CHECK: PASS\n"
        assert rdr_mod._fix_check_pointer_lines(
            "nexus_rdr/207-fix-check-abc1234", "abc1234", is_regate=True,
            record_exists=True, record_content=content,
        ) == []


class TestLayerTwoCensus:
    """Item 8: Layer 2 is computed, and zero research records is a named
    vacuity, never a silent pass."""

    def test_no_research_records_is_named_vacuous(self, rdr_env, monkeypatch):
        _write_rdr(rdr_env["rdr_dir"], "rdr-209-x.md", {"title": "X", "status": "draft"}, _BODY)
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _FakeT2ResearchClient())
        res = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "209"])
        assert "Layer 2" in res.output and "VACUOUS" in res.output, res.output

    def test_census_counts_classification_and_method(self, rdr_env, monkeypatch):
        _write_rdr(rdr_env["rdr_dir"], "rdr-209-x.md", {"title": "X", "status": "draft"}, _BODY)
        fake = _FakeT2ResearchClient(entries={
            "209-research-1": "rdr_id: 209\nseq: 1\nclassification: verified\nverification_method: spike\nfinding: a\n",
            "209-research-2": "rdr_id: 209\nseq: 2\nclassification: assumed\nverification_method: docs_only\nfinding: b\n",
            "209-research-3": "rdr_id: 209\nseq: 3\nfinding: c\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, ["preamble", "rdr-gate", "--", "209"])
        out = res.output
        assert "3 research records" in out, out
        assert "verified 1" in out and "assumed 1" in out and "unclassified 1" in out, out
        assert "HIGH RISK" in out and "[seq 2]" in out, out

    def test_research_add_records_classification_and_method(self, monkeypatch):
        fake = _FakeT2ResearchClient()
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, [
            "preamble", "rdr-research", "--", "add", "209",
            "--classification", "verified", "--method", "spike", "the", "finding",
        ])
        assert res.exit_code == 0, res.output
        content = fake._store["209-research-1"]
        assert "classification: verified\n" in content and "verification_method: spike\n" in content
        assert "finding: the finding\n" in content
        assert "--classification" not in content

    def test_research_add_refuses_a_value_outside_the_vocabulary(self, monkeypatch):
        fake = _FakeT2ResearchClient()
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, [
            "preamble", "rdr-research", "--", "add", "209", "--classification", "true", "x",
        ])
        assert res.exit_code != 0
        assert fake.put_calls == []


class TestClosePreamble:
    """Items 3 and 5."""

    def _accepted(self, rdr_env, history: str) -> None:
        body = _BODY + history
        _write_rdr(
            rdr_env["rdr_dir"], "rdr-210-x.md",
            {"title": "X", "status": "accepted", "accepted_date": "2026-09-01"}, body,
        )

    def test_revision_history_that_stops_at_accept_is_named(self, rdr_env, monkeypatch):
        self._accepted(rdr_env, "- 2026-08-30: drafted\n- 2026-09-01: gate PASSED; accepted\n")
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _FakeT2ResearchClient())
        res = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "210", "--reason", "implemented"])
        assert "Revision History stops at accept" in res.output, res.output

    def test_an_entry_after_acceptance_satisfies_it(self, rdr_env, monkeypatch):
        self._accepted(rdr_env, "- 2026-09-01: accepted\n- 2026-09-05: Phase 1 landed (nexus-abc)\n")
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _FakeT2ResearchClient())
        res = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "210", "--reason", "implemented"])
        assert "Revision History stops at accept" not in res.output, res.output

    def test_force_implemented_prints_the_override_record_to_write(self, rdr_env, monkeypatch):
        self._accepted(rdr_env, "- 2026-09-05: phase\n")
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _FakeT2ResearchClient())
        res = _runner().invoke(rdr, [
            "preamble", "rdr-close", "--", "210", "--reason", "implemented",
            "--force-implemented", "critic disagreed on scope, Sam decided",
        ])
        assert "210-close-override-" in res.output, res.output
        assert "user_reason: critic disagreed on scope, Sam decided" in res.output


class TestAuditProcessRows:
    """Items 3, 4 and 10 as audit rows."""

    def test_override_ratio_over_thirty_days(self):
        rows = [
            {"title": "201", "content": "id: RDR-201\nstatus: closed\nclosed_date: 2026-09-10\n"},
            {"title": "202", "content": "id: RDR-202\nstatus: closed\nclosed_date: 2026-09-12\n"},
            {"title": "203", "content": "id: RDR-203\nstatus: closed\nclosed_date: 2026-01-01\n"},
            {"title": "201-close-override-2026-09-10", "content": "rdr_id: 201\n"},
        ]
        lines = rdr_mod._close_override_lines(rows, today="2026-09-17")
        text = "\n".join(lines)
        assert "1 override" in text and "2 closes" in text and "50%" in text, text
        assert "above the 20% trigger" in text

    def test_terminated_records_without_a_reason_are_listed(self):
        rows = [
            {"title": "150", "content": "id: RDR-150\nstatus: abandoned\nclose_reason: premise void\n"},
            {"title": "151", "content": "id: RDR-151\nstatus: abandoned\nscrap_reason: dropped\n"},
            {"title": "152", "content": "id: RDR-152\nstatus: deferred\n"},
            {"title": "153", "content": "id: RDR-153\nstatus: closed\n"},
        ]
        lines = rdr_mod._terminated_reason_lines(rows)
        text = "\n".join(lines)
        assert "3 terminated" in text, text
        assert "no reason: 1" in text and "RDR-152" in text, text
        assert "scrap_reason" in text and "RDR-151" in text, text

    def test_post_mortem_coverage_by_status(self, tmp_path: Path):
        pm = tmp_path / "post-mortem"
        pm.mkdir()
        (pm / "rdr-160-x.md").write_text("x")
        rows = [
            {"title": "160", "content": "status: closed\n"},
            {"title": "161", "content": "status: closed\n"},
            {"title": "162", "content": "status: abandoned\n"},
        ]
        text = "\n".join(rdr_mod._post_mortem_coverage_lines(rows, pm))
        assert "closed: 1 of 2" in text and "abandoned: 0 of 1" in text, text
