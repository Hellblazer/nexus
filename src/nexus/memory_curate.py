# SPDX-License-Identifier: AGPL-3.0-or-later
"""The report behind ``nx memory curate`` (RDR-238 Technical Design 4, nexus-qo7ie.17).

Three sections, ``--files``, ``--t2`` and ``--agents``; only the ``--files`` read counts read
transcripts (nexus-qo7ie.18). The fourth, ``--cold``, lives in :mod:`nexus.memory_curate_cold`
(nexus-qo7ie.19) and shares this module's window, session and path helpers. Each section
function returns its body lines; :func:`build_report` adds the headings and never lets one
section stop another. The report only lists. Nothing here deletes, quarantines, rewrites or
restores a file or a row; retiring anything is a person's decision.

``--files`` (over the directory :func:`nexus._hook_runtime.memory_dir.memory_dir` finds)
shares its parser with the SessionStart hook: unindexed files, files over 8 KB, index entries
naming a missing file, and declared ``replaces`` targets that still exist. Sam's decision S-7
(2026-11-03) gives it NO heuristic that infers a file replaced without saying so; it lists
likely near-duplicates for review instead, by the measure on :func:`near_duplicates`. Its read
counts (nexus-qo7ie.18, Technical Design 4 and 5) come from RDR-240's ``memory`` query through
``run_procedure`` (:func:`load_reads`): unread files over the window, T2 reads per title and per
project, or ``not available`` with the reason, never an empty list in place of an error.

``--t2`` lists live T2 rows that a later row names as replaced, skipping audit records (the
AUDIT_TAGS table, tags and title backstop), rows tagged ``superseded`` and titles that are only a
number, and RDR-209 Phase 3 Step 4's census flag (a phase is closed when its RDR's status is
closed, or when its own ``## Phase N`` to ``#### Phase N`` heading carries the marker the phase-review-gate close step
writes, nexus-qo7ie.28). It
reads every row of the repo's projects once (``get_all``), so a project with thousands of
rows costs one large read; run it at a release step, not in a loop.

``--agents`` checks each ``<config dir>/agent-memory/<agent>/`` directory that holds a
``MEMORY.md`` (the reviewers' own memory, ``memory: user``).

The stored report carries a 90-day TTL, which puts it on RDR-207's quarantine path: only a
rolled-up row is ever reaped, so old reports sit quarantined. That is accepted (gate round 3).
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from nexus._hook_runtime.memory_scan import (
    INDEX_FILES,
    OVERSIZE_BYTES,
    frontmatter_value,
    index_link_targets,
    index_mentions,
    read_text,
    scan,
)
from nexus.tables.audit_tags import audit_tags_of, is_audit_title, split_tags

#: A pair of memory files is a likely near-duplicate when the Jaccard overlap of their name and
#: description tokens is at least this. Deterministic, no model, no weights.
NEAR_DUP_THRESHOLD = 0.5
#: A file with fewer distinct tokens than this is not compared (two tiny files overlap by chance).
NEAR_DUP_MIN_TOKENS = 4
_STOPWORDS = frozenset(
    "the and for with that this from are was you your not but all any can has have its into "
    "will when what which they them their then than also only more some such".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")
_BULLET_START = re.compile(r"^\s*[-*](?:\s|$)")  # "- x", "* x", and a line cut to just "-" or "*"

#: The reviewer definitions say "keep its index under 40 lines", so an index of this many lines or more is flagged.
AGENT_INDEX_CAP = 40
CENSUS_DAYS = 30
_ENTRY = re.compile(r"^\s*[-*]\s*\[([^\]]*)\]\(([^)\s]+)\)")
# "Names a repo" (--agents), applied to an entry's index line and to the file it links. Three forms,
# each a candidate name that counts unless it is a generic word (_GENERIC_REPO_WORDS):
#   repo: <name>          (also "repository: <name>", "repo=<name>")
#   learned in <name>     (optionally "learned in the <name>", with or without a trailing "repo")
#   <name> repo           (also "<name> repository")
# "whole repo", "the Java repo", "any git repo", "this repo" and "learned in a session" name no repo.
# The generic list is open (a deny-list); no allow-list of known repo names was built, so an unlisted
# generic word still counts as a name. Known limit the other way: a repo whose name is one of those
# words ("go", "swift", "git", "main") never counts.
_REPO_FORMS = (
    re.compile(r"\brepo(?:sitory)?\s*[:=]\s*([\w./-]+)", re.IGNORECASE),
    re.compile(r"\blearned\s+in\s+(?:the\s+)?([\w.-]+)", re.IGNORECASE),
    re.compile(r"(?<![\w-])([\w.-]+)\s+repo(?:sitory)?\b", re.IGNORECASE),
)
_GENERIC_REPO_WORDS = frozenset(
    # determiners and quantifiers
    "the a an this that these those any each every one your their its our my which same another other "
    "no per all some both either neither whole entire full main whichever whatever "
    # the words around "repo" that are not names
    "repo repository repos repositories project projects codebase code git local remote upstream parent "
    "child target source current given first second last next previous "
    # where heuristics are learned, as opposed to which repo
    "session sessions practice general review reviews production testing ci prior new separate "
    "different earlier older recent past several multiple long "
    # languages and stacks ("the Java repo")
    "java python rust go golang kotlin scala typescript javascript node nodejs ruby php swift c cpp "
    "csharp dotnet shell bash sql".split()
)


def names_a_repo(text: str) -> bool:
    """True when *text* names a specific repository by one of the three forms above."""
    for form in _REPO_FORMS:
        for m in form.finditer(text):
            name = m.group(1).strip(".-_/").lower()
            if name and name not in _GENERIC_REPO_WORDS:
                return True
    return False


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if len(t) >= 2 and t not in _STOPWORDS}


def near_duplicates(
    directory: Path, threshold: float = NEAR_DUP_THRESHOLD
) -> list[tuple[str, str, float]]:
    """Pairs of memory files whose name and description tokens overlap, for a person to review.

    The measure: tokens are the lowercase ``[a-z0-9]+`` words of length 2 or more, minus a short
    stopword list, drawn from the frontmatter ``name`` (the file stem when absent) and
    ``description``. A pair is listed when ``|A & B| / |A | B| >= threshold`` and both sets hold
    at least :data:`NEAR_DUP_MIN_TOKENS` tokens. Pairs come back as ``(a, b, score)`` with
    ``a < b``, ordered by descending score then name. Report only: nothing is acted on.
    """
    sets: dict[str, set[str]] = {}
    for p in sorted(directory.glob("*.md")):
        if p.name in INDEX_FILES:
            continue
        text = read_text(p)
        name = frontmatter_value(text, "name") or p.stem.replace("_", " ").replace("-", " ")
        toks = _tokens(f"{name} {frontmatter_value(text, 'description')}")
        if len(toks) >= NEAR_DUP_MIN_TOKENS:
            sets[p.name] = toks
    names = sorted(sets)
    out: list[tuple[str, str, float]] = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            union = sets[a] | sets[b]
            score = len(sets[a] & sets[b]) / len(union)
            if score >= threshold:
                out.append((a, b, round(score, 2)))
    out.sort(key=lambda t: (-t[2], t[0], t[1]))
    return out


#: The longest window a read count asks for. Claude Code keeps transcripts about 30 days
#: (inferred from file dates, RDR-238 research 9); within that, the window is also capped at
#: the oldest session of this repository still on disk, so a shorter ``cleanupPeriodDays``
#: shows as a shorter covered span instead of a 30-day claim.
RETENTION_DAYS = 30
#: Pages a read-count window may take before the loop stops. Each page advances at least an
#: hour, so a 30-day window needs at most 720; a page that does not advance stops it at once.
MAX_READ_PAGES = 1000
#: Lines read from the top of a transcript looking for its first timestamped record.
_START_SCAN_LINES = 200
#: Memory-query kinds that count as a read of a memory file: a Read of it, and its loading
#: into a subagent's instructions. A Write or an Edit is not a read.
_FILE_READ_KINDS = frozenset({"file_read", "loaded"})
#: What the report says it counts, printed under the unread list (critic r1, nexus-qo7ie.18):
#: RDR-240's memory query sees the Read tool and subagent loads only.
READ_SCOPE_NOTE = ("counts a Read-tool open or a subagent load only; a file read with cat, grep "
                   "or sed through Bash, found by Grep, or attached to a prompt is not counted, and "
                   "neither is a session that started before the window and was resumed in it")


@dataclass(frozen=True)
class ReadWindow:
    """The UTC window a read count covers: ``[since, until)``, both ``YYYY-MM-DDTHH:MM:SSZ``."""

    since: str
    until: str
    requested: int
    covered: int
    #: Why ``covered`` is less than ``requested``: the retention cap or the oldest session.
    cap_reason: str = ""

    def describe(self) -> str:
        if self.covered < self.requested:
            span = f"requested {self.requested} days, capped at {self.covered}"
            if self.cap_reason:
                span += f": {self.cap_reason}"
        else:
            span = f"{self.covered} days"
        return f"covered {self.since} .. {self.until} ({span})"


_FMT = "%Y-%m-%dT%H:%M:%SZ"


def read_window(days: int, now: datetime, oldest: datetime | None = None) -> ReadWindow:
    """*days* back from *now*, capped at :data:`RETENTION_DAYS` and, when *oldest* (the
    oldest session start on disk) is later than that, at the whole days since it."""
    covered, reason = days, ""
    if covered > RETENTION_DAYS:
        covered, reason = RETENTION_DAYS, "transcripts are kept about 30 days"
    if oldest is not None:
        held = max(1, math.ceil((now - oldest).total_seconds() / 86400))
        if held < covered:
            covered, reason = held, f"the oldest session on disk started {oldest.strftime(_FMT)}"
    since = now - timedelta(days=covered)
    return ReadWindow(since.strftime(_FMT), now.strftime(_FMT), days, covered, reason)


@dataclass(frozen=True)
class ReadCounts:
    """The joined rows of RDR-240's ``memory`` query over one window."""

    window: ReadWindow
    rows: list[dict[str, Any]]
    truncated_records: int = 0
    #: Main sessions of this repository that started in the window: the sessions counted.
    sessions: int = 0


