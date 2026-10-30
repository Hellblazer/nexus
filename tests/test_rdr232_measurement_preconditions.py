# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 3 measurement (nexus-jypmj.3.10, critique S4 and S7): the preconditions the build left out.

The bead's 2026-10-27 18:30 comment lists preconditions of turning the coverage check on: negative controls where
nothing bears on the facet; an accepted-to-rejected ratio rule; a skip that rests on one usable reply; misses caused by
reassessing only the new anchors. The first build named none of them in ``NOT_MEASURED``, had no unanswerable request,
and could not count a skip that rests on a single reply. This file pins the fix, plus the near-miss synthesis controls
the compound-lookup set lacked (S7). Model-free and tenant-free.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from bench import rdr232_extract_measure as em  # noqa: E402
from bench import rdr232_phase3_measure as driver  # noqa: E402
from bench import rdr232_replay as replay  # noqa: E402
from bench.rdr232_envelope import envelope_facts  # noqa: E402
from bench.schema import load_golden_set  # noqa: E402

from nexus.plans import coverage_ledger  # noqa: E402
from nexus.plans.request_coverage import derive_request_coverage, needs_structured_extract  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "rdr232_envelope_g06_stub.json"
RECORDED = json.loads(FIXTURE.read_text(encoding="utf-8"))
NEAR_MISS = REPO / "bench" / "queries" / "golden_near_miss_synthesis_v1.yaml"
NEGATIVE = REPO / "bench" / "queries" / "golden_negative_controls_v1.yaml"
COMPOUND = REPO / "bench" / "queries" / "golden_compound_lookup_v1.yaml"

# ── NOT_MEASURED ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["negative_controls", "accepted_to_rejected_ratio_rule", "single_reply_skip", "reassess_only_new_anchors"],
)
def test_not_measured_names_each_precondition_the_bead_lists(name: str) -> None:
    assert any(line.startswith(name) for line in driver.NOT_MEASURED), f"{name} is not named in NOT_MEASURED"


def test_not_measured_keeps_the_four_it_already_named() -> None:
    for name in ("continuation_fallback_repay_cost", "terminal_completeness", "any_support_wins_precision", "engine_old_and_cached_capability_rollback_legs"):
        assert any(line.startswith(name) for line in driver.NOT_MEASURED), name


def test_every_not_measured_line_says_why() -> None:
    assert all(": " in line and len(line.split(": ", 1)[1]) > 20 for line in driver.NOT_MEASURED)


def test_the_duplicated_enumerations_equal_the_ledgers() -> None:
    """The driver imports nothing from ``nexus`` at module level, so it copies these; the copy is pinned here."""
    assert driver.UNASSESSED_CAUSES == coverage_ledger.UNASSESSED_CAUSES
    assert driver.REPLY_CLASSES == coverage_ledger.REPLY_CLASSES
    assert driver.USABLE_REPLY_CLASSES == coverage_ledger.REPLY_CLASSES[:2]


# ── a skip that rests on one usable reply ────────────────────────────────────────────────────────────────


def _run(qid: str, *, skip: bool, replies: list[str]) -> driver.ArmRun:
    env = json.loads(json.dumps(RECORDED))
    env["coverage"]["skip_generation"] = skip
    env["coverage"]["assessment"]["call_log"] = [["vector", 1, r, 0, 0] for r in replies]
    return driver.ArmRun(qid, "on", 1000, facts=envelope_facts(env), envelope=env)


def _summary(runs: list[driver.ArmRun]) -> dict[str, Any]:
    return driver.summarize_arm(runs, queries={}, resolutions={}, oracle=None)


def test_a_skipped_run_with_exactly_one_usable_reply_is_counted() -> None:
    runs = [
        _run("A", skip=True, replies=["neutral_or_contradict", "no_records"]),      # one usable: counted
        _run("B", skip=True, replies=["neutral_or_contradict", "neutral_or_contradict"]),  # two usable: not
        _run("C", skip=True, replies=["support", "all_rejected", "raised"]),         # one usable: counted
        _run("D", skip=False, replies=["neutral_or_contradict", "no_records"]),      # not skipped: not
        _run("E", skip=True, replies=["no_records"]),                                # zero usable: not (cannot rest on a reply)
    ]
    one = _summary(runs)["skip_on_one_usable_reply"]
    assert one == {"skipped_runs": 4, "runs": 2, "qids": ["A", "C"]}


