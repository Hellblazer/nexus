# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.8 and nexus-6pfgb.23: the source_format, tags and category an indexed document carries in its
catalog meta, as pure rules, and the backfill verb's fill of source_format.

Storage-free; the catalog writes are ``tests/integration/test_6pfgb8_23_register_stamps.py``. The tag and
category constants mirror the row writers (``code_indexer``, ``prose_indexer``, ``doc_indexer``, the repo PDF
path in ``indexer``), and the source text of each is pinned below so a writer that changes its constant fails
here instead of drifting from the catalog's copy. ``source_format`` mirrors the engine's
``nexus.catalog_source_format`` (vectors-043); the rows of ``CatalogSourceFormatParityTest`` (the Java
parity test of the same function) are read from that file, so a row added there without an answer here fails.
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus.catalog import meta_backfill as mb
from nexus.catalog.indexed_meta import (
    SOURCE_FORMATS,
    catalog_source_format,
    document_register_meta,
    markdown_register_meta,
    pdf_register_meta,
    source_format_meta,
)

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "nexus"
JAVA_PARITY = ROOT / "service" / "src" / "test" / "java" / "dev" / "nexus" / "service" / "vectors" / "CatalogSourceFormatParityTest.java"

#: (content_type, file_path, answer). The answers are written out by hand from the rule (the extension is
#: read first), not computed by the function under test. The pairs are the matrix of the Java parity test.
MATRIX: list[tuple[str | None, str | None, str | None]] = [
    ("code", "notes/readme.md", "markdown"),
    ("code", "src/alpha.py", "code"),
    ("prose", "notes/d.md", "markdown"),
    ("rdr", "docs/rdr/x.MARKDOWN", "markdown"),
    ("paper", "papers/c.pdf", "pdf"),
    ("code", "src/scan.PDF", "pdf"),
    ("knowledge", "x.Md", "markdown"),
    ("code", "Makefile", "code"),
    ("prose", "LICENSE", "prose"),
    ("rdr", "", "prose"),
    ("pdf", "", "pdf"),
    ("paper", "", "pdf"),
    ("", "papers/C.PDF", "pdf"),
    ("", "dir.pdf/file", None),
    ("prose", "dir.md/file", "prose"),
    ("code", "dir.pdf/file", "code"),
    ("", ".gitignore", None),
    ("", ".pdf", None),
    ("", "dir/.pdf", None),
    ("", "a.tar.pdf", "pdf"),
    ("", "a.pdf.gz", None),
    ("prose", "a.md.bak", "prose"),
    ("", "a.", None),
    ("", "a.pdf/", None),
    ("", "a.pdf\n", None),
    ("", "résumé.PDF", "pdf"),
    ("knowledge", "", None),
    ("docs", "a.txt", None),
    ("", "", None),
    (None, None, None),
    ("code", None, "code"),
    (None, "a.md", "markdown"),
    (None, "a.pdf", "pdf"),
    ("Code", "x", None),
    ("PDF", "", None),
    (" code", "", None),
    # The order of the first two branches: a pdf catalog type or extension is read before a markdown extension.
    ("paper", "notes/x.md", "pdf"),
    ("pdf", "x.MARKDOWN", "pdf"),
    ("code", "x.md", "markdown"),
    # A double dot leaves the regex no extension (it needs a non-dot before the dot), where Path.suffix reads one.
    ("", "a..pdf", None),
    ("prose", "a..md", "prose"),
]


@pytest.mark.parametrize("content_type,path,answer", MATRIX)
def test_source_format_answers_what_the_engine_derives(content_type, path, answer) -> None:
    assert catalog_source_format(content_type, path) == answer


def test_the_matrix_reaches_every_answer_the_rule_has() -> None:
    assert {a for _c, _p, a in MATRIX} == SOURCE_FORMATS | {None}


def _java_pairs() -> set[tuple[str | None, str | None]]:
    text = JAVA_PARITY.read_text(encoding="utf-8")
    block = text[text.index("MATRIX = List.of("):]
    block = block[: block.index(");")]

    def arg(raw: str) -> str | None:
        raw = raw.strip()
        if raw == "null":
            return None
        assert raw.startswith('"') and raw.endswith('"'), raw
        return raw[1:-1].replace("\\n", "\n")

    pairs = re.findall(r'new In\(((?:"[^"]*"|null)),\s*((?:"[^"]*"|null))\)', block)
    return {(arg(c), arg(p)) for c, p in pairs}


def test_the_matrix_is_the_java_parity_tests_matrix() -> None:
    java = _java_pairs()
    assert len(java) >= 41, "the Java matrix was not read: the parity pin would be vacuous"
    assert {(c, p) for c, p, _a in MATRIX} == java


