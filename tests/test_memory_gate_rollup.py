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
from nexus.db.t2 import T2Database
from nexus.mcp.core import store_get
from nexus.memory_curate import gate_rollup as gr
from nexus.memory_curate import gate_ttl as gt
from tests._catalog_fixture_ops import documents_by_title
from tests._engine_substrate import ensure_engine, mint_test_tenant
from tests._t2_fixture_ops import backdate_memory, rewrite_memory_row, set_memory_access_count

pytestmark = pytest.mark.t2_tenant

REPO = "gaterepo"
RDR = 209
POINTER_P3 = "gate-records-rdr-209-phase-3"


def _write_rdr(root: Path, number: int, status: str, phases: dict[str, str] | None = None) -> None:
    """An RDR file under ``root/docs/rdr``: the frontmatter status, and one heading per phase (``{"3": "CLOSED"}``)."""
    d = root / "docs" / "rdr"
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(f"\n### Phase {n}{f' ({mark}, 2026-11-06)' if mark else ''}: work\n" for n, mark in (phases or {}).items())
    (d / f"rdr-{number:03d}-test.md").write_text(f"---\ntitle: t\nid: RDR-{number:03d}\nstatus: {status}\n---\n{body}")


@pytest.fixture(autouse=True)
def _closed_rdr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """RDR-209 is closed in the repository the command runs in (its working directory), unless a test says otherwise."""
    _write_rdr(tmp_path, RDR, "closed")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _put(db: T2Database, title: str, content: str = "body", tags: str = "", project: str = REPO,
         ttl: int | None = None, age_days: float | None = None) -> None:
    db.put(project=project, title=title, content=content, tags=tags, ttl=ttl)
    if age_days is not None:
        backdate_memory(db, project, title, days=age_days)


def _gate(db: T2Database, bead: str, *, kind: str = "review", sha: str = "abc1234", outcome: str = "justified",
          rdr: int | None = RDR, date: str = "2026-11-10", body: str = "Finding text.", rnd: int | None = None,
          tags: str | None = None, project: str = REPO, age_days: float | None = None,
          ttl: int | None = None, title: str | None = None) -> str:
    """Seed a gate record the way ``gate_record.lua`` writes it: header fields, a blank line, the body."""
    fields = f"kind: {kind}\nbead: {bead}\n"
    default_tags = f"kind:gate,bead:{bead}"
    if rdr is not None:
        fields += f"rdr: {rdr:03d}\n"
        default_tags += f",rdr:{rdr:03d}"
    fields += f"sha: {sha}\noutcome: {outcome}\ndate: {date}\n"
    name = title or f"{kind}-{sha}-{bead}-{date}" + (f"-r{rnd}" if rnd else "")
    _put(db, name, fields + "\n" + body, tags=default_tags if tags is None else tags, project=project,
         age_days=age_days, ttl=ttl)
    return name


def _rows(db: T2Database, project: str = REPO) -> dict[str, dict]:
    return {r["title"]: r for r in db.get_all(project)}


def _run(*args: str, expect: int = 0) -> str:
    result = CliRunner().invoke(main, ["memory", "gate-rollup", "--rdr", str(RDR), "--project", REPO, *args])
    assert result.exit_code == expect, result.output
    return result.output


def _phase3(beads: str, *extra: str, expect: int = 0) -> str:
    return _run("--phase", "3", "--beads", beads, *extra, expect=expect)


def _pointer(title: str = POINTER_P3) -> gr.Pointer:
    got = gr.read_pointer(make_t3(), title)
    assert got is not None, f"no pointer record titled {title!r}"
    return got


def _titles(records: list[dict]) -> list[str]:
    return sorted(r["title"] for r in records)


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