@dataclass(frozen=True)
class ReadsUnavailable:
    """Why a window's read counts could not be read; the report says so instead of listing."""

    window: ReadWindow
    reason: str


def _session_start(path: Path) -> datetime | None:
    """A main transcript's start by the verb's own rule (``verbs_transcripts._scan``): the
    first record that parses as a JSON object, is not a sidechain replay and carries a
    timestamp ``datetime.fromisoformat`` reads."""
    try:
        with path.open("rb") as fh:
            for _, raw in zip(range(_START_SCAN_LINES), fh, strict=False):
                try:
                    rec = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(rec, dict) or rec.get("isSidechain") is True:
                    continue
                stamp = rec.get("timestamp")
                if not isinstance(stamp, str) or len(stamp) > 64:
                    continue
                try:
                    t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                except ValueError:
                    continue
                return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
    except OSError:
        return None
    return None


def repo_session_starts(cwd: Path) -> list[datetime] | str:
    """The start of every main session of this repository still on disk, or why they could
    not be listed. The project directories come from the runtime's ``repo_project_dirs``,
    the rule project ``repo`` uses, and the files are the verb's own: top-level ``*.jsonl``,
    links excluded. Only the first records are read; this is no transcript scanner."""
    from nexus.lua_runtime.transcripts_projects import repo_project_dirs  # noqa: PLC0415 — deferred: only a read count pays

    root = Path(os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")) / "projects"
    if not root.is_dir():
        return f"no Claude Code projects directory at {root}"
    try:
        names = repo_project_dirs(root, cwd, deadline=time.monotonic() + 30)
    except Exception as exc:  # noqa: BLE001 — a failed listing is a reason, never "no reads"
        return f"the repository's project directories could not be listed: {type(exc).__name__}: {exc}"
    starts: list[datetime] = []
    for name in names:
        if (root / name).is_symlink():
            continue  # the verb reads plain directories only (verbs_transcripts._plain_dir)
        for f in sorted((root / name).glob("*.jsonl")):
            if f.is_symlink() or not f.is_file():
                continue
            start = _session_start(f)
            if start is not None:
                starts.append(start)
    return starts


def _unmatchable(directory: Path) -> str | None:
    """Why RDR-240's memory query cannot see reads of *directory*, or None. The query marks a
    Read as a memory-file read only when its path contains ``/memory/`` (transcripts.lua
    ``is_memory_path``): a relocated ``autoMemoryDirectory`` or a Windows path never matches."""
    if os.name == "nt":
        return "the memory query matches '/'-separated paths only, and Windows paths use '\\'"
    if "/memory/" not in directory.as_posix() + "/":
        return f"the memory query only sees paths containing /memory/, and {directory} has none"
    return None


def load_reads(days: int, now: datetime, directory: Path, cwd: Path | None = None) -> ReadCounts | ReadsUnavailable:
    """RDR-240's ``memory`` query for this repository (project ``repo``: the main checkout and
    its worktrees) over *days* back from *now*, every page joined; or why it could not be read.

    Never an empty count in place of an error, since an empty count lists every file as
    unread. The counts are unavailable when the query cannot see *directory*'s paths, or when
    no session of this repository started in the window (the verb counts a session in the
    window that holds its first record, so a session resumed in the window but started before
    it is not counted, and with none in the window there is nothing to count). Called from
    Python only (RDR-238 Technical Design 5), so ``nx memory curate`` adds no Lua.
    """
    from nexus.memory_curate_cold import query_pages  # noqa: PLC0415 — deferred: only a read count pays for the runtime

    starts = repo_session_starts(cwd or Path.cwd())
    window = read_window(days, now, min(starts) if isinstance(starts, list) and starts else None)
    if isinstance(starts, str):
        return ReadsUnavailable(window, starts)
    blocked = _unmatchable(directory)
    if blocked:
        return ReadsUnavailable(window, blocked)
    lo = datetime.strptime(window.since, _FMT).replace(tzinfo=timezone.utc)
    counted = sum(1 for s in starts if lo <= s < now)
    if not counted:
        return ReadsUnavailable(window, "no session of this repository started in the window")
    result = query_pages("memory", window.since, window.until, MAX_READ_PAGES)
    if isinstance(result, str):
        return ReadsUnavailable(window, result)
    return ReadCounts(window, result[0], result[1], counted)


def _same_file(name: str, directory: Path) -> str | None:
    """The file name in *directory* that the read path *name* names, or None."""
    path = Path(name)
    if not path.is_absolute():
        return None
    try:
        same = path.parent.resolve() == directory.resolve()
    except OSError:
        return None
    return path.name if same else None


def file_read_counts(reads: ReadCounts, directory: Path) -> dict[str, int]:
    """Reads per memory file in *directory* over the window, summed over sessions."""
    counts: dict[str, int] = {}
    for row in reads.rows:
        if row.get("kind") in _FILE_READ_KINDS and isinstance(row.get("name"), str):
            name = _same_file(row["name"], directory)
            if name is not None:
                counts[name] = counts.get(name, 0) + int(row.get("count") or 0)
    return counts


def _summed(reads: ReadCounts, kind: str) -> list[str]:
    totals: dict[str, int] = {}
    for row in reads.rows:
        if row.get("kind") == kind and isinstance(row.get("name"), str):
            totals[row["name"]] = totals.get(row["name"], 0) + int(row.get("count") or 0)
    return [f"{name} ({n})" for name, n in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))]


