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

from pathlib import Path



def _touch_relic(path: Path, size: int = 4096) -> Path:
    """A leftover pre-PG ``.catalog.db``. Nothing parses it any more (2026-08-29:
    there is no path back to that era), so its content is arbitrary bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path



class TestCatalogLegacyFileCheck:
    def test_absent_file_produces_no_row(self, tmp_path: Path) -> None:
        """A clean install must not grow a permanent warning."""
        from nexus.health import _check_catalog_legacy_file

        assert _check_catalog_legacy_file(config_dir=tmp_path) == []

    def test_populated_file_is_named_as_a_relic_without_counts(
        self, tmp_path: Path,
    ) -> None:
        """Steve's shape, revised 2026-08-29: the row used to carry row counts
        read from the file so a human could tell which store was
        authoritative. Nothing reads the file any more (there is no path back
        to the Chroma/SQLite era), so the row names it as a relic with its
        size and mtime, says Postgres is authoritative, and says to delete it."""
        from nexus.health import _check_catalog_legacy_file

        _touch_relic(tmp_path / "catalog" / ".catalog.db", size=155648)
        results = _check_catalog_legacy_file(config_dir=tmp_path)

        assert len(results) == 1
        r = results[0]
        assert not r.ok            # visible, not buried in the green
        assert not r.fatal         # a relic is expected, not broken
        detail = r.detail.lower()
        assert "relic" in detail
        assert "no path back" in detail
        assert "postgres" in detail
        assert "155648" in r.detail
        assert "document rows" not in detail   # no probe, no counts
        assert any("delete" in s.lower() for s in r.fix_suggestions)
        assert any("restore" in s.lower() for s in r.fix_suggestions)

    def test_empty_but_present_file_is_still_flagged(self, tmp_path: Path) -> None:
        """The dev-box shape. An empty legacy file is still named — it is
        still a relic on disk, and the operator should know it is dead."""
        from nexus.health import _check_catalog_legacy_file

        _touch_relic(tmp_path / "catalog" / ".catalog.db", size=1)
        results = _check_catalog_legacy_file(config_dir=tmp_path)

        assert len(results) == 1
        assert "relic" in results[0].detail.lower()

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
        assert "relic" in results[0].detail.lower()

    def test_both_files_present_yields_one_row_each(self, tmp_path: Path) -> None:
        from nexus.health import _check_catalog_legacy_file

        _touch_relic(tmp_path / "catalog" / ".catalog.db")
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


def test_states_the_disclaimer(tmp_path: Path) -> None:
    """The row must positively state that the file is a relic nothing reads,
    that there is no path back to it, and that Postgres is authoritative.

    Formerly parametrised over populations read from the file; the probe is
    gone (2026-08-29), so there is one shape to pin.
    """
    from nexus.health import _check_catalog_legacy_file

    _touch_relic(tmp_path / "catalog" / ".catalog.db")
    detail = _check_catalog_legacy_file(config_dir=tmp_path)[0].detail.lower()
    assert "relic" in detail
    assert "nothing reads it" in detail
    assert "no path back" in detail
    assert "postgres" in detail
