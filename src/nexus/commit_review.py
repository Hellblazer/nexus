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

import hashlib
import os
import re
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


#: The ``agent`` attribution every review record is written with. The
#: engine filters ``GET /v1/memory/list?project=&agent=`` on it server-side,
#: so consumers fetch only the reviewer's own rows instead of downloading
#: the whole shared project and sieving it (critique [24283] (a)): a
#: foreign note cannot carry this attribution by accident, where a title
#: prefix can and did.
REVIEW_AGENT: Final = "commit-review"


def iter_review_records(memory) -> list[dict]:
    """Every record the reviewer wrote, with content, from a T2 memory store.

    Lists by ``agent`` (a summary view: id, title, timestamp), then fetches
    each candidate's content and keeps only rows :func:`is_review_record`
    accepts. Two selectors, both required: the attribution says who wrote
    it, the first line says what it is. Raises whatever the store raises;
    callers decide how to report an unreachable T2.
    """
    out: list[dict] = []
    for summary in memory.list_entries(project=REVIEW_PROJECT, agent=REVIEW_AGENT) or []:
        title = str(summary.get("title", ""))
        if not title.startswith(RECORD_PREFIX):
            continue
        row = memory.get(project=REVIEW_PROJECT, title=title)
        if row and is_review_record(row):
            out.append(row)
    return out


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

    ``total_bytes`` is the length of the untruncated ``git show`` text (characters,
    since the stream is decoded; the cap is applied to the same measure), so a
    record can say "reviewed 200,000 of 2,074,000 characters" rather than a bare
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


#: Number of lines ``commit_diff``'s ``--format=%H%n%s%n%an%n`` header
#: occupies before the patch body: sha, subject, author, blank.
_HEADER_LINES: Final = 4


def patch_body(diff_text: str) -> str:
    """The ``git show`` text without its sha/subject/author header.

    The header is exactly the part an amend or rebase rewrites while the
    change stays the same, so nothing that wants to say "same diff" may
    include it.
    """
    return "\n".join(diff_text.split("\n")[_HEADER_LINES:])


def header_subject(diff_text: str) -> str:
    """The commit subject from ``commit_diff``'s own header.

    Read here rather than by a second ``git log`` after the dispatch:
    record review-b0c4039fcdae (2026-09-04) said ``Subject: (unavailable)``
    because the reviewing agent worktree was removed during the minute the
    dispatch took, and the late call ran in a deleted directory. The diff
    was captured with its subject before that could happen.
    """
    lines = diff_text.split("\n")
    return lines[1].strip() if len(lines) > 1 else ""


def diff_hash(diff_text: str) -> str:
    """sha256 of :func:`patch_body`, hex. Two commits with the same hash
    are the same change (nexus-yh25a): an amended or rebased commit keeps
    its hash and needs no second review.
    """
    return hashlib.sha256(patch_body(diff_text).encode("utf-8")).hexdigest()


#: The line :func:`render_record` writes and :func:`record_diff_hash`
#: reads. One constant, two consumers.
DIFF_HASH_LINE_PREFIX: Final = "Diff-Hash: "


def record_diff_hash(content: str) -> str | None:
    """The diff hash a rendered record carries, or ``None`` for a record
    written before the line existed."""
    for line in content.splitlines():
        if line.startswith(DIFF_HASH_LINE_PREFIX):
            value = line[len(DIFF_HASH_LINE_PREFIX) :].strip()
            return value or None
    return None


