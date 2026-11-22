# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx memory gate-rollup``, the loss check and the TTL write (RDR-209 Phase 3 Steps 2 and 3, nexus-1gdv0.2.4 and
.6 to .14): the closed-phase refusal, the verdict chain and its order, the pointer read-back, the 30-day TTL through
the import route, the engine's expiry rule, the read-back against the expire sweep, a restored record, the dry run
and the count-first guard.

Every TTL here is written to the self-provisioned test engine, never the live store."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
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
from nexus import memory_curate as mc
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
    _run_raw,
    _phase3,
    _pointer,
    _titles,
)

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]


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
    real_read, real_write = gr.read_pointer, gr.write_pointer
    written = []

    def lossy(t3, title):
        got = real_read(t3, title)
        if written and got is not None:              # a read after the pointer was written is the read-back
            got.listed = got.listed[:1]
        return got

    def mark_written(*a, **k):
        written.append(1)
        return real_write(*a, **k)

    monkeypatch.setattr(gr, "read_pointer", lossy)
    monkeypatch.setattr(gr, "write_pointer", mark_written)
    out = _phase3("nexus-r1", expect=1)
    assert "did not read back" in out and written


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
    test process resolves neither (conftest drops CLAUDE_CODE_SESSION_ID, no token-file session), a put
    would also leave them null, and "stays null" would pass whether or not the write used put."""
    monkeypatch.setenv("NX_AGENT", "rollup-test-agent")
    monkeypatch.setenv("NX_SESSION_ID", "rollup-test-session")
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


# ── --dry_run applies the loss check and says what it skips (nexus-1gdv0.2.12) ───

def test_dry_run_names_a_missing_title_and_stops(db: T2Database) -> None:
    _gate(db, "nexus-dm")
    ghost = {"project": REPO, "title": "review-ghost-nexus-dm0-2026-11-01", "kind": "review", "bead": "nexus-dm0",
             "sha": "0000000", "verdict": "justified", "timestamp": "2026-11-01T00:00:00Z",
             "archive": gr.archive_title(REPO, "review-ghost-nexus-dm0-2026-11-01"), "parts": 1}
    planted, _ = gr.render_pointer(gr.parse_scope(rdr=RDR, phase="3", beads="nexus-dm", all_phases=False), REPO,
                                   gr.Selection(listed=[ghost]))
    gr.write_parts(make_t3(), title=POINTER_P3, text=planted, tags="gate-pointer", category=gr.POINTER_CATEGORY)
    out = _phase3("nexus-dm", "--dry-run", expect=1)
    assert ghost["title"] in out and "nothing was written" in out


def test_dry_run_applies_the_verdict_chain_and_accept_open(db: T2Database) -> None:
    worse = _gate(db, "nexus-dv", outcome="not-justified")
    out = _phase3("nexus-dv", "--dry-run", expect=1)
    assert "nexus-dv" in out and worse in out and "not justified" in out
    out = _phase3("nexus-dv", "--dry-run", "--accept-open", "nexus-dv")
    assert "would be quarantined by the next expire sweep: 0 of 1" in out
    assert _rows(db)[worse]["ttl"] is None


def test_dry_run_skips_the_pointer_and_says_so_and_writes_nothing_to_t3(
        db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    name = _gate(db, "nexus-ds")
    reads: list[str] = []
    real = gr.read_pointer

    def counting_read(t3, title):
        reads.append(title)
        return real(t3, title)

    def no_write(*_a, **_k):
        raise AssertionError("a dry run wrote to T3")

    monkeypatch.setattr(gr, "read_pointer", counting_read)
    monkeypatch.setattr(gr, "store_note", no_write)
    out = _phase3("nexus-ds", "--dry-run")
    assert "archives, the pointer write and its read-back skipped" in out
    assert reads == [POINTER_P3]          # the earlier pointer, once: no read-back of a pointer it never wrote
    assert documents_by_title(POINTER_P3) == [] and documents_by_title(f"{REPO}/{name}") == []


def test_dry_run_leaves_every_access_count_unchanged(db: T2Database) -> None:
    a = _gate(db, "nexus-da")
    db.get(project=REPO, title=a)
    before = {t: r["access_count"] for t, r in _rows(db).items()}
    _phase3("nexus-da", "--dry-run")
    assert {t: r["access_count"] for t, r in _rows(db).items()} == before == {a: 1}


def test_dry_run_does_not_count_an_unmapped_or_restored_record(db: T2Database) -> None:
    mapped = _gate(db, "nexus-dn", age_days=40)
    bare = "review-abc1234-no-bead-p3-2026-11-10"
    _put(db, bare, "kind: review\n\nbody", tags="kind:gate,rdr:209", age_days=60)
    restored = _gate(db, "nexus-dn", sha="bbb2222", age_days=50)
    _phase3("nexus-dn")
    _restore_by_hand(db, restored)
    out = _phase3("nexus-dn", "--dry-run")
    assert "quarantined by the next expire sweep: 0 of 0" in out and f"restored, left alone: {restored}" in out
    candidates = [ln for ln in out.splitlines() if ln.startswith("  candidate")]
    assert candidates == []                       # the mapped record already has its TTL, the others are not candidates
    assert _rows(db)[mapped]["ttl"] == 30 and _rows(db)[bare]["ttl"] is None


# ── the count-first guard (nexus-1gdv0.2.13) ─────────────────────────────────────
COUNT_P3 = "gate-rollup-count-rdr-209-phase-3"


def test_guard_a_real_run_with_no_recorded_count_refuses_and_writes_nothing(db: T2Database) -> None:
    name = _gate(db, "nexus-g1")
    out = _run_raw("--phase", "3", "--beads", "nexus-g1", expect=1)
    assert "no dry-run count is recorded for RDR-209 phase 3" in out and "--dry-run" in out
    assert _rows(db)[name]["ttl"] is None and COUNT_P3 not in _rows(db, with_counts=True)
    assert documents_by_title(POINTER_P3) == [] and documents_by_title(f"{REPO}/{name}") == []


def test_guard_a_dry_run_records_one_count_row_in_repo_only_and_the_real_run_then_proceeds(db: T2Database) -> None:
    _gate(db, "nexus-g2", age_days=40)
    _gate(db, "nexus-g2", sha="bbb2222", age_days=5)
    _put(db, "209-gate-latest", "gate flow row", tags="rdr", project=f"{REPO}_rdr")
    before_rdr = _rows(db, f"{REPO}_rdr", with_counts=True)
    out = _phase3("nexus-g2", "--dry-run")
    assert "quarantined by the next expire sweep: 1 of 2" in out and f"count recorded: {REPO}/{COUNT_P3}" in out
    counts = [t for t in _rows(db, with_counts=True) if t.startswith("gate-rollup-count-")]
    assert counts == [COUNT_P3]
    assert _rows(db, f"{REPO}_rdr", with_counts=True) == before_rdr
    row = _rows(db, with_counts=True)[COUNT_P3]
    assert row["ttl"] is None and "kind:gate" not in row["tags"].split(",") and "gate" not in row["tags"].split(",")
    out = _run_raw("--phase", "3", "--beads", "nexus-g2", "--counted", "1")
    assert "ttl: wrote 2" in out
    assert all(r["ttl"] == 30 for r in _rows(db).values())


def test_guard_the_counted_number_must_be_given_and_equal_the_recorded_count(db: T2Database) -> None:
    name = _gate(db, "nexus-g3", age_days=40)
    _phase3("nexus-g3", "--dry-run")                                  # records a count of 1
    out = _run_raw("--phase", "3", "--beads", "nexus-g3", expect=1)
    assert "--counted" in out and "the number the dry run reported" in out
    out = _run_raw("--phase", "3", "--beads", "nexus-g3", "--counted", "0", expect=1)
    assert "--counted 0" in out and "recorded count" in out
    assert _rows(db)[name]["ttl"] is None and documents_by_title(POINTER_P3) == []
    _run_raw("--phase", "3", "--beads", "nexus-g3", "--counted", "1")
    assert _rows(db)[name]["ttl"] == 30


def test_guard_a_count_recorded_for_another_phase_or_rdr_does_not_unlock_this_one(db: T2Database, tmp_path: Path) -> None:
    _gate(db, "nexus-g4")
    _write_rdr(tmp_path, RDR, "closed")
    _run_raw("--phase", "4", "--beads", "nexus-g4", "--dry-run")      # phase 4's count
    assert "gate-rollup-count-rdr-209-phase-4" in _rows(db, with_counts=True)
    out = _run_raw("--phase", "3", "--beads", "nexus-g4", "--counted", "0", expect=1)
    assert "no dry-run count is recorded for RDR-209 phase 3" in out
    _run_raw("--all", "--dry-run")                                    # --all has its own count, not phase 3's
    assert "gate-rollup-count-rdr-209-all" in _rows(db, with_counts=True)
    out = _run_raw("--phase", "3", "--beads", "nexus-g4", "--counted", "0", expect=1)
    assert "no dry-run count is recorded" in out


def test_guard_a_record_the_dry_run_did_not_count_refuses_the_real_run(db: T2Database) -> None:
    _gate(db, "nexus-g5")
    _phase3("nexus-g5", "--dry-run")
    new = _gate(db, "nexus-g5", sha="bbb2222")
    out = _run_raw("--phase", "3", "--beads", "nexus-g5", "--counted", "0", expect=1)
    assert new in out and "not in the dry run's count" in out
    assert all(r["ttl"] is None for r in _rows(db).values())


def test_guard_a_run_with_nothing_to_write_needs_no_count(db: T2Database) -> None:
    _gate(db, "nexus-g6")
    _phase3("nexus-g6")                                               # dry run, count, real run
    for r in db.get_all(REPO):
        if r["title"].startswith("gate-rollup-count-"):
            db.delete(project=REPO, title=r["title"])
    out = _run_raw("--phase", "3", "--beads", "nexus-g6")             # every record has its TTL: nothing to count
    assert "ttl: wrote 0" in out


def test_guard_the_count_row_is_never_listed_and_the_census_does_not_flag_it(db: T2Database) -> None:
    _gate(db, "nexus-g7")
    _phase3("nexus-g7", "--dry-run")
    db_row = _rows(db, with_counts=True)[COUNT_P3]
    backdate_memory(db, REPO, COUNT_P3, days=90)
    sel = gr.select_records(db.get_all(REPO), project=REPO, rdr=RDR, beads=(COUNT_P3, "nexus-g7"), phase="3")
    assert COUNT_P3 not in [e["title"] for e in sel.listed + sel.unmapped] and db_row
    found = mc.t2_findings(db, REPO, closed_rdrs={"209"}, now=datetime.now(UTC))
    assert COUNT_P3 not in [c.title for c in found.census]


def test_guard_a_later_dry_run_replaces_the_count(db: T2Database) -> None:
    _gate(db, "nexus-g8", age_days=40)
    _phase3("nexus-g8", "--dry-run")
    _gate(db, "nexus-g8", sha="bbb2222", age_days=50)
    _phase3("nexus-g8", "--dry-run")
    assert [t for t in _rows(db, with_counts=True) if t.startswith("gate-rollup-count-")] == [COUNT_P3]
    _run_raw("--phase", "3", "--beads", "nexus-g8", "--counted", "2")


# ── the dry run uses the engine's whole expiry rule (nexus-1gdv0.2.14) ───────────
def test_dry_run_the_import_route_cannot_store_a_null_timestamp(db: T2Database) -> None:
    """The engine skips a null timestamp (MemoryRepository.java:695); the dry run's test of that uses a hand-made row
    (tests/test_memory_gate_logic.py) because the import route cannot store one, which is checked here."""
    with pytest.raises(Exception, match=r"(?i)timestamp|400|required"):
        db.memory.import_entry(project=REPO, title="null-ts", content="x", timestamp=None)   # type: ignore[arg-type]


# ── review round 1 (nexus-1gdv0.2.16) ────────────────────────────────────────────

def test_a_record_edited_after_it_was_archived_gets_no_ttl_is_named_and_a_rerun_archives_it(
        db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    edited = _gate(db, "nexus-ed", sha="aaa1111")
    steady = _gate(db, "nexus-ed", sha="bbb2222")
    real = gr.write_pointer
    calls: list[int] = []

    def edit_after_first_pointer(*a, **k):
        done = real(*a, **k)
        if not calls:                          # between the archive and the TTL write, a reviewer rewrites the record
            row = _rows(db)[edited]
            db.put(project=REPO, title=edited, content=row["content"] + "\nA late finding.", tags=row["tags"])
        calls.append(1)
        return done

    with monkeypatch.context() as m:
        m.setattr(gr, "write_pointer", edit_after_first_pointer)
        out = _phase3("nexus-ed")
    rows = _rows(db)
    assert rows[edited]["ttl"] is None and rows[steady]["ttl"] == 30      # no TTL for text that has no archive
    assert f"changed since it was archived, no TTL: {edited}" in out
    assert gr.read_archive(make_t3(), REPO, edited, 1) != rows[edited]["content"]
    out = _phase3("nexus-ed")                                            # a rerun archives the new text, then writes it
    assert gr.read_archive(make_t3(), REPO, edited, 1) == _rows(db)[edited]["content"]
    assert _rows(db)[edited]["ttl"] == 30 and "changed since" not in out


def test_readback_after_a_carry_looks_the_quarantined_listing_up_afresh(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """The carry reads the quarantined listing before the writes (and finds nothing). A row the sweep quarantines after
    its import is not in that listing, so the read-back must list again, or it reports a false mismatch."""
    _gate(db, "nexus-qa", sha="aaa1111")
    reaped = _gate(db, "nexus-qa", sha="bbb2222")
    _phase3("nexus-qa")
    db.delete(project=REPO, title=reaped)                              # the carry now has a record to look up
    old = _gate(db, "nexus-qa", sha="ccc3333", age_days=40)            # new, and already past its effective TTL
    with monkeypatch.context() as m:
        _sweep_after_each_import(m)
        out = _phase3("nexus-qa")
    assert "mismatch" not in out and f"quarantined (restorable: nx memory restore" in out and old in out
    assert old not in _rows(db)


@pytest.mark.parametrize("failure", ["archive write refused", "archive does not read back"])
def test_no_record_gets_a_ttl_until_every_archive_has_been_written_and_read_back(
        db: T2Database, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    names = [_gate(db, "nexus-ab", sha="aaa1111"), _gate(db, "nexus-ab", sha="bbb2222"), _gate(db, "nexus-ab", sha="ccc3333")]
    archive_prefix = f"{REPO}/"
    if failure == "archive write refused":
        real_write = gr.write_parts

        def refuse_the_second_archive(t3, *, title, **kw):
            if title == archive_prefix + sorted(names)[1]:
                raise gr.GateRollupError(f"{title!r} was not stored (forced)")
            return real_write(t3, title=title, **kw)

        monkeypatch.setattr(gr, "write_parts", refuse_the_second_archive)
    else:
        real_read = gr.read_note
        monkeypatch.setattr(gr, "read_note", lambda t3, col, title: None if title.startswith(archive_prefix) else real_read(t3, col, title))
    out = _phase3("nexus-ab", expect=1)
    assert "forced" in out or "did not read back" in out
    assert all(r["ttl"] is None for r in _rows(db).values()) and set(_rows(db)) == set(names)


def test_guard_a_clock_moved_past_a_records_effective_ttl_refuses_the_real_run(db: T2Database, tmp_path: Path) -> None:
    """The count is a snapshot. A record 20 days old is not past its 30-day TTL at the dry run (count 0), and is at the
    real run 15 days later, so the sweep would quarantine one more than Sam was told: the run recounts and refuses."""
    name = _gate(db, "nexus-gc", age_days=20)
    kw = {"project": REPO, "scope": gr.parse_scope(rdr=RDR, phase="3", beads="nexus-gc", all_phases=False),
          "accept_open": (), "root": tmp_path, "t3_factory": make_t3}
    t0 = datetime.now(UTC)
    dry = gt.run(db.memory, dry_run=True, now=t0, **kw)
    assert "quarantined by the next expire sweep: 0 of 1" in "\n".join(dry.lines)
    with pytest.raises(gr.GateRollupError, match=r"now 1 .* recorded 0|recounted"):
        gt.run(db.memory, dry_run=False, counted=0, now=t0 + timedelta(days=15), **kw)
    assert _rows(db)[name]["ttl"] is None and documents_by_title(POINTER_P3) == []
    gt.run(db.memory, dry_run=False, counted=0, now=t0 + timedelta(days=1), **kw)        # unchanged count: it proceeds
    assert _rows(db)[name]["ttl"] == 30


def test_project_must_be_the_repository_the_command_runs_in_and_never_a_repo_rdr_project(db: T2Database) -> None:
    _gate(db, "nexus-pj")
    out = _run_raw("--phase", "3", "--beads", "nexus-pj", "--dry-run", "--project", f"{REPO}_rdr", expect=2)
    assert "_rdr" in out and "never" in out
    out = _run_raw("--phase", "3", "--beads", "nexus-pj", "--dry-run", "--project", "otherrepo", expect=2)
    assert "otherrepo" in out and REPO in out and "RDR files" in out
    assert [t for t in _rows(db, with_counts=True) if t.startswith("gate-rollup-count-")] == []   # nothing was recorded
    assert "1 listed" in _run_raw("--phase", "3", "--beads", "nexus-pj", "--dry-run")           # the repo's own project passes


def _lossy_after_write(monkeypatch: pytest.MonkeyPatch, *, on_write: int, change) -> None:
    """From the *on_write*-th pointer write on, a pointer read hands back the records with *change* applied."""
    real_read, real_write = gr.read_pointer, gr.write_pointer
    writes: list[int] = []

    def counting_write(*a, **k):
        writes.append(1)
        return real_write(*a, **k)

    def lossy_read(t3, title):
        got = real_read(t3, title)
        if got is not None and len(writes) >= on_write:
            for rec in got.listed:
                change(rec)
        return got

    monkeypatch.setattr(gr, "write_pointer", counting_write)
    monkeypatch.setattr(gr, "read_pointer", lossy_read)


def test_the_pointer_read_back_also_compares_the_ttl_marks(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    name = _gate(db, "nexus-pm")
    _lossy_after_write(monkeypatch, on_write=2, change=lambda rec: rec.update(ttl_set=False))   # the second write carries the marks
    out = _phase3("nexus-pm", expect=1)
    assert "1 record(s) now have the TTL but the pointer's ttl marks were not written" in out and "did not read back" in out
    assert _rows(db)[name]["ttl"] == 30


def test_the_pointer_read_back_also_compares_the_state_of_a_carried_record(db: T2Database, monkeypatch: pytest.MonkeyPatch) -> None:
    _gate(db, "nexus-ps", sha="aaa1111")
    reaped = _gate(db, "nexus-ps", sha="bbb2222")
    _phase3("nexus-ps")
    db.delete(project=REPO, title=reaped)
    _lossy_after_write(monkeypatch, on_write=1, change=lambda rec: rec.pop("state", None))     # the carried state is lost
    out = _phase3("nexus-ps", expect=1)
    assert "did not read back from T3 with the same list" in out


def test_the_output_counts_the_rows_examined_and_the_gate_rows_seen_so_zero_listed_is_not_vacuous(db: T2Database) -> None:
    _gate(db, "nexus-ex", sha="aaa1111")
    _gate(db, "nexus-ex", sha="bbb2222")
    _put(db, "a-plain-note", "not a gate record", tags="notes")
    out = _phase3("nexus-ex", "--dry-run")
    assert f"examined 3 row(s) of {REPO}, 2 with the tag kind:gate" in out and "2 listed" in out
    out = _phase3("nexus-wrong-bead", "--dry-run")                       # a wrong --beads: nothing listed, but visibly so
    assert "0 listed" in out and "examined 4 row(s)" in out and "3 with the tag kind:gate" not in out
    assert "none of the 2 kind:gate rows is in this run" in out and "--beads" in out and "--project" in out
    out = _run_raw("--phase", "3", "--beads", "nexus-ex", "--dry-run")
    assert "2 with the tag kind:gate" in out                              # the count row is not a gate row
