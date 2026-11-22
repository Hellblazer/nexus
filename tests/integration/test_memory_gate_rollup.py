# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx memory gate-rollup`` (RDR-209 Phase 3 Step 1, nexus-1gdv0.2.1 to .5): the listing of a phase's gate
records, the T3 pointer record and the T3 archive of each record's full text.

Every check pairs a planted case that must be listed with a clean case that must not be, so a command that
lists nothing, or everything, fails one of the pair. The T2 rows and the T3 notes live in the self-provisioned
test engine (``db``, ``make_t3``), never the live store.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import make_t3
from nexus.db.limits import QUOTAS
import nexus.session as session_mod
from nexus.db.t2 import T2Database
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.mcp.core import store_get
from nexus.memory_curate import gate_rollup as gr
from nexus.memory_curate import gate_ttl as gt
from tests._catalog_fixture_ops import documents_by_title
from tests._engine_substrate import ensure_engine, mint_test_tenant
from tests._t2_fixture_ops import backdate_memory, rewrite_memory_row, set_memory_access_count, utc_stamp

from tests._gate_rollup_support import (  # noqa: F401 — _closed_rdr is an autouse fixture
    REPO,
    RDR,
    POINTER_P3,
    _write_rdr,
    _closed_rdr,
    _put,
    _gate,
    _rows,
    _run,
    _phase3,
    _pointer,
    _titles,
)

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]


# ── the whole-tag match ──────────────────────────────────────────────────────────

def test_only_whole_tag_matches_are_listed(db: T2Database) -> None:
    a = _gate(db, "nexus-a1")
    _put(db, "mentions-gate", "kind:gate and bead:nexus-a1 appear in the body only", tags="notes")
    _put(db, "near-miss", tags="gates,kind:gate-x,bead:nexus-a1")
    _put(db, "kind-gate-in-title-only-kind:gate", tags="")
    _phase3("nexus-a1")
    got = _pointer()
    assert _titles(got.listed) == [a]
    assert got.unmapped == []


def test_bead_one_does_not_match_bead_twelve(db: T2Database) -> None:
    one = _gate(db, "nexus-1")
    _gate(db, "nexus-12", sha="def5678")
    _phase3("nexus-1")
    assert _titles(_pointer().listed) == [one]


def test_a_space_after_the_comma_still_matches(db: T2Database) -> None:
    name = _gate(db, "nexus-x", tags="kind:gate, bead:nexus-x, rdr:209")
    _phase3("nexus-x")
    assert _titles(_pointer().listed) == [name]


def test_the_beads_option_lists_several_beads(db: T2Database) -> None:
    a = _gate(db, "nexus-a")
    b = _gate(db, "nexus-b", sha="bbb2222")
    _gate(db, "nexus-c", sha="ccc3333")
    _phase3("nexus-a, nexus-b")
    assert _titles(_pointer().listed) == sorted([a, b])


# ── the title predicate (nexus-1gdv0.2.3) ────────────────────────────────────────

def test_title_match_lists_an_older_untagged_record(db: T2Database) -> None:
    old = "review-59e45b852-nexus-rc4pz-2026-11-18"
    _put(db, old, "older record, no header.\n- **outcome**: justified\n")
    _put(db, "review-59e45b852-nexus-rc4pzz-2026-11-18", "another bead entirely\n- **outcome**: justified\n")
    _phase3("nexus-rc4pz")
    got = _pointer()
    assert _titles(got.listed) == [old]
    assert got.listed[0]["bead"] == "nexus-rc4pz"


def test_title_match_does_not_cross_nexus_1_and_nexus_12(db: T2Database) -> None:
    _put(db, "review-abc1234-nexus-12-2026-11-10", "x\n- **outcome**: justified\n")
    out = _phase3("nexus-1")
    assert "0 listed, 0 unmapped" in out
    assert documents_by_title(POINTER_P3) == []


def test_a_tagged_row_is_not_title_matched(db: T2Database) -> None:
    # kind:gate but no bead tag: it is unmapped, never title-matched to the bead its title names
    bare = "review-abc1234-nexus-t1-p3-2026-11-10"
    _put(db, bare, "x\n- **outcome**: justified\n", tags="kind:gate,rdr:209")
    # tagged, not a gate record: never read at all
    _put(db, "review-abc1234-nexus-t1-2026-11-11", "x\n- **outcome**: justified\n", tags="notes")
    _phase3("nexus-t1")
    got = _pointer()
    assert got.listed == []
    assert _titles(got.unmapped) == [bare]