def find_prior_review(memory, wanted_hash: str) -> str | None:
    """Title of an existing record for *wanted_hash*, or ``None``.

    One full-text search on the hash token (the engine tokenises a hex
    run as one word; probed live 2026-09-04 on a 40-hex and a 12-hex
    token), confirmed by content: the row must be a review record
    (:func:`is_review_record`) whose own ``Diff-Hash`` line equals the
    hash. Fetching every review record per commit was the first cut and
    is O(records) HTTP calls on every commit (review [24376] Major 1).

    A prior record carrying a FIX-NOW is not a match: the defect is still
    in the diff, and the SessionStart notice only surfaces records from
    the last seven days, so a rebase must produce a fresh record for it
    to stay visible (review [24376] Major 2).

    Raises whatever the store raises; :func:`review_commit` treats that
    as "no prior record" because a T2 outage must never block a commit.
    """
    for row in memory.search(wanted_hash, project=REVIEW_PROJECT, access="silent") or []:
        if not isinstance(row, dict) or not is_review_record(row):
            continue
        content = str(row.get("content", "") or "")
        if record_diff_hash(content) != wanted_hash:
            continue
        if parse_record_verdicts(content).get("FIX-NOW"):
            return None
        return str(row.get("title", ""))
    return None


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
    diff_hash_hex: str | None = None,
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
    if diff_hash_hex:
        lines.append(f"{DIFF_HASH_LINE_PREFIX}{diff_hash_hex}")
    if merge:
        lines.append(
            "Diff: MERGE commit, first-parent view (what the merge brought onto "
            "the branch); the other parent's own history was not reviewed here."
        )
    if truncated:
        sizes = (
            f" Reviewed {seen_bytes:,} of {total_bytes:,} characters."
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
    memory=None,
) -> ReviewResult:
    """Review one commit and record the result. Never raises.

    *dispatch* and *put* are injected (constructor injection, house style)
    so tests drive the real orchestration against fakes rather than
    proving a narrower thing one layer down. *memory* is the T2 memory
    store used to look for a prior record of the same diff; ``None``
    skips the lookup.
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

    this_hash = diff_hash(diff_text)
    if memory is not None and not truncated:
        # Same patch, new sha: an amend or a rebase. Measured 2026-09-04
        # over 16 live records: two pairs were one change reviewed twice,
        # a quarter of the spend (nexus-yh25a). Best-effort by design.
        # Never on a truncated diff: two commits identical up to the cap
        # and different past it hash the same, and the second would be
        # skipped as a duplicate with no signal (critique [24377] C1).
        try:
            prior = find_prior_review(memory, this_hash)
        except Exception as exc:  # noqa: BLE001 - a hook must never block a commit
            _log.warning("commit_review_dedupe_lookup_failed", sha=sha[:12], error=str(exc))
            prior = None
        if prior:
            _log.info("commit_review_duplicate_diff", sha=sha[:12], prior=prior)
            return ReviewResult(
                sha=sha,
                skipped_reason=f"same diff already reviewed as {prior}",
                truncated=truncated,
            )

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
        subject=header_subject(diff_text),
        findings=findings,
        cost_usd=cost,
        truncated=truncated,
        merge=merge,
        seen_bytes=len(diff_text),
        total_bytes=total_bytes,
        diff_hash_hex=this_hash,
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
            agent=REVIEW_AGENT,
        )
    except Exception as exc:  # noqa: BLE001 - a hook must never block a commit
        _log.warning("commit_review_write_failed", sha=sha[:12], error=str(exc))
        return ReviewResult(
            sha=sha, findings=findings, skipped_reason=str(exc), truncated=truncated
        )

    return ReviewResult(sha=sha, findings=findings, row_id=row_id, truncated=truncated)


# ── burst queue and coverage (the post-commit reviewer's drop, 2026-09-04) ────
#
# The hook serialises a burst with a pgrep guard. Until 2026-09-04 a hit
# DROPPED the commit: it logged "SKIPPED (review already running)" and
# nothing ever came back for it, so 6 of 9 commits in one push went
# unreviewed while the log looked healthy. Sam's ruling (session nexus-65):
# queue, and make the gap visible. The hook now appends the sha to
# :func:`review_queue_path` and the running reviewer, dispatched with
# ``--drain``, pops and reviews every queued sha before it exits.
# :func:`review_coverage` is the other half: the census names every commit
# since the newest reachable tag that has no record, so a stranded queue
# entry (the reviewer exited between the hook's pgrep and its append) is a
# named gap, not silence. The next commit's ``--drain`` picks it up.

QUEUE_FILENAME: Final = "nx-review-queue"
_DRAINING_SUFFIX: Final = ".draining."
_SHA_RE: Final = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")


def _git_common_dir(repo: Path) -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if not out:
        raise CommitReviewError(f"git rev-parse --git-common-dir returned nothing in {repo}")
    common = Path(out)
    return (common if common.is_absolute() else repo / common).resolve()


def review_queue_path(repo: Path) -> Path:
    """The burst queue for *repo*: one full sha per line, in the COMMON git
    dir, so every linked worktree of the repository shares one queue with
    the hook that lives beside it. The hook computes the same path in shell
    (``cd "$(git rev-parse --git-common-dir)" && pwd -P``); the two are
    held together by ``tests/test_commit_review_hook.py``."""
    return _git_common_dir(repo) / QUEUE_FILENAME


def _draining_files(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + _DRAINING_SUFFIX + "*"))


def _take(candidate: Path) -> list[str]:
    """Read and unlink *candidate*; ``[]`` when another popper took it first."""
    try:
        text = candidate.read_text()
        candidate.unlink()
    except FileNotFoundError:
        return []
    return text.splitlines()


def pop_review_queue(repo: Path) -> list[str]:
    """Take every queued sha, oldest first, deduplicated. Never loses one.

    Renames the queue aside to a name unique to THIS process before reading
    it, so a hook appending concurrently lands in a fresh file the next pop
    sees, and two poppers racing (the hook's pgrep guard is not a lock)
    cannot rename onto the same target: ``rename`` of the shared queue
    succeeds for exactly one of them, and each reads only what it renamed.
    Remnants of a drainer that died between its rename and its read, from
    any pid, are folded in first rather than left behind. A remnant a live
    sibling is about to read may be taken from under it: the sibling then
    sees nothing (:func:`_take`) and the sha is reviewed here instead.
    Review [24406] Critical reproduced the shared-name version losing a sha
    outright; that name is gone. Never raises for a missing queue.
    """
    path = review_queue_path(repo)
    lines: list[str] = []
    for remnant in _draining_files(path):
        lines.extend(_take(remnant))
    mine = path.with_name(f"{path.name}{_DRAINING_SUFFIX}{os.getpid()}")
    try:
        path.replace(mine)
    except FileNotFoundError:
        pass
    else:
        lines.extend(_take(mine))
    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        sha = raw.strip()
        if _SHA_RE.match(sha) and sha not in seen:
            seen.add(sha)
            out.append(sha)
    return out


def review_queue_depth(repo: Path) -> int:
    """Shas waiting in the queue (and any draining remnant), without taking them."""
    path = review_queue_path(repo)
    n = 0
    for candidate in (path, *_draining_files(path)):
        try:
            n += sum(1 for line in candidate.read_text().splitlines() if _SHA_RE.match(line.strip()))
        except FileNotFoundError:
            continue
    return n


@dataclass(frozen=True)
class ReviewGap:
    sha: str
    subject: str


@dataclass(frozen=True)
class ReviewCoverage:
    since: str  #: the ref the walk started after (a tag name, or the fallback label)
    commits: int  #: commits walked
    gaps: list[ReviewGap]  #: walked commits with a patch and no record
    patchless: int  #: walked commits with no patch (merge -s ours, --allow-empty): never reviewed by design


#: With no tag reachable from HEAD the walk is bounded here instead of
#: running to the root commit.
COVERAGE_FALLBACK_COMMITS: Final = 100
#: Ceiling on any walk, tag or not: each unreviewed commit costs one
#: ``git show``, and a branch a thousand commits past its last tag is a
#: census that should say so rather than run for a minute.
COVERAGE_MAX_COMMITS: Final = 500


def newest_reachable_tag(repo: Path) -> str | None:
    proc = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def review_coverage(
    repo: Path,
    records: list[dict],
    *,
    since: str | None = None,
    max_diff_bytes: int,
) -> ReviewCoverage:
    """Which commits since *since* (default: the newest reachable tag) have
    no review record.

    A commit is covered when a record carries its sha (``review-<12hex>``)
    OR a record's ``Diff-Hash`` equals its own patch hash: a rebase or
    amend is reviewed under the sha it first had, and
    :func:`review_commit` deliberately writes nothing for the new sha. A
    truncated diff is never matched by hash, for the reason
    :func:`review_commit` never dedupes one. Patch-less commits are
    counted, not listed: the reviewer skips them on purpose.

    *records* is the census's own review-record list (:func:`iter_review_records`),
    so the two reports walk the same rows.
    """
    label = since or newest_reachable_tag(repo)
    if label:
        rev_args = [f"{label}..HEAD", f"--max-count={COVERAGE_MAX_COMMITS}"]
    else:
        label = f"(no tag reachable; last {COVERAGE_FALLBACK_COMMITS} commits)"
        rev_args = ["HEAD", f"--max-count={COVERAGE_FALLBACK_COMMITS}"]
    shas = subprocess.run(
        ["git", "rev-list", *rev_args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if len(shas) >= COVERAGE_MAX_COMMITS:
        label = f"{label} (walk capped at {COVERAGE_MAX_COMMITS} commits)"

    reviewed_shas: set[str] = set()
    reviewed_hashes: set[str] = set()
    for row in records:
        title = str(row.get("title", ""))
        if title.startswith(RECORD_PREFIX):
            reviewed_shas.add(title[len(RECORD_PREFIX) :])
        digest = record_diff_hash(str(row.get("content", "") or ""))
        if digest:
            reviewed_hashes.add(digest)

    gaps: list[ReviewGap] = []
    patchless = 0
    for sha in shas:
        if sha[:12] in reviewed_shas:
            continue
        try:
            diff_text, truncated, _ = commit_diff(repo, sha, max_bytes=max_diff_bytes)
        except CommitReviewError:
            gaps.append(ReviewGap(sha=sha, subject="(diff unavailable)"))
            continue
        if not has_patch(diff_text):
            patchless += 1
            continue
        if not truncated and diff_hash(diff_text) in reviewed_hashes:
            continue
        gaps.append(ReviewGap(sha=sha, subject=header_subject(diff_text)))
    return ReviewCoverage(since=label, commits=len(shas), gaps=gaps, patchless=patchless)
