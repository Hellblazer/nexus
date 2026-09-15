# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-207 Phase 3 (beads nexus-l3yuc.15 and .16): ``nx memory rollup``.

Every test uses a STUBBED summarizer; no live LLM call. Rows are quarantined
the way production does it (a TTL, a backdated timestamp, then expire) on the
engine substrate (the ``db`` fixture: one freshly minted tenant per test).
Rows backdated by 2, 40 and 80 days always land in three different months,
because each gap is longer than any month.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2 import T2Database
from nexus.memory_rollup import (
    PRODUCED_BY,
    RollupGroup,
    dispatch_summarizer,
    group_prompt_content,
    missing_titles,
    plan_groups,
    rollup,
)
from tests._t2_fixture_ops import backdate_memory

PROJECT = "rollup-proj"


class StubSummarizer:
    """Names every title unless told to omit one; raises on chosen calls."""

    def __init__(self, *, omit_title: bool = False, raise_on: tuple[int, ...] = ()) -> None:
        self.calls: list[RollupGroup] = []
        self._omit_title = omit_title
        self._raise_on = raise_on

    def __call__(self, group: RollupGroup) -> str:
        self.calls.append(group)
        if len(self.calls) in self._raise_on:
            raise RuntimeError(f"stub dispatch failure on call {len(self.calls)}")
        titles = group.titles[1:] if self._omit_title else group.titles
        return f"Summary of {group.month}: " + "; ".join(titles)


def _quarantine(db: T2Database, title: str, *, days: int) -> int:
    row_id = db.put(project=PROJECT, title=title, content=f"body of {title}", ttl=1)
    backdate_memory(db, PROJECT, title, days=days)
    assert row_id in db.memory.expire().quarantined_ids
    return row_id


def _marked(db: T2Database) -> set[int]:
    return {r["id"] for r in db.memory.list_quarantined(project=PROJECT) if r["rolled_up_at"]}


def _run(db: T2Database, stub: StubSummarizer, *, dry_run: bool = False):
    return rollup(db.memory, PROJECT, stub, model="stub-model", dry_run=dry_run)


# ── the two forced-failure legs (RDR-207 failure modes 5 and 6) ─────────────


def test_a_summary_that_omits_a_title_marks_nothing_and_reap_deletes_nothing(
    db: T2Database,
) -> None:
    ids = [_quarantine(db, "alpha.md", days=2), _quarantine(db, "beta.md", days=2)]
    stub = StubSummarizer(omit_title=True)

    [outcome] = _run(db, stub)

    assert len(stub.calls) == 1, "non-vacuity: the check can only refuse a summary it was given"
    assert outcome.status == "check_failed"
    assert outcome.missing_titles == ("alpha.md",)
    assert db.memory.list_summaries(project=PROJECT) == [], "no summary row"
    assert _marked(db) == set(), "no mark"
    assert db.memory.reap() == [], "a following reap deletes nothing"
    assert {r["id"] for r in db.memory.list_quarantined(project=PROJECT)} == set(ids)


def test_a_dispatch_failure_on_group_2_of_3_leaves_groups_1_and_3_marked(db: T2Database) -> None:
    oldest = _quarantine(db, "oldest.md", days=80)
    middle = _quarantine(db, "middle.md", days=40)
    newest = _quarantine(db, "newest.md", days=2)
    stub = StubSummarizer(raise_on=(2,))

    outcomes = _run(db, stub)

    assert len(stub.calls) == 3, "the groups after the failure still run"
    assert [o.status for o in outcomes] == ["marked", "dispatch_failed", "marked"]
    assert "stub dispatch failure on call 2" in (outcomes[1].error or "")
    assert _marked(db) == {oldest, newest}, "group 2 is still quarantined and unmarked"
    summaries = db.memory.list_summaries(project=PROJECT)
    assert sorted(s["source_ids"] for s in summaries) == [[oldest], [newest]]
    assert {s["produced_by"] for s in summaries} == {PRODUCED_BY}
    assert middle in {r["id"] for r in db.memory.list_quarantined(project=PROJECT)}


# ── dry run, grouping, and the check itself ─────────────────────────────────


def test_dry_run_writes_nothing(db: T2Database) -> None:
    _quarantine(db, "one.md", days=2)
    _quarantine(db, "two.md", days=40)
    stub = StubSummarizer()

    outcomes = _run(db, stub, dry_run=True)

    assert len(stub.calls) == 2, "the summarizer and the check still run"
    assert [o.status for o in outcomes] == ["dry_run", "dry_run"]
    assert db.memory.list_summaries(project=PROJECT) == []
    assert _marked(db) == set()


