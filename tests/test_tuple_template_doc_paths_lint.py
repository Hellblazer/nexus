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


_TRAILING_COMMENT = re.compile(r"\s+#.*$")


def _key_value(stripped: str) -> tuple[str, str]:
    """``(key, value)`` of a ``key: value`` line, the key unquoted and a
    trailing comment dropped from the value."""
    key, _, value = stripped.partition(":")
    return key.strip().strip("\"'"), _TRAILING_COMMENT.sub("", value).strip()


def _citing_lines(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """``(items, unreadable)``: ``(field, line)`` for every list item under
    writers/readers/see_also of the top-level ``doc:`` block, and one message per
    line this reader cannot read. The only layout it reads is a ``doc:`` line at
    indent 0 with its keys at indent 2, a citing key ending in ``:`` (or ``[]``,
    which lists nothing) and its ``    - item`` lines at indent 4; a trailing
    comment and a quoted key are allowed. Anything else under ``doc`` or a citing
    key (a value on the key's own line, a compact sequence at the key's indent, a
    wrapped continuation, a doc block at another indent, a flow mapping, no doc
    block at all) is reported, never skipped: the Java check parses YAML and
    would see what this one missed."""
    out: list[tuple[str, str]] = []
    unreadable: list[str] = []
    in_doc = False
    doc_seen = False
    doc_key_seen = False
    field: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        stripped = raw.strip()
        if indent == 0:
            field = None
            key, value = _key_value(stripped)
            in_doc = key == "doc"
            if in_doc:
                doc_seen = True
                doc_key_seen = False
                if value:
                    unreadable.append(f"doc cannot be read: a value on the doc line ({stripped!r}); "
                                      "write it as a block mapping")
                    in_doc = False
            continue
        if not in_doc:
            continue
        if indent == 2 and stripped.startswith("- "):
            if not doc_key_seen:
                unreadable.append(f"doc cannot be read: item {stripped!r} makes the doc block a list; "
                                  "it is a mapping of summary, writers, readers, ...")
                in_doc = False
            elif field:
                unreadable.append(f"{field} cannot be read: item {stripped!r} sits at the key's indent; "
                                  "indent each item to 4")
            continue
        if indent == 2:
            doc_key_seen = True
            key, value = _key_value(stripped)
            field = key if key in _CITING_FIELDS else None
            if field and value not in ("", "[]"):
                unreadable.append(f"{field} cannot be read: a value on the key's own line ({stripped!r}); "
                                  "write it as a block list")
                field = None
            elif field and value == "[]":
                field = None
        elif not doc_key_seen:
            unreadable.append(f"doc cannot be read: line {stripped!r} is at indent {indent}; "
                              "the doc block's keys sit at indent 2")
            in_doc = False
        elif field:
            if indent == 4 and stripped.startswith("- "):
                out.append((field, stripped[2:]))
            else:
                unreadable.append(
                    f"{field} cannot be read: line {stripped!r} is not an item; "
                    "each item is one '    - ' line at indent 4, with no wrapped continuation")
    if not doc_seen:
        unreadable.append("doc cannot be read: no top-level doc block found")
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


# Whole-file layouts the line reader cannot see (residuals review, nexus-mdbds.2.24):
# each must be reported, never read as a file that cites nothing.
_UNREADABLE_DOC_LAYOUTS = {
    "a compact block sequence (dash at the key's indent)": (
        "name: t\ndoc:\n  summary: s\n  writers:\n  - docs/gone.md\n", "writers"),
    "a flow mapping on the doc line": (
        "name: t\ndoc: {summary: s, writers: [docs/gone.md]}\n", "doc"),
    "a doc block indented 4": (
        "name: t\ndoc:\n    summary: s\n    writers:\n      - docs/gone.md\n", "doc"),
    "no doc block at all": (
        "name: t\nsubspace: t\n", "doc"),
}


@pytest.mark.parametrize("layout", sorted(_UNREADABLE_DOC_LAYOUTS))
def test_the_check_fails_loud_on_a_doc_layout_it_cannot_read(layout: str, tmp_path: Path) -> None:
    """Positive control, one per layout: a template whose doc block the line
    reader cannot read is reported naming the field or ``doc``, so a file is
    never counted as clean because nothing in it was seen."""
    text, field = _UNREADABLE_DOC_LAYOUTS[layout]
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "t.yaml").write_text(text)
    problems, files, _paths, _rdrs = check_templates(tdir, tmp_path)
    assert files == 1
    assert any(f"{field} " in p and "cannot be read" in p for p in problems), (layout, problems)


def test_comments_after_a_key_and_a_quoted_key_read_clean(tmp_path: Path) -> None:
    """Negative control: a trailing comment on the doc line or a citing key is
    not a value, and a quoted citing key is the same key."""
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    (tmp_path / "docs" / "real.md").write_text("x")
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "t.yaml").write_text(
        "name: t\ndoc:  # the block\n  summary: s\n  writers: []  # none today\n"
        "  \"readers\":  # who\n    - docs/real.md\n"
    )
    problems, _files, paths, _rdrs = check_templates(tdir, tmp_path)
    assert problems == [] and paths == 1


def test_a_doc_block_that_is_a_list_is_reported(tmp_path: Path) -> None:
    """Positive control: ``doc:`` followed by a sequence is no mapping; the Java
    loader refuses it, so the line reader must not read it as an empty block."""
    (tmp_path / "docs" / "rdr").mkdir(parents=True)
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "t.yaml").write_text("name: t\ndoc:\n  - docs/gone.md\n")
    problems, _files, _paths, _rdrs = check_templates(tdir, tmp_path)
    assert any("doc " in p and "cannot be read" in p for p in problems), problems
