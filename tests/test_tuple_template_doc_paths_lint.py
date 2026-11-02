# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A template doc block cites only repository paths and RDR ids that exist.

RDR-239 Phase 3 (bead nexus-mdbds.2.19, review round nexus-mdbds.2.17 M1/S-C).
The Java ``TemplateDocLintTest`` check 4 asserts the same, but the engine CI job
runs only when ``service/**`` changes, and the files these blocks cite live
outside ``service/`` (skills, ``docs/``, ``scripts/``). This is the cheap
mirror the lint job runs on any PR that changes code, so a rename under
``conexus/`` or ``scripts/`` is caught in its own PR. CI skips the lint job for
a PR that touches only ``docs/**``, so a renamed ``docs/`` file or RDR is caught
on the next PR that changes code. No engine, no YAML parser (the blocks
hold ``- Unverified: ...`` lines that a YAML reader turns into mappings), just
the ``writers`` / ``readers`` / ``see_also`` lines of each ``doc:`` block read
as text, with the same path and RDR-id rules as the Java test.

What it does not cover (the Java check shares the list):

* paths whose extension is outside the list below, extensionless paths
  (``scripts/lab-run``), directories, and root-level files (``CHANGELOG.md``
  has no slash);
* prose fields other than writers, readers and see_also (summary, fields, body);
* cross-repo paths (a path in another repository does not exist under this root);
* hex runs of any length but 40 and 64 (a different check, in the Java test).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "service" / "src" / "main" / "resources" / "tuples" / "templates"

_CITING_FIELDS = ("writers", "readers", "see_also")

# Same rules as TemplateDocLintTest.REPO_PATH / RDR_ID.
_REPO_PATH = re.compile(
    r"(?<![\w/.:-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.(?:sh|md|py|java|yaml|yml|json|xml|toml|txt))(?![\w/-])"
)
_RDR_ID = re.compile(r"RDR-(\d{3,4})")


def _citing_lines(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """``(items, unreadable)``: ``(field, line)`` for every list item under
    writers/readers/see_also of the top-level ``doc:`` block, and one message per
    line this reader cannot read. The only forms it reads are a key line ending
    in ``:`` (or ``[]``, which lists nothing) followed by ``    - item`` lines at
    indent 4. A value on the key's own line (a flow list, a scalar), a wrapped
    continuation, or any other line under the key is reported, never skipped:
    the Java check parses YAML and would see what this one missed."""
    out: list[tuple[str, str]] = []
    unreadable: list[str] = []
    in_doc = False
    field: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            in_doc = raw.rstrip() == "doc:"
            field = None
            continue
        if not in_doc:
            continue
        stripped = raw.strip()
        if indent == 2:
            key, _, value = stripped.partition(":")
            field = key if key in _CITING_FIELDS else None
            if field and value.strip() not in ("", "[]"):
                unreadable.append(f"{field} cannot be read: a value on the key's own line ({stripped!r}); "
                                  "write it as a block list")
                field = None
            elif field and value.strip() == "[]":
                field = None
        elif field:
            if indent == 4 and stripped.startswith("- "):
                out.append((field, stripped[2:]))
            else:
                unreadable.append(
                    f"{field} cannot be read: line {stripped!r} is not an item; "
                    "each item is one '    - ' line at indent 4, with no wrapped continuation")
    return out, unreadable


def _rdr_exists(root: Path, rdr_id: str) -> bool:
    return any((root / "docs" / "rdr").glob(f"rdr-{rdr_id}-*.md"))


def check_templates(templates_dir: Path, root: Path) -> tuple[list[str], int, int, int]:
    """``(problems, files, paths_checked, rdr_ids_checked)``."""
    problems: list[str] = []
    files = paths = rdrs = 0
    for f in sorted(templates_dir.glob("*.yaml")):
        files += 1
        items, unreadable = _citing_lines(f.read_text(encoding="utf-8"))
        problems.extend(f"{f.name}: {u}" for u in unreadable)
        for field, line in items:
            for m in _REPO_PATH.finditer(line):
                paths += 1
                if not (root / m.group(1)).exists():
                    problems.append(f"{f.name}: {field} cites {m.group(1)}, which does not exist under {root}")
            for m in _RDR_ID.finditer(line):
                rdrs += 1
                if not _rdr_exists(root, m.group(1)):
                    problems.append(f"{f.name}: {field} cites RDR-{m.group(1)}, which has no docs/rdr/rdr-{m.group(1)}-*.md")
    return problems, files, paths, rdrs


def test_cited_repository_paths_and_rdr_ids_in_template_docs_exist() -> None:
    problems, files, paths, rdrs = check_templates(TEMPLATES_DIR, REPO_ROOT)
    # non-vacuity: the shipped blocks cite paths and RDR ids; a run that saw
    # none read nothing (a moved templates directory, a changed layout)
    assert files > 0 and paths > 0 and rdrs > 0, (files, paths, rdrs)
    assert not problems, "template doc blocks cite things that do not exist:\n" + "\n".join(problems)


def test_the_check_flags_a_missing_path_and_a_missing_rdr_and_ignores_prose(tmp_path: Path) -> None:
    """Positive control: the same function, over a planted template, reports
    what is missing and counts what it examined."""
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    (tmp_path / "docs" / "rdr" / "rdr-001-real.md").write_text("x")
    (tmp_path / "docs" / "real.md").write_text("x")
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "t.yaml").write_text(
        "name: t\n"
        "doc:\n"
        "  summary: cites docs/not-checked-in-summary.md and RDR-999 in prose\n"
        "  writers:\n"
        "    - Unverified: docs/real.md and docs/gone.md\n"
        "  see_also:\n"
        "    - RDR-001\n"
        "    - RDR-998\n"
        "  body: docs/also-prose.md\n"
    )
    problems, files, paths, rdrs = check_templates(tdir, tmp_path)
    assert (files, paths, rdrs) == (1, 2, 2)
    assert len(problems) == 2
    assert any("docs/gone.md" in p for p in problems)
    assert any("RDR-998" in p for p in problems)


# Planted forms the line reader cannot see: each must fail loud, never pass silently.
_UNREADABLE_FORMS = {
    "a flow-style list on the key's own line": (
        "  writers: [docs/real.md, docs/gone.md]\n", "writers"),
    "a scalar on the key's own line": (
        "  readers: docs/gone.md\n", "readers"),
    "a bare scalar under the key": (
        "  see_also:\n    docs/gone.md\n", "see_also"),
    "a wrapped continuation under a list item": (
        "  writers:\n    - Unverified: docs/real.md and\n      docs/gone.md\n", "writers"),
    "a list item at the wrong indent": (
        "  readers:\n      - docs/gone.md\n", "readers"),
}


@pytest.mark.parametrize("form", sorted(_UNREADABLE_FORMS))
def test_the_check_fails_loud_on_a_form_it_cannot_read(form: str, tmp_path: Path) -> None:
    """Positive control, one per unreadable form: a planted template whose
    writers/readers/see_also is written in a form the line reader cannot see
    (the Java check parses YAML and would see it) is reported, naming the field,
    not passed over."""
    body, field = _UNREADABLE_FORMS[form]
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    (tmp_path / "docs" / "real.md").write_text("x")
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "t.yaml").write_text("name: t\ndoc:\n  summary: s\n" + body)
    problems, files, _paths, _rdrs = check_templates(tdir, tmp_path)
    assert files == 1
    assert any(field in p and "cannot be read" in p for p in problems), (form, problems)


def test_an_empty_flow_list_and_the_shipped_forms_are_not_flagged(tmp_path: Path) -> None:
    """Negative control: ``writers: []`` lists nothing, so nothing is missed; a
    plain block list with a comment line, and a non-citing field's wrapped text,
    read clean."""
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    (tmp_path / "docs" / "real.md").write_text("x")
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "t.yaml").write_text(
        "name: t\ndoc:\n  summary: s\n  writers: []\n"
        "  readers:\n    # a note\n    - docs/real.md\n"
        "  fields:\n    k: wrapped\n      text is not a citing field\n"
    )
    problems, _files, paths, _rdrs = check_templates(tdir, tmp_path)
    assert problems == [] and paths == 1