@pytest.mark.parametrize(("title", "bead", "expected"), [
    ("review-abc1234-nexus-1-2026-11-10", "nexus-1", True),            # hyphen-joined, as gate titles are
    ("nexus-1gdv0-gate-critique", "nexus-1gdv0", True),                # at the start of the title
    ("critique-c218ddc93-nexus-1gdv0.1.3-2026-11-18", "nexus-1gdv0.1.3", True),
    ("notes on nexus-1.", "nexus-1", True),                            # a sentence period after it
    ("review-abc1234-nexus-12-2026-11-10", "nexus-1", False),          # nexus-1 is not nexus-12
    ("review-abc1234-nexus-1gdv0.2.3-2026-11-10", "nexus-1gdv0.2", False),   # a parent is not its child
    ("review-abc1234-conexus-abc-2026-11-10", "nexus-abc", False),     # nor the tail of another id
    ("review-abc1234-nexus-1.2-2026-11-10", "nexus-1", False),
    ("review-abc1234-NEXUS-1-2026-11-10", "nexus-1", False),           # bead ids are lower case: case matters
    ("review-abc1234-2026-11-10", "nexus-1", False),
])
def test_title_names_bead_predicate(title: str, bead: str, expected: bool) -> None:
    assert gr.title_names_bead(title, bead) is expected


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
    assert col.startswith("knowledge__nexus-rdr-research__")
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


def test_split_text_keeps_every_character_within_the_byte_cap() -> None:
    text = "é" * 9000 + "\n" + "x" * 30000 + "\nshort\n" + "日本語" * 4000
    parts = gr.split_text_bytes(text, 4096)
    assert "".join(parts) == text
    assert all(0 < len(p.encode()) <= 4096 for p in parts)
    assert gr.split_text_bytes("tiny", 4096) == ["tiny"]


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
    result = CliRunner().invoke(main, ["memory", "gate-rollup", "--rdr", "209", "--phase", "3", "--beads", "nexus-dp"])
    assert result.exit_code == 0, result.output
    assert _titles(_pointer().listed) == [name]


def test_pointer_titles() -> None:
    assert gr.pointer_title(209, "3") == POINTER_P3 and gr.pointer_title(7, None) == "gate-records-rdr-007-all"


def test_archive_titles_carry_the_project_so_repos_never_collide() -> None:
    assert gr.archive_title("alpha", "review-abc-x-2026") != gr.archive_title("beta", "review-abc-x-2026")


def test_selection_is_pure_over_the_rows_it_is_given() -> None:
    now = datetime.now(UTC).isoformat()
    rows = [{"id": 1, "project": REPO, "title": "review-abc1234-nexus-s-2026-11-10", "tags": "kind:gate,bead:nexus-s,rdr:209",
             "content": "kind: review\nbead: nexus-s\nsha: abc1234\noutcome: justified\ndate: 2026-11-10\n\nx",
             "timestamp": now, "ttl": None}]
    sel = gr.select_records(rows, project=REPO, rdr=209, beads=("nexus-s",), phase="3")
    assert [r["bead"] for r in sel.listed] == ["nexus-s"] and sel.unmapped == []
    assert json.loads(json.dumps(sel.listed[0]))["sha"] == "abc1234"


# ── Step 2: the loss check (nexus-1gdv0.2.6) ─────────────────────────────────────

def test_an_open_phase_is_refused_and_nothing_is_written(db: T2Database, tmp_path: Path) -> None:
    _gate(db, "nexus-o1")
    _write_rdr(tmp_path, RDR, "accepted", {"3": "", "4": "CLOSED"})      # phase 3 has no close marker
    out = _phase3("nexus-o1", expect=1)
    assert "not closed" in out and "phase 3" in out
    assert documents_by_title(POINTER_P3) == [] and documents_by_title(f"{REPO}/{_titles_all(db)[0]}") == []
    out = _run("--all", expect=1)                                          # --all needs the RDR itself closed
    assert "not closed" in out


def test_a_phase_with_the_close_marker_passes_in_an_open_rdr_and_a_sub_phase_follows_its_phase(
        db: T2Database, tmp_path: Path) -> None:
    name = _gate(db, "nexus-o2")
    _write_rdr(tmp_path, RDR, "accepted", {"3": "CLOSED"})
    _phase3("nexus-o2")
    assert _titles(_pointer().listed) == [name]
    _run("--phase", "3a", "--beads", "nexus-o2")                           # 3a is closed when phase 3 is
    assert _pointer("gate-records-rdr-209-phase-3a").listed
    _run("--phase", "4", "--beads", "nexus-o2", expect=1)                  # phase 4 has no heading at all


def _titles_all(db: T2Database) -> list[str]:
    return sorted(_rows(db))