def test_a_title_naming_two_listed_beads_is_unmapped(db: T2Database) -> None:
    both = "review-abc1234-nexus-a-and-nexus-b-2026-11-10"
    _put(db, both, "x\n- **outcome**: justified\n")
    _phase3("nexus-a,nexus-b")
    got = _pointer()
    assert got.listed == [] and _titles(got.unmapped) == [both]
    assert "more than one" in got.unmapped[0]["reason"]


# ── the unmapped list (nexus-1gdv0.2.2) ──────────────────────────────────────────

def test_unmapped_under_all_a_bead_less_gate_record_of_the_rdr(db: T2Database) -> None:
    mapped = _gate(db, "nexus-m1")
    bare = "review-abc1234-no-bead-2026-11-10"
    _put(db, bare, "kind: review\nrdr: 209\noutcome: justified\n\nbody", tags="kind:gate,rdr:209")
    _put(db, "review-abc1234-other-rdr", "x", tags="kind:gate,rdr:777")
    _run("--all")
    got = _pointer("gate-records-rdr-209-all")
    assert _titles(got.listed) == [mapped]
    assert _titles(got.unmapped) == [bare]
    assert got.unmapped[0]["reason"] == "no bead tag"


def test_unmapped_under_phase_needs_the_phase_in_the_title(db: T2Database) -> None:
    _put(db, "review-abc1234-p3-2026-11-10", "x", tags="kind:gate,rdr:209")
    _put(db, "review-abc1234-p3a-2026-11-10", "x", tags="kind:gate,rdr:209")        # sub-phase 3a is phase 3
    _put(db, "review-abc1234-phase-3-2026-11-10", "x", tags="kind:gate,rdr:209")
    _put(db, "review-abc1234-p2-2026-11-10", "x", tags="kind:gate,rdr:209")        # another phase
    _put(db, "review-abc1234-p13-2026-11-10", "x", tags="kind:gate,rdr:209")       # p13 is not phase 3
    _put(db, "review-abc1234-p3-2026-11-11", "x", tags="kind:gate,rdr:777")        # another RDR
    _put(db, "review-abc1234-2026-11-10", "x", tags="kind:gate,rdr:209")           # names no phase
    _phase3("nexus-z")
    got = _pointer()
    assert _titles(got.unmapped) == [
        "review-abc1234-p3-2026-11-10", "review-abc1234-p3a-2026-11-10", "review-abc1234-phase-3-2026-11-10",
    ]
    assert got.listed == []


def test_unmapped_a_gate_record_with_no_rdr_and_no_bead_is_out_of_scope(db: T2Database) -> None:
    _put(db, "review-abc1234-p3-2026-11-10", "x", tags="kind:gate")
    assert "0 listed, 0 unmapped" in _run("--all")
    assert documents_by_title("gate-records-rdr-209-all") == []


def test_unmapped_a_listed_record_whose_verdict_cannot_be_read(db: T2Database) -> None:
    fine = _gate(db, "nexus-v1")
    bad = _gate(db, "nexus-v2", sha="bbb2222", outcome="justified")
    # a record whose own outcome line is not in the verdict vocabulary
    _put(db, bad, "kind: review\nbead: nexus-v2\nrdr: 209\nsha: bbb2222\noutcome: PASSED-ish\ndate: 2026-11-10\n\nbody",
         tags="kind:gate,bead:nexus-v2,rdr:209")
    _phase3("nexus-v1,nexus-v2")
    got = _pointer()
    assert _titles(got.listed) == [fine]
    assert _titles(got.unmapped) == [bad]
    assert got.unmapped[0]["reason"] == "verdict unreadable"


def test_unmapped_records_are_not_archived_and_get_no_ttl(db: T2Database) -> None:
    bare = "review-abc1234-no-bead-p3-2026-11-10"
    _put(db, bare, "kind: review\n\nbody", tags="kind:gate,rdr:209")
    _phase3("nexus-z")
    assert gr.read_archive(make_t3(), REPO, bare, 1) is None
    assert _rows(db)[bare]["ttl"] is None


