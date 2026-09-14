"""T2 retention and tool-signature guidance stays accurate in model-facing
plugin text (nexus-cnzei.3).

O(repo) meta-tests, hence the lint marker. Three doctrines these checks
protect, each reversed or clarified after guidance had already taught the
old shape:

- ``memory_put`` AND ``plan_save`` share one ``ttl: int | None = None``
  contract (``mcp/core.py``), reversed 2026-09-12 (nexus-473mx): omitting
  ``ttl`` now means permanent, and the engine REJECTS ``ttl<=0`` (0
  included) outright with a 400 — there is no "0 means permanent"
  coercion. ``ttl=30`` for every write is the retired default, not a
  convention worth reproducing in new guidance. And the type is ``int``,
  never a string — ``ttl="permanent"`` or ``ttl="30d"`` is ``store_put``'s
  contract (``ttl: str``, ``"Nd"``/``"Nw"``/``"permanent"``) leaking into
  the wrong tool; a real nexus-cnzei.3 fix round found ``ttl="permanent"``
  prescribed at 15 sites across the RDR skill/command family, none caught
  by this lint's first cut because it only matched the two retired bare
  integers.
- ``query()`` (``mcp/core.py``) has no ``topic`` parameter — only
  ``search()`` and the ``search_*_scoped`` tools do. ``query()`` filters by
  ``content_type`` / ``author`` / ``follow_links`` / ``subtree`` instead.
- ``nx_tidy`` (``mcp/core.py``) is read-only: it reports a consolidated
  summary and performs no writes. The T3 write a tidy workflow ends in is a
  separate ``store_put`` call, never something ``nx_tidy`` itself does.

A reader who follows stale guidance for any of these gets a 400, a
validation error, or a false belief that a review is unnecessary because
"nx_tidy already persisted it".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONEXUS_DIR = REPO_ROOT / "conexus"

SCAN_DIRS = [
    CONEXUS_DIR / "skills",
    CONEXUS_DIR / "agents",
    CONEXUS_DIR / "commands",
    CONEXUS_DIR / "resources",
]

#: Floor for the non-vacuity assert (nexus-moht0 doctrine: a sweep that
#: found nothing to check is a failure, not a pass). 99 real files exist
#: under SCAN_DIRS as of this writing; the floor is well below that so a
#: reorganisation does not need to touch this test, but well above zero so
#: a broken glob or a moved directory cannot pass silently.
MIN_SCANNED_FILES = 60

pytestmark = pytest.mark.lint

#: (relative path, violation kind, matched snippet) -> reason a human can
#: check. Add a row only when the text is deliberately showing the
#: *retired* or *rejected* form for explanatory purposes (e.g. "ttl=0 is
#: refused with a 400") — fix the guidance instead when it is prescribing
#: the bad call. test_allowlist_entries_are_all_still_live() fails on a
#: stale entry, so an exemption cannot silently outlive the text it excuses.
ALLOWLIST: dict[tuple[str, str, str], str] = {}

#: memory_put and plan_save share the ttl: int | None contract; store_put's
#: ttl is a genuinely different, string-typed contract ("Nd"/"Nw"/
#: "permanent") and stays OUT of scope for this check on purpose.
#:
#: Anchored on the bare NAME, not `name(` — this repo's PRODUCE-section
#: guidance routinely writes "via memory_put tool: project=..., ttl=..."
#: with no call-syntax parens at all, and that prose form carries the exact
#: same ttl defect as a real call. Safe to widen: a bare mention with no
#: ttl anywhere in its window (fenced block or line) never matches
#: _BAD_TTL_RE, so this only ever adds true positives, never false ones.
#:
#: NO leading \b: every real call in this repo is fully-qualified
#: (``mcp__plugin_conexus_nexus__memory_put(``), so the character right
#: before the name is ``_`` — a word character — and a leading \b would
#: never fire there, silently missing every prefixed call and leaving
#: only the unprefixed prose mentions matched. Trailing \b stays, to keep
#: this from matching as a prefix of some unrelated longer identifier.
_TTL_BEARING_CALL_RE = re.compile(r"(?:memory_put|plan_save)\b")
_QUERY_CALL_RE = re.compile(r"query\(")
#: Flags two distinct wrong shapes for memory_put/plan_save's ttl:
#: (a) the retired bare-int literals 0 and 30, any spacing around `=`;
#: (b) ANY quoted value at all (ttl="30", ttl='permanent', ttl="30d") —
#: the type itself is wrong, since ttl here is int | None, never a string.
_BAD_TTL_RE = re.compile(
    r"""\bttl\s*=\s*(?:
        (?:0|30)\b(?!\d)
        |
        ["'][^"'\n]*["']
    )""",
    re.VERBOSE,
)
_TOPIC_KWARG_RE = re.compile(r"\btopic\s*=")

#: Historical wrong phrasings that asserted nx_tidy performs the T3 write
#: itself (found in commands/knowledge-tidy.md and
#: skills/using-nx-skills/SKILL.md before nexus-cnzei.3). nx_tidy is
#: read-only; store_put is the write.
_NX_TIDY_T3_WRITE_PHRASES = [
    "Persist and organize knowledge into the T3 store using mcp__plugin_conexus_nexus__nx_tidy",
    "`/conexus:knowledge-tidy` to T3 (permanent, cross-project)",
]


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for d in SCAN_DIRS:
        if d.exists():
            files.extend(sorted(d.rglob("*.md")))
    return files


def _in_fenced_block(text: str, pos: int) -> bool:
    """Whether *pos* sits inside a ``` fenced code block.

    An odd number of ``` fence markers before *pos* means the position is
    inside an open fence.
    """
    return text.count("```", 0, pos) % 2 == 1


def _window(text: str, start: int, size: int = 400) -> str:
    """A bounded slice of *text* after *start*, scoped to avoid bleed.

    A real multi-line call in this repo's guidance only ever spans
    multiple lines inside a ``` fenced code block; every other occurrence
    (inline code spans in prose or markdown table cells) is single-line.
    Table rows in particular are newline-separated with NO blank line
    between them, so a plain size-capped window bleeds a match into the
    NEXT, unrelated table row describing a different tool. Scope the
    window to the fence (cut at the closing fence or a blank line) when
    inside one, and to the current line otherwise.

    The non-fenced branch does NOT also apply *size* — the newline is
    already the correct boundary, and a single very long guidance line
    (a memory_put call whose ``content=`` argument runs to hundreds of
    chars before reaching ``ttl=``) must not be truncated away from its
    own ttl kwarg. *size* only bounds the fenced branch, where an
    unrelated later call could otherwise share the same fence.
    """
    if _in_fenced_block(text, start):
        end = min(len(text), start + size)
        fence = text.find("```", start)
        if 0 <= fence < end:
            end = fence
        blank = text.find("\n\n", start)
        if 0 <= blank < end:
            end = blank
        return text[start:end]
    newline = text.find("\n", start)
    end = newline if newline != -1 else len(text)
    return text[start:end]