def read_lines(directory: Path, files: list[str], reads: ReadCounts | ReadsUnavailable | None) -> list[str]:
    """The ``--files`` read-count rows: unread files over the window, then T2 reads per title
    (``memory_get``) and searches per project (``memory_search``; a search names no title)."""
    if reads is None:
        return ["unread files: not available (no read counts were given)"]
    if isinstance(reads, ReadsUnavailable):
        return [f"read counts: {reads.window.describe()}",
                f"unread files: not available: {reads.reason}", f"T2 read counts: not available: {reads.reason}"]
    counts = file_read_counts(reads, directory)
    candidates = sorted(f for f in files if f not in INDEX_FILES)
    unread = [f for f in candidates if counts.get(f, 0) == 0]
    started = f"{reads.sessions} session{'' if reads.sessions == 1 else 's'} started in it"
    lines = [f"read counts: {reads.window.describe()}, {started}"]
    if reads.truncated_records:
        lines.append(f"read counts: {reads.truncated_records} records were cut, so these counts are floors")
    label = "no Read-tool open or subagent load in any session that started in the window"
    if unread:
        lines.append(f"{label}: {len(unread)} of {len(candidates)} files (index files excluded)")
        lines += [f"  - {f}" for f in unread]
    else:
        lines.append(f"{label}: none")
    lines.append(f"  ({READ_SCOPE_NOTE})")
    lines += _listing("T2 reads per title (memory_get)", _summed(reads, "t2_get"))
    lines += _listing("T2 searches per project (memory_search)", _summed(reads, "t2_search"))
    return lines