def test_the_summary_reports_the_accepted_and_rejected_record_totals() -> None:
    s = _summary([_run("A", skip=False, replies=["support"])])
    assert s["records"] == {"accepted": RECORDED["coverage"]["assessment"]["accepted_records"], "rejected": RECORDED["coverage"]["assessment"]["rejected_records"]}


def test_the_unassessed_table_names_the_stop_after_failure_cause() -> None:
    env = json.loads(json.dumps(RECORDED))
    env["coverage"]["unassessed_by_cause"] = {**env["coverage"]["unassessed_by_cause"], "stopped_after_failure": 2, "raised_call": 1}
    run = driver.ArmRun("A", "on", 1, facts=envelope_facts(env), envelope=env)
    by_cause = _summary([run])["unassessed"]["facets_by_cause"]
    assert by_cause["stopped_after_failure"] == 2 and by_cause["raised_call"] == 1 and set(by_cause) == set(coverage_ledger.UNASSESSED_CAUSES)


# ── the negative-control set ─────────────────────────────────────────────────────────────────────────────

#: Marker terms of what each negative control asks about; none may occur anywhere in this repository's own RDRs, docs
#: or source. This pins the REPOSITORY only. The live tenant also holds other owners' RDRs, docs, notes and code, and
#: the first N01-N03 (Cassandra, Elasticsearch, SAML / Istio) passed this scan while occurring in them, so they were
#: not controls there (nexus-jypmj.23). The tenant scan is the measurement's job, not this test's; the yaml header
#: records the lexical scan that found these replacements absent from the tenant, and it has to be re-run before a
#: measurement relies on the set.
ABSENT_TERMS: dict[str, list[str]] = {
    "N01": ["Couchbase", "vbucket"], "N02": ["Hazelcast"], "N03": ["Auth0", "Aerospike"],
    "N04": ["Redis Sentinel", "canary release"],
}


def test_the_negative_set_is_a_loadable_golden_set_of_unanswerable_requests() -> None:
    qs = load_golden_set(NEGATIVE)
    assert 3 <= len(qs) <= 4 and [q.qid for q in qs] == sorted(ABSENT_TERMS)
    assert any(len(q.required_facets) == 1 for q in qs) and any(len(q.required_facets) > 1 for q in qs), "single- and multi-facet"
    assert all(q.required_sources == [] and q.scope == "rdr" for q in qs), "no named source decides a facet before the assessor does"
    others = {replay.question_key(q.text) for p in (driver.GOLDEN_PATH, driver.HELDOUT_PATH, COMPOUND, NEAR_MISS) for q in load_golden_set(p)}
    assert not others & {replay.question_key(q.text) for q in qs}


def test_what_each_negative_control_asks_about_is_absent_from_the_repository() -> None:
    """Non-vacuity of 'nothing in the corpus bears on the facet', for THIS repository: the marker terms occur in no RDR,
    doc or source file under docs/ and src/. It says nothing about the live tenant (see ``ABSENT_TERMS``)."""
    qs = {q.qid: q for q in load_golden_set(NEGATIVE)}
    for qid, terms in ABSENT_TERMS.items():
        for term in terms:
            assert term.lower() in qs[qid].text.lower(), f"{qid} no longer asks about {term}"
    haystacks = [p for root in ("docs", "src") for p in (REPO / root).rglob("*") if p.is_file() and p.suffix in (".md", ".py", ".yaml", ".yml", ".toml", ".xml", ".java")]
    assert len(haystacks) > 500, "the scan examined the corpus"
    for p in haystacks:
        text = p.read_text(encoding="utf-8", errors="ignore").lower()
        for terms in ABSENT_TERMS.values():
            for term in terms:
                assert term.lower() not in text, f"{term!r} occurs in {p.relative_to(REPO)}: it is no longer absent from the corpus"


