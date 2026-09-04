# SPDX-License-Identifier: AGPL-3.0-or-later
"""Contract tests for the per-commit automated review (nexus-jh86x).

The dispatch is INJECTED at every seam here. That is deliberate and it is
the RDR-201 post-mortem's own lesson applied: a rule proven below the
layer that uses it is not proven, so the tests that matter feed
:func:`nexus.commit_review.review_commit` the shape ``claude_dispatch``
actually returns, not a literal that resembles it.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from nexus.commands.review_cmd import reviews_census
from nexus.commit_review import (
    REVIEW_PROJECT,
    VERDICTS,
    CommitReviewError,
    Finding,
    ReviewResult,
    build_prompt,
    commit_diff,
    commit_parent_count,
    has_patch,
    parse_findings,
    parse_record_verdicts,
    record_title,
    render_record,
    review_commit,
)
from nexus.config import (
    COMMIT_REVIEW_DEFAULT_MODEL,
    CommitReviewConfig,
    get_commit_review_config,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


def _run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(
        cmd, cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """A real git repo with one real commit. Integration over mocks."""
    repo = tmp_path / "tiny"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@example.invalid"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "a.py").write_text("def f():\n    return 1\n")
    _run(["git", "add", "a.py"], repo)
    _run(["git", "commit", "-q", "-m", "feat: add f"], repo)
    return repo


# ── the verdict vocabulary ────────────────────────────────────────────────────


def test_verdict_vocabulary_is_the_three_declared_values() -> None:
    """FIX-NOW / FILE / DROP, and nothing else."""
    assert VERDICTS == ("FIX-NOW", "FILE", "DROP")


def test_parse_findings_rejects_an_out_of_vocabulary_verdict() -> None:
    """A verdict outside the closed set is a defect, not a new case.

    The json_schema is the enforcement at the dispatch boundary; this is
    the belt to that suspenders, because a schema-conformant model can
    still be swapped for one that is not.
    """
    with pytest.raises(CommitReviewError, match="verdict"):
        parse_findings({"findings": [{"verdict": "MAYBE", "summary": "s", "reason": "r"}]})


def test_parse_findings_accepts_every_declared_verdict() -> None:
    payload = {
        "findings": [
            {"verdict": v, "summary": f"s{i}", "reason": f"r{i}"}
            for i, v in enumerate(VERDICTS)
        ]
    }
    findings = parse_findings(payload)
    assert [f.verdict for f in findings] == list(VERDICTS)


def test_parse_findings_accepts_an_empty_review() -> None:
    """A clean commit is the common case and must not be an error."""
    assert parse_findings({"findings": []}) == []


def test_parse_findings_rejects_a_missing_findings_key() -> None:
    with pytest.raises(CommitReviewError):
        parse_findings({})


# ── the diff the reviewer actually sees ───────────────────────────────────────


def test_commit_diff_reads_a_real_commit(tiny_repo: Path) -> None:
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()
    text, truncated, _total = commit_diff(tiny_repo, sha, max_bytes=100_000)
    assert "def f()" in text
    assert "feat: add f" in text
    assert truncated is False


def test_commit_diff_truncates_and_says_so(tiny_repo: Path) -> None:
    """Truncation is reported, never silent.

    A silently truncated diff would let the reviewer report a clean
    verdict over code it never saw.
    """
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()
    text, truncated, total = commit_diff(tiny_repo, sha, max_bytes=40)
    assert truncated is True
    assert len(text) <= 40
    assert total > 40, "the untruncated size must survive so the record can state the ratio"
    rendered = render_record(
        sha=sha, subject="s", findings=[], cost_usd=None,
        truncated=True, seen_bytes=len(text), total_bytes=total,
    )
    assert f"Reviewed {len(text):,} of {total:,} characters" in rendered


def test_commit_diff_of_a_merge_shows_everything_the_merge_brought(tiny_repo: Path) -> None:
    """A bare ``git show`` on a merge emits the combined diff, which hides
    every file that matches either parent. On the v7.29.0 back-merge that
    was 11 of 13 files, the whole version surface, reviewed as clean
    (reanalysis 2026-09-04). The first-parent diff must carry the file
    the merge brought in from the other branch.
    """
    _run(["git", "checkout", "-q", "-b", "side"], tiny_repo)
    (tiny_repo / "version.py").write_text('VERSION = "2"\n')
    _run(["git", "add", "version.py"], tiny_repo)
    _run(["git", "commit", "-q", "-m", "chore: bump"], tiny_repo)
    _run(["git", "checkout", "-q", "main"], tiny_repo)
    (tiny_repo / "b.py").write_text("x = 1\n")
    _run(["git", "add", "b.py"], tiny_repo)
    _run(["git", "commit", "-q", "-m", "feat: b"], tiny_repo)
    _run(["git", "merge", "-q", "--no-ff", "-m", "merge side", "side"], tiny_repo)
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()
    assert commit_parent_count(tiny_repo, sha) == 2
    text, _, _total = commit_diff(tiny_repo, sha, max_bytes=100_000)
    assert 'VERSION = "2"' in text, "the file the merge brought in must be in the diff the reviewer sees"
    assert "version.py" in text
    assert commit_parent_count(tiny_repo, "HEAD~1") == 1


def test_build_prompt_names_a_merge_commit() -> None:
    assert "MERGE commit" in build_prompt("a" * 40, "diff", truncated=False, merge=True)
    assert "MERGE commit" not in build_prompt("a" * 40, "diff", truncated=False)


def test_the_record_says_when_it_was_a_first_parent_view() -> None:
    """A reader of T2 [24274] could not tell whether "No findings" covered
    2 files or 13 (critique [24283] S3). The record carries the view."""
    rendered = render_record(sha="a" * 40, subject="merge", findings=[], cost_usd=0.01, merge=True)
    assert "MERGE commit, first-parent view" in rendered
    assert "No findings." in rendered
    plain = render_record(sha="a" * 40, subject="s", findings=[], cost_usd=0.01)
    assert "MERGE" not in plain
    assert parse_record_verdicts(rendered) == {}


def test_a_tree_less_commit_is_skipped_not_reviewed_clean(tiny_repo: Path) -> None:
    """--allow-empty and ``merge -s ours`` produce a header-only diff. The
    old ``.strip()`` guard never fired because the --format header is
    always there, so the reviewer recorded "No findings" over a diff with
    no files in it (code review [24285] Major 2)."""
    _run(["git", "commit", "-q", "--allow-empty", "-m", "chore: empty"], tiny_repo)
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()
    text, _, _total = commit_diff(tiny_repo, sha, max_bytes=100_000)
    assert text.strip(), "the header is always present; that is the whole point"
    assert has_patch(text) is False
    full, _, _total = commit_diff(tiny_repo, "HEAD~1", max_bytes=100_000)
    assert has_patch(full) is True


def test_commit_diff_on_an_unknown_sha_raises(tiny_repo: Path) -> None:
    with pytest.raises(CommitReviewError):
        commit_diff(tiny_repo, "0" * 40, max_bytes=1000)


def test_the_commit_body_never_reaches_the_reviewer(tmp_path: Path) -> None:
    """ANTI-LEAK: the diff carries the subject, never the body.

    Raised by the substantive critique as a load-bearing property that was
    shipped untested. Commit bodies in this repo routinely cite bead ids,
    RDR numbers and design rationale -- exactly the decision-record context
    the reviewer is supposed to be blind to. `git show --format=%H%n%s%n%an`
    selects the subject (%s) and not the body (%b); a well-meant edit to
    %B or an added %b would silently pipe the design record into a child
    whose whole value is not having seen it.
    """
    repo = tmp_path / "bodyleak"
    repo.mkdir()
    _run(["git", "init", "-q", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@example.invalid"], repo)
    _run(["git", "config", "user.name", "Test"], repo)
    (repo / "x.py").write_text("x = 1\n")
    _run(["git", "add", "x.py"], repo)
    _run(
        [
            "git", "commit", "-q",
            "-m", "feat: subject line is fine",
            "-m", "SECRET_BODY_MARKER: per RDR-201 and bead nexus-j9z30 the design says X",
        ],
        repo,
    )
    sha = _run(["git", "rev-parse", "HEAD"], repo).strip()
    text, _, _total = commit_diff(repo, sha, max_bytes=100_000)

    assert "subject line is fine" in text, "the subject should be present"
    assert "SECRET_BODY_MARKER" not in text, (
        "the commit BODY leaked into the reviewer's prompt; it carries bead "
        "and RDR references the reviewer must not see"
    )


def test_build_prompt_marks_a_truncated_diff(tiny_repo: Path) -> None:
    full = build_prompt("abc1234", "diff --git a b", truncated=False)
    cut = build_prompt("abc1234", "diff --git a b", truncated=True)
    assert "truncated" not in full.lower()
    assert "truncated" in cut.lower()


# ── the record ────────────────────────────────────────────────────────────────


def test_record_title_is_stable_and_short() -> None:
    sha = "bd221c69b0f1e2a3d4c5b6a7988776655443322110"
    assert record_title(sha) == "review-bd221c69b0f1"
    assert record_title(sha) == record_title(sha)


def test_render_record_names_every_verdict_it_carries() -> None:
    findings = [
        Finding(verdict="FIX-NOW", summary="off-by-one", reason="loop bound", file="a.py"),
        Finding(verdict="DROP", summary="style", reason="not worth it", file=""),
    ]
    text = render_record(
        sha="abc1234def56", subject="feat: add f", findings=findings, cost_usd=0.01
    )
    assert "FIX-NOW" in text
    assert "DROP" in text
    assert "off-by-one" in text
    assert "abc1234def56" in text
    assert "feat: add f" in text


def test_render_record_of_a_clean_commit_says_so_explicitly() -> None:
    """Zero findings is a reported outcome, not an empty record.

    The nexus-moht0 vacuous-gate doctrine: a review that examined a
    commit and found nothing must be distinguishable from a review that
    never ran.
    """
    text = render_record(sha="abc1234def56", subject="chore: noop", findings=[], cost_usd=0.0)
    assert "abc1234def56" in text
    assert "no findings" in text.lower()


# ── config: env var + config key (Sam's ruling, 2026-09-02) ───────────────────


def test_config_defaults_to_the_ruled_cap() -> None:
    cfg = get_commit_review_config(cfg={})
    assert cfg.enabled is True
    assert cfg.max_budget_usd == 0.25


def test_the_default_model_is_the_strong_tier_alias() -> None:
    """Pin the literal to the tier table it was copied from.

    STRONG, not cheap. model_tiers splits cheap for the mechanical and
    structural operators and strong for the ones that synthesize or judge,
    pinning aggregate/summarize to strong for want of a quality proxy. A
    code reviewer judges, and no quality proxy exists for "did it find the
    real defect", so strong is what the doctrine already implies. An
    earlier version defaulted to cheap on one planted defect that both
    tiers caught -- an N=1 result standing in for the pre-registered
    multi-pair A/B the tier doctrine actually requires.

    ``config.py`` cannot import ``model_tiers`` (repo-wide two-consumer
    guard whose env-gating assertions do not fit this call), so the
    coupling is held here, where importing it is free.
    """
    from nexus.operators.model_tiers import resolve_model_for_tier

    assert COMMIT_REVIEW_DEFAULT_MODEL == resolve_model_for_tier("strong")


def test_the_reviewer_never_defaults_to_the_inherited_model() -> None:
    """model=None inherits the parent's model, which aborts on the cap.

    Measured 2026-09-02: an isolated child costs ~26k cache-creation
    tokens before emitting anything, ~$0.49 at Opus rates, against a
    $0.25 cap. A None default would ship a feature that always aborts.
    """
    assert get_commit_review_config(cfg={}).model is not None


def test_config_key_disables(tmp_path: Path) -> None:
    cfg = get_commit_review_config(cfg={"commit_review": {"enabled": False}})
    assert cfg.enabled is False


def test_env_var_overrides_the_config_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env beats config, so a noisy afternoon needs no file edit."""
    monkeypatch.setenv("NX_COMMIT_REVIEW", "0")
    cfg = get_commit_review_config(cfg={"commit_review": {"enabled": True}})
    assert cfg.enabled is False