def files_section(directory: Path | None, reads: ReadCounts | ReadsUnavailable | None = None) -> list[str]:
    if directory is None:
        return ["not available: no memory directory found for this repository"]
    result = scan(directory)
    lines = [f"directory: {directory}", f"files: {len(result.files)} (index files: {', '.join(result.index_files) or 'none'})"]
    lines += _listing("unindexed", list(result.unindexed))
    lines += _listing(
        f"oversized (over {OVERSIZE_BYTES} bytes, index files excluded)",
        [f"{n} ({size} bytes)" for n, size in result.oversized],
        key="oversized",
    )
    lines += _listing("index entries naming a missing file", [f"{idx}: {t}" for idx, t in result.dangling], key="dangling")
    lines += _listing(
        "declared replaces still present (delete the old file)",
        [f"{src} replaces {slug} -> {tgt}" for src, slug, tgt in result.replaced_present],
        key="replaced",
    )
    pairs = near_duplicates(directory)
    lines += _listing(
        f"likely near-duplicates (name+description token Jaccard >= {NEAR_DUP_THRESHOLD}; review only)",
        [f"{a} ~ {b} ({score})" for a, b, score in pairs],
        key="near-duplicates",
    )
    lines += read_lines(directory, list(result.files), reads)
    return lines


def _listing(label: str, items: list[str], key: str | None = None) -> list[str]:
    short = key or label
    if not items:
        return [f"{short}: none"]
    return [f"{label}: {len(items)}"] + [f"  - {i}" for i in items]