def _find_bad_ttl_calls(text: str) -> list[str]:
    hits = []
    for call in _TTL_BEARING_CALL_RE.finditer(text):
        window = _window(text, call.start())
        for bad in _BAD_TTL_RE.finditer(window):
            hits.append(window[max(0, bad.start() - 20) : bad.end() + 5])
    return hits


def _find_query_topic_calls(text: str) -> list[str]:
    hits = []
    for call in _QUERY_CALL_RE.finditer(text):
        window = _window(text, call.start(), size=200)
        for bad in _TOPIC_KWARG_RE.finditer(window):
            hits.append(window[: bad.end() + 10])
    return hits


def _find_nx_tidy_t3_write_claims(text: str) -> list[str]:
    return [phrase for phrase in _NX_TIDY_T3_WRITE_PHRASES if phrase in text]


def _all_findings() -> dict[tuple[str, str, str], None]:
    """Every (path, kind, hit) currently present, allowlisted or not."""
    findings: dict[tuple[str, str, str], None] = {}
    for path in _scan_files():
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        for hit in _find_bad_ttl_calls(text):
            findings[(rel, "bad-ttl", hit)] = None
        for hit in _find_query_topic_calls(text):
            findings[(rel, "query-topic", hit)] = None
        for hit in _find_nx_tidy_t3_write_claims(text):
            findings[(rel, "nx_tidy-t3-write", hit)] = None
    return findings


@pytest.fixture(scope="module")
def scanned_files() -> list[Path]:
    files = _scan_files()
    assert len(files) >= MIN_SCANNED_FILES, (
        f"only {len(files)} model-facing .md files found under "
        f"{[str(d) for d in SCAN_DIRS]}; the scan itself is broken "
        f"(non-vacuity guard, nexus-moht0 doctrine) rather than the "
        f"guidance being clean"
    )
    return files


def _unallowed(files: list[Path], finder, kind: str) -> list[str]:
    problems = []
    for path in files:
        rel = str(path.relative_to(REPO_ROOT))
        text = path.read_text(encoding="utf-8")
        for hit in finder(text):
            if (rel, kind, hit) in ALLOWLIST:
                continue
            problems.append(f"{rel}: {hit!r}")
    return problems


def test_memory_put_and_plan_save_never_prescribe_a_bad_ttl(scanned_files: list[Path]) -> None:
    problems = _unallowed(scanned_files, _find_bad_ttl_calls, "bad-ttl")
    assert not problems, (
        "memory_put(...) and plan_save(...) examples must omit ttl for "
        "permanent (nexus-473mx) and must never prescribe ttl=0 (rejected "
        "with a 400 — there is no 0-means-permanent coercion), ttl=30 (the "
        "retired default, not a convention to reproduce), or ANY quoted ttl "
        "value (ttl is int | None on both tools — a string like "
        "\"permanent\" or \"30d\" is store_put's contract, not theirs):\n"
        + "\n".join(problems)
    )


def test_query_never_takes_a_topic_kwarg(scanned_files: list[Path]) -> None:
    problems = _unallowed(scanned_files, _find_query_topic_calls, "query-topic")
    assert not problems, (
        "query() has no topic parameter (mcp/core.py) — only search() and "
        "the search_*_scoped tools do. query() filters by content_type / "
        "author / follow_links / subtree instead:\n" + "\n".join(problems)
    )


def test_nx_tidy_is_never_described_as_the_t3_write(scanned_files: list[Path]) -> None:
    problems = _unallowed(scanned_files, _find_nx_tidy_t3_write_claims, "nx_tidy-t3-write")
    assert not problems, (
        "nx_tidy (mcp/core.py) is read-only — it reports a consolidated "
        "summary and performs no writes. The T3 write a tidy workflow ends "
        "in is a separate store_put call:\n" + "\n".join(problems)
    )


def test_allowlist_entries_are_all_still_live() -> None:
    """A stale allowlist entry means the text it excused already changed.

    Mirrors test_docs_reference_rot.py's dead-SHA-allowlist check: an
    exemption that matches nothing is masking a fix that already landed,
    not documenting a live exception.
    """
    live = _all_findings()
    dead = sorted(key for key in ALLOWLIST if key not in live)
    assert not dead, f"ALLOWLIST entries no longer match anything found: {dead}"
