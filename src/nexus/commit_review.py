# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-commit automated review (bead nexus-jh86x).

Reviews one commit with a tool-free ``claude -p`` over its diff and
records typed findings in T2. Nothing is auto-applied and nothing is
auto-filed: triage is a human act.

Shape, deliberately narrow:

- **Tool-free, and hermetic.** ``allowed_tools`` is never passed and the
  dispatch is ``isolated``, so the child sees the diff and nothing else --
  not the bead board, not the RDR corpus, not prior reviews. That
  independence is the whole value: a reviewer that has read the design
  record tends to agree with it. Per the T1 sub-agent contract in
  AGENTS.md a dispatch mints a T1 session only when it grants tool access
  (nexus-bjltu), so a tool-free child also leaves nothing behind.
- **Never blocks.** Every failure path returns a :class:`ReviewResult`
  carrying ``skipped_reason``; nothing here raises out to its caller once
  :func:`review_commit` is entered. A post-commit hook that can block is a
  footgun during a tag-push sequence, which has to land in tight
  succession.
- **Never edits.** Findings are recorded and reported, never applied.

Expect an instrument that mostly comments on test quality and
occasionally catches a design error, and treat that as the success case.
Nothing about its value here has been measured yet: as of 2026-09-02 the
observed yield is one finding on a deliberately planted defect in a
throwaway repo, which shows the pipeline works and says nothing about
real commits. If the FIX-NOW rate turns out to be dominated by noise, the
honest response is to narrow or retire this, not to habituate people to
ignoring a verdict whose whole meaning is "fix before the work goes
further".

The verdict vocabulary is FIX-NOW / FILE / DROP. It is deliberately NOT
an RDR-201 checked table: that checker proves COVERAGE and OVERLAP over
declared guard dimensions, and this is a flat three-value enum with no
guard dimensions at all, so a table would have nothing to prove about it.
Enforcement lives where the values enter the system -- the dispatch
``json_schema`` constrains the model's output and :func:`parse_findings`
re-checks it, because a schema-conformant model can be swapped for one
that is not. Reach for a table if these verdicts ever gain a guard (a
severity, a scope, an "applies only when"), since that is when coverage
becomes a real question.
"""
from __future__ import annotations

import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import structlog

from nexus.config import CommitReviewConfig

_log = structlog.get_logger(__name__)

#: A verdict outside this set is a defect rather than a new case.
VERDICTS: Final[tuple[str, ...]] = ("FIX-NOW", "FILE", "DROP")

#: T2 project for review records.
#:
#: Sam ruled the shared ``nexus`` project on 2026-09-02, over a dedicated
#: namespace this first shipped with. Consequence, handled rather than
#: ignored: the project carries thousands of entries, so nothing may
#: assume a whole-project read is all reviews. :func:`is_review_record`
#: is the one selector; it needs the title prefix AND the record's own
#: first line, because the prefix alone matched 401 human review notes.
REVIEW_PROJECT: Final = "nexus"

#: Title prefix for review records. Necessary, not sufficient: see
#: :func:`is_review_record`.
RECORD_PREFIX: Final = "review-"

#: First line of every record :func:`render_record` writes, and the only
#: thing the census may select on besides the title prefix. Human and
#: agent review notes in the same project also start ``review-``; none
#: of them starts with this line.
RECORD_MARKER: Final = "Commit review: "


def is_review_record(row: dict) -> bool:
    """True only for a record :func:`render_record` wrote.

    The title prefix alone is not a selector: on 2026-09-04 the shared
    project held 401 ``review-*`` titles (``review-range-<sha>``,
    ``review-completed``, per-bead reviewer notes) and not one commit
    review, and the census reported 401 commits reviewed and clean while
    the hook had never fired on the box. The record's own first line is
    the marker. Both consumers (the census and the SessionStart FIX-NOW
    notice) select through this one function; the first fix landed it in
    the census only and the notice kept the prefix (critique [24283] S1).
    """
    title = str(row.get("title", "") if isinstance(row, dict) else "")
    content = str((row.get("content", "") if isinstance(row, dict) else "") or "")
    return title.startswith(RECORD_PREFIX) and content.startswith(RECORD_MARKER)

#: Output contract for the dispatch. ``claude_dispatch`` passes this to
#: ``--json-schema``, so the enum is enforced at the boundary;
#: :func:`parse_findings` re-checks it because a schema-conformant model
#: can still be swapped for one that is not.
REVIEW_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "summary": {"type": "string"},
                    "reason": {"type": "string"},
                    "file": {"type": "string"},
                },
                "required": ["verdict", "summary", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}


class CommitReviewError(Exception):
    """A review could not be produced. Caught at the ``review_commit`` seam."""


@dataclass(frozen=True)
class Finding:
    """One triaged observation about one commit."""

    verdict: str
    summary: str
    reason: str
    file: str = ""


@dataclass(frozen=True)
class ReviewResult:
    """Outcome of one review attempt.

    ``skipped_reason`` is ``None`` only when a record was actually
    written. Every other outcome -- disabled, empty diff, dispatch
    failure, malformed output, T2 unreachable -- names itself here, so a
    caller can tell "reviewed and found nothing" from "never ran".
    """

    sha: str
    findings: list[Finding] = field(default_factory=list)
    row_id: int | None = None
    skipped_reason: str | None = None
    truncated: bool = False


def record_title(sha: str) -> str:
    """Stable T2 title for *sha*'s review record.

    Twelve hex is the same width ``git log --abbrev=12`` uses here and is
    collision-safe well past this repo's size, while staying short enough
    to read in a title listing.
    """
    return f"{RECORD_PREFIX}{sha[:12]}"


def commit_diff(repo: Path, sha: str, *, max_bytes: int) -> tuple[str, bool, int]:
    """Return ``(text, truncated, total_bytes)`` for *sha* in *repo*.

    ``total_bytes`` is the size of the untruncated ``git show`` output, so a
    record can say "reviewed 200,000 of 2,074,000 bytes" rather than a bare
    flag: 199 of 200 KB and 200 of 2,074 KB are different reviews and a
    boolean made them read the same (critique [24283] S2).

    Truncation is REPORTED, never silent: a quietly cut diff would let the
    reviewer return a clean verdict over code it never saw, which is the
    exact failure the RDR-201 post-mortem calls "a rule proven below the
    layer that uses it".

    ``-m --first-parent`` because a bare ``git show`` on a two-parent commit
    emits the combined ``--cc`` diff, which shows only hunks that differ
    from BOTH parents. Measured on the v7.29.0 back-merge (31b20c305,
    reanalysis 2026-09-04): 2 of 13 files carried a patch body, the eleven
    elided ones being the whole release version surface, and the reviewer
    recorded "No findings" over them. Back-merges are mandatory after every
    release, so the bare form fired on exactly the commits that matter.
    The first-parent diff is what the merge brought onto the branch.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "show",
                "--no-color",
                "-m",
                "--first-parent",
                "--stat",
                "--patch",
                "--format=%H%n%s%n%an%n",
                sha,
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise CommitReviewError(f"git show {sha[:12]} failed: {exc}") from exc

    text = proc.stdout
    if len(text) > max_bytes:
        return text[:max_bytes], True, len(text)
    return text, False, len(text)


