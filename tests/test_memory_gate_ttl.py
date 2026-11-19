# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx memory gate-rollup``, the loss check and the TTL write (RDR-209 Phase 3 Steps 2 and 3, nexus-1gdv0.2.4 and
.6 to .14): the closed-phase refusal, the verdict chain and its order, the pointer read-back, the 30-day TTL through
the import route, the engine's expiry rule, the read-back against the expire sweep, a restored record, the dry run
and the count-first guard.

Every TTL here is written to the self-provisioned test engine, never the live store."""
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

pytestmark = pytest.mark.t2_tenant


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

    calls: list[int] = []

    def write_then_read(*a, **k):
        done = real(*a, **k)
        if not calls:                          # the first pointer write: the one before the TTL writes
            db.get(project=REPO, title=name)   # a reader's access lands after the listing, before the import
        calls.append(1)
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
    name = _gate(db, "nexus-mm")
    real = HttpMemoryStore.import_entry

    def altered(self, **kw):
        return real(self, **{**kw, "tags": kw["tags"] + ",tampered"})

    monkeypatch.setattr(HttpMemoryStore, "import_entry", altered)
    out = _phase3("nexus-mm", expect=1)
    assert f"mismatch: {name!r} field tags" in out and "tampered" in out and "original row" in out


def test_ttl_null_attribution_stays_null_where_a_put_would_fill_it(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control is real: NX_AGENT and a resolvable session are set, and a put under them writes both. Without that a
    test process resolves neither (conftest drops CLAUDE_CODE_SESSION_ID, the config dir has no session file), a put
    would also leave them null, and "stays null" would pass whether or not the write used put."""
    monkeypatch.setenv("NX_AGENT", "rollup-test-agent")
    monkeypatch.setenv("NX_SESSION_ID", "rollup-test-session")
    monkeypatch.setattr(session_mod, "read_claude_session_id", lambda: None)
    db.memory.put(REPO, "null-attribution-control", "scratch", tags="notes", ttl=30)         # no agent, no session given
    control = _rows(db)["null-attribution-control"]
    assert (control["agent"], control["session"]) == ("rollup-test-agent", "rollup-test-session"), (
        "NX_AGENT and a session are set, so a put must fill both; if it did not, this test cannot tell put from import")
    name = "review-abc1234-nexus-nu-2026-11-10"
    db.memory.import_entry(project=REPO, title=name, tags="kind:gate,bead:nexus-nu,rdr:209", timestamp=utc_stamp(days=3),
                           content="kind: review\nbead: nexus-nu\nrdr: 209\nsha: abc1234\noutcome: justified\n"
                                   "date: 2026-11-10\n\nbody", agent=None, session=None)
    seeded = _rows(db)[name]
    assert seeded["agent"] is None and seeded["session"] is None and seeded["ttl"] is None
    _phase3("nexus-nu")
    after = _rows(db)[name]
    assert after["ttl"] == 30
    assert after["agent"] is None and after["session"] is None


# ── read-back and rerun against the expire sweep (nexus-1gdv0.2.11) ──────────────