def test_a_planted_unresolvable_title_stops_the_run_and_names_it(db: T2Database) -> None:
    mine = _gate(db, "nexus-u1")
    ghost = {"project": REPO, "title": "review-ghost-nexus-u0-2026-11-01", "kind": "review", "bead": "nexus-u0",
             "sha": "0000000", "verdict": "justified", "timestamp": "2026-11-01T00:00:00Z",
             "archive": gr.archive_title(REPO, "review-ghost-nexus-u0-2026-11-01"), "parts": 1}
    planted, parts = gr.render_pointer(gr.parse_scope(rdr=RDR, phase="3", beads="nexus-u0", all_phases=False), REPO,
                                       gr.Selection(listed=[ghost]))
    t3 = make_t3()
    gr.write_parts(t3, title=POINTER_P3, text=planted, tags="gate-pointer", category=gr.POINTER_CATEGORY)
    out = _phase3("nexus-u1", expect=1)
    assert ghost["title"] in out and "nothing was written" in out
    assert gr.read_archive(t3, REPO, mine, 1) is None                      # no archive was written
    assert _titles(gr.read_pointer(t3, POINTER_P3).listed) == [ghost["title"]]   # the planted pointer is untouched
    assert parts == 1


def test_a_not_justified_newest_record_stops_the_run_and_is_named_and_accept_open_lets_it_through(
        db: T2Database) -> None:
    _gate(db, "nexus-n1", kind="review", sha="aaa1111", outcome="justified", date="2026-11-08")
    worse = _gate(db, "nexus-n1", kind="critique", sha="bbb2222", outcome="not-justified", date="2026-11-09")
    out = _phase3("nexus-n1", expect=1)
    assert "nexus-n1" in out and worse in out and "not justified" in out
    assert documents_by_title(POINTER_P3) == [] and documents_by_title(f"{REPO}/{worse}") == []
    out = _phase3("nexus-n1", "--accept-open", "nexus-n1")
    assert worse in out                                                    # the record is still printed for a person
    got = _pointer()
    assert got.accepted_open == ("nexus-n1",) and len(got.listed) == 2
    out = _phase3("nexus-n1", "--accept-open", "nexus-nope", expect=1)
    assert "nexus-nope" in out and "no listed record" in out


def test_an_older_not_justified_record_does_not_stop_a_bead_whose_newest_is_justified(db: T2Database) -> None:
    _gate(db, "nexus-n2", kind="review", sha="aaa1111", outcome="not-justified", date="2026-11-08")
    _gate(db, "nexus-n2", kind="confirm", sha="bbb2222", outcome="justified", date="2026-11-09")
    out = _phase3("nexus-n2")
    assert "not justified: nexus-n2" in out and "not-justified" in out      # still printed for a person
    assert len(_pointer().listed) == 2


def test_a_bead_less_record_takes_no_part_in_the_chain_check(db: T2Database) -> None:
    _gate(db, "nexus-b1")
    bare = "review-abc1234-no-bead-p3-2026-11-10"
    _put(db, bare, "kind: review\noutcome: not-justified\n\nbody", tags="kind:gate,rdr:209")
    _phase3("nexus-b1")                                                    # a not-justified unmapped record is no bead's chain
    assert _titles(_pointer().unmapped) == [bare]


def test_the_loss_check_leaves_every_access_count_unchanged(db: T2Database) -> None:
    a = _gate(db, "nexus-c1")
    for _ in range(3):
        db.get(project=REPO, title=a)                                      # a real read: the counter is 3
    before = {t: r["access_count"] for t, r in _rows(db).items()}
    assert before[a] == 3
    _phase3("nexus-c1", "--dry-run")
    _phase3("nexus-c1")
    assert {t: r["access_count"] for t, r in _rows(db).items()} == before


