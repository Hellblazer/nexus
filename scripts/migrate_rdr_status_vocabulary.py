#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-201 Phase 1.7: scripted status-vocabulary sweep of docs/rdr.

One script, two legs, both dry-run-safe by default.

LEG 1 (files) rewrites ``docs/rdr/*.md`` frontmatter onto the six-value
closed vocabulary decided for RDR-201 (draft / accepted / deferred / closed
/ superseded / abandoned):

  * ``scrapped`` -> ``abandoned`` (the retired value; Sam's ruling).
  * ``companion-note`` / ``frozen`` / ``frozen-pending-question-set`` /
    ``complete`` (the RDR-200 sub-documents) -> ``kind: companion``, status
    key REMOVED entirely (companions carry no lifecycle status).
  * ``revised-after-implementation`` -> ``status: closed`` PLUS
    ``kind: companion`` (unlike the four above, this one keeps a status).
  * The six canonical values pass through unchanged.

The ``docs/rdr/README.md`` index is rewritten in the SAME run, via a
shape-aware cell parser: ~37 of its ~194 status cells are DECORATED
(``Closed (implemented)``, ``**Scrapped 2026-05-19**``,
``Superseded by RDR-108``, ...) and an exact-match rewriter (the one both
``nexus.commands.rdr._update_readme_status_row`` and
``tests/test_rdr_close_tripwire.py``'s ``_readme_status_cell`` use) is
silently vacuous over every one of them. This rewriter instead finds the
status WORD inside the cell via a word-boundary regex scoped to the Status
column only, substitutes just that word when its mapped value differs
(only ``scrapped`` -> ``abandoned`` ever does), and leaves every other
character of the cell -- bold markers, dates, successor RDR ids -- alone.

A before/after CENSUS is always computed and written (default:
``docs/rdr/status-census-2026-09-01.md``), including the set of statused
files with NO README index row at all, and the non-zero residual between
index and frontmatter status counts (T2 [24001]/[23999]: these two surfaces
already disagree today; a sweep that assumes convergence to zero is wrong).

Any on-disk status NOT in the twelve-value table this bead's plan enumerated
is left COMPLETELY UNTOUCHED and reported under "unmapped" -- never guessed
into a bucket. (Measured 2026-09-01: a 13th status, ``frozen-before-arms``,
appeared on disk after the plan's census was taken -- exactly the class of
drift ``nexus-j9z30.8``'s out-of-vocabulary audit exists to catch.)

LEG 2 (T2) mirrors the same status mapping against T2 project
``nexus_rdr`` through an injectable client (``T2Client`` protocol below,
matching ``nexus.db.t2.T2Database``'s ``get_all``/``put`` delegates).
Default is ``--dry-run``: the full proposed diff (record, old, new) is
printed and NOTHING is written. ``--apply`` is required to write, and is a
SEPARATE decision from leg 1's ``--apply`` -- ``--leg t2`` never touches a
file, and ``--leg files`` never touches T2. A record whose file becomes
``kind: companion`` gets its T2 status CLEARED regardless of what leg 1 did
to the file's own frontmatter (``revised-after-implementation`` keeps
``status: closed`` on disk but is still cleared in T2 -- the two legs use
independently-stated rules, per the bead). Disk-only and T2-only records are
REPORTED, never silently created or deleted. A single RDR number claimed by
more than one disk file or more than one T2 record (e.g. RDR-200's one
primary file plus four ``frozen*``/``complete`` companion sub-documents, all
sharing the digit group "200") is AMBIGUOUS and is reported rather than
guessed -- this runs against live production T2, and picking one candidate
over another with no principled signal is not this script's call to make.

Usage::

    python3 scripts/migrate_rdr_status_vocabulary.py --leg files --apply
    python3 scripts/migrate_rdr_status_vocabulary.py --leg t2          # dry-run
    python3 scripts/migrate_rdr_status_vocabulary.py --leg t2 --apply  # NEVER
        # run without Sam's explicit go on the printed diff (bead nexus-j9z30.7)
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# ---------------------------------------------------------------------------
# Status vocabulary (Sam's ruling, RDR-201 Technical Design; T2
# nexus/plan-rdr-201-enrichment-deltas [24001], nexus/plan-rdr-201-audit-
# round-3-residuals [23999]).
# ---------------------------------------------------------------------------

LIFECYCLE_STATUSES: frozenset[str] = frozenset(
    {"draft", "accepted", "deferred", "closed", "superseded", "abandoned"}
)

#: Values retired by this sweep. Recorded to T2 (project nexus_rdr) by the
#: caller before committing -- the word is retired, not erased from memory.
RETIRED_STATUS_VALUES: tuple[str, ...] = ("scrapped",)


@dataclass(frozen=True)
class StatusOutcome:
    """Per-status leg-1 (file) transform.

    ``new_status`` is ``None`` when the status key is removed entirely (a
    companion note carries no lifecycle status). ``kind`` is ``"companion"``
    when the file's frontmatter gains ``kind: companion``.
    """

    new_status: str | None
    kind: str | None = None


STATUS_TRANSITIONS: dict[str, StatusOutcome] = {
    "draft": StatusOutcome("draft"),
    "accepted": StatusOutcome("accepted"),
    "deferred": StatusOutcome("deferred"),
    "closed": StatusOutcome("closed"),
    "superseded": StatusOutcome("superseded"),
    "abandoned": StatusOutcome("abandoned"),
    "scrapped": StatusOutcome("abandoned"),
    "companion-note": StatusOutcome(None, kind="companion"),
    "frozen": StatusOutcome(None, kind="companion"),
    "frozen-pending-question-set": StatusOutcome(None, kind="companion"),
    "complete": StatusOutcome(None, kind="companion"),
    "revised-after-implementation": StatusOutcome("closed", kind="companion"),
}

EXCLUDE_FILENAMES: frozenset[str] = frozenset({"agents.md", "readme.md"})

DEFAULT_PROJECT = "nexus_rdr"
DEFAULT_CENSUS_FILENAME = "status-census-2026-09-01.md"

README_WORD_MAP: dict[str, str] = {"scrapped": "abandoned"}

_STATUS_LINE_RE = re.compile(r"^(\s*)status:\s*(.*)$")
_KIND_LINE_RE = re.compile(r"^(\s*)kind:\s*(.*)$")
_FILENAME_NUMBER_RE = re.compile(r"^rdr-?(\d+)", re.IGNORECASE)
_README_ROW_PREFIX_RE = re.compile(r"^\|\s*\[RDR-(\d+)[^\]]*\]\(([^)]+)\)")
_T2_TITLE_NUMBER_RE = re.compile(r"^(?:RDR-)?(\d+)$", re.IGNORECASE)

_STATUS_WORD_PATTERN = re.compile(
    r"\b("
    + "|".join(
        sorted(
            (re.escape(w) for w in LIFECYCLE_STATUSES | set(RETIRED_STATUS_VALUES)),
            key=len,
            reverse=True,
        )
    )
    + r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Leg 1 — file-level frontmatter helpers
# ---------------------------------------------------------------------------


def extract_status(block: str) -> str | None:
    """Return the ``status:`` value from a frontmatter block, or None."""
    for line in block.splitlines():
        m = _STATUS_LINE_RE.match(line)
        if m:
            val = m.group(2).strip().strip('"').strip("'")
            return val or None
    return None


def _rewrite_frontmatter_lines(lines: list[str], outcome: StatusOutcome) -> list[str]:
    """Single-pass line transform: replace or remove the ``status:`` line per
    *outcome*, and (re)insert a ``kind:`` line at the position the status
    line occupied.

    A pre-existing ``kind:`` line's VALUE is preserved unless *outcome*
    itself sets ``kind == "companion"`` (in which case it is authoritatively
    overwritten to ``kind: companion`` -- this sweep's whole point for those
    five statuses). No file in docs/rdr carries a non-companion ``kind:``
    line today (verified 2026-09-02), but a migration script silently
    dropping a field it doesn't understand is a latent data-loss bug
    regardless of whether current data happens to trigger it."""
    # Pre-scan (not inline) so a `kind:` line's value is captured regardless
    # of whether it appears before or after `status:` in the original file.
    existing_kind_line: str | None = next((ln for ln in lines if _KIND_LINE_RE.match(ln)), None)
    new_lines: list[str] = []
    status_seen = False
    for line in lines:
        if _KIND_LINE_RE.match(line):
            continue  # dropped here; reinserted at the status line's position below
        if _STATUS_LINE_RE.match(line):
            status_seen = True
            if outcome.new_status is not None:
                new_lines.append(f"status: {outcome.new_status}")
            if outcome.kind == "companion":
                new_lines.append("kind: companion")
            elif existing_kind_line is not None:
                new_lines.append(existing_kind_line)
            continue
        new_lines.append(line)
    if not status_seen:
        if outcome.new_status is not None:
            new_lines.append(f"status: {outcome.new_status}")
        if outcome.kind == "companion":
            new_lines.append("kind: companion")
        elif existing_kind_line is not None:
            new_lines.append(existing_kind_line)
    return new_lines


def rewrite_file_text(text: str, outcome: StatusOutcome) -> str:
    """Rewrite one RDR file's full text per *outcome* (mirrors the
    ``"---\\n" + block.strip() + "\\n---" + rest`` reconstruction convention
    already used by ``conexus/hooks/scripts/rdr_hook.py``'s
    ``_update_file_status``)."""
    parts = text.split("---", 2)
    new_fm = _rewrite_frontmatter_lines(parts[1].splitlines(), outcome)
    return "---\n" + "\n".join(new_fm).strip() + "\n---" + parts[2]


def extract_rdr_number(path: Path) -> str | None:
    """Digit group from a filename like ``rdr-201-slug.md`` or the
    misnamed ``rdr137-...md`` -- deliberately more robust than
    ``rdr_hook.py``'s ``_extract_rdr_id`` (``re.match(r"(\\d+)", stem)``),
    which cannot match any real ``rdr-NNN-*`` filename at all since the
    stem starts with the letters "rdr", not a digit (that dead code path
    is nexus-e19sa)."""
    m = _FILENAME_NUMBER_RE.match(path.stem)
    return m.group(1) if m else None


@dataclass
class FileResult:
    path: Path
    number: str
    old_status: str | None  # None: no frontmatter / no status field at all
    outcome: StatusOutcome | None  # None: status present but unmapped
    changed: bool


def iter_rdr_files(rdr_dir: Path) -> list[Path]:
    """Non-recursive ``*.md`` glob, excluding AGENTS.md/README.md. Glob is
    single-level by construction, so ``post-mortem/`` is excluded without a
    special case."""
    return sorted(p for p in rdr_dir.glob("*.md") if p.name.lower() not in EXCLUDE_FILENAMES)


def process_file(path: Path, *, apply: bool) -> FileResult:
    text = path.read_text(encoding="utf-8")
    number = extract_rdr_number(path) or ""
    if not text.startswith("---"):
        return FileResult(path, number, None, None, False)
    parts = text.split("---", 2)
    if len(parts) < 3:
        return FileResult(path, number, None, None, False)
    old_status = extract_status(parts[1])
    if old_status is None:
        return FileResult(path, number, None, None, False)
    outcome = STATUS_TRANSITIONS.get(old_status.lower())
    if outcome is None:
        return FileResult(path, number, old_status, None, False)
    new_text = rewrite_file_text(text, outcome)
    changed = new_text != text
    if changed and apply:
        path.write_text(new_text, encoding="utf-8")
    return FileResult(path, number, old_status, outcome, changed)


def compute_file_results(rdr_dir: Path, *, apply: bool) -> list[FileResult]:
    return [process_file(p, apply=apply) for p in iter_rdr_files(rdr_dir)]


# ---------------------------------------------------------------------------
# Leg 1 — README index rewriter (shape-aware; status-word substitution only)
# ---------------------------------------------------------------------------


@dataclass
class ReadmeRowResult:
    filename: str
    rdr_id: str
    old_cell: str
    new_cell: str
    status_word: str | None  # lowercase recognized word, or None if cell unrecognized
    mapped_word: str | None


def _match_case(original: str, new_word: str) -> str:
    if original.isupper():
        return new_word.upper()
    if original[:1].isupper() and original[1:].islower():
        return new_word[:1].upper() + new_word[1:].lower()
    if original.islower():
        return new_word.lower()
    return new_word


def rewrite_readme_row(line: str) -> tuple[str, ReadmeRowResult | None]:
    """Rewrite one README index row. Only the STATUS COLUMN's recognized
    word is ever substituted; every other character -- decoration, other
    columns -- passes through untouched."""
    m = _README_ROW_PREFIX_RE.match(line)
    if not m:
        return line, None
    rdr_id, filename = m.group(1), m.group(2)
    cols = line.split("|")
    if len(cols) < 6:
        return line, None
    status_cell = cols[4]
    sm = _STATUS_WORD_PATTERN.search(status_cell)
    if not sm:
        return line, ReadmeRowResult(filename, rdr_id, status_cell, status_cell, None, None)

    old_word = sm.group(1)
    old_lower = old_word.lower()
    mapped_lower = README_WORD_MAP.get(old_lower, old_lower)
    if mapped_lower == old_lower:
        return line, ReadmeRowResult(filename, rdr_id, status_cell, status_cell, old_lower, mapped_lower)

    new_word = _match_case(old_word, mapped_lower)
    new_cell = status_cell[: sm.start()] + new_word + status_cell[sm.end() :]
    cols[4] = new_cell
    return "|".join(cols), ReadmeRowResult(filename, rdr_id, status_cell, new_cell, old_lower, mapped_lower)


def rewrite_readme_text(text: str) -> tuple[str, list[ReadmeRowResult]]:
    out_lines: list[str] = []
    results: list[ReadmeRowResult] = []
    for line in text.splitlines():
        new_line, result = rewrite_readme_row(line)
        out_lines.append(new_line)
        if result is not None:
            results.append(result)
    new_text = "\n".join(out_lines)
    if text.endswith("\n"):
        new_text += "\n"
    return new_text, results


# ---------------------------------------------------------------------------
# Census
# ---------------------------------------------------------------------------


@dataclass
class Census:
    total_files: int
    raw_counts: Counter = field(default_factory=Counter)
    unmapped: dict[str, str] = field(default_factory=dict)
    no_status: list[str] = field(default_factory=list)
    no_readme_row: list[str] = field(default_factory=list)
    readme_row_count: int = 0
    readme_raw_counts: Counter = field(default_factory=Counter)
    mismatches: list[tuple[str, str, str]] = field(default_factory=list)


def build_census(
    file_results: list[FileResult], readme_results: list[ReadmeRowResult], *, after: bool
) -> Census:
    raw_counts: Counter = Counter()
    unmapped: dict[str, str] = {}
    no_status: list[str] = []
    status_by_filename: dict[str, str] = {}
    total_files = 0

    for r in file_results:
        if r.old_status is None:
            no_status.append(r.path.name)
            continue
        total_files += 1
        if r.outcome is None:
            unmapped[r.path.name] = r.old_status
            if not after:
                raw_counts[r.old_status.lower()] += 1
            continue
        if after:
            key = r.outcome.new_status if r.outcome.new_status is not None else "<companion>"
            raw_counts[key] += 1
            if r.outcome.new_status is not None:
                status_by_filename[r.path.name] = r.outcome.new_status
        else:
            raw_counts[r.old_status.lower()] += 1
            status_by_filename[r.path.name] = r.old_status.lower()

    readme_row_count = len(readme_results)
    word_attr = "mapped_word" if after else "status_word"
    readme_raw_counts: Counter = Counter(
        getattr(res, word_attr) for res in readme_results if getattr(res, word_attr) is not None
    )
    readme_status_by_filename = {
        res.filename: getattr(res, word_attr) for res in readme_results if getattr(res, word_attr) is not None
    }

    no_readme_row = sorted(
        set(status_by_filename) - set(readme_status_by_filename)
        | ({fn for fn in unmapped} - set(readme_status_by_filename))
    )

    mismatches = [
        (fn, status_by_filename[fn], readme_status_by_filename[fn])
        for fn in status_by_filename
        if fn in readme_status_by_filename and readme_status_by_filename[fn] != status_by_filename[fn]
    ]

    return Census(
        total_files=total_files,
        raw_counts=raw_counts,
        unmapped=unmapped,
        no_status=no_status,
        no_readme_row=no_readme_row,
        readme_row_count=readme_row_count,
        readme_raw_counts=readme_raw_counts,
        mismatches=mismatches,
    )


def _render_census_section(title: str, c: Census) -> list[str]:
    lines = [f"## {title}", ""]
    lines.append(f"- Statused files: {c.total_files}")
    lines.append("- Status counts:")
    for k, v in sorted(c.raw_counts.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        lines.append(f"  - {k}: {v}")
    if c.unmapped:
        lines.append(
            f"- UNMAPPED statuses ({len(c.unmapped)}, out of this sweep's scope, left "
            "untouched — see nexus-j9z30.8):"
        )
        for fn, st in sorted(c.unmapped.items()):
            lines.append(f"  - {fn}: `{st}`")
    lines.append(f"- README index rows: {c.readme_row_count}")
    lines.append(f"- Statused files with NO README row ({len(c.no_readme_row)}):")
    for fn in c.no_readme_row:
        lines.append(f"  - {fn}")
    lines.append(
        f"- Index-vs-frontmatter residual (non-zero expected — the two surfaces "
        f"disagreed before this sweep too): {len(c.mismatches)} mismatches"
    )
    for fn, fm, rd in c.mismatches:
        lines.append(f"  - {fn}: frontmatter=`{fm}` README=`{rd}`")
    lines.append("")
    return lines


def render_census_md(before: Census, after: Census) -> str:
    lines = [
        "# RDR Status-Vocabulary Sweep Census",
        "",
        "Generated by `scripts/migrate_rdr_status_vocabulary.py` (nexus-j9z30.7, RDR-201 P1.7).",
        "",
    ]
    lines += _render_census_section("Before", before)
    lines += _render_census_section("After", after)
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Leg 2 — T2
# ---------------------------------------------------------------------------


class T2Client(Protocol):
    def get_all(self, project: str) -> list[dict[str, Any]]: ...

    def put(
        self, project: str, title: str, content: str, tags: str = "", ttl: int | None = None
    ) -> int: ...


def extract_status_from_content(content: str) -> str | None:
    """Scan every line of a T2 record's content for a ``status:`` line
    (mirrors ``rdr_hook.py``'s ``_load_all_t2_statuses`` — T2 content is not
    guaranteed to carry the ``---`` frontmatter delimiters a file does)."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("status:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            return val or None
    return None


def set_status_in_content(content: str, new_status: str) -> str:
    """Replace the value of an existing ``status:`` line, or PREPEND one when
    the content has none (a diff reporting ``None -> <new_status>`` must
    actually produce that status on apply — a silent no-op here would leave
    the printed diff a lie about what ``--apply`` had done)."""
    lines = content.splitlines()
    out: list[str] = []
    replaced = False
    for line in lines:
        m = _STATUS_LINE_RE.match(line)
        if m and not replaced:
            indent, raw_val = m.group(1), m.group(2).strip()
            if raw_val[:1] in ('"', "'"):
                q = raw_val[0]
                out.append(f"{indent}status: {q}{new_status}{q}")
            else:
                out.append(f"{indent}status: {new_status}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.insert(0, f"status: {new_status}")
    return "\n".join(out)


def clear_status_in_content(content: str) -> str:
    lines = [line for line in content.splitlines() if not _STATUS_LINE_RE.match(line)]
    return "\n".join(lines)


@dataclass
class T2Diff:
    title: str
    old_status: str | None
    new_status: str | None  # None: cleared (companion outcome)
    #: "migrate" (T2's OWN value changes under STATUS_TRANSITIONS, e.g.
    #: scrapped -> abandoned) | "clear-companion" (file became kind:
    #: companion; T2 status cleared regardless of its own text) |
    #: "drift-report-only" (T2 disagrees with the file's target for some
    #: OTHER reason -- rdr_hook / nexus-e19sa's reconcile scope, not this
    #: script's). Only the first two classes are ever written under --apply.
    reason: str


@dataclass
class T2Report:
    diffs: list[T2Diff] = field(default_factory=list)  # apply-eligible: migrate + clear-companion
    drift: list[T2Diff] = field(default_factory=list)  # report-only, NEVER applied
    disk_only: list[str] = field(default_factory=list)
    t2_only: list[str] = field(default_factory=list)
    ambiguous: dict[str, list[str]] = field(default_factory=dict)
    #: apply-eligible diffs whose write was skipped because the source T2
    #: record carries no `ttl` field at all -- see ``_resolve_apply_ttl``.
    ttl_unknown: list[str] = field(default_factory=list)


def _resolve_apply_ttl(entry: dict[str, Any]) -> tuple[bool, int | None]:
    """Return ``(safe, ttl)`` for forwarding *entry*'s CURRENT ttl policy
    verbatim on a content-only ``put()``.

    ``safe=False`` means *entry* carries no ``"ttl"`` key at all -- refuse to
    guess. ``dict.get("ttl")`` cannot distinguish "explicitly ``None``
    (permanent)" from "field genuinely absent", and guessing wrong in either
    direction is a real correctness bug on a live store: guessing ``None``
    would silently PERMANENT a record that was actually time-boxed; guessing
    a number would silently expire what was actually permanent. A
    content-only status edit must never carry that side effect as a guess."""
    if "ttl" not in entry:
        return False, None
    return True, entry.get("ttl")


def run_t2_leg(
    client: T2Client, project: str, file_results: list[FileResult], *, apply: bool
) -> T2Report:
    disk_by_number: dict[str, list[FileResult]] = {}
    for r in file_results:
        if r.outcome is None or not r.number:
            continue
        disk_by_number.setdefault(r.number, []).append(r)

    entries = client.get_all(project)
    t2_by_number: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        title = str(e.get("title", ""))
        m = _T2_TITLE_NUMBER_RE.match(title)
        if not m:
            continue
        t2_by_number.setdefault(m.group(1), []).append(e)

    report = T2Report()
    for number in sorted(set(disk_by_number) | set(t2_by_number)):
        disk_group = disk_by_number.get(number, [])
        t2_group = t2_by_number.get(number, [])

        if not disk_group:
            report.t2_only.append(number)
            continue
        if not t2_group:
            report.disk_only.append(number)
            continue
        if len(disk_group) > 1 or len(t2_group) > 1:
            report.ambiguous[number] = [r.path.name for r in disk_group] + [
                str(e.get("title", "")) for e in t2_group
            ]
            continue

        file_result = disk_group[0]
        entry = t2_group[0]
        title = str(entry.get("title", ""))
        content = entry.get("content", "") or ""
        old_t2_status = extract_status_from_content(content)
        outcome = file_result.outcome
        assert outcome is not None  # disk_by_number excludes unmapped outcomes

        if outcome.kind == "companion":
            # The bead's OWN, independent T2 rule -- not derived from
            # STATUS_TRANSITIONS at all: a file that becomes kind: companion
            # gets its T2 status CLEARED regardless of what T2's own text
            # says and regardless of what leg 1 kept on disk
            # (revised-after-implementation keeps status: closed on the
            # file but is still cleared here).
            if old_t2_status is None:
                continue
            new_content = clear_status_in_content(content)
            report.diffs.append(
                T2Diff(title=title, old_status=old_t2_status, new_status=None, reason="clear-companion")
            )
        else:
            # "Map by the same rules as leg 1": translate T2's OWN status
            # through STATUS_TRANSITIONS -- NOT the file's target status.
            # Only a T2 value that itself changes under the table (today:
            # only scrapped -> abandoned) is a genuine vocabulary migration.
            # Reconciling T2 against whatever the file currently says
            # (regardless of vocabulary) is drift reconciliation -- rdr_
            # hook's job (nexus-e19sa), out of this bead's scope.
            t2_outcome = STATUS_TRANSITIONS.get(old_t2_status.lower()) if old_t2_status is not None else None
            if (
                t2_outcome is not None
                and t2_outcome.kind is None
                and t2_outcome.new_status is not None
                and t2_outcome.new_status != old_t2_status.lower()
            ):
                new_content = set_status_in_content(content, t2_outcome.new_status)
                report.diffs.append(
                    T2Diff(
                        title=title,
                        old_status=old_t2_status,
                        new_status=t2_outcome.new_status,
                        reason="migrate",
                    )
                )
            else:
                if old_t2_status is None or old_t2_status.lower() != outcome.new_status:
                    report.drift.append(
                        T2Diff(
                            title=title,
                            old_status=old_t2_status,
                            new_status=outcome.new_status,
                            reason="drift-report-only",
                        )
                    )
                continue  # drift (or genuine agreement) is never applied

        if apply:
            safe, ttl = _resolve_apply_ttl(entry)
            if not safe:
                report.ttl_unknown.append(title)
                continue
            client.put(project, title, new_content, tags=entry.get("tags", "") or "", ttl=ttl)

    return report


def render_t2_report(report: T2Report, *, applied: bool) -> str:
    lines = [
        f"## T2 leg ({'APPLY' if applied else 'DRY-RUN'}) — {len(report.diffs)} proposed diff(s), "
        f"{len(report.drift)} drift-only (never applied)"
    ]
    for d in report.diffs:
        lines.append(f"  - [{d.reason}] {d.title}: {d.old_status!r} -> {d.new_status!r}")
    if report.drift:
        lines.append(
            f"Drift — T2-vs-file disagreement NOT migrated by this script "
            f"(rdr_hook / nexus-e19sa reconcile scope; never in the apply set) [{len(report.drift)}]:"
        )
        for d in report.drift:
            lines.append(f"  - {d.title}: T2={d.old_status!r} file={d.new_status!r}")
    if report.ttl_unknown:
        lines.append(
            f"TTL policy undeterminable, SKIPPED under --apply (record has no `ttl` field) "
            f"[{len(report.ttl_unknown)}]: " + ", ".join(report.ttl_unknown)
        )
    if report.disk_only:
        lines.append(
            f"Disk-only (file has a mapped status, no T2 record) [{len(report.disk_only)}]: "
            + ", ".join(report.disk_only)
        )
    if report.t2_only:
        lines.append(
            f"T2-only (T2 record, no matching disk file) [{len(report.t2_only)}]: "
            + ", ".join(report.t2_only)
        )
    if report.ambiguous:
        lines.append(f"Ambiguous number correlations, SKIPPED [{len(report.ambiguous)}]:")
        for num, cands in sorted(report.ambiguous.items()):
            lines.append(f"  - {num}: {cands}")
    return "\n".join(lines)


def _t2_client_cm(t2_client: T2Client | None):
    if t2_client is not None:
        return nullcontext(t2_client)
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415
    from nexus.db.t2 import T2Database  # noqa: PLC0415

    return T2Database(default_db_path())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rdr-dir", type=Path, default=Path("docs/rdr"))
    p.add_argument("--leg", choices=("files", "t2", "both"), default="files")
    p.add_argument("--apply", action="store_true", help="Write changes. Default is dry-run (writes nothing).")
    p.add_argument("--project", default=DEFAULT_PROJECT)
    p.add_argument("--census-out", type=Path, default=None)
    return p


def main(argv: list[str] | None = None, *, t2_client: T2Client | None = None) -> int:
    args = build_parser().parse_args(argv)
    rdr_dir: Path = args.rdr_dir

    file_results: list[FileResult] = []

    if args.leg in ("files", "both"):
        file_results = compute_file_results(rdr_dir, apply=args.apply)

        readme_path = rdr_dir / "README.md"
        readme_results: list[ReadmeRowResult] = []
        if readme_path.is_file():
            readme_text = readme_path.read_text(encoding="utf-8")
            new_readme_text, readme_results = rewrite_readme_text(readme_text)
            if args.apply and new_readme_text != readme_text:
                readme_path.write_text(new_readme_text, encoding="utf-8")

        before = build_census(file_results, readme_results, after=False)
        after = build_census(file_results, readme_results, after=True)
        report = render_census_md(before, after)
        print(report)

        census_out = args.census_out or (rdr_dir / DEFAULT_CENSUS_FILENAME)
        census_out.write_text(report, encoding="utf-8")
        print(f"[files] {'APPLIED' if args.apply else 'DRY-RUN'} — census written to {census_out}")

    if args.leg in ("t2", "both"):
        if not file_results:
            # t2-only invocation: computed read-only, regardless of --apply,
            # so a `--leg t2` call NEVER writes a file no matter the flags.
            file_results = compute_file_results(rdr_dir, apply=False)

        with _t2_client_cm(t2_client) as client:
            t2_report = run_t2_leg(client, args.project, file_results, apply=args.apply)
        print(render_t2_report(t2_report, applied=args.apply))
        if not args.apply:
            print("[t2] DRY-RUN — nothing written. Re-run with --apply only after explicit review.")
        else:
            print("[t2] APPLIED.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
