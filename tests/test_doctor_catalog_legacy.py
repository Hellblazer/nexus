# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-aoqnb (GH #1419 Issue 4): doctor must label a legacy catalog SQLite
file as a FROZEN MIGRATION SOURCE, never a live mirror.

Steve Harris's backup showed ``catalog.db`` holding 532 docs / 13 links while
the authoritative PG catalog held 592 / 52. Nothing in the product said which
one was real. A stale-but-PLAUSIBLE file is the dangerous shape: a recovery
procedure reaches for it first precisely because it parses, opens, and looks
like a catalog.

Two shapes seen in the wild, both of which must be labelled:

* POPULATED-BUT-STALE (Steve's box) — rows present, silently behind PG.
* EMPTY-BUT-PRESENT (this dev box, 2026-07-24: 11 tables, 0 documents,
  mtime a month after the migration) — arguably worse for a restore, since
  it yields a catalog that "restores successfully" and is simply empty.

A zero-byte stray (``~/.config/nexus/catalog.db``, 0 bytes) is also present
on real installs and must not be mistaken for either.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _make_catalog_db(path: Path, *, documents: int = 0, links: int = 0) -> None:
    """A minimally realistic legacy catalog file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE documents (tumbler TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE links (id INTEGER PRIMARY KEY, from_t TEXT, to_t TEXT)")
    for i in range(documents):
        conn.execute("INSERT INTO documents VALUES (?, ?)", (f"1.1.{i}", f"doc{i}"))
    for i in range(links):
        conn.execute("INSERT INTO links VALUES (?, ?, ?)", (i, "1.1.0", "1.1.1"))
    conn.commit()
    conn.close()


class TestCatalogLegacyFileCheck:
    def test_absent_file_produces_no_row(self, tmp_path: Path) -> None:
        """A clean install must not grow a permanent warning."""
        from nexus.health import _check_catalog_legacy_file

        assert _check_catalog_legacy_file(config_dir=tmp_path) == []

    def test_populated_file_is_flagged_frozen_with_its_counts(
        self, tmp_path: Path,
    ) -> None:
        """Steve's shape. The row must carry the counts, because the whole
        failure was a human unable to tell which store was authoritative."""
        from nexus.health import _check_catalog_legacy_file

        _make_catalog_db(tmp_path / "catalog" / ".catalog.db", documents=532, links=13)
        results = _check_catalog_legacy_file(config_dir=tmp_path)

        assert len(results) == 1
        r = results[0]
        assert not r.ok            # visible, not buried in the green
        assert not r.fatal         # an orphaned source is expected, not broken
        assert "frozen" in r.detail.lower()
        assert "532" in r.detail
        assert "13" in r.detail
        # The load-bearing instruction: do not restore from it.
        assert any("restore" in s.lower() for s in r.fix_suggestions)

    def test_empty_but_present_file_is_still_flagged(self, tmp_path: Path) -> None:
        """The dev-box shape. An empty legacy file must NOT read as "nothing
        to see" — a restore from it succeeds and silently yields no catalog."""
        from nexus.health import _check_catalog_legacy_file

        _make_catalog_db(tmp_path / "catalog" / ".catalog.db", documents=0, links=0)
        results = _check_catalog_legacy_file(config_dir=tmp_path)

        assert len(results) == 1
        assert "frozen" in results[0].detail.lower()
        assert "0 document" in results[0].detail

    def test_zero_byte_stray_is_named_separately_not_as_a_catalog(
        self, tmp_path: Path,
    ) -> None:
        """``~/.config/nexus/catalog.db`` exists at 0 bytes on real installs.
        It is not a catalog and must not be described as one, or the operator
        chases a file with nothing in it."""
        from nexus.health import _check_catalog_legacy_file

        (tmp_path / "catalog.db").write_bytes(b"")
        results = _check_catalog_legacy_file(config_dir=tmp_path)

        assert len(results) == 1
        detail = results[0].detail.lower()
        assert "empty" in detail or "0 byte" in detail
        assert "stray" in detail or "not a catalog" in detail

    def test_unreadable_file_is_reported_not_crashed(self, tmp_path: Path) -> None:
        """A corrupt legacy file must still be NAMED — that is exactly when an
        operator most needs to be told not to restore from it. doctor must
        never crash on it either."""
        from nexus.health import _check_catalog_legacy_file

        p = tmp_path / "catalog" / ".catalog.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"this is not a sqlite database")

        results = _check_catalog_legacy_file(config_dir=tmp_path)
        assert len(results) == 1
        assert "frozen" in results[0].detail.lower()

    def test_both_files_present_yields_one_row_each(self, tmp_path: Path) -> None:
        from nexus.health import _check_catalog_legacy_file

        _make_catalog_db(tmp_path / "catalog" / ".catalog.db", documents=5)
        (tmp_path / "catalog.db").write_bytes(b"")
        results = _check_catalog_legacy_file(config_dir=tmp_path)
        assert len(results) == 2

    def test_check_is_wired_into_doctor(self) -> None:
        """A check nothing calls is a check that does not exist."""
        import inspect

        from nexus import health

        src = inspect.getsource(health)
        assert src.count("_check_catalog_legacy_file") >= 2, (
            "_check_catalog_legacy_file is defined but never called from the "
            "doctor assembly"
        )


@pytest.mark.parametrize("docs,links", [(532, 13), (0, 0), (17829, 1779)])
def test_states_the_disclaimer_at_every_population(
    tmp_path: Path, docs: int, links: int,
) -> None:
    """Across every population — including one large enough to look like a
    real catalog — the row must positively state that this file is frozen and
    that Postgres is authoritative.

    Originally written as a FORBIDDEN-substring check ("live mirror",
    "authoritative" must not appear). That was wrong: those words appear
    legitimately when CONTRASTING the two stores ("not a live mirror", "the
    authoritative catalog is in Postgres"), so the check failed correct
    copy. Asserting the disclaimer positively pins the property that actually
    matters and cannot be satisfied by silence.
    """
    from nexus.health import _check_catalog_legacy_file

    _make_catalog_db(tmp_path / "catalog" / ".catalog.db", documents=docs, links=links)
    detail = _check_catalog_legacy_file(config_dir=tmp_path)[0].detail.lower()
    assert "frozen" in detail
    assert "not a live mirror" in detail
    assert "postgres" in detail