def test_env_var_can_also_enable_over_a_disabling_config_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_COMMIT_REVIEW", "1")
    cfg = get_commit_review_config(cfg={"commit_review": {"enabled": False}})
    assert cfg.enabled is True


def test_malformed_budget_falls_back_to_the_default_loudly() -> None:
    cfg = get_commit_review_config(cfg={"commit_review": {"max_budget_usd": "banana"}})
    assert cfg.max_budget_usd == 0.25


# ── orchestration: fed the shape claude_dispatch really returns ───────────────


def test_review_commit_writes_one_record_with_typed_findings(tiny_repo: Path) -> None:
    """The bead's first acceptance criterion, end to end with a fake dispatch."""
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()
    written: list[dict] = []

    async def fake_dispatch(prompt, json_schema, **kwargs):
        assert "def f()" in prompt, "the reviewer must actually see the diff"
        assert kwargs["max_budget_usd"] == 0.25
        assert kwargs.get("allowed_tools") is None, "the child must stay tool-free"
        # isolated=True is a CORRECTNESS fix, not a cost one: without it
        # the child boots the caller's SessionStart hooks and sees the bead
        # board it is supposed to be blind to. Untested, a regression
        # dropping it would pass every other assertion here.
        assert kwargs["isolated"] is True, "the reviewer's child must be hermetic"
        return {
            "findings": [
                {"verdict": "FIX-NOW", "summary": "no docstring", "reason": "house style"}
            ]
        }

    def fake_put(**kwargs):
        written.append(kwargs)
        return 4242

    result = asyncio.run(
        review_commit(
            repo=tiny_repo,
            sha=sha,
            cfg=CommitReviewConfig(),
            dispatch=fake_dispatch,
            put=fake_put,
        )
    )

    assert isinstance(result, ReviewResult)
    assert result.row_id == 4242
    assert [f.verdict for f in result.findings] == ["FIX-NOW"]
    assert len(written) == 1
    assert written[0]["title"] == record_title(sha)
    assert "no docstring" in written[0]["content"]
    assert written[0]["ttl"] == 90, "review records age out; they are not permanent"


