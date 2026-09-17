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


class TestSetStatusWritesTheReason:
    """Item 4, forward half: a reason given to set-status lands on the file
    and on the T2 record as `close_reason`, so new terminated records never
    join the ones with no machine-readable reason."""

    def test_reason_lands_in_frontmatter_and_t2(self, rdr_env, monkeypatch):
        d = rdr_env["rdr_dir"]
        _write_rdr(d, "rdr-150-x.md", {"title": "X", "status": "draft"}, "x\n")
        fake = _FakeT2ResearchClient(entries={"150": "id: RDR-150\nstatus: draft\n"})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, ["set-status", "150", "abandoned", "--reason", "premise void after RDR-155"])
        assert res.exit_code == 0, res.output
        assert "close_reason: premise void after RDR-155" in (d / "rdr-150-x.md").read_text()
        assert "close_reason: premise void after RDR-155\n" in fake._store["150"]

    def test_no_reason_writes_no_field(self, rdr_env, monkeypatch):
        d = rdr_env["rdr_dir"]
        _write_rdr(d, "rdr-151-x.md", {"title": "X", "status": "accepted"}, "x\n")
        fake = _FakeT2ResearchClient(entries={"151": "id: RDR-151\nstatus: accepted\n"})
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, ["set-status", "151", "deferred"])
        assert res.exit_code == 0, res.output
        assert "close_reason" not in (d / "rdr-151-x.md").read_text()
        assert "close_reason" not in fake._store["151"]


class TestReviewRoundFixes:
    """Round-1 code review of 637b8149b, findings 1 to 6."""

    def test_verdict_count_keeps_a_prior_round_that_this_round_re_raises_verbatim(self, rdr_env, monkeypatch):
        _write_rdr(rdr_env["rdr_dir"], "rdr-207-x.md", {"title": "X", "status": "draft"}, _BODY)
        crit = "## Critical Issues\n\n### Issue: same finding\n- Location: a\n\n## Verdict\n- **outcome**: not-justified\n- **critical_count**: 1\n"
        fake = _FakeT2ResearchClient(entries={
            "207-gate-critique-2026-09-10-r1": crit,
            "207-gate-critique-2026-09-12-r2": crit,
            "207-gate-latest": "outcome: BLOCKED\ndate: 2026-09-10\ncritique: nexus_rdr/207-gate-critique-2026-09-10-r1\n",
        })
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, ["preamble", "rdr-verdict", "--", "207", "207-gate-critique-2026-09-12-r2"])
        assert "Gate round 2" in res.output, res.output

    def test_research_add_accepts_the_equals_form(self, monkeypatch):
        fake = _FakeT2ResearchClient()
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: fake)
        res = _runner().invoke(rdr, [
            "preamble", "rdr-research", "--", "add", "209", "--classification=verified", "--method=spike", "the", "finding",
        ])
        assert res.exit_code == 0, res.output
        content = fake._store["209-research-1"]
        assert "classification: verified\n" in content and "verification_method: spike\n" in content
        assert "=" not in content.split("finding:")[1]

    def test_a_declared_dispatch_count_above_the_verdict_lines_is_not_trusted(self):
        lines = rdr_mod._fix_check_pointer_lines(
            "nexus_rdr/207-fix-check-abc1234", "abc1234", is_regate=True,
            record_exists=True, record_content="dispatches: 3\nFIX CHECK: PASS\n",
        )
        assert any("three dispatches" in ln for ln in lines), lines

    def test_an_accepted_rdr_with_no_acceptance_date_anywhere_is_named(self, rdr_env, monkeypatch):
        _write_rdr(rdr_env["rdr_dir"], "rdr-210-x.md", {"title": "X", "status": "accepted"},
                   _BODY + "- 2026-09-05: phase one\n")
        monkeypatch.setattr(rdr_mod, "_t2_client_factory", lambda: _FakeT2ResearchClient())
        res = _runner().invoke(rdr, ["preamble", "rdr-close", "--", "210", "--reason", "implemented"])
        assert "no acceptance date" in res.output, res.output

    def test_an_entry_is_dated_by_its_leading_date_not_a_date_it_mentions(self):
        text = "## Revision History\n\n- 2026-09-05: following the 2020-01-01 baseline, phase one landed\n"
        assert rdr_mod._revision_history_after_accept_lines(text, {"accepted_date": "2026-09-01"}) == []

    def test_unparseable_closed_dates_are_counted_in_the_override_row(self):
        rows = [
            {"title": "201", "content": "status: closed\nclosed_date: 09/17/2026\n"},
            {"title": "202", "content": "status: closed\nclosed_date: 2026-09-12\n"},
        ]
        text = "\n".join(rdr_mod._close_override_lines(rows, today="2026-09-17"))
        assert "1 closed record" in text and "could not be dated" in text, text
