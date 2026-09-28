# SPDX-License-Identifier: AGPL-3.0-or-later
"""catalog_search must find a document wherever it sits in a large catalog.

nexus-3bafq. The 7.64.1 shakeout ran catalog ``search`` against the live
catalog (23,334 documents). ``file_path`` alone and ``author`` alone read ONE
``limit+offset+1`` page of ``all_documents`` and filtered that page, so any
document past the first ~21 rows came back as absent. The free-text branch
took the engine's default 50-row ``/search`` cap and then dropped
``file_path``/``owner``/``corpus`` entirely, so ``query="core.py",
file_path="src/nexus/mcp/core.py"`` returned a different file.

Every earlier test seeded a handful of rows, all inside that first page, so
each passed against the broken code. These seed 1,200 rows and put every
target at the END, past the old window, past the old 50-row cap, and past the
1,000-row page boundary of ``all_documents``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog
from nexus.mcp_server import _reset_singletons, catalog_search

N_FILLER = 1200
TARGET_REL = "src/deep/target_module.py"
TARGET_ABS = "/Users/someone/git/proj/src/deep/target_module.py"
ABS_STORED = "/Users/someone/git/proj/lib/stored_absolute.py"


@pytest.fixture(autouse=True)
def clean_singletons():
    _reset_singletons()
    yield
    _reset_singletons()


@pytest.fixture
def big(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))
    cat = ActiveCatalog()
    cat.register_owner("test-repo", "repo", repo_hash="abcd1234")
    filler = [
        {
            "title": f"scale doc {i}",
            "content_type": "code",
            "file_path": f"src/filler/mod_{i}.py",
            "author": "Filler Person",
        }
        for i in range(N_FILLER)
    ]
    cat.register_many("1.1", filler)
    targets = cat.register_many("1.1", [
        {"title": "scale doc target", "content_type": "code",
         "file_path": TARGET_REL, "author": "Leslie Lamport"},
        {"title": "scale doc absolute", "content_type": "code",
         "file_path": ABS_STORED, "author": "Filler Person"},
    ])
    return {"target": str(targets[0]), "absolute": str(targets[1])}


def _tumblers(rows: list[dict]) -> list[str]:
    assert not any("error" in r for r in rows), rows
    return [r["tumbler"] for r in rows if "tumbler" in r]


def test_file_path_alone_finds_a_late_document(big) -> None:
    assert _tumblers(catalog_search(file_path=TARGET_REL)) == [big["target"]]


def test_absolute_file_path_alone_finds_the_relative_stored_row(big) -> None:
    assert _tumblers(catalog_search(file_path=TARGET_ABS)) == [big["target"]]


def test_relative_file_path_finds_an_absolute_stored_row(big) -> None:
    assert _tumblers(catalog_search(file_path="lib/stored_absolute.py")) == [big["absolute"]]


def test_author_alone_finds_a_late_document(big) -> None:
    assert _tumblers(catalog_search(author="lamport")) == [big["target"]]


def test_author_alone_paginates_across_the_whole_catalog(big) -> None:
    rows = catalog_search(author="Filler Person", limit=50, offset=N_FILLER - 10)
    # 1,201 matches: offset 1,190 leaves 11, so the page is short with no
    # pagination marker. The old window returned nothing at all here.
    assert len(_tumblers(rows)) == 11
    assert not any("_pagination" in r for r in rows)


def test_query_plus_file_path_applies_the_file_path(big) -> None:
    # 1,202 docs match "scale"; the target is past the old 50-row cap, and
    # file_path must narrow the result to it rather than being dropped.
    assert _tumblers(catalog_search(query="scale", file_path=TARGET_REL)) == [big["target"]]


def test_query_plus_owner_and_corpus_are_applied(big) -> None:
    assert _tumblers(catalog_search(query="scale", owner="1.2")) == []
    assert _tumblers(catalog_search(query="scale", corpus="no-such-corpus")) == []


def test_query_pages_past_the_engine_default_cap(big) -> None:
    rows = catalog_search(query="scale", limit=10, offset=100)
    assert len(_tumblers(rows)) == 10
    assert rows[-1] == {"_pagination": {"next_offset": 110, "limit": 10}}


# Siblings of the same cap (critic finding on 43cc2820b): callers that filter,
# count, or write over cat.find() saw at most the engine's 50-row default.


def test_find_all_returns_every_match(big) -> None:
    cat = ActiveCatalog()
    assert len(cat.find("scale")) == 50, "the cap this bead is about"
    assert len(cat.find_all("scale")) == N_FILLER + 2
    assert [str(e.tumbler) for e in cat.find_by_title_exact("scale doc target")] == [big["target"]]


def test_catalog_update_search_writes_every_match(big) -> None:
    """``nx catalog update --search`` is a WRITE: over the capped page it
    updated the first 50 of 1,202 matches and reported success."""
    from click.testing import CliRunner

    from nexus.commands.catalog import catalog

    result = CliRunner().invoke(catalog, ["update", "--search", "scale", "--corpus", "bulk-3bafq"])
    assert result.exit_code == 0, result.output
    assert len(ActiveCatalog().by_corpus("bulk-3bafq")) == N_FILLER + 2


def test_catalog_search_cli_pages_past_fifty(big) -> None:
    from click.testing import CliRunner

    from nexus.commands.catalog import catalog

    result = CliRunner().invoke(catalog, ["search", "scale", "--limit", "10", "--offset", "100"])
    assert result.exit_code == 0, result.output
    assert len([ln for ln in result.output.splitlines() if ln.startswith("1.1.")]) == 10
    assert "Next page: --offset 110" in result.output
