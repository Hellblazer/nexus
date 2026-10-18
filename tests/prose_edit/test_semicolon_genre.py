# SPDX-License-Identifier: AGPL-3.0-or-later
"""The no-semicolons rule belongs to web/ how-to pages only (nexus-ger02.23, Sam's decision).

site-page section 3 states the narrowed rule as its own bullet, and brief.py keeps that bullet out of the
style sheet of an exploration essay by genre, not by a stored memory entry. Docs/exploration essays may use
semicolons, so the editor must not be told to remove them.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.prose_edit.conftest import ROOT, Prose
from tests.prose_edit.test_brief import brief_ok, run_brief, seed

SITE_PAGE = ROOT / ".claude" / "skills" / "site-page" / "SKILL.md"
ESSAY = "docs/exploration/e.md"
SEMI_OPENING = "No semicolons..."


def _section3(text: str) -> str:
    start = text.index("## 3. Register")
    return text[start:text.index("\n## 4.", start)]


def _bullets(text: str) -> list[str]:
    return re.findall(r"^- (.+(?:\n  .+)*)", _section3(text), flags=re.M)


def _sheet(text: str) -> str:
    return text[text.index("## 3. Style sheet"):text.index("## 4. Not a defect")]


def _page(repo: Path) -> str:
    (repo / "web").mkdir(exist_ok=True)
    (repo / "web" / "page.html").write_text("<p>hi</p>\n")
    return "web/page.html"


def test_site_page_section_3_states_the_semicolon_rule_for_web_how_to_pages_only() -> None:
    bullets = _bullets(SITE_PAGE.read_text(encoding="utf-8"))
    hits = [b for b in bullets if "semicolon" in b.lower()]
    assert len(hits) == 1, hits  # one bullet carries the rule; the plain-English bullet no longer does
    rule = hits[0]
    assert rule.startswith("No semicolons")
    assert "web/" in rule and "how-to" in rule
    assert "docs/exploration" in rule and "may use" in rule


def test_an_exploration_essay_brief_does_not_carry_the_semicolon_rule(prose: Prose) -> None:
    sheet = _sheet(brief_ok(prose, "build", ESSAY, "--genre", "exploration-essay"))
    assert "semicolon" not in sheet.lower()
    assert "Plain technical English" in sheet  # the rest of section 3 still applies to an essay


def test_a_stdin_exploration_essay_brief_does_not_carry_it_either(prose: Prose, tmp_path: Path) -> None:
    text = tmp_path / "essay.txt"
    text.write_text("An essay; it has a semicolon.\n")
    sheet = _sheet(brief_ok(prose, "build", "-", "--genre", "exploration-essay", "--file", str(text)))
    assert "semicolon" not in sheet.lower()


def test_a_how_to_brief_still_carries_the_semicolon_rule_labelled_genre(prose: Prose, repo: Path) -> None:
    sheet = _sheet(brief_ok(prose, "build", _page(repo), "--genre", "how-to"))
    rule = next(ln for ln in sheet.splitlines() if "No semicolons" in ln)
    assert rule.endswith(" (layer: genre)")


def test_a_treatment_naming_the_semicolon_bullet_is_not_stale_for_an_essay(prose: Prose, repo: Path) -> None:
    """The bullet is dropped for an essay after treatments are matched, so an entry for it (made for a how-to
    page) still finds its bullet and does not stop the essay's build as stale."""
    seed(prose, "repo", lists={"site_page_section3_query_only": [f'"{SEMI_OPENING}" (a how-to page asks)']})
    proc = run_brief(prose, "build", ESSAY, "--genre", "exploration-essay")
    assert proc.returncode == 0, proc.stderr
    assert "semicolon" not in _sheet(proc.stdout).lower()
    sheet = _sheet(brief_ok(prose, "build", _page(repo), "--genre", "how-to"))
    assert "QUERY ONLY" in sheet and "No semicolons" in sheet[sheet.index("QUERY ONLY"):]
