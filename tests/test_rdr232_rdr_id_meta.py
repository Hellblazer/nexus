# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 named-source sibling rule (nexus-jypmj.7): the RDR frontmatter ``id`` is recorded on the catalog
document at registration, and the resolver reads it first.

Until this bead the rule read each sibling's id from the ``rdr-frontmatter-v1`` aspect row, which the worker
fills asynchronously, then from the file where the host has it. The indexer has the frontmatter when it
registers the document, so it now writes the raw ``id`` into the document's ``meta`` under
``RDR_FRONTMATTER_ID_META_KEY`` and :func:`recorded_frontmatter_id` trusts that key before it asks the aspect
store. This file is substrate-free; ``test_rdr232_rdr_id_meta_engine.py`` runs the same claims against the engine.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.aspect_extractor import _parse_rdr_aspects
from nexus.catalog.rdr_canonical import rdr_frontmatter_meta
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import RDR_FRONTMATTER_ID_META_KEY, CatalogEntry
from nexus.plans.primary_evidence import (
    FILE_ABSENT,
    NO_ASPECT_ROW,
    FrontmatterId,
    _file_frontmatter_id,
    recorded_frontmatter_id,
)

KEY = RDR_FRONTMATTER_ID_META_KEY
COL = "rdr__1-2__bge-base-en-v15-768__v1"
PATH = "docs/rdr/rdr-200-nx-answer-continuation-mode.md"


def _entry(tumbler: str = "1.2.20", *, meta: dict | None = None, source_uri: str = "") -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title="t", author="", year=0, content_type="rdr", file_path=PATH, corpus="",
        physical_collection=COL, chunk_count=0, head_hash="", indexed_at="", meta=meta or {}, source_uri=source_uri,
    )


# ── what registration records ───────────────────────────────────────────────────────────────────────────


def test_the_key_is_a_stable_catalog_meta_name() -> None:
    assert KEY == "rdr_frontmatter_id"


@pytest.mark.parametrize("header, stamped", [
    ("---\nid: RDR-200\n---\n# x\n", "RDR-200"),
    ("---\nid: 200\n---\n# x\n", 200),
    ("---\nid: companion-note\n---\n# x\n", "companion-note"),
    ("---\ntitle: y\n---\n# x\n", ""),      # parsed, no id: a recorded absence, not an unknown
    ("# no frontmatter at all\n", ""),       # the file read treats this as parsed-with-no-id too
])
def test_the_registration_meta_carries_the_raw_id_as_the_file_read_would(header: str, stamped: Any) -> None:
    assert rdr_frontmatter_meta(header) == {KEY: stamped}


def test_an_unclosed_fence_is_not_stamped() -> None:
    """The header did not parse, so nothing is known: no key, and the aspect row or the file decide."""
    assert rdr_frontmatter_meta("---\nid: RDR-200\n# body with no closing fence\n") == {}


@pytest.mark.parametrize("text", [
    "---\nid: RDR-200\n---\n",
    "---\nid: 040\n---\n",
    "---\nid: [a, b]\n---\n",
    "---\nid: companion-note\n---\n",
    "---\ntitle: z\n---\n",
    "no header\n",
])
def test_the_stamped_id_is_the_id_the_file_fallback_and_the_aspect_row_read(text: str, tmp_path) -> None:
    """One parser, three readers. A stamp that read differently from the file would let the sibling rule pick
    different carriers depending on which source answered."""
    d = tmp_path / "docs" / "rdr"
    d.mkdir(parents=True)
    f = d / "rdr-200-x.md"
    f.write_text(text, encoding="utf-8")
    from_file = _file_frontmatter_id(_entry(source_uri=f.as_uri()))
    stamped = rdr_frontmatter_meta(text)
    assert from_file.known and stamped, "both read this header"
    assert FrontmatterId.of(stamped[KEY]) == from_file
    assert (_parse_rdr_aspects(text)["extras"].get("rdr_id") or "") == (stamped[KEY] or "")


# ── what the resolver reads ─────────────────────────────────────────────────────────────────────────────


def test_the_resolver_reads_the_registration_meta_before_asking_the_aspect_store() -> None:
    asked: list[str] = []

    def aspects(doc_id: str, timeout: float) -> Any:
        asked.append(doc_id)
        raise AssertionError("the aspect store must not be asked when the registration carries the id")

    got = recorded_frontmatter_id(aspects)(_entry(meta={KEY: "RDR-200"}), 1.0, "")
    assert (got.known, got.value) == (True, "RDR-200")
    assert asked == []


def test_a_recorded_absence_in_meta_is_known_and_empty() -> None:
    got = recorded_frontmatter_id(lambda d, t: None)(_entry(meta={KEY: ""}), 1.0, "")
    assert (got.known, got.value) == (True, "")


def test_without_the_key_the_aspect_row_and_the_file_still_decide() -> None:
    """An older registration: the meta has a content hash and nothing else."""
    got = recorded_frontmatter_id(lambda d, t: None)(_entry(meta={"content_hash": "ab" * 32}), 1.0, "")
    assert not got.known and got.reasons == (NO_ASPECT_ROW, "no_file_source")
    gone = recorded_frontmatter_id(None)(_entry(meta={"content_hash": "ab" * 32}, source_uri="file:///nowhere/docs/rdr/x.md"), 1.0, "")
    assert not gone.known and gone.reasons == (FILE_ABSENT,)


def test_a_failing_aspect_store_does_not_matter_when_the_registration_has_the_id() -> None:
    def aspects(doc_id: str, timeout: float) -> Any:
        raise TimeoutError("t2 slow")

    lookup = recorded_frontmatter_id(aspects)
    assert lookup(_entry("1.2.20", meta={KEY: "RDR-200"}), 1.0, "").value == "RDR-200"
    assert lookup(_entry("1.2.21", meta={KEY: "companion-note"}), 1.0, "").value == "companion-note"