# ── --agents ─────────────────────────────────────────────────────────────────────

@dataclass
class AgentFindings:
    name: str
    line_count: int = 0
    over_cap: bool = False
    entries: int = 0
    no_repo: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    truncated: str | None = None
    dangling: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.over_cap or self.no_repo or self.duplicates or self.truncated
                    or self.dangling or self.orphans)


def scan_agent_dir(directory: Path) -> AgentFindings:
    """Check one reviewer memory directory. The damage signs (two sessions, one MEMORY.md,
    last writer wins) are: a duplicate entry (an identical line, or two lines linking one
    file); a truncated last line (no final newline and an entry that does not close); a
    dangling entry (links a file the directory lacks); an orphan file (no index line names it).
    """
    index = read_text(directory / "MEMORY.md")
    lines = index.splitlines()
    out = AgentFindings(name=directory.name, line_count=len(lines))
    out.over_cap = out.line_count >= AGENT_INDEX_CAP

    seen_lines: set[str] = set()
    titles_by_target: dict[str, list[str]] = {}
    for line in lines:
        norm = " ".join(line.split())
        if not norm:
            continue
        if norm in seen_lines:
            out.duplicates.append(f"repeated line: {norm}")
            continue
        seen_lines.add(norm)
        m = _ENTRY.match(line)
        if not m:
            continue
        out.entries += 1
        title, target = m.group(1), m.group(2)
        titles_by_target.setdefault(target, []).append(title)
        linked = directory / target if "://" not in target else None
        body = read_text(linked) if linked is not None and linked.is_file() else ""
        if not names_a_repo(line) and not names_a_repo(body):
            out.no_repo.append(title)
    for target, titles in titles_by_target.items():
        if len(titles) > 1:
            out.duplicates.append(f"{len(titles)} entries link {target}: {', '.join(titles)}")

    if index and not index.endswith("\n"):
        last = lines[-1] if lines else ""
        if _BULLET_START.match(last) and not _ENTRY.match(last):
            out.truncated = last

    out.dangling = [t for t in index_link_targets(index) if not (directory / t).is_file()]
    mentioned = index_mentions(index)
    out.orphans = sorted(
        p.name for p in directory.glob("*.md") if p.name != "MEMORY.md" and p.name not in mentioned
    )
    return out


def agents_section(root: Path | None) -> list[str]:
    if root is None:
        return ["not available: no agent-memory directory"]
    if not root.is_dir():
        return [f"not available: {root} does not exist"]
    found = [scan_agent_dir(d) for d in sorted(root.iterdir()) if d.is_dir() and (d / "MEMORY.md").is_file()]
    lines = [f"directories scanned: {len(found)}"]
    for f in found:
        head = f"{f.name}: {f.line_count} index lines (keep under {AGENT_INDEX_CAP}), {f.entries} entries"
        if f.clean:
            lines.append(f"{head}, clean")
            continue
        lines.append(head)
        if f.over_cap:
            lines.append(f"  - {f.line_count} lines; the index must stay under {AGENT_INDEX_CAP}")
        lines += [f"  - entry names no repo: {t}" for t in f.no_repo]
        lines += [f"  - duplicate entry (concurrent-write damage): {d}" for d in f.duplicates]
        if f.truncated is not None:
            lines.append(f"  - truncated last line (concurrent-write damage): {f.truncated}")
        lines += [f"  - dangling entry (concurrent-write damage): links missing {t}" for t in f.dangling]
        lines += [f"  - orphan file (concurrent-write damage): {t} is named by no index line" for t in f.orphans]
    return lines