def test_the_driver_reads_the_negative_set_and_counts_the_assessors_false_covers(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    rc = driver.main(["--set", "negative", "--stub", str(FIXTURE), "--out", str(out)])
    assert rc == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    neg = report["negative_controls"]
    assert report["meta"]["requests"] == 4 and neg["requests"] == 4 and neg["in_sample"] is True
    # the recorded envelope covers 5 of its 6 facets: against a request nothing answers that is 20 false covers
    assert neg["facets"] == 24 and neg["covered"] == 20 and neg["unsupported"] == 4
    assert neg["falsely_covered"] == pytest.approx(20 / 24) and neg["falsely_covered_ci95"][0] > 0.6
    assert neg["generation_skipped"] == 0 and neg["answer_shapes"] == {"insufficient_evidence": 4}
    assert report["oracle_labels"]["requests"] == 24, "the oracle does not label negative controls (an extra set reads the golden files)"


def test_a_run_without_negative_controls_has_no_negative_section(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    assert driver.main(["--set", "golden", "--qid", "G01-lookup-tumblers", "--stub", str(FIXTURE), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["negative_controls"] is None


# ── the near-miss synthesis set ──────────────────────────────────────────────────────────────────────────


def test_the_near_miss_set_is_a_loadable_golden_set_of_one_source_synthesis_requests() -> None:
    qs = load_golden_set(NEAR_MISS)
    assert 4 <= len(qs) <= 6
    assert {q.answer_kind for q in qs} == {"synthesis"}
    assert all(len(q.required_facets) >= 3 for q in qs), "an account spread over sections names several parts"
    assert all(len(q.required_sources) == 1 for q in qs), "one source, as the compound-lookup set has"
    others = {replay.question_key(q.text) for p in (driver.GOLDEN_PATH, driver.HELDOUT_PATH, COMPOUND, NEGATIVE) for q in load_golden_set(p)}
    assert not others & {replay.question_key(q.text) for q in qs}
    for q in qs:
        assert list((REPO / "docs" / "rdr").glob(f"rdr-{q.required_sources[0].split('-')[1]}-*.md")), q.qid
    compound_sources = {s for q in load_golden_set(COMPOUND) for s in q.required_sources}
    assert {s for q in qs for s in q.required_sources} & compound_sources, "at least one source is also asked about as a compound lookup (a true near miss)"


def test_no_near_miss_request_is_settled_as_a_lookup_by_the_first_pass() -> None:
    """Non-vacuity: the first pass must not already call these lookups, and most should reach the extraction, where the
    model draws the line."""
    sent = 0
    for q in load_golden_set(NEAR_MISS):
        cov = derive_request_coverage(q.text)
        assert cov.answer_kind in ("synthesis", "unknown"), (q.qid, cov.answer_kind)
        sent += needs_structured_extract(cov)
    assert sent >= 3, f"only {sent} near-miss requests reach the extraction; the set no longer probes the prompt"


def test_the_extract_measurement_runs_the_near_miss_set_and_marks_it_in_sample(tmp_path: Path) -> None:
    assert em.SET_PATHS["near_miss"] == NEAR_MISS and "near_miss" in em.IN_SAMPLE_SETS
    out = tmp_path / "nm.json"
    assert em.main(["--set", "near_miss", "--dry-run", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["set"] == "near_miss" and report["questions"] == len(load_golden_set(NEAR_MISS)) and report["in_sample"] is True
    assert report["extraction"]["calls_made"] == 0


def test_the_phase3_driver_runs_the_compound_near_miss_and_negative_sets(tmp_path: Path) -> None:
    for which, expected in (("compound", 6), ("near_miss", 6), ("negative", 4)):
        out = tmp_path / f"{which}.json"
        assert driver.main(["--set", which, "--stub", str(FIXTURE), "--out", str(out)]) == 0, which
        assert json.loads(out.read_text(encoding="utf-8"))["meta"]["requests"] == expected, which