def test_the_recorded_cost_comes_from_the_real_usage_field(tiny_repo: Path) -> None:
    """Feed the recorder a REAL DispatchUsage, not a stand-in.

    The first version of this code read ``total_cost_usd`` -- the WIRE
    name -- via getattr with a None default, so every record shipped with
    no cost line and nothing failed. A fake object with whatever
    attribute the code happens to ask for would have passed that too.
    """
    from nexus.operators.dispatch import DispatchUsage

    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()
    written: list[dict] = []

    async def usage_writing_dispatch(prompt, json_schema, **kwargs):
        kwargs["usage_sink"].append(
            DispatchUsage(
                model="claude-haiku-4-5",
                cost_usd=0.0123,
                input_tokens=100,
                output_tokens=10,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
                duration_ms=900,
                duration_api_ms=800,
                num_turns=1,
                model_usage=None,
            )
        )
        return {"findings": []}

    asyncio.run(
        review_commit(
            repo=tiny_repo,
            sha=sha,
            cfg=CommitReviewConfig(),
            dispatch=usage_writing_dispatch,
            put=lambda **k: written.append(k) or 1,
        )
    )
    assert "0.0123" in written[0]["content"]


def test_review_commit_is_a_noop_when_disabled(tiny_repo: Path) -> None:
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()

    async def exploding_dispatch(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("dispatch ran while disabled")

    result = asyncio.run(
        review_commit(
            repo=tiny_repo,
            sha=sha,
            cfg=CommitReviewConfig(enabled=False),
            dispatch=exploding_dispatch,
            put=lambda **k: 0,
        )
    )
    assert result.skipped_reason == "disabled"
    assert result.row_id is None


def test_a_dispatch_failure_never_raises_out_of_review_commit(tiny_repo: Path) -> None:
    """A hook failure must never block a commit.

    This is the bead's second acceptance criterion, proven at the layer
    the hook calls rather than at the shell layer above it.
    """
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()

    async def failing_dispatch(*a, **k):
        raise RuntimeError("budget exceeded")

    result = asyncio.run(
        review_commit(
            repo=tiny_repo,
            sha=sha,
            cfg=CommitReviewConfig(),
            dispatch=failing_dispatch,
            put=lambda **k: 0,
        )
    )
    assert result.skipped_reason is not None
    assert "budget exceeded" in result.skipped_reason
    assert result.row_id is None


def test_a_t2_write_failure_never_raises_either(tiny_repo: Path) -> None:
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()

    async def ok_dispatch(*a, **k):
        return {"findings": []}

    def failing_put(**kwargs):
        raise RuntimeError("T2 unreachable")

    result = asyncio.run(
        review_commit(
            repo=tiny_repo, sha=sha, cfg=CommitReviewConfig(),
            dispatch=ok_dispatch, put=failing_put,
        )
    )
    assert result.skipped_reason is not None
    assert "T2 unreachable" in result.skipped_reason


# ── the producer/consumer seam (RDR-201 post-mortem's own lesson) ─────────────


def test_the_census_parser_is_fed_the_renderers_own_output() -> None:
    """Composition, not a literal that resembles the producer's output.

    The RDR-201 post-mortem found this exact shape four times: a test
    existed, passed, and asked a narrower question than the production
    caller asks. So this feeds parse_record_verdicts what render_record
    actually emits, rather than a hand-written 'Verdicts: ...' line.
    """
    findings = [
        Finding(verdict="FIX-NOW", summary="a", reason="r"),
        Finding(verdict="FIX-NOW", summary="b", reason="r"),
        Finding(verdict="DROP", summary="c", reason="r"),
    ]
    rendered = render_record(
        sha="abc1234def56", subject="feat: x", findings=findings, cost_usd=0.02
    )
    assert parse_record_verdicts(rendered) == {"FIX-NOW": 2, "DROP": 1}


def test_a_clean_record_round_trips_to_no_verdicts() -> None:
    rendered = render_record(
        sha="abc1234def56", subject="chore: noop", findings=[], cost_usd=0.0
    )
    assert parse_record_verdicts(rendered) == {}


def test_every_verdict_in_the_vocabulary_survives_the_round_trip() -> None:
    """Non-vacuity: the round trip must cover the whole closed set.

    A parser proven on one verdict would pass while silently dropping the
    other two.
    """
    findings = [Finding(verdict=v, summary=v, reason="r") for v in VERDICTS]
    rendered = render_record(
        sha="abc1234def56", subject="s", findings=findings, cost_usd=None
    )
    assert parse_record_verdicts(rendered) == dict.fromkeys(VERDICTS, 1)


def test_census_counts_across_records_built_by_the_renderer() -> None:
    """The census consumes stored records, so build them with the renderer.

    Rows carry real titles because the records share the ``nexus`` project
    with thousands of unrelated entries (Sam's ruling, 2026-09-02) and the
    census selects them by title prefix. A fixture without titles would
    prove the counter and skip the filter that stands in front of it.
    """

    def _row(sha: str, subject: str, findings: list[Finding]) -> dict:
        return {
            "title": record_title(sha),
            "content": render_record(
                sha=sha, subject=subject, findings=findings, cost_usd=0.01
            ),
        }

    class FakeMemory:
        def get_all(self, project):
            assert project == REVIEW_PROJECT
            return [
                _row("a" * 40, "one", [Finding("FIX-NOW", "s", "r")]),
                _row("b" * 40, "two", [Finding("DROP", "s", "r"), Finding("FILE", "s", "r")]),
                _row("c" * 40, "clean", []),
                # A NEIGHBOUR in the same project that is not a review. The
                # prefix filter must drop it; counted, it would read as a
                # commit that was reviewed and found clean.
                {"title": "continuation-state.md", "content": "unrelated note"},
                # The neighbours that DID get counted (2026-09-04): 401
                # human and agent review notes whose titles start with the
                # prefix. The record's first line is what distinguishes a
                # commit review from a note about a review.
                {"title": "review-completed", "content": "Reviewed nexus-x; clean."},
                {"title": "review-range-abc123def456", "content": "Range review of ...\nVerdicts: FIX-NOW=3"},
            ]

    class FakeDB:
        memory = FakeMemory()

    totals = reviews_census(FakeDB())
    assert totals["FIX-NOW"] == 1
    assert totals["FILE"] == 1
    assert totals["DROP"] == 1
    assert totals["_records"] == 3, "the non-review neighbours must not be counted"
    assert totals["_clean"] == 1


def test_an_out_of_vocabulary_verdict_from_the_model_is_not_written(
    tiny_repo: Path,
) -> None:
    """A malformed review is dropped, not persisted as a half-record."""
    sha = _run(["git", "rev-parse", "HEAD"], tiny_repo).strip()
    written: list[dict] = []

    async def rogue_dispatch(*a, **k):
        return {"findings": [{"verdict": "ESCALATE", "summary": "s", "reason": "r"}]}

    result = asyncio.run(
        review_commit(
            repo=tiny_repo, sha=sha, cfg=CommitReviewConfig(),
            dispatch=rogue_dispatch, put=lambda **k: written.append(k) or 1,
        )
    )
    assert result.row_id is None
    assert written == []


# ── SessionStart delivery (Sam's ruling, 2026-09-02) ──────────────────────────


class TestSessionStartNotice:
    """FIX-NOW findings must be pushed at a human, not merely stored.

    Without this the reviewer was theatre: a verdict meaning "fix before
    this work goes further" landed in T2 and a log file, and nothing
    surfaced it until somebody thought to look.
    """

    @staticmethod
    def _rows(*specs) -> list[dict]:
        from datetime import datetime, timedelta, timezone

        out = []
        for sha, verdict, age_days in specs:
            findings = [Finding(verdict, "s", "r")] if verdict else []
            when = datetime.now(timezone.utc) - timedelta(days=age_days)
            out.append({
                "title": record_title(sha),
                "timestamp": when.isoformat(),
                "content": render_record(
                    sha=sha, subject="s", findings=findings, cost_usd=0.0
                ),
            })
        return out

    def _notice(self, monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> str:
        import contextlib

        from nexus import hooks as hooks_mod

        class FakeMemory:
            def get_all(self, project):
                return rows

        class FakeDB:
            memory = FakeMemory()

        @contextlib.contextmanager
        def fake_handle():
            yield FakeDB()

        monkeypatch.setattr("nexus.commands._helpers.t2_handle", fake_handle)
        return hooks_mod._pending_fix_now_notice()

    def test_reports_recent_fix_now_commits(self, monkeypatch) -> None:
        notice = self._notice(monkeypatch, self._rows(("a" * 40, "FIX-NOW", 1)))
        assert "FIX-NOW" in notice
        assert "1 commit" in notice

    def test_counts_commits_not_findings(self, monkeypatch) -> None:
        """Two FIX-NOWs on one commit is ONE thing to go and look at."""
        from datetime import datetime, timedelta, timezone

        when = datetime.now(timezone.utc) - timedelta(days=1)
        rows = [{
            "title": record_title("a" * 40),
            "timestamp": when.isoformat(),
            "content": render_record(
                sha="a" * 40, subject="s",
                findings=[Finding("FIX-NOW", "x", "r"), Finding("FIX-NOW", "y", "r")],
                cost_usd=0.0,
            ),
        }]
        assert "1 commit" in self._notice(monkeypatch, rows)

    def test_silent_when_nothing_is_outstanding(self, monkeypatch) -> None:
        assert self._notice(monkeypatch, self._rows(("a" * 40, None, 1))) == ""

    def test_silent_when_there_are_no_records_at_all(self, monkeypatch) -> None:
        assert self._notice(monkeypatch, []) == ""

    def test_old_findings_roll_off(self, monkeypatch) -> None:
        """Bounded on purpose: an unbounded nag becomes the thing people
        learn to scroll past, which is the failure this notice prevents."""
        assert self._notice(monkeypatch, self._rows(("a" * 40, "FIX-NOW", 30))) == ""

    def test_other_verdicts_do_not_nag(self, monkeypatch) -> None:
        rows = self._rows(("a" * 40, "DROP", 1), ("b" * 40, "FILE", 1))
        assert self._notice(monkeypatch, rows) == ""

    def test_non_review_entries_in_the_project_are_ignored(self, monkeypatch) -> None:
        """The records share the nexus project with thousands of notes."""
        rows = [
            {"title": "continuation-state.md", "content": "Verdicts: FIX-NOW=9"},
            # The prefix-only selector counted these (critique [24283] S1):
            # a review NOTE about a range, carrying its own Verdicts: line.
            {"title": "review-range-abc123def456", "content": "Range review.\nVerdicts: FIX-NOW=3"},
        ]
        assert self._notice(monkeypatch, rows) == ""

    def test_a_t2_failure_never_breaks_session_start(self, monkeypatch) -> None:
        import contextlib

        from nexus import hooks as hooks_mod

        @contextlib.contextmanager
        def exploding_handle():
            raise RuntimeError("T2 unreachable")
            yield  # pragma: no cover

        monkeypatch.setattr("nexus.commands._helpers.t2_handle", exploding_handle)
        assert hooks_mod._pending_fix_now_notice() == ""