def test_unmapped_never_comes_from_the_rdr_project(db: T2Database) -> None:
    _put(db, "209-gate-critique-x", "x", tags="rdr,gate,critique", project=f"{REPO}_rdr")
    _put(db, "209-fix-check-x", "x", tags="kind:gate,rdr:209", project=f"{REPO}_rdr")
    assert "0 listed, 0 unmapped" in _run("--all")
    assert documents_by_title("gate-records-rdr-209-all") == []


# ── what is never listed ─────────────────────────────────────────────────────────

def test_a_quarantined_record_is_not_listed(db: T2Database) -> None:
    kept = _gate(db, "nexus-q1")
    gone = _gate(db, "nexus-q1", sha="ddd4444", ttl=1, age_days=5)
    assert db.memory.expire().quarantined_ids   # the sweep hid it: the engine's getAll no longer returns it
    assert gone not in _rows(db) and kept in _rows(db)
    _phase3("nexus-q1")
    assert _titles(_pointer().listed) == [kept]


def test_another_tenants_rows_are_not_listed(db: T2Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = _gate(db, "nexus-o1")
    own_token = os.environ["NX_SERVICE_TOKEN"]
    _tenant, other_token = mint_test_tenant(ensure_engine())
    monkeypatch.setenv("NX_SERVICE_TOKEN", other_token)
    other = T2Database(tmp_path / "other.db")
    try:
        _gate(other, "nexus-o1", sha="eee5555")
        assert len(other.get_all(REPO)) == 1
    finally:
        other.close()
    monkeypatch.setenv("NX_SERVICE_TOKEN", own_token)
    _phase3("nexus-o1")
    assert _titles(_pointer().listed) == [mine]


def test_the_rdr_project_rows_are_neither_listed_nor_changed(db: T2Database) -> None:
    mine = _gate(db, "nexus-r1")
    for title in ("209-gate-latest", "209-gate-critique-2026-11-16-r1", "209-fix-check-abc"):
        _put(db, title, "gate flow row", tags="rdr,gate,kind:gate,bead:nexus-r1,rdr:209", project=f"{REPO}_rdr",
             age_days=40)
    before = _rows(db, f"{REPO}_rdr")
    _phase3("nexus-r1")
    _run("--all")
    assert _titles(_pointer().listed) == [mine]
    assert _rows(db, f"{REPO}_rdr") == before
    assert len(before) == 3


# ── the pointer ──────────────────────────────────────────────────────────────────

def test_a_closed_phase_yields_one_pointer_listing_its_three_records(db: T2Database) -> None:
    names = [_gate(db, "nexus-p1", kind="review"), _gate(db, "nexus-p1", kind="critique", sha="bbb2222"),
             _gate(db, "nexus-p2", kind="confirm", sha="ccc3333", outcome="partial")]
    _gate(db, "nexus-p9", sha="fff6666")   # a bead outside the phase
    out = _phase3("nexus-p1,nexus-p2", "--accept-open", "nexus-p2")
    assert "3 listed" in out and "0 unmapped" in out
    got = _pointer()
    assert _titles(got.listed) == sorted(names)
    by_title = {r["title"]: r for r in got.listed}
    one = by_title[names[2]]
    assert (one["project"], one["kind"], one["bead"], one["sha"], one["verdict"]) == (
        REPO, "confirm", "nexus-p2", "ccc3333", "partial")
    assert len(documents_by_title(POINTER_P3)) == 1


def test_pointer_is_a_knowledge_note_in_the_research_subject_with_its_category(db: T2Database) -> None:
    name = _gate(db, "nexus-c1")
    _phase3("nexus-c1")
    col = gr.collection_name(make_t3())
    # RDR-229 P3.8: the subject's collection is the label the engine minted (c<id>); the subject is its row's owner.
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — local to this one check

    row = make_catalog_reader().get_collection(col)
    assert row and (row["content_type"], row["owner_id"]) == ("knowledge", "nexus-rdr-research"), row
    # a note's own keys live on its catalog document (RDR-236)
    (pointer,) = documents_by_title(POINTER_P3)
    (archive,) = documents_by_title(f"{REPO}/{name}")
    assert pointer.physical_collection == archive.physical_collection == col
    assert pointer.meta["category"] == "gate_pointer" and archive.meta["category"] == "gate_record"
    assert "gate-pointer" in pointer.meta["tags"] and "gate-archive" in archive.meta["tags"]


def test_pointer_lists_timestamp_per_record_as_get_all_returned_it(db: T2Database) -> None:
    a = _gate(db, "nexus-t1", age_days=3)
    b = _gate(db, "nexus-t2", sha="bbb2222", age_days=9)
    stamps = {t: r["timestamp"] for t, r in _rows(db).items()}
    _phase3("nexus-t1,nexus-t2")
    got = {r["title"]: r for r in _pointer().listed}
    assert got[a]["timestamp"] == stamps[a] and got[b]["timestamp"] == stamps[b]
    assert got[a]["timestamp"] != got[b]["timestamp"]
    for r in got.values():
        datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))   # a real timestamp, to the second


