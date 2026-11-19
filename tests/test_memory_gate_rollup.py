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

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db import make_t3
from nexus.db.limits import QUOTAS
from nexus.db.t2 import T2Database
from nexus.memory_curate import gate_rollup as gr
from tests._catalog_fixture_ops import documents_by_title
from tests._engine_substrate import ensure_engine, mint_test_tenant
from tests._t2_fixture_ops import backdate_memory

pytestmark = pytest.mark.t2_tenant

REPO = "gaterepo"
RDR = 209
POINTER_P3 = "gate-records-rdr-209-phase-3"


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

# ── the unmapped list (nexus-1gdv0.2.2) ──────────────────────────────────────────

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
    out = _phase3("nexus-p1,nexus-p2")
    assert "3 listed" in out and "0 unmapped" in out
    got = _pointer()
    assert _titles(got.listed) == sorted(names)
    by_title = {r["title"]: r for r in got.listed}
    one = by_title[names[2]]
    assert (one["project"], one["kind"], one["bead"], one["sha"], one["verdict"]) == (
        REPO, "confirm", "nexus-p2", "ccc3333", "partial")
    assert len(documents_by_title(POINTER_P3)) == 1


def test_pointer_is_a_knowledge_note_in_the_research_subject_with_its_category(db: T2Database) -> None:
    _gate(db, "nexus-c1")
    _phase3("nexus-c1")
    col = gr.collection_name(make_t3())
    assert col.startswith("knowledge__nexus-rdr-research__")
    # a note's own keys live on its catalog document (RDR-236)
    (pointer,) = documents_by_title(POINTER_P3)
    assert pointer.physical_collection == col
    assert pointer.meta["category"] == "gate_pointer" and "gate-pointer" in pointer.meta["tags"]


def test_the_verdict_comes_from_the_header_or_from_an_older_records_last_outcome_line(db: T2Database) -> None:
    head = _gate(db, "nexus-h1", outcome="not-justified")
    old = "review-abc1234-nexus-h2-2026-11-10"
    _put(db, old, "first pass\n- **outcome**: partial\nsecond pass after the fixes\n- **Outcome:** `justified`\n",
         tags="kind:gate,bead:nexus-h2")
    plain = "review-abc1234-nexus-h3-2026-11-10"
    _put(db, plain, "text\noutcome: not-justified\n", tags="kind:gate,bead:nexus-h3")
    _phase3("nexus-h1,nexus-h2,nexus-h3")
    got = {r["title"]: r["verdict"] for r in _pointer().listed}
    assert got == {head: "not-justified", old: "justified", plain: "not-justified"}


def test_a_body_line_does_not_stand_in_for_the_header_outcome(db: T2Database) -> None:
    name = _gate(db, "nexus-h4", outcome="partial", body="quoted example:\noutcome: justified")
    _phase3("nexus-h4")
    assert {r["title"]: r["verdict"] for r in _pointer().listed} == {name: "partial"}


def test_a_rerun_replaces_the_pointer(db: T2Database) -> None:
    _gate(db, "nexus-rr1")
    _phase3("nexus-rr1")
    second = _gate(db, "nexus-rr1", sha="bbb2222")
    _phase3("nexus-rr1")
    got = _pointer()
    assert second in _titles(got.listed) and len(got.listed) == 2
    assert len(documents_by_title(POINTER_P3)) == 1


# ── the archive: the full text stays in T3 (nexus-1gdv0.2.5) ─────────────────────

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
    for i in range(130):
        _gate(db, f"nexus-many{i:03d}", sha=f"{i:07x}", body=f"finding {i}")
    _run("--all")
    assert _pointer("gate-records-rdr-209-all").parts >= 2
    for row in list(db.get_all(REPO))[3:]:
        db.delete(project=REPO, title=row["title"])
    assert len(db.get_all(REPO)) == 3
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
    assert _rows(db) == before


def test_the_command_writes_no_ttl_and_leaves_every_t2_field_as_it_was(db: T2Database) -> None:
    _gate(db, "nexus-n1")
    _gate(db, "nexus-n1", sha="bbb2222", age_days=40)
    before = _rows(db)
    _phase3("nexus-n1")
    _run("--all")
    assert _rows(db) == before
    assert all(r["ttl"] is None for r in _rows(db).values())


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


def test_selection_is_pure_over_the_rows_it_is_given() -> None:
    now = datetime.now(UTC).isoformat()
    rows = [{"id": 1, "project": REPO, "title": "review-abc1234-nexus-s-2026-11-10", "tags": "kind:gate,bead:nexus-s,rdr:209",
             "content": "kind: review\nbead: nexus-s\nsha: abc1234\noutcome: justified\ndate: 2026-11-10\n\nx",
             "timestamp": now, "ttl": None}]
    sel = gr.select_records(rows, project=REPO, rdr=209, beads=("nexus-s",), phase="3")
    assert [r["bead"] for r in sel.listed] == ["nexus-s"] and sel.unmapped == []
    assert json.loads(json.dumps(sel.listed[0]))["sha"] == "abc1234"
