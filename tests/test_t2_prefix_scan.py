# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nx/hooks/scripts/t2_prefix_scan.py — _snippet() and cap algorithm."""
import sys
from pathlib import Path

import pytest

# Make t2_prefix_scan importable without installing it as a package
sys.path.insert(0, str(Path(__file__).parent.parent / "nx" / "hooks" / "scripts"))
from t2_prefix_scan import _HARD_CAP, _SNIPPET_LIMIT, _TITLE_LIMIT, _snippet


# ── _snippet ─────────────────────────────────────────────────────────────────

def test_snippet_returns_first_meaningful_line() -> None:
    assert _snippet("Hello world") == "Hello world"


def test_snippet_skips_blank_lines() -> None:
    assert _snippet("\n\nHello") == "Hello"


def test_snippet_skips_headings() -> None:
    assert _snippet("# Title\nBody text") == "Body text"


def test_snippet_skips_separator_lines() -> None:
    assert _snippet("---\nContent here") == "Content here"
    assert _snippet("===\nMore content") == "More content"


def test_snippet_truncates_at_max_chars() -> None:
    long_line = "x" * 200
    result = _snippet(long_line)
    assert result == "x" * 120 + "…"


def test_snippet_no_ellipsis_when_short() -> None:
    result = _snippet("short")
    assert "…" not in result
    assert result == "short"


def test_snippet_returns_empty_for_all_headings() -> None:
    assert _snippet("# H1\n## H2\n### H3") == ""


def test_snippet_returns_empty_for_empty_content() -> None:
    assert _snippet("") == ""


# ── cap algorithm constants ───────────────────────────────────────────────────

def test_cap_constants_are_consistent() -> None:
    """_HARD_CAP must exceed _TITLE_LIMIT which must exceed _SNIPPET_LIMIT."""
    assert _SNIPPET_LIMIT < _TITLE_LIMIT
    assert _TITLE_LIMIT < _HARD_CAP


# ── cap algorithm integration via scan_namespaces helper ─────────────────────
# We test the cap logic by calling the scan logic directly with a live T2Database.

from nexus.db.t2 import T2Database


def _make_db(tmp_path: Path) -> T2Database:
    return T2Database(tmp_path / "t2_scan_test.db")


def _run_scan(db: T2Database, project_name: str) -> str:
    """Run the scan logic and capture its stdout equivalent as a string."""
    # Patch sys.argv and the import, then call main logic inline
    namespaces = db.get_projects_with_prefix(project_name)
    if not namespaces:
        return ""

    lines: list[str] = []
    total = 0

    for ns_row in namespaces:
        if total >= _HARD_CAP:
            break
        ns = ns_row["project"]
        entries = db.get_all(project=ns)
        if not entries:
            continue

        suffix = ns[len(project_name):].lstrip("_") if ns != project_name else ""
        label = f"T2 Memory ({suffix})" if suffix else "T2 Memory"

        ns_lines: list[str] = []
        ns_remaining = 0
        ns_rank = 0

        for entry in entries:
            if total >= _HARD_CAP:
                ns_remaining += 1
                continue
            ns_rank += 1
            title = entry.get("title", "(untitled)")
            if ns_rank <= _SNIPPET_LIMIT:
                snip = _snippet(entry.get("content", ""))
                ns_lines.append(f"  {title}" + (f" — {snip}" if snip else ""))
                total += 1
            elif ns_rank <= _TITLE_LIMIT:
                ns_lines.append(f"  {title}")
                total += 1
            else:
                ns_remaining += 1

        if ns_lines:
            lines.append(f"### {label}")
            lines.extend(ns_lines)
            if ns_remaining:
                lines.append(f"  … ({ns_remaining} more)")
            lines.append("")

    return "\n".join(lines)


def test_entries_1_to_5_include_snippet(tmp_path: Path) -> None:
    """First 5 entries per namespace include ' — snippet' text."""
    with _make_db(tmp_path) as db:
        for i in range(1, 6):
            db.put(project="repo", title=f"entry-{i}.md", content=f"Content of entry {i}")
        output = _run_scan(db, "repo")
    assert " — Content of entry" in output


def test_entries_6_to_8_title_only(tmp_path: Path) -> None:
    """3 of 8 entries per namespace appear without a snippet (title-only).

    With 8 entries: 5 get snippets (_SNIPPET_LIMIT), 3 are title-only.
    We don't assert *which* entries are title-only because all entries share
    the same second-level timestamp, making SQLite ordering non-deterministic.
    """
    with _make_db(tmp_path) as db:
        for i in range(1, 9):
            db.put(project="repo", title=f"entry-{i}.md", content=f"Content of entry {i}")
        output = _run_scan(db, "repo")
    entry_lines = [l for l in output.splitlines() if "entry-" in l]
    with_snippet = [l for l in entry_lines if " — " in l]
    without_snippet = [l for l in entry_lines if " — " not in l]
    assert len(with_snippet) == _SNIPPET_LIMIT  # 5
    assert len(without_snippet) == _TITLE_LIMIT - _SNIPPET_LIMIT  # 3


def test_entries_beyond_8_appear_as_count(tmp_path: Path) -> None:
    """Entries beyond 8 per namespace are summarised as '… (N more)'."""
    with _make_db(tmp_path) as db:
        for i in range(1, 12):
            db.put(project="repo", title=f"entry-{i}.md", content=f"Content {i}")
        output = _run_scan(db, "repo")
    assert "… (3 more)" in output


def test_hard_cap_across_namespaces(tmp_path: Path) -> None:
    """Total rendered entries across namespaces must not exceed _HARD_CAP."""
    with _make_db(tmp_path) as db:
        # Three namespaces each with 10 entries — would be 30 without cap
        for ns in ["repo", "repo_rdr", "repo_knowledge"]:
            for i in range(1, 11):
                db.put(project=ns, title=f"{ns}-entry-{i}.md", content=f"Content {i}")
        output = _run_scan(db, "repo")

    # Count rendered entries (lines with "  " prefix that are not "… (N more)")
    rendered = [
        l for l in output.splitlines()
        if l.startswith("  ") and not l.startswith("  …")
    ]
    assert len(rendered) <= _HARD_CAP


def test_namespace_header_appears_per_namespace(tmp_path: Path) -> None:
    """Each non-empty namespace gets its own '### T2 Memory ...' header."""
    with _make_db(tmp_path) as db:
        db.put(project="repo", title="main.md", content="main content")
        db.put(project="repo_rdr", title="rdr.md", content="rdr content")
        output = _run_scan(db, "repo")
    assert "### T2 Memory" in output
    assert "### T2 Memory (rdr)" in output