@pytest.mark.parametrize("content_type", ["code", "prose", "rdr", "pdf"])
def test_a_stamped_source_format_never_disagrees_with_the_derivation(content_type) -> None:
    for _c, path, _a in MATRIX:
        if path is None:
            continue
        derived = catalog_source_format(content_type, path)
        assert document_register_meta(content_type, path).get("source_format") == derived
        assert source_format_meta(content_type, path) == ({"source_format": derived} if derived else {})


@pytest.mark.parametrize("content_type,path,expected", [
    ("code", "src/app.py", {"tags": ["py"], "category": "code", "source_format": "code"}),
    ("code", "src/UPPER.PY", {"tags": ["py"], "category": "code", "source_format": "code"}),
    ("code", "lib/x.tar.gz", {"tags": ["gz"], "category": "code", "source_format": "code"}),
    ("code", "Makefile", {"category": "code", "source_format": "code"}),
    ("code", ".bashrc", {"category": "code", "source_format": "code"}),
    ("code", "weird.", {"category": "code", "source_format": "code"}),
    ("prose", "docs/guide.md", {"tags": ["markdown"], "category": "prose", "source_format": "markdown"}),
    ("prose", "docs/Guide.MARKDOWN", {"tags": ["markdown"], "category": "prose", "source_format": "markdown"}),
    ("prose", "docs/notes.txt", {"tags": ["txt"], "category": "prose", "source_format": "prose"}),
    ("prose", "docs/README", {"category": "prose", "source_format": "prose"}),
    ("rdr", "docs/rdr/rdr-1-x.md", {"tags": ["markdown"], "category": "prose", "source_format": "markdown"}),
    # the repo path: category prose, where nx index pdf says paper (recorded, not fixed)
    ("pdf", "papers/p.pdf", {"tags": ["pdf"], "category": "prose", "source_format": "pdf"}),
    # a double dot: the tag is Path.suffix (".md"), the format the engine's regex (no extension, so the catalog type
    # speaks). The two keys differ on this name, as the row writer's tag and the engine's rule always have.
    ("prose", "docs/a..md", {"tags": ["markdown"], "category": "prose", "source_format": "prose"}),
    ("code", "src/a..py", {"tags": ["py"], "category": "code", "source_format": "code"}),
])
def test_the_repo_catalog_pass_rules(content_type, path, expected) -> None:
    assert document_register_meta(content_type, path) == expected


@pytest.mark.parametrize("content_type", ["knowledge", "docs", "paper", "", "anything-else"])
def test_a_content_type_the_indexers_do_not_write_gets_no_invented_values(content_type) -> None:
    assert document_register_meta(content_type, "a/b.py") == {}


def test_the_single_file_entry_points_state_what_their_row_writers_state() -> None:
    assert markdown_register_meta("prose", "/x/a.md") == {"tags": ["markdown"], "category": "prose", "source_format": "markdown"}
    assert markdown_register_meta("rdr", Path("/x/rdr-1.md")) == {"tags": ["markdown"], "category": "prose", "source_format": "markdown"}
    assert pdf_register_meta("/x/p.pdf") == {"tags": ["pdf"], "category": "paper", "source_format": "pdf"}


def test_a_markdown_entry_point_file_the_rule_calls_prose_is_stamped_prose_not_markdown() -> None:
    """The row writer chunks it as markdown; the document is stamped with what the ENGINE derives, so the two
    never disagree on the document. The tags stay the row writer's."""
    assert markdown_register_meta("prose", "/x/notes.txt") == {"tags": ["markdown"], "category": "prose", "source_format": "prose"}


def test_nx_index_pdf_on_a_repo_registered_pdf_keeps_the_repo_paths_category() -> None:
    """Else `nx index pdf` flips the category to paper and the next `nx index repo` back to prose."""
    assert pdf_register_meta("/r/p.pdf", "pdf") == document_register_meta("pdf", "/r/p.pdf")
    assert pdf_register_meta("/r/p.pdf", "paper")["category"] == "paper"
    assert pdf_register_meta("/r/p.pdf", "")["category"] == "paper"


def _kwargs(path: str, near: str) -> tuple[str, str]:
    """The ``tags=`` and ``category=`` expressions of the ``make_chunk_metadata`` / dict call that follows *near*."""
    text = (SRC / path).read_text()
    block = text[text.index(near):][:1800]
    tags = re.search(r'tags(?:=|"\s*:\s*)([^,\n]+),', block)
    category = re.search(r'category(?:=|"\s*:\s*)([^,\n]+),', block)
    assert tags and category, (path, near)
    return tags.group(1).strip(), category.group(1).strip()


@pytest.mark.parametrize("path,near,tags,category", [
    ("code_indexer.py", 'content_type="code"', 'ext.lstrip(".")', '"code"'),
    ("prose_indexer.py", 'content_type="markdown"', '"markdown"', '"prose"'),
    ("prose_indexer.py", 'content_type="prose"', 'ext.lstrip(".")', '"prose"'),
    ("doc_indexer.py", 'content_type="markdown"', '"markdown"', '"prose"'),
    ("doc_indexer.py", 'content_type="pdf"', '"pdf"', '"paper"'),
    ("indexer.py", '"tags": "pdf"', '"pdf"', '"prose"'),
])
def test_the_row_writers_still_stamp_the_constants_this_module_mirrors(path, near, tags, category) -> None:
    assert _kwargs(path, near) == (tags, category)