# ── --t2 ─────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Replaced:
    project: str
    title: str
    named_by: str


@dataclass(frozen=True)
class CensusHit:
    project: str
    title: str
    rdr: str
    age_days: int
    phase: str | None = None  # set when the flag comes from a closed phase of an open RDR


@dataclass
class T2Findings:
    projects: list[str] = field(default_factory=list)
    rows_read: int = 0
    replaced: list[Replaced] = field(default_factory=list)
    skipped_audit: int = 0
    census: list[CensusHit] = field(default_factory=list)
    census_unmapped: int = 0


_NAMES_REPLACED = re.compile(r"\b(?:replaces|supersedes)\b\s*:?\s*(.+)", re.IGNORECASE)
_STRIP = " \t\"'`()[]{}.:;,"
_RDR_TAG = re.compile(r"^rdr:0*(\d+)$")
_RDR_TITLE = re.compile(r"^(?:rdr-)?0*(\d+)(?!\d)", re.IGNORECASE)
#: A row titled "100" or "RDR-107" is never a replaced candidate: an incidental number after "supersedes"
#: in a research finding names an RDR, not a row (measured on the first live run: 2 of 6 candidates).
_BARE_NUMBER_TITLE = re.compile(r"^(?:rdr-)?[0-9]+$", re.IGNORECASE)
#: A title piece naming a phase: ``-p3-``, ``-p3a-`` (sub-phase 3a), ``-phase-2-``, ``-phase2-``.
_PHASE_TITLE = re.compile(r"(?:^|[-_])(?:p|phase[-_]?)0*(\d+[a-z]?)(?=[-_]|$)", re.IGNORECASE)
#: A phase heading in an RDR: ``## Phase 3`` to ``#### Phase 3`` (``# Phase`` is a document title); a dotted
#: ``Phase 2.5`` is not Phase 2.
_PHASE_HEADING = re.compile(r"^#{2,4}\s+Phase\s+0*(\d+[a-z]?)(?![.\d])\b(.*)$", re.IGNORECASE)
#: The close marker: a parenthetical or a dash-led CLOSED, SHIPPED, DONE or COMPLETE, as the phase-review-gate
#: close step writes it (``### Phase 3 (CLOSED, 2026-11-06)``) and rdr-222 and rdr-227 already do. A marker word in
#: the heading's own title (``Phase 2: Complete the migration``) is not one.
_PHASE_CLOSED_MARK = re.compile(
    r"(?:\(\s*(?:closed|shipped|done|complete)\b(?=\s*(?:[,;)]|with\b|\d{4}-))"   # (CLOSED, ...) (shipped with X)
    r"|(?:[—–]|\s-)\s*(?:closed|shipped|done|complete)\b(?=\s*(?:$|[,;(]|\d{4}-)))",  # — done 2026-10-09
    re.IGNORECASE)


def _phases_of(title: str) -> list[str]:
    """The phases a gate record's title names (lower case, sub-phase letter kept)."""
    return [m.lower() for m in _PHASE_TITLE.findall(title)]


def _all_closed(named: list[str], closed: set[str]) -> bool:
    """True when the title names at least one phase and each is closed, or a sub-phase of a closed phase."""
    return bool(named) and all(p in closed or p.rstrip("abcdefghijklmnopqrstuvwxyz") in closed for p in named)


def _named_titles(title: str, content: str) -> set[str]:
    """Candidate titles a row names as replaced: what follows ``replaces``/``supersedes`` on a line."""
    out: set[str] = set()
    for line in f"{title}\n{content}".splitlines():
        for rest in _NAMES_REPLACED.findall(line):
            out.update(re.findall(r"`([^`]+)`", rest))
            out.add(rest.strip(_STRIP))
            for piece in re.split(r"[;,]", rest):
                out.add(piece.strip(_STRIP))
    out.discard("")
    return out


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _repo_projects(db: Any, repo: str) -> list[str]:
    rows = db.get_projects_with_prefix(repo)
    names = {(r.get("project") if isinstance(r, dict) else r) for r in rows}
    return sorted(n for n in names if n == repo or (n and n.startswith(f"{repo}_")))