def has_patch(diff_text: str) -> bool:
    """True when the ``git show`` output carries at least one file diff.

    The ``--format=%H%n%s%n%an%n`` header is always present, so emptiness
    must be judged on the body: a commit with no tree change (``--allow-
    empty``, ``merge -s ours``) has a header and no ``diff --git`` line.
    """
    return "\ndiff --git " in diff_text or diff_text.startswith("diff --git ")


def commit_parent_count(repo: Path, sha: str) -> int:
    """Number of parents of *sha*: 0 for a root commit, and 0 when git
    cannot answer (never raises). Callers only ask "more than one".
    """
    try:
        line = subprocess.run(
            ["git", "rev-list", "--parents", "-n", "1", sha],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, OSError):
        return 0
    return max(len(line) - 1, 0)


def commit_subject(repo: Path, sha: str) -> str:
    """Best-effort one-line subject for *sha*; empty string if unavailable."""
    try:
        return subprocess.run(
            ["git", "log", "-1", "--format=%s", sha],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


def build_prompt(sha: str, diff_text: str, *, truncated: bool, merge: bool = False) -> str:
    """Assemble the fixed reviewer prompt.

    Repo-sandboxed by construction: the child gets this text and no tools,
    so it cannot consult the RDR corpus, the bead board, or prior reviews.
    That is what keeps a finding independent evidence rather than an echo
    of the design record.
    """
    truncation_note = (
        "\nNOTE: this diff was TRUNCATED to fit a size cap. You are seeing a "
        "prefix of the change, not all of it. Do not report on what you "
        "cannot see, and do not claim the commit is clean.\n"
        if truncated
        else ""
    )
    merge_note = (
        "\nNOTE: this is a MERGE commit. The diff below is against its first "
        "parent only: everything the merge brought onto the branch, whether "
        "or not it also exists on the other parent. Review it as such.\n"
        if merge
        else ""
    )
    return f"""You are reviewing a single git commit. You can see only the diff below.

Report defects you can justify FROM THE DIFF ALONE. Do not speculate about
code you cannot see. An empty findings list is the correct and common answer
for a clean commit; do not invent findings to appear useful.

Assign each finding exactly one verdict:
  FIX-NOW  a defect that should be corrected before this work goes further
  FILE     a real issue worth tracking, but not urgent
  DROP     an observation you considered and are explicitly setting aside

For each finding give: verdict, a one-line summary, a reason grounded in the
diff, and the file it concerns when identifiable.
{truncation_note}{merge_note}
--- commit {sha} ---
{diff_text}
"""


def parse_findings(payload: Any) -> list[Finding]:
    """Validate a dispatch payload into typed findings.

    Raises :class:`CommitReviewError` on a missing ``findings`` key or an
    out-of-vocabulary verdict, so a malformed review is dropped rather
    than persisted as a half-record.
    """
    if not isinstance(payload, dict) or "findings" not in payload:
        raise CommitReviewError("review payload has no 'findings' key")
    raw = payload["findings"]
    if not isinstance(raw, list):
        raise CommitReviewError("review payload 'findings' is not a list")

    findings: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            raise CommitReviewError(f"finding is not an object: {item!r}")
        verdict = item.get("verdict")
        if verdict not in VERDICTS:
            raise CommitReviewError(
                f"out-of-vocabulary verdict {verdict!r}; expected one of {VERDICTS}"
            )
        findings.append(
            Finding(
                verdict=verdict,
                summary=str(item.get("summary", "")).strip(),
                reason=str(item.get("reason", "")).strip(),
                file=str(item.get("file", "") or "").strip(),
            )
        )
    return findings


def render_record(
    *,
    sha: str,
    subject: str,
    findings: list[Finding],
    cost_usd: float | None,
    truncated: bool = False,
    merge: bool = False,
    seen_bytes: int | None = None,
    total_bytes: int | None = None,
) -> str:
    """Render the T2 record body.

    A clean commit produces a record that SAYS it found nothing, rather
    than an empty one: per the nexus-moht0 vacuous-gate doctrine, a review
    that examined a commit and found nothing must be distinguishable from
    a review that never ran.
    """
    lines = [
        f"{RECORD_MARKER}{sha}",
        f"Subject: {subject}" if subject else "Subject: (unavailable)",
        f"Reviewer: claude -p, tool-free, per-commit hook (nexus-jh86x)",
    ]
    if cost_usd is not None:
        lines.append(f"Cost: ${cost_usd:.4f}")
    if merge:
        lines.append(
            "Diff: MERGE commit, first-parent view (what the merge brought onto "
            "the branch); the other parent's own history was not reviewed here."
        )
    if truncated:
        sizes = (
            f" Reviewed {seen_bytes:,} of {total_bytes:,} bytes."
            if seen_bytes is not None and total_bytes is not None
            else ""
        )
        lines.append(f"Diff: TRUNCATED at the configured cap; review is partial.{sizes}")
    lines.append("")

    if not findings:
        lines.append("No findings. The diff was reviewed and nothing was reported.")
        return "\n".join(lines)

    counts: dict[str, int] = {}
    for f in findings:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1
    lines.append(
        VERDICT_LINE_PREFIX
        + ", ".join(f"{v}={counts[v]}" for v in VERDICTS if v in counts)
    )
    lines.append("")
    for f in findings:
        where = f" [{f.file}]" if f.file else ""
        lines.append(f"- {f.verdict}{where}: {f.summary}")
        if f.reason:
            lines.append(f"  reason: {f.reason}")
    return "\n".join(lines)


#: The line :func:`render_record` writes and :func:`parse_record_verdicts`
#: reads. One constant, two consumers, so the census can never drift from
#: the writer by a stray space.
VERDICT_LINE_PREFIX: Final = "Verdicts: "


def parse_record_verdicts(content: str) -> dict[str, int]:
    """Read the verdict counts back out of a rendered record.

    Returns ``{}`` for a clean record (one with no findings), which the
    census counts as a reviewed-and-clean commit rather than as an
    unparseable one.

    This is the consumer half of a seam the RDR-201 post-mortem says to
    test by composition: ``tests/test_commit_review.py`` feeds this
    function :func:`render_record`'s own output, not a hand-written line
    that resembles it. That is the check that catches the failure where a
    producer and a consumer are each proven against a literal and neither
    is proven against the other.
    """
    for line in content.splitlines():
        if not line.startswith(VERDICT_LINE_PREFIX):
            continue
        counts: dict[str, int] = {}
        for chunk in line[len(VERDICT_LINE_PREFIX) :].split(","):
            key, _, value = chunk.strip().partition("=")
            if key in VERDICTS and value.strip().isdigit():
                counts[key] = int(value.strip())
        return counts
    return {}


DispatchFn = Callable[..., Awaitable[dict[str, Any]]]
PutFn = Callable[..., int]


async def review_commit(
    *,
    repo: Path,
    sha: str,
    cfg: CommitReviewConfig,
    dispatch: DispatchFn | None = None,
    put: PutFn | None = None,
) -> ReviewResult:
    """Review one commit and record the result. Never raises.

    *dispatch* and *put* are injected (constructor injection, house style)
    so tests drive the real orchestration against fakes rather than
    proving a narrower thing one layer down.
    """
    if not cfg.enabled:
        return ReviewResult(sha=sha, skipped_reason="disabled")

    try:
        diff_text, truncated, total_bytes = commit_diff(repo, sha, max_bytes=cfg.max_diff_bytes)
    except CommitReviewError as exc:
        _log.warning("commit_review_diff_failed", sha=sha[:12], error=str(exc))
        return ReviewResult(sha=sha, skipped_reason=str(exc))

    if not has_patch(diff_text):
        # commit_diff always emits the --format header, so a bare
        # ``.strip()`` check never fired (code review [24285] Major 2): a
        # ``merge -s ours`` that discards a whole branch, or --allow-empty,
        # dispatched a header-only diff and recorded "No findings" over it.
        return ReviewResult(sha=sha, skipped_reason="empty diff")

    if dispatch is None:  # pragma: no cover - exercised via the CLI path
        from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — deliberate function-local import: heavy operator dep deferred to call time

        dispatch = claude_dispatch

    usage: list[Any] = []
    merge = commit_parent_count(repo, sha) > 1
    try:
        payload = await dispatch(
            build_prompt(sha, diff_text, truncated=truncated, merge=merge),
            REVIEW_SCHEMA,
            timeout=cfg.timeout_seconds,
            model=cfg.model,
            max_budget_usd=cfg.max_budget_usd,
            usage_sink=usage,
            operator="commit_review",
            # Hermetic child (nexus-jh86x, measured 2026-09-02). Without
            # this the child boots the caller's SessionStart hooks -- eight
            # of them on this box, injecting the skills preamble, the beads
            # workflow context and the ready-bead list. That is not a cost
            # nuisance here, it is the feature inverting: the reviewer's
            # whole value is that it sees the diff and NOT the decision
            # record. Whether every operator should be isolated is
            # nexus-11nm2.
            isolated=True,
        )
    except Exception as exc:  # noqa: BLE001 - a hook must never block a commit
        _log.warning("commit_review_dispatch_failed", sha=sha[:12], error=str(exc))
        return ReviewResult(sha=sha, skipped_reason=str(exc), truncated=truncated)

    try:
        findings = parse_findings(payload)
    except CommitReviewError as exc:
        _log.warning("commit_review_malformed_output", sha=sha[:12], error=str(exc))
        return ReviewResult(sha=sha, skipped_reason=str(exc), truncated=truncated)

    # DIRECT attribute access, deliberately. A ``getattr(..., default)``
    # here shipped a silent bug: the field is ``cost_usd`` and the first
    # version asked for ``total_cost_usd`` (the WIRE name), so every
    # record was written with no cost line and nothing said so. Direct
    # access makes a future rename fail loudly instead.
    cost = usage[0].cost_usd if usage else None
    content = render_record(
        sha=sha,
        subject=commit_subject(repo, sha),
        findings=findings,
        cost_usd=cost,
        truncated=truncated,
        merge=merge,
        seen_bytes=len(diff_text),
        total_bytes=total_bytes,
    )

    if put is None:
        # Returns, never raises. The docstring's "never raises" has to hold
        # on EVERY path or it is not a contract, and this one was a bare
        # ``raise`` reachable by any non-CLI caller (code review of
        # a461db0b7, Important #3). Only review_cmd.py's outer try/except
        # was holding the never-blocks-a-commit guarantee up here.
        _log.warning("commit_review_no_writer", sha=sha[:12])
        return ReviewResult(
            sha=sha,
            findings=findings,
            skipped_reason="no T2 writer supplied",
            truncated=truncated,
        )

    try:
        row_id = put(
            project=REVIEW_PROJECT,
            title=record_title(sha),
            content=content,
            tags="commit-review,nexus-jh86x",
            ttl=cfg.ttl_days,
            agent="commit-review",
        )
    except Exception as exc:  # noqa: BLE001 - a hook must never block a commit
        _log.warning("commit_review_write_failed", sha=sha[:12], error=str(exc))
        return ReviewResult(
            sha=sha, findings=findings, skipped_reason=str(exc), truncated=truncated
        )

    return ReviewResult(sha=sha, findings=findings, row_id=row_id, truncated=truncated)