def _sweep_after_each_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The session-end expire sweep landing between the write and the read-back: after each import it runs."""
    real = HttpMemoryStore.import_entry

    def import_then_sweep(self, **kw):
        out = real(self, **kw)
        self.expire()
        return out

    monkeypatch.setattr(HttpMemoryStore, "import_entry", import_then_sweep)


def test_readback_reports_a_row_the_sweep_already_quarantined_as_quarantined_not_a_mismatch(
        db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _gate(db, "nexus-q1", age_days=40)                 # 40 days old, ttl 30: past its effective TTL
    sent = dict(_rows(db)[old])
    _sweep_after_each_import(monkeypatch)
    out = _phase3("nexus-q1")                                 # exit 0: nothing was lost
    assert old not in _rows(db)                               # the sweep hid it, so get_all cannot read it back
    assert "mismatch" not in out
    assert f"quarantined (restorable: nx memory restore" in out and old in out
    (back,) = [r for r in db.memory.list_quarantined(project=REPO) if r["title"] == old]
    assert back["ttl"] == 30 and {k: back[k] for k in gt.KEPT_FIELDS} == {k: sent[k] for k in gt.KEPT_FIELDS}


def test_readback_a_row_that_is_neither_live_nor_quarantined_is_a_mismatch(
        db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    name = _gate(db, "nexus-q2")
    real = HttpMemoryStore.import_entry

    def import_then_lose(self, **kw):
        out = real(self, **kw)
        self.delete(kw["project"], kw["title"]) if hasattr(self, "delete") else None
        return out

    monkeypatch.setattr(HttpMemoryStore, "import_entry", import_then_lose)
    out = _phase3("nexus-q2", expect=1)
    assert f"mismatch: {name!r} is not in the read after the write" in out


def test_rerun_carries_a_quarantined_title_marked_quarantined(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _gate(db, "nexus-q3", age_days=40)
    kept = _gate(db, "nexus-q3", sha="bbb2222", age_days=1)
    with monkeypatch.context() as m:
        _sweep_after_each_import(m)
        _phase3("nexus-q3")
    assert old not in _rows(db) and kept in _rows(db)         # the sweep quarantined only the old one
    out = _phase3("nexus-q3")
    got = {r["title"]: r for r in _pointer().listed}
    assert sorted(got) == sorted([old, kept])
    assert got[old]["state"] == "quarantined" and "state" not in got[kept]
    assert "carried: 1" in out
    # restoring it makes it live again, so the next run lists it as a record of the read
    (q,) = [r for r in db.memory.list_quarantined(project=REPO) if r["title"] == old]
    assert db.memory.restore(q["id"])
    _phase3("nexus-q3")
    assert "state" not in {r["title"]: r for r in _pointer().listed}[old]


def test_rerun_a_quarantined_record_whose_archive_is_gone_is_still_carried(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _gate(db, "nexus-q4", age_days=40)
    with monkeypatch.context() as m:
        _sweep_after_each_import(m)
        _phase3("nexus-q4")
    prev = _pointer()
    prev.listed[0]["archive"] = "gaterepo/no-such-archive"       # the archive is gone, but the quarantined row is restorable
    text, parts = gr.render_pointer(gr.parse_scope(rdr=RDR, phase="3", beads="nexus-q4", all_phases=False), REPO,
                                    gr.Selection(listed=prev.listed))
    gr.write_parts(make_t3(), title=POINTER_P3, text=text, tags="gate-pointer", category=gr.POINTER_CATEGORY)
    _phase3("nexus-q4")
    assert _pointer().listed[0]["title"] == old and _pointer().listed[0]["state"] == "quarantined"


# ── a record a person restored is not given a TTL again (nexus-1gdv0.2.4, second half) ──

def _restore_by_hand(db: T2Database, title: str) -> None:
    """What ``nx memory restore`` does to a row: permanent again (ttl NULL), every other field as it was."""
    row = _rows(db)[title]
    db.memory.import_entry(project=REPO, title=title, content=row["content"], timestamp=row["timestamp"],
                           tags=row["tags"], ttl=None, agent=row["agent"], session=row["session"],
                           access_count=row["access_count"], last_accessed=row["last_accessed"] or None)


def test_rerun_skips_restored_record_names_it_and_keeps_it_marked(db: T2Database) -> None:
    kept = _gate(db, "nexus-rs1")
    restored = _gate(db, "nexus-rs1", sha="bbb2222")
    _phase3("nexus-rs1")
    assert _rows(db)[restored]["ttl"] == 30
    _restore_by_hand(db, restored)
    assert _rows(db)[restored]["ttl"] is None
    calls: list[str] = []
    real = HttpMemoryStore.import_entry

    def spy(self, **kw):
        calls.append(kw["title"])
        return real(self, **kw)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(HttpMemoryStore, "import_entry", spy)
        out = _phase3("nexus-rs1")
    assert calls == []                                              # no import for either record
    assert _rows(db)[restored]["ttl"] is None and _rows(db)[kept]["ttl"] == 30
    assert f"restored, left alone: {restored}" in out
    got = {r["title"]: r for r in _pointer().listed}
    assert got[restored]["restored"] is True and got[restored]["ttl_set"] is True and "restored" not in got[kept]
    with pytest.MonkeyPatch.context() as m:                         # and the next rerun skips it too
        m.setattr(HttpMemoryStore, "import_entry", spy)
        _phase3("nexus-rs1")
    assert calls == [] and _rows(db)[restored]["ttl"] is None


def test_rerun_ttls_a_new_record_that_is_not_in_the_previous_pointer(db: T2Database) -> None:
    restored = _gate(db, "nexus-rs2")
    _phase3("nexus-rs2")
    _restore_by_hand(db, restored)
    new = _gate(db, "nexus-rs2", sha="ccc3333")                     # the control: no earlier pointer lists it
    out = _phase3("nexus-rs2")
    rows = _rows(db)
    assert rows[new]["ttl"] == 30 and rows[restored]["ttl"] is None
    assert "ttl: wrote 1" in out


def test_rerun_after_a_partial_ttl_write_finishes_the_rest_and_does_not_call_them_restored(
        db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    a = _gate(db, "nexus-rs3", sha="aaa1111")
    b = _gate(db, "nexus-rs3", sha="bbb2222")
    first, second = sorted([a, b])
    real = HttpMemoryStore.import_entry

    def fail_on_second(self, **kw):
        if kw["title"] == second:
            raise RuntimeError("engine went away")
        return real(self, **kw)

    with monkeypatch.context() as m:
        m.setattr(HttpMemoryStore, "import_entry", fail_on_second)
        out = _phase3("nexus-rs3", expect=1)
    assert "TTL write stopped" in out and _rows(db)[first]["ttl"] == 30 and _rows(db)[second]["ttl"] is None
    out = _phase3("nexus-rs3")                                       # the earlier pointer never marked `second` as TTL'd
    assert _rows(db)[second]["ttl"] == 30 and "restored" not in out


def test_pointer_still_lists_each_timestamp_after_the_ttl_write(db: T2Database) -> None:
    a = _gate(db, "nexus-ts1", age_days=7)
    b = _gate(db, "nexus-ts1", sha="bbb2222", age_days=12)
    stamps = {t: r["timestamp"] for t, r in _rows(db).items()}
    _phase3("nexus-ts1")
    assert all(r["ttl"] == 30 for r in _rows(db).values())
    got = {r["title"]: r for r in _pointer().listed}
    assert got[a]["timestamp"] == stamps[a] and got[b]["timestamp"] == stamps[b]      # the import kept them
    assert all(_rows(db)[t]["timestamp"] == stamps[t] for t in (a, b))
    assert got[a]["ttl_set"] is True and got[b]["ttl_set"] is True