@pytest.mark.parametrize("path", ["code_indexer.py", "prose_indexer.py"])
def test_the_row_writers_still_lower_case_the_suffix_the_tag_is_cut_from(path) -> None:
    """``document_register_meta`` lower-cases the suffix (``A.PY`` is tagged ``py``) because the row writers do; a
    writer that stopped would tag ``PY`` on the row and leave the catalog's copy saying ``py``."""
    assert document_register_meta("code", "src/A.PY")["tags"] == ["py"]
    assert document_register_meta("prose", "docs/B.TXT")["tags"] == ["txt"]
    assert "ext = file_path.suffix.lower()" in (SRC / path).read_text()


# ── the backfill verb's fill of source_format ──────────────────────────────────────────────────────

def _entry(meta, content_type, file_path, tumbler="1.2.3"):
    return SimpleNamespace(
        tumbler=tumbler, title="t", indexed_at="2026-10-01T12:00:00+00:00", content_type=content_type,
        file_path=file_path, author="Ann", meta=meta, physical_collection="docs__x__m__v1", alias_of="",
    )


@pytest.mark.parametrize("content_type,path,value", [
    ("paper", "", "pdf"), ("pdf", "p.pdf", "pdf"), ("prose", "a.md", "markdown"), ("rdr", "r.md", "markdown"),
    ("code", "a.py", "code"), ("prose", "a.txt", "prose"), ("code", "readme.md", "markdown"),
])
def test_backfill_fills_the_derived_source_format(content_type, path, value) -> None:
    plan = mb.plan_source_format(_entry({}, content_type, path))
    assert plan.meta == {"source_format": value} and plan.outcome == {"source_format": mb.STAMPED}


@pytest.mark.parametrize("held", ["pdf", "markdown", "", None, "anything"])
def test_backfill_is_fill_only_by_presence_whatever_the_held_value(held) -> None:
    plan = mb.plan_source_format(_entry({"source_format": held}, "paper", "p.pdf"))
    assert plan.meta == {} and plan.outcome == {"source_format": mb.PRESENT}


@pytest.mark.parametrize("content_type,path,reason", [
    ("docs", "a.txt", "no_rule"), ("", "LICENSE", "no_rule"), ("docs", "", "no_file_path"), ("", "", "no_file_path"),
])
def test_backfill_names_why_a_document_the_rule_derives_nothing_for_is_residue(content_type, path, reason) -> None:
    plan = mb.plan_source_format(_entry({}, content_type, path))
    assert plan.meta == {} and plan.outcome == {"source_format": f"residue:{reason}"}


def test_a_run_stamps_source_format_without_reading_a_chunk_row_and_a_second_run_stamps_nothing() -> None:
    from tests.test_6pfgb10_meta_backfill import _Manifest, _World  # the module's own fake engine

    w = _World()
    w.entries = [
        _entry({}, "paper", "p.pdf", "1.1.1"),
        _entry({}, "prose", "a.md", "1.1.2"),
        _entry({}, "docs", "a.txt", "1.1.3"),            # residue: no_rule
        _entry({}, "knowledge", "", "1.1.4"),            # a note: not an indexed document, not examined for it
    ]
    # chunks every document shares: the row-sourced keys skip them, source_format needs no row
    w.manifests = {"1.1.1": [_Manifest("x")], "1.1.2": [_Manifest("x")], "1.1.3": [_Manifest("x")]}
    w.owners = {"x": ["1.1.1", "1.1.2", "1.1.3"]}
    dry = mb.backfill_meta(w, w, None, apply=False)
    st = dry.keys["doc.source_format"]
    assert (st.examined, st.stamped, st.present, dict(st.residue)) == (3, 2, 0, {"no_rule": 1})
    assert "note.source_format" not in dry.keys
    assert w.updates == []

    mb.backfill_meta(w, w, w, apply=True)
    written = {u["tumbler"]: u["meta"].get("source_format") for u in w.updates}
    assert written["1.1.1"] == "pdf" and written["1.1.2"] == "markdown" and written.get("1.1.3") is None
    for e in w.entries:
        for u in w.updates:
            if u["tumbler"] == str(e.tumbler):
                e.meta = {**(e.meta or {}), **u["meta"]}
    w.updates.clear()
    again = mb.backfill_meta(w, w, w, apply=True)
    assert again.keys["doc.source_format"].stamped == 0 and again.keys["doc.source_format"].present == 2
    assert not any("source_format" in u["meta"] for u in w.updates)