def test_the_verdict_comes_from_the_header_or_from_an_older_records_last_outcome_line(db: T2Database) -> None:
    head = _gate(db, "nexus-h1", outcome="not-justified")
    old = "review-abc1234-nexus-h2-2026-11-10"
    _put(db, old, "first pass\n- **outcome**: partial\nsecond pass after the fixes\n- **Outcome:** `justified`\n",
         tags="kind:gate,bead:nexus-h2")
    plain = "review-abc1234-nexus-h3-2026-11-10"
    _put(db, plain, "text\noutcome: not-justified\n", tags="kind:gate,bead:nexus-h3")
    _phase3("nexus-h1,nexus-h2,nexus-h3", "--accept-open", "nexus-h1", "--accept-open", "nexus-h3")
    got = {r["title"]: r["verdict"] for r in _pointer().listed}
    assert got == {head: "not-justified", old: "justified", plain: "not-justified"}


def test_a_body_line_does_not_stand_in_for_the_header_outcome(db: T2Database) -> None:
    name = _gate(db, "nexus-h4", outcome="partial", body="quoted example:\noutcome: justified")
    _phase3("nexus-h4", "--accept-open", "nexus-h4")
    assert {r["title"]: r["verdict"] for r in _pointer().listed} == {name: "partial"}


def test_a_rerun_replaces_the_pointer(db: T2Database) -> None:
    _gate(db, "nexus-rr1")
    _phase3("nexus-rr1")
    second = _gate(db, "nexus-rr1", sha="bbb2222")
    _phase3("nexus-rr1")
    got = _pointer()
    assert second in _titles(got.listed) and len(got.listed) == 2
    assert len(documents_by_title(POINTER_P3)) == 1


def test_a_rerun_writes_no_archive_that_is_already_whole(db: T2Database) -> None:
    _gate(db, "nexus-rr2")
    first = _phase3("nexus-rr2")
    again = _phase3("nexus-rr2")
    assert "archived 1" in first
    assert "archived 0" in again and "1 already whole" in again


# ── the archive: the full text stays in T3 (nexus-1gdv0.2.5) ─────────────────────

def test_the_archive_holds_each_records_full_text(db: T2Database) -> None:
    a = _gate(db, "nexus-f1", body="Finding one.\nFinding two, with ünïcode and `code`.\n")
    old = "review-abc1234-nexus-f2-2026-11-10"
    _put(db, old, "headerless body\n- **outcome**: justified\n")
    _phase3("nexus-f1,nexus-f2")
    t3 = make_t3()
    for title in (a, old):
        stored = gr.read_archive(t3, REPO, title, 1)
        assert stored == _rows(db)[title]["content"]
    # and through the MCP read the user would make, not only through this module's reader
    with patch("nexus.mcp.core._get_t3", return_value=t3):
        got = store_get(f"{REPO}/{a}", collection="nexus-rdr-research")
    assert "Finding two, with ünïcode and `code`." in got