def t2_findings(db: Any, repo: str, *, closed_rdrs: set[str], now: datetime,
                closed_phases: dict[str, set[str]] | None = None) -> T2Findings:
    """The ``--t2`` findings. *db* is a ``T2Database``; reads only, writes nothing. *closed_phases*
    maps an RDR number to its closed phases (:func:`closed_phase_numbers`)."""
    result = T2Findings(projects=_repo_projects(db, repo))
    cutoff = now - timedelta(days=CENSUS_DAYS)
    census_projects = {repo, f"{repo}_rdr"}
    for project in result.projects:
        rows = list(db.get_all(project))  # live rows only: the engine's getAll hides quarantined ones
        result.rows_read += len(rows)
        by_title = {r["title"]: r for r in rows}
        named: dict[str, list[dict[str, Any]]] = {}
        for later in rows:
            for cand in _named_titles(later["title"], later.get("content") or ""):
                earlier = by_title.get(cand)
                if earlier is None or earlier["id"] == later["id"] or _BARE_NUMBER_TITLE.match(cand):
                    continue
                if (later.get("timestamp") or "") > (earlier.get("timestamp") or ""):
                    named.setdefault(earlier["title"], []).append(later)
        for title in sorted(named):
            earlier = by_title[title]
            if "superseded" in split_tags(earlier.get("tags")):
                continue  # already marked as replaced: nothing to confirm
            if audit_tags_of(earlier.get("tags")) or is_audit_title(title):
                result.skipped_audit += 1
                continue
            namer = min(r["title"] for r in named[title])
            result.replaced.append(Replaced(project, title, namer))

        if project in census_projects:
            _census(result, project, repo, rows, closed_rdrs, closed_phases or {}, cutoff, now)
    return result


def _census(
    result: T2Findings, project: str, repo: str, rows: list[dict[str, Any]],
    closed: set[str], closed_phases: dict[str, set[str]], cutoff: datetime, now: datetime,
) -> None:
    """RDR-209 Phase 3 Step 4: gate records older than 30 days, no TTL, phase closed.

    A gate record is a row whose whole tags include ``gate`` or ``kind:gate``. Its RDR comes from the
    ``rdr:<NNN>`` tag or, in ``<repo>_rdr``, the leading number of its title. It is flagged when that
    RDR's frontmatter ``status`` is ``closed``, or (nexus-qo7ie.28) when every phase its title names
    (``-p<N>``, ``-phase-<N>``) is a closed phase of that RDR (:func:`closed_phase_numbers`). A record
    that maps to no RDR is counted as unmapped and never flagged; one of an open RDR whose title names
    no phase is not judged.
    """
    for r in rows:
        tags = split_tags(r.get("tags"))
        if "gate" not in tags and "kind:gate" not in tags:
            continue
        stamp = _parse_ts(r.get("timestamp") or "")
        if stamp is None or stamp >= cutoff or r.get("ttl") is not None:
            continue
        number = next((m.group(1) for t in tags if (m := _RDR_TAG.match(t))), None)
        if number is None and project == f"{repo}_rdr":
            m = _RDR_TITLE.match(r["title"])
            number = m.group(1) if m else None
        if number is None:
            result.census_unmapped += 1
        elif number in closed:
            result.census.append(CensusHit(project, r["title"], number, (now - stamp).days))
        elif _all_closed(named := _phases_of(r["title"]), closed_phases.get(number, set())):
            result.census.append(CensusHit(project, r["title"], number, (now - stamp).days, ", ".join(named)))


