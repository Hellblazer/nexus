# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure logic of ``nx memory gate-rollup`` (RDR-209 Phase 3): the title predicate, the byte splitter, the pointer
and archive titles, the selection over given rows, the verdict-chain order and the engine's expiry rule with a fixed
clock. No test here needs an engine or a tenant; the command's tests are ``tests/integration/test_memory_gate_rollup.py``
and ``test_memory_gate_ttl.py``."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from nexus.memory_curate import gate_rollup as gr
from nexus.memory_curate import gate_ttl as gt
from tests._gate_rollup_support import RDR, REPO

POINTER_P3 = "gate-records-rdr-209-phase-3"


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


def test_split_text_keeps_every_character_within_the_byte_cap() -> None:
    text = "é" * 9000 + "\n" + "x" * 30000 + "\nshort\n" + "日本語" * 4000
    parts = gr.split_text_bytes(text, 4096)
    assert "".join(parts) == text
    assert all(0 < len(p.encode()) <= 4096 for p in parts)
    assert gr.split_text_bytes("tiny", 4096) == ["tiny"]


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


def test_order_reads_a_round_in_the_middle_of_the_title() -> None:
    """Live titles put the round before the date (confirm-nexus-mdbds.11.1-r2-2026-11-15); the first live dry run
    (bead nexus-1gdv0.2.17, RDR-249) read both as round 0 and let the earlier partial review win the tie."""
    review = _rec("review-nexus-ord-r1-2026-11-15", "partial", date="2026-11-15")
    confirm = _rec("confirm-nexus-ord-r2-2026-11-15", "justified", date="2026-11-15")
    assert _chain(review, confirm) == ("justified", [confirm["title"]])
    assert _chain(confirm, review) == ("justified", [confirm["title"]])


def test_order_a_bead_id_with_r_and_digits_is_not_a_round() -> None:
    """``-r<N>`` counts only when a ``-`` or the end follows the digits, so ``nexus-r0tui`` is no round 0."""
    assert gt.order_key({"title": "review-nexus-r0tui-2026-11-15", "timestamp": ""}, "")[1] == 0
    assert gt.order_key({"title": "critique-abc-nexus-x-r3", "timestamp": ""}, "")[1] == 3
    assert gt.order_key({"title": "critique-abc-r2-nexus-x-r7-2026-11-15", "timestamp": ""}, "")[1] == 7


def test_order_same_day_and_round_the_later_timestamp_is_newer() -> None:
    """Dates are day-granular and the round is optional, but every row carries its write time: within one day and
    round, the later write is the newer record, and the worse verdict decides only an exact tie."""
    review = _rec("review-aaa1111-nexus-ord-2026-11-15", "partial", date="2026-11-15", stamp="2026-11-15T21:57:49Z")
    confirm = _rec("confirm-ccc3333-nexus-ord-2026-11-15", "justified", date="2026-11-15", stamp="2026-11-15T22:15:35Z")
    assert _chain(review, confirm) == ("justified", [confirm["title"]])
    late_partial = _rec("critique-ddd4444-nexus-ord-2026-11-15", "partial", date="2026-11-15", stamp="2026-11-15T23:00:00Z")
    assert _chain(confirm, late_partial) == ("partial", [late_partial["title"]])


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


NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


def _aged(*, days: float = 0, seconds: float = 0, access: int = 0, stamp: str | None = "x") -> dict:
    when = NOW - timedelta(days=days, seconds=seconds)
    return {"timestamp": when.strftime("%Y-%m-%dT%H:%M:%SZ") if stamp else None, "access_count": access}


def _dry_count(rows: list[dict]) -> int:
    entries = [{"title": f"t{i}"} for i in range(len(rows))]
    lines, past = gt.dry_run_lines(entries, {f"t{i}": r for i, r in enumerate(rows)}, NOW)
    assert lines[-1] == f"would be quarantined by the next expire sweep: {past} of {len(rows)} candidate record(s)"
    return past


def test_dry_run_heat_record_older_than_30_days_but_inside_its_effective_ttl_is_not_counted() -> None:
    # access_count 3: effective TTL 30 * (1 + ln 4) = 71.6 days, so 40 days old is not past it
    assert gt.effective_ttl_days(3) == pytest.approx(71.588, abs=0.001)
    assert _dry_count([_aged(days=40, access=3)]) == 0
    assert _dry_count([_aged(days=40, access=0)]) == 1                 # the same age with no heat is past 30 days
    assert _dry_count([_aged(days=72, access=3)]) == 1                 # and past the heat-lengthened TTL at 72
    assert _dry_count([_aged(days=71, access=3)]) == 0


def test_dry_run_boundary_age_equal_to_the_effective_ttl_is_not_counted_and_one_second_more_is() -> None:
    assert _dry_count([_aged(days=30, access=0)]) == 0                 # exactly 30.0 days: strictly greater is required
    assert _dry_count([_aged(days=30, seconds=1, access=0)]) == 1
    assert gt.would_expire(_aged(days=30), now=NOW) is False
    assert gt.would_expire(_aged(days=30, seconds=1), now=NOW) is True


def test_dry_run_boundary_the_age_is_whole_seconds_as_the_engine_truncates_it() -> None:
    # the engine divides Duration.toSeconds() by 86,400: 30 days and 0.9 s is still 30.0 days
    later = NOW + timedelta(milliseconds=900)
    assert gt.would_expire(_aged(days=30), now=later) is False
    assert gt.would_expire(_aged(days=30), now=later + timedelta(seconds=1)) is True


def test_dry_run_counts_only_what_is_past_across_a_mixed_set() -> None:
    rows = [_aged(days=10), _aged(days=31), _aged(days=40, access=3), _aged(days=200, access=3), _aged(days=29.99)]
    assert _dry_count(rows) == 2


def test_dry_run_a_row_with_no_timestamp_is_not_counted() -> None:
    """The engine skips a null timestamp (MemoryRepository.java:695), so a hand-made row with none is not counted."""
    assert _dry_count([_aged(days=400, stamp=None), {"timestamp": "", "access_count": 0}]) == 0