def test_a_body_over_the_store_put_cap_is_split_into_titled_parts_and_reads_back_whole(db: T2Database) -> None:
    line = "A finding line that is long enough to matter, repeated until the body passes the cap. " * 4 + "\n"
    body = line * (QUOTAS.MAX_DOCUMENT_BYTES // len(line.encode()) * 2 + 3)
    big = _gate(db, "nexus-big", body=body)
    full = _rows(db)[big]["content"]
    assert len(full.encode()) > 2 * QUOTAS.MAX_DOCUMENT_BYTES
    out = _phase3("nexus-big")
    got = _pointer().listed[0]
    assert got["parts"] >= 3
    t3 = make_t3()
    assert gr.read_archive(t3, REPO, big, got["parts"]) == full
    # every part is a titled document of its own, each within the cap
    col = gr.collection_name(t3)
    assert t3.find_ids_by_title(col, f"{REPO}/{big}")
    assert t3.find_ids_by_title(col, f"{REPO}/{big} (part 2)")
    assert not t3.find_ids_by_title(col, f"{REPO}/{big} (part {got['parts'] + 1})")
    assert "archived 1" in out


def test_a_pointer_over_the_cap_is_split_and_lists_every_record(db: T2Database) -> None:
    names = [_gate(db, f"nexus-many{i:03d}", sha=f"{i:07x}", body=f"finding {i}") for i in range(130)]
    out = _run("--all")
    assert "pointer parts: " in out
    got = _pointer("gate-records-rdr-209-all")
    assert _titles(got.listed) == sorted(names)
    assert got.parts >= 2
    t3 = make_t3()
    col = gr.collection_name(t3)
    assert t3.find_ids_by_title(col, "gate-records-rdr-209-all (part 2)")
    # the first part alone is within the cap
    first = gr.read_note(t3, col, "gate-records-rdr-209-all")
    assert first is not None and len(first.encode()) <= QUOTAS.MAX_DOCUMENT_BYTES


def test_a_rerun_that_needs_fewer_parts_names_the_stale_ones(db: T2Database) -> None:
    # unmapped records are not carried, so a shorter unmapped list is what makes a pointer shorter
    _gate(db, "nexus-keep")
    for i in range(130):
        _put(db, f"review-{i:07x}-no-bead-{i:03d}-2026-11-10", "x", tags="kind:gate,rdr:209")
    _run("--all")
    assert _pointer("gate-records-rdr-209-all").parts >= 2
    for row in db.get_all(REPO):
        if "no-bead" in row["title"]:
            db.delete(project=REPO, title=row["title"])
    out = _run("--all")
    assert _pointer("gate-records-rdr-209-all").parts == 1
    assert "stale: 'gate-records-rdr-209-all (part 2)'" in out


# ── dry run and the options ──────────────────────────────────────────────────────

def test_dry_run_prints_the_listing_and_writes_nothing(db: T2Database) -> None:
    name = _gate(db, "nexus-d1")
    before = _rows(db)
    out = _phase3("nexus-d1", "--dry-run")
    assert name in out and "dry run" in out
    assert documents_by_title(POINTER_P3) == []
    assert documents_by_title(f"{REPO}/{name}") == []
    assert _rows(db) == before


def test_usage_errors(db: T2Database) -> None:
    for args in (["--phase", "3"],                                    # --phase needs --beads
                 ["--beads", "nexus-a"],                              # --beads needs --phase
                 ["--all", "--phase", "3", "--beads", "nexus-a"],     # one scope
                 [],                                                  # a scope is required
                 ["--phase", "three", "--beads", "nexus-a"],          # a phase is a number, maybe with a letter
                 ["--phase", "3", "--beads", " , "]):                 # an empty bead list
        out = _run(*args, expect=2)
        assert "Usage" in out or "Error" in out, (args, out)


def test_an_empty_listing_writes_no_pointer(db: T2Database) -> None:
    out = _phase3("nexus-none")
    assert "0 listed" in out
    assert documents_by_title(POINTER_P3) == []


def test_phase_two_digit_and_sub_phase_names_the_pointer_title(db: T2Database) -> None:
    _gate(db, "nexus-s1")
    _run("--phase", "03", "--beads", "nexus-s1")
    assert _pointer("gate-records-rdr-209-phase-3").listed
    _run("--phase", "3A", "--beads", "nexus-s1")
    assert _pointer("gate-records-rdr-209-phase-3a").listed


def test_default_project_is_the_repository_the_command_runs_in(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    name = _gate(db, "nexus-dp")
    monkeypatch.setattr("nexus.repo_identity._repo_identity", lambda cwd: (REPO, "hash"))
    argv = ["memory", "gate-rollup", "--rdr", "209", "--phase", "3", "--beads", "nexus-dp"]      # no --project
    dry = CliRunner().invoke(main, [*argv, "--dry-run"])
    assert dry.exit_code == 0 and "gaterepo" in dry.output, dry.output
    result = CliRunner().invoke(main, [*argv, "--counted", "0"])
    assert result.exit_code == 0, result.output
    assert _titles(_pointer().listed) == [name]