def repo_toplevel(cwd: Path) -> Path:
    """The working tree root containing *cwd* (a linked worktree's own root), else *cwd*."""
    from nexus.bounded_subprocess import run_bounded  # noqa: PLC0415 — deferred: only this lookup needs it
    from nexus.util.nx_argv import argv_off_cwd  # noqa: PLC0415 — deferred with run_bounded

    try:
        proc = run_bounded(argv_off_cwd(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"]), timeout=5)
    except Exception:  # noqa: BLE001 — no git, a timeout or a non-repository: fall back to *cwd*
        return cwd
    top = proc.stdout.strip() if proc.returncode == 0 else ""
    return Path(top) if top else cwd


def closed_rdr_numbers(root: Path) -> set[str]:
    """Numbers (no leading zeros) of the RDRs under *root* whose frontmatter status is ``closed``."""
    rdr_dir = _rdr_dir(root)
    out: set[str] = set()
    try:
        files = sorted(rdr_dir.glob("rdr-*.md"))
    except OSError:
        return out
    for p in files:
        m = re.match(r"^rdr-0*(\d+)(?:-|$)", p.stem)
        if m and frontmatter_value(read_text(p), "status") == "closed":
            out.add(m.group(1))
    return out


def closed_phase_numbers(root: Path) -> dict[str, set[str]]:
    """RDR number -> the phases whose own heading marks them closed (nexus-qo7ie.28).

    The marker is the one the phase-review-gate close step writes and rdr-222 and rdr-227 already
    carry: ``### Phase 3 (CLOSED, 2026-11-06)``, ``### Phase 0 (SHIPPED, ...)``,
    ``#### Phase 4: Measure ... — done 2026-10-09``. Prose that says a phase closed is not read.
    """
    out: dict[str, set[str]] = {}
    try:
        files = sorted(_rdr_dir(root).glob("rdr-*.md"))
    except OSError:
        return out
    for p in files:
        m = re.match(r"^rdr-0*(\d+)(?:-|$)", p.stem)
        if not m:
            continue
        for line in read_text(p).splitlines():
            h = _PHASE_HEADING.match(line)
            if h and _PHASE_CLOSED_MARK.search(h.group(2)):
                out.setdefault(m.group(1), set()).add(h.group(1).lower())
    return out


def _rdr_dir(root: Path) -> Path:
    cfg = root / ".nexus.yml"
    if cfg.is_file():
        try:
            import yaml  # noqa: PLC0415 — deferred: only a repo with a .nexus.yml pays for it

            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            paths = (data.get("indexing") or {}).get("rdr_paths") or []
            if isinstance(paths, str):
                paths = [paths]
            if paths:
                return root / paths[0]
        except Exception:  # noqa: BLE001 — a malformed .nexus.yml falls back to the default location
            pass
    return root / "docs" / "rdr"


def t2_section(db: Any, repo: str, *, closed_rdrs: set[str], now: datetime,
               closed_phases: dict[str, set[str]] | None = None) -> list[str]:
    f = t2_findings(db, repo, closed_rdrs=closed_rdrs, closed_phases=closed_phases, now=now)
    lines = [
        f"projects read: {', '.join(f.projects) or 'none'} ({f.rows_read} live rows)",
        "report only: this list is for a person to confirm; nothing is deleted or quarantined",
    ]
    lines += _listing(
        "replaced candidates (a later row names them as replaced)",
        [f"{r.project}/{r.title} (named by {r.named_by})" for r in f.replaced],
        key="replaced candidates",
    )
    lines.append(f"skipped as audit records (AUDIT_TAGS tag or title): {f.skipped_audit}")
    lines += _listing(
        f"census: gate records older than {CENSUS_DAYS} days, no TTL, RDR or phase closed",
        [f"{c.project}/{c.title} (RDR-{c.rdr}{f' Phase {c.phase}' if c.phase else ''}, {c.age_days} days old)"
         for c in f.census],
        key="census",
    )
    if f.census_unmapped:
        lines.append(f"census: {f.census_unmapped} old gate record(s) with no TTL map to no RDR and were not judged")
    return lines


# ── the report ───────────────────────────────────────────────────────────────────

def section(title: str, body: Any) -> list[str]:
    """Run *body* (a no-argument callable returning lines); a failure becomes one line."""
    try:
        lines = body()
    except Exception as exc:  # noqa: BLE001 — a section failing never stops the report or the exit status
        lines = [f"not available: {type(exc).__name__}: {exc}" if str(exc) else f"not available: {type(exc).__name__}"]
    return [f"== {title} ==", *lines, ""]


def report_title(now: datetime, *, files: bool, t2: bool, agents: bool, cold: bool) -> str:
    """The T2 title the report is stored under.

    A run with --files, --t2 and --agents (the default; --cold may ride along) is the day's full
    report: ``curation-report-<date>``. A run of fewer sections stores
    ``curation-report-<date>-<sections>`` (the selected sections in files, t2, agents, cold order,
    joined with ``-``), so a partial run never replaces the full report that a T2 upsert on
    (project, title) would otherwise overwrite.
    """
    base = f"curation-report-{now:%Y-%m-%d}"
    if files and t2 and agents:
        return base
    chosen = [n for n, on in (("files", files), ("t2", t2), ("agents", agents), ("cold", cold)) if on]
    return f"{base}-{'-'.join(chosen)}"