def test_the_pointer_must_read_back_with_the_same_list(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate(db, "nexus-r1")
    _gate(db, "nexus-r1", sha="bbb2222")
    real = gr.read_pointer
    calls = []

    def lossy(t3, title):
        got = real(t3, title)
        calls.append(title)
        if len(calls) > 1 and got is not None:       # the second read is the read-back
            got.listed = got.listed[:1]
        return got

    monkeypatch.setattr(gr, "read_pointer", lossy)
    out = _phase3("nexus-r1", expect=1)
    assert "did not read back" in out and len(calls) == 2


def test_a_dry_run_refuses_an_open_phase_as_the_real_run_does(db: T2Database, tmp_path: Path) -> None:
    _gate(db, "nexus-o3")
    _write_rdr(tmp_path, RDR, "accepted", {"3": ""})
    out = _phase3("nexus-o3", "--dry-run", expect=1)
    assert "not closed" in out


def test_a_rerun_keeps_a_record_the_read_no_longer_lists_because_its_archive_is_whole(db: T2Database) -> None:
    kept = _gate(db, "nexus-k1")
    reaped = _gate(db, "nexus-k1", sha="bbb2222")
    _phase3("nexus-k1")
    assert db.delete(project=REPO, title=reaped)                    # what RDR-207's reap leaves: no row, no quarantine
    out = _phase3("nexus-k1")
    got = {r["title"]: r for r in _pointer().listed}
    assert sorted(got) == sorted([kept, reaped])
    assert got[reaped]["state"] == "absent" and "state" not in got[kept]
    assert "carried: 1" in out
    assert gr.read_archive(make_t3(), REPO, reaped, 1) is not None    # the full text is still there
    _phase3("nexus-k1")                                              # and a third run keeps it too
    assert sorted(r["title"] for r in _pointer().listed) == sorted([kept, reaped])


# ── Step 2.3 ordering (nexus-1gdv0.2.7) ──────────────────────────────────────────

def _rec(title: str, verdict: str, *, date: str | None, stamp: str = "2026-11-01T00:00:00Z", bead: str = "nexus-ord") -> dict:
    """A row as get_all returns it: headered when *date* is given (as gate_record writes it), headerless otherwise."""
    head = f"kind: review\nbead: {bead}\nsha: abc1234\noutcome: {verdict}\ndate: {date}\n\nbody" if date else \
        f"older record\n- **outcome**: {verdict}\n"
    return {"id": 1, "project": REPO, "title": title, "tags": f"kind:gate,bead:{bead}", "content": head,
            "timestamp": stamp, "ttl": None}


def _chain(*rows: dict) -> tuple[str, list[str]]:
    sel = gr.select_records(rows, project=REPO, rdr=RDR, beads=("nexus-ord",), phase="3")
    verdict, newest = gt.verdict_chains(sel)["nexus-ord"]
    return verdict, [e["title"] for e in newest]


def test_order_same_day_no_round_the_worse_verdict_wins_the_tiebreak() -> None:
    a = _rec("review-aaa1111-nexus-ord-2026-11-10", "justified", date="2026-11-10")
    b = _rec("critique-bbb2222-nexus-ord-2026-11-10", "not-justified", date="2026-11-10")
    c = _rec("confirm-ccc3333-nexus-ord-2026-11-10", "partial", date="2026-11-10")
    assert _chain(a, b)[0] == "not-justified"
    assert _chain(b, a)[0] == "not-justified"          # the order the rows arrive in decides nothing
    assert _chain(a, c)[0] == "partial"
    verdict, newest = _chain(a, b, c)
    assert verdict == "not-justified" and newest == sorted(newest) and len(newest) == 3


def test_order_a_later_round_is_newer_and_a_no_round_record_sorts_before_r1() -> None:
    r1 = _rec("critique-bbb2222-nexus-ord-2026-11-10-r1", "not-justified", date="2026-11-10")
    r2 = _rec("critique-bbb2222-nexus-ord-2026-11-10-r2", "justified", date="2026-11-10")
    plain = _rec("critique-bbb2222-nexus-ord-2026-11-10", "not-justified", date="2026-11-10")
    assert _chain(r1, r2) == ("justified", [r2["title"]])
    assert _chain(plain, r1)[0] == "not-justified" and _chain(plain, r1)[1] == [r1["title"]]
    r1j = _rec("critique-bbb2222-nexus-ord-2026-11-10-r1", "justified", date="2026-11-10")
    assert _chain(plain, r1j) == ("justified", [r1j["title"]])
    r9 = _rec("critique-bbb2222-nexus-ord-2026-11-10-r9", "not-justified", date="2026-11-10")
    r10 = _rec("critique-bbb2222-nexus-ord-2026-11-10-r10", "justified", date="2026-11-10")
    assert _chain(r9, r10) == ("justified", [r10["title"]])    # the round is a number: r10 is newer than r9


def test_order_the_date_beats_the_round_and_the_round_beats_the_title() -> None:
    late = _rec("a-nexus-ord-2026-11-12", "justified", date="2026-11-12")
    early_r5 = _rec("z-nexus-ord-2026-11-09-r5", "not-justified", date="2026-11-09")
    assert _chain(late, early_r5) == ("justified", [late["title"]])


def test_headerless_record_is_dated_from_the_date_in_its_title() -> None:
    old = _rec("review-aaa1111-nexus-ord-2026-11-12", "justified", date=None, stamp="2026-11-01T00:00:00Z")
    head = _rec("critique-bbb2222-nexus-ord-2026-11-10", "not-justified", date="2026-11-10")
    old["tags"] = ""        # an older record: no tags, matched by its title
    sel = gr.select_records([old, head], project=REPO, rdr=RDR, beads=("nexus-ord",), phase="3")
    assert gt.verdict_chains(sel)["nexus-ord"][0] == "justified"     # 11-12 (title) is newer than 11-10 (header)
    old["title"] = "review-aaa1111-nexus-ord-2026-11-08"
    sel = gr.select_records([old, head], project=REPO, rdr=RDR, beads=("nexus-ord",), phase="3")
    assert gt.verdict_chains(sel)["nexus-ord"][0] == "not-justified"


def test_headerless_record_with_no_date_in_its_title_is_dated_from_its_timestamp() -> None:
    old = _rec("review-nexus-ord-first-pass", "justified", date=None, stamp="2026-11-14T08:00:00Z")
    old["tags"] = ""
    head = _rec("critique-bbb2222-nexus-ord-2026-11-10", "not-justified", date="2026-11-10")
    sel = gr.select_records([old, head], project=REPO, rdr=RDR, beads=("nexus-ord",), phase="3")
    assert gt.verdict_chains(sel)["nexus-ord"][0] == "justified"     # the timestamp's day, 11-14, is newest
    old["timestamp"] = "2026-11-05T08:00:00Z"
    sel = gr.select_records([old, head], project=REPO, rdr=RDR, beads=("nexus-ord",), phase="3")
    assert gt.verdict_chains(sel)["nexus-ord"][0] == "not-justified"


def test_order_a_terminal_partial_stops_the_run_like_not_justified(db: T2Database) -> None:
    _gate(db, "nexus-tp", kind="review", sha="aaa1111", outcome="justified", date="2026-11-08")
    name = _gate(db, "nexus-tp", kind="confirm", sha="bbb2222", outcome="partial", date="2026-11-09")
    out = _phase3("nexus-tp", expect=1)
    assert "nexus-tp" in out and name in out and "partial" in out
    assert "--accept-open" in out and documents_by_title(POINTER_P3) == []


# ── Step 3: the 30-day TTL (nexus-1gdv0.2.8) ─────────────────────────────────────

def _same_but_ttl(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != "ttl"}


def test_ttl_three_records_get_ttl_30_with_every_other_field_unchanged(db: T2Database) -> None:
    names = []
    for i, (agent, session, count) in enumerate([("agent-a", "sess-a", 0), ("agent-b", "sess-b", 4), ("agent-c", "sess-c", 9)]):
        name = _gate(db, f"nexus-t{i}", sha=f"{i}{i}{i}1111", age_days=3 + i)
        names.append(name)
        db.put(project=REPO, title=name, content=_rows(db)[name]["content"], tags=_rows(db)[name]["tags"],
               agent=agent, session=session)
        backdate_memory(db, REPO, name, days=3 + i)
        set_memory_access_count(db, REPO, name, count)
        rewrite_memory_row(db, REPO, name, last_accessed="2026-11-01T12:00:00Z")
    before = _rows(db)
    assert {before[n]["agent"] for n in names} == {"agent-a", "agent-b", "agent-c"}
    out = _phase3("nexus-t0,nexus-t1,nexus-t2")
    after = _rows(db)
    assert "ttl: wrote 3" in out
    for n in names:
        assert after[n]["ttl"] == 30
        assert _same_but_ttl(after[n]) == _same_but_ttl(before[n]), n      # content, tags, agent, session, timestamp,
    # access_count and last_accessed included: the import wrote them as they were


def test_ttl_a_never_accessed_record_stays_never_accessed(db: T2Database) -> None:
    name = _gate(db, "nexus-la")
    assert _rows(db)[name]["last_accessed"] == ""
    _phase3("nexus-la")
    assert _rows(db)[name]["last_accessed"] == "" and _rows(db)[name]["access_count"] == 0


def test_ttl_a_rerun_changes_nothing(db: T2Database) -> None:
    _gate(db, "nexus-rr")
    _gate(db, "nexus-rr", sha="bbb2222")
    first = _phase3("nexus-rr")
    after_first = _rows(db)
    again = _phase3("nexus-rr")
    assert "ttl: wrote 2" in first and "ttl: wrote 0" in again and "2 already have a TTL" in again
    assert _rows(db) == after_first


def test_ttl_a_forced_pointer_write_failure_leaves_no_ttl(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    name = _gate(db, "nexus-pf")

    def refuse(*_a, **_k):
        raise gr.GateRollupError("the pointer was not stored (forced)")

    monkeypatch.setattr(gr, "write_pointer", refuse)
    out = _phase3("nexus-pf", expect=1)
    assert "forced" in out
    assert _rows(db)[name]["ttl"] is None


def test_ttl_a_pointer_that_does_not_read_back_leaves_no_ttl(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    name = _gate(db, "nexus-pg")
    monkeypatch.setattr(gr, "read_pointer", lambda t3, title: None)
    _phase3("nexus-pg", expect=1)
    assert _rows(db)[name]["ttl"] is None


def test_ttl_an_unmapped_record_keeps_no_ttl(db: T2Database) -> None:
    mapped = _gate(db, "nexus-um")
    bad = "review-abc1234-nexus-um-bad-2026-11-10"
    _put(db, bad, "kind: review\nbead: nexus-um\noutcome: PASSED-ish\n\nbody", tags="kind:gate,bead:nexus-um,rdr:209")
    bare = "review-abc1234-no-bead-p3-2026-11-10"
    _put(db, bare, "kind: review\n\nbody", tags="kind:gate,rdr:209")
    _phase3("nexus-um")
    rows = _rows(db)
    assert rows[mapped]["ttl"] == 30 and rows[bad]["ttl"] is None and rows[bare]["ttl"] is None


def test_ttl_the_rdr_project_rows_get_none(db: T2Database) -> None:
    _gate(db, "nexus-rd")
    for title in ("209-gate-latest", "209-gate-critique-2026-11-16-r1", "209-fix-check-abc"):
        _put(db, title, "gate flow row", tags="rdr,gate,kind:gate,bead:nexus-rd,rdr:209", project=f"{REPO}_rdr", age_days=40)
    _phase3("nexus-rd")
    _run("--all")
    assert all(r["ttl"] is None for r in _rows(db, f"{REPO}_rdr").values()) and len(_rows(db, f"{REPO}_rdr")) == 3


def test_ttl_an_access_between_the_listing_and_the_import_keeps_its_count(
        db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    name = _gate(db, "nexus-ac")
    real = gr.write_pointer

    def write_then_read(*a, **k):
        done = real(*a, **k)
        db.get(project=REPO, title=name)       # a reader's access lands after the listing, before the import
        return done

    monkeypatch.setattr(gr, "write_pointer", write_then_read)
    _phase3("nexus-ac")
    assert _rows(db)[name]["access_count"] == 1 and _rows(db)[name]["ttl"] == 30


def test_dry_run_counts_exactly_the_record_past_its_effective_ttl_and_writes_nothing(db: T2Database) -> None:
    old = _gate(db, "nexus-dr", sha="aaa1111", age_days=40)
    fresh = _gate(db, "nexus-dr", sha="bbb2222", age_days=10)
    before = _rows(db)
    out = _phase3("nexus-dr", "--dry-run")
    assert "quarantined by the next expire sweep: 1 of 2" in out
    line = {t: next(ln for ln in out.splitlines() if t in ln and ln.startswith("  candidate")) for t in (old, fresh)}
    assert "past its effective TTL" in line[old] and "within its effective TTL" in line[fresh]
    assert _rows(db) == before and documents_by_title(POINTER_P3) == []


def test_ttl_a_field_that_does_not_read_back_is_named_with_the_original_row(
        db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    from nexus.db.t2.http_memory_store import HttpMemoryStore

    name = _gate(db, "nexus-mm")
    real = HttpMemoryStore.import_entry

    def altered(self, **kw):
        return real(self, **{**kw, "tags": kw["tags"] + ",tampered"})

    monkeypatch.setattr(HttpMemoryStore, "import_entry", altered)
    out = _phase3("nexus-mm", expect=1)
    assert f"mismatch: {name!r} field tags" in out and "tampered" in out and "original row" in out