def test_rows_from_two_months_make_two_groups(db: T2Database) -> None:
    first = _quarantine(db, "first.md", days=40)
    second = _quarantine(db, "second.md", days=2)

    groups = plan_groups(db.memory.list_quarantined(project=PROJECT))

    assert len(groups) == 2
    assert [g.source_ids for g in groups] == [[first], [second]], "oldest month first"
    assert groups[0].month < groups[1].month


def test_plan_groups_skips_rows_already_marked() -> None:
    rows = [
        {"id": 1, "title": "a", "timestamp": "2026-08-03T00:00:00Z", "rolled_up_at": None},
        {"id": 2, "title": "b", "timestamp": "2026-08-20T00:00:00Z", "rolled_up_at": "2026-09-01T00:00:00Z"},
        {"id": 3, "title": "c", "timestamp": "2026-09-01T00:00:00Z", "rolled_up_at": None},
    ]
    groups = plan_groups(rows)
    assert [(g.month, g.source_ids) for g in groups] == [("2026-08", [1]), ("2026-09", [3])]


@pytest.mark.parametrize(
    "summary,expected",
    [
        ("covers a.md and b.md", []),
        ("covers a.md only", ["b.md"]),
        ("covers nothing", ["a.md", "b.md"]),
    ],
    ids=["all-named", "one-missing", "none-named"],
)
def test_missing_titles_is_a_substring_floor(summary: str, expected: list[str]) -> None:
    group = RollupGroup(month="2026-09", rows=({"id": 1, "title": "a.md"}, {"id": 2, "title": "b.md"}))
    assert missing_titles(summary, group) == expected


# ── the real summarizer reuses operator_summarize's dispatch ────────────────


def test_dispatch_summarizer_uses_the_summarize_operator_dispatch(monkeypatch) -> None:
    seen: dict = {}

    async def _fake_dispatch(prompt, schema, timeout=300.0, **kwargs):
        seen.update(prompt=prompt, schema=schema, timeout=timeout, **kwargs)
        return {"summary": "the summary"}

    monkeypatch.setattr("nexus.operators.dispatch.claude_dispatch", _fake_dispatch)
    group = RollupGroup(month="2026-09", rows=({"id": 7, "title": "t.md", "content": "body"},))

    assert dispatch_summarizer(model="m", timeout=12.0)(group) == "the summary"
    assert seen["operator"] == "operator_summarize" and seen["model"] == "m"
    assert seen["timeout"] == 12.0 and seen["schema"]["required"] == ["summary"]
    assert group_prompt_content(group) in seen["prompt"]
    assert "## t.md\nbody" in seen["prompt"]


# ── the CLI verb ────────────────────────────────────────────────────────────


def _t2_cm(db: object) -> MagicMock:
    return MagicMock(__enter__=MagicMock(return_value=db), __exit__=MagicMock(return_value=False))


def _nx_rollup(db: T2Database, stub: StubSummarizer, *args: str):
    with (
        patch("nexus.commands.memory.t2_handle", return_value=_t2_cm(db)),
        patch("nexus.memory_rollup.dispatch_summarizer", return_value=stub),
    ):
        return CliRunner().invoke(main, ["memory", "rollup", "--project", PROJECT, *args])


def test_rollup_cmd_prints_the_groups_then_marks(db: T2Database) -> None:
    row_id = _quarantine(db, "cli.md", days=2)
    out = _nx_rollup(db, StubSummarizer())
    assert out.exit_code == 0, out.output
    assert out.output.index("1 group (1 entry)") < out.output.index("marked 1 entry")
    assert _marked(db) == {row_id}


def test_rollup_cmd_exits_nonzero_when_a_group_is_not_marked(db: T2Database) -> None:
    _quarantine(db, "a.md", days=2)
    _quarantine(db, "b.md", days=2)
    out = _nx_rollup(db, StubSummarizer(omit_title=True))
    assert out.exit_code == 1, out.output
    assert "NOT marked, the summary omits a.md" in out.output
    assert "1 of 1 group not marked" in out.output


def test_rollup_cmd_dry_run_prints_the_summary_and_writes_nothing(db: T2Database) -> None:
    _quarantine(db, "dry.md", days=2)
    out = _nx_rollup(db, StubSummarizer(), "--dry-run")
    assert out.exit_code == 0, out.output
    assert "would mark 1 entry" in out.output and "dry.md" in out.output
    assert _marked(db) == set() and db.memory.list_summaries(project=PROJECT) == []


def test_rollup_cmd_with_nothing_quarantined(db: T2Database) -> None:
    stub = StubSummarizer()
    out = _nx_rollup(db, stub)
    assert out.exit_code == 0 and "Nothing to roll up" in out.output
    assert stub.calls == []
