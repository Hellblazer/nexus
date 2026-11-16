# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Typora viewer, the marked-up copy and `memory.py viewer` are gone (nexus-scmcs.2.11, T8).

After this change no code path writes a review copy or opens a viewer. `render` still sets the session up
(session.json: apply, answer and a stdin run need it), and `apply` still asks T2 one question before it writes the
file, through a verb that survives.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from tests.prose_edit.conftest import ROOT, Prose
from tests.prose_edit.test_brief import brief_ok, edit, fenced, proposal, run_brief
from tests.prose_edit.test_review import DOC, E1, E2, REVIEW, review_ok, run_review, start

SCRIPTS = ROOT / "prose-edit" / "skills" / "prose-edit" / "scripts"
MEMORY = SCRIPTS / "memory.py"
GONE_KEYS = {"copy", "viewer", "opened", "reason"}
GONE_SYMBOLS = (
    "build_copy", "open_copy", "_run_open", "_anchor", "_anchored", "_gist", "_GIST_ESCAPES", "_index", "_html_excerpts",
    "html_blocks", "_flat", "_quote", "_first", "_block", "_dropped_lines", "_unplaced_note", "_show", "change_note",
    "INDEX_GIST",
)


def test_render_still_sets_the_session_up_and_apply_still_asks_t2_one_question(prose: Prose, repo: Path) -> None:
    """The session `render` sets up (counts, unplaced, dropped, warnings; no copy, viewer or opened) for a path run and for
    a stdin run, and the one T2 question `apply` asks before it writes: a down service stops an accept-only apply
    (nexus-h72g3: one tenant for the three)."""
    # --- render returns the session counts and no copy, viewer or opened
    work, out = start(prose, repo, [E1, E2])
    assert not GONE_KEYS & set(out), sorted(GONE_KEYS & set(out))
    assert out["genre"] == "reference-doc" and out["counts"] == {"edits": 2, "paragraphs": 0, "queries": 0, "dropped": 0}, out
    assert {"unplaced", "dropped", "dropped_queries", "dropped_paragraphs", "warnings"} <= set(out), sorted(out)
    assert not list(work.glob("*.review.md")), "render wrote a review copy"
    assert (work / "session.json").is_file(), "no session.json"  # apply, answer and decisions read it

    # --- apply asks T2 through a surviving verb, so a down service stops an accept-only apply
    fake = Path(__file__).parent / "acceptance" / "fake_nx_unavailable.py"
    proc = run_review(prose, "apply", "--work", str(work), "--accept", "1",
                      env={**prose.env, "PROSE_EDIT_NX": f"{sys.executable} {fake}"})
    assert proc.returncode == 3 and "unavailable" in proc.stderr and proc.stdout == "", (proc.returncode, proc.stderr, proc.stdout)
    assert (repo / "docs" / "s.md").read_text(encoding="utf-8") == DOC and work.is_dir(), "apply wrote the file or dropped WORK"  # nothing written, WORK kept

    # --- render for a stdin run still returns counts, unplaced, dropped and warnings
    head = dict(line.split("=", 1) for line in brief_ok(prose, "tmpdir", "--ready").strip().splitlines())
    work = Path(head["WORK"])
    Path(head["INPUT"]).write_text(DOC, encoding="utf-8")
    proc = run_brief(prose, "filter", "-", "--file", head["INPUT"], "--save", head["FILTERED"],
                     stdin=fenced(proposal([E1, edit(9, "not in the text", "x", "r")])))
    assert proc.returncode == 0, proc.stderr
    out = review_ok(prose, "render", "-", "--work", str(work), "--file", head["INPUT"], "--genre", "reference-doc")
    assert not GONE_KEYS & set(out), sorted(GONE_KEYS & set(out))
    assert set(out) >= {"genre", "counts", "unplaced", "dropped", "dropped_queries", "dropped_paragraphs", "warnings"}, sorted(out)
    assert out["counts"]["edits"] == 1 and out["dropped"] and not list(work.glob("*.review.md")), out


def test_the_review_script_names_no_viewer_and_builds_no_marked_up_text() -> None:
    src = REVIEW.read_text(encoding="utf-8")
    for needle in ("open -a", "PROSE_EDIT_OPEN", "Typora", "<del>", "<ins>", "viewer", ".review.md", "marked-up"):
        assert needle not in src, needle
    names = {n.name for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    names |= {t.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
    assert not names & set(GONE_SYMBOLS), sorted(names & set(GONE_SYMBOLS))
    assert {"plan_edits", "apply_plan", "_occurrences", "_within"} <= names  # the placement the app route shares
    assert 'memory_json(["viewer"])' not in src and 'memory_json(["status"])' in src, \
        "apply must ask T2 through `status`, not the removed `viewer` verb"  # the verb apply's one T2 question uses


def test_the_memory_script_has_no_viewer_verb(bare: Prose) -> None:
    src = MEMORY.read_text(encoding="utf-8")
    assert not re.search(r"\bviewer\b", src, re.IGNORECASE), "memory.py still names a viewer"
    proc = bare.run("viewer")
    assert proc.returncode == 2 and "invalid choice" in proc.stderr


def test_no_script_doc_or_ledger_line_still_speaks_of_a_review_copy_or_a_removed_t2_record() -> None:
    """Gate 2 (S2, S8): the copy is gone, and removing the `viewer` verb left any stored record in T2 untouched."""
    for script in SCRIPTS.glob("*.py"):
        text = script.read_text(encoding="utf-8")
        assert "review copy" not in text and "marked-up copy" not in text, script.name
    for doc in (ROOT / "tests" / "prose_edit" / "acceptance" / "README.md", ROOT / "tests" / "prose_edit" / "test_review.py"):
        assert "marked-up copy" not in doc.read_text(encoding="utf-8"), doc.name
    # The note lives in the pending-release ledger until a release empties it, then in the
    # plugin changelog's section for that release, so read both (8.3.0 cut emptied the ledger).
    ledger = (ROOT / "conexus" / "PENDING_RELEASE.md").read_text(encoding="utf-8")
    shipped = (ROOT / "conexus" / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "its T2 record are removed" not in ledger + shipped
    assert "the `viewer` verb and its record shape are removed" in ledger + shipped
