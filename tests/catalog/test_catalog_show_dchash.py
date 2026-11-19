# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-171: ``nx catalog show`` lists a document's links when one stored dchash span is malformed.

The RDR commits to this (Test Plan: "a stored malformed dchash span: nx catalog show lists the document's
links"). The read path is the real one: the fake catalog's ``resolve_span_text`` calls
``catalog_spans.resolve_span_text_for_entry``, so the dchash branch (parse, source lookup, canonical read)
runs and its failure handling is what keeps ``show`` alive. Tenant-free: no engine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nexus.catalog import catalog_spans
from nexus.catalog.dchash import text_hash
from nexus.catalog.tumbler import Tumbler
from nexus.catalog.types import CatalogEntry, CatalogLink
from nexus.cli import main

SRC, BAD_END, GOOD_END = "1.1.1", "1.1.2", "1.1.3"
MALFORMED = "dchash:abc:1-2"
BODY = "Intro line.\nthe passage we quote and more.\nEnd.\n"
PASSAGE = "the passage we quote"


def _entry(tumbler: str, title: str, path: str = "") -> CatalogEntry:
    return CatalogEntry(
        tumbler=Tumbler.parse(tumbler), title=title, author="", year=2020, content_type="prose",
        file_path=path, corpus="c", physical_collection="docs__1-1__m__v1", chunk_count=1, head_hash="h",
        indexed_at="2026-01-01T00:00:00Z", source_uri=f"file://{path}" if path else "",
    )


def _link(frm: str, to: str, link_type: str, to_span: str = "") -> CatalogLink:
    return CatalogLink(
        from_tumbler=Tumbler.parse(frm), to_tumbler=Tumbler.parse(to), link_type=link_type,
        from_span="", to_span=to_span, created_by="t", created_at="", meta={},
    )


class _FakeCatalog:
    def __init__(self, links: list[CatalogLink], entries: list[CatalogEntry]) -> None:
        self.links = links
        self.entries = entries

    def _by_tumbler(self, t: Any) -> CatalogEntry | None:
        return next((e for e in self.entries if str(e.tumbler) == str(t)), None)

    def resolve(self, t: Any, *, follow_alias: bool = True) -> CatalogEntry | None:
        return self._by_tumbler(t)

    def resolve_many(self, tumblers: Any) -> dict:
        return {t: e for t in tumblers if (e := self._by_tumbler(t)) is not None}

    def find_all(self, query: str, *, content_type: str = "") -> list:
        return [e for e in self.entries if e.title == query]

    def by_source_uri(self, _uri: str) -> None:
        return None

    def links_from(self, t: Any, *_a: Any, **_k: Any) -> list[CatalogLink]:
        return [lnk for lnk in self.links if str(lnk.from_tumbler) == str(t)]

    def links_to(self, t: Any, *_a: Any, **_k: Any) -> list[CatalogLink]:
        return [lnk for lnk in self.links if str(lnk.to_tumbler) == str(t)]

    def resolve_path(self, t: Any) -> Path | None:
        entry = self._by_tumbler(t)
        return Path(entry.file_path) if entry is not None and entry.file_path else None

    def owner_for(self, _t: Any) -> Any:
        return None

    def resolve_span_text(self, t: Any, span: str) -> str | None:
        entry = self._by_tumbler(t)
        return None if entry is None else catalog_spans.resolve_span_text_for_entry(entry, span, catalog=self)


@pytest.fixture
def show(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    good = tmp_path / "good.md"
    good.write_text(BODY)
    start = BODY.index(PASSAGE)
    good_span = f"dchash:{text_hash(BODY)}:{start}-{start + len(PASSAGE)}"
    fake = _FakeCatalog(
        links=[
            _link(SRC, BAD_END, "cites", MALFORMED),
            _link(SRC, GOOD_END, "relates"),
            _link(SRC, GOOD_END, "quotes", good_span),
        ],
        entries=[
            _entry(SRC, "Source"),
            _entry(BAD_END, "Bad target", str(tmp_path / "bad.md")),
            _entry(GOOD_END, "Good target", str(good)),
        ],
    )
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: fake)

    def run(*args: str):
        return CliRunner().invoke(main, ["catalog", "show", *args])

    return run


def test_show_lists_every_link_when_one_dchash_span_is_malformed(show) -> None:
    result = show(SRC)
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert f"→ {BAD_END} (cites) [{MALFORMED}]" in out
    assert f"→ {GOOD_END} (relates)" in out
    assert f"→ {GOOD_END} (quotes)" in out
    # The malformed span has no text to preview; the valid one beside it still does.
    assert f'"{PASSAGE}"' in out
    assert "Traceback" not in out
