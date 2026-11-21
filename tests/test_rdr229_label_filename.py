# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.6 review I4 (nexus-p2m1x.26): a label is free-form (up to 255 bytes; any of ``:`` ``\\`` ``..`` ``CON``), so a
file or directory named after one goes through ``safe_label_filename``. The sites: ``dump_csvs`` (a directory per
collection) and ``nx store export`` (the default ``.nxexp`` name).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.catalog.orphan_backfill import dump_csvs
from nexus.label_filename import safe_label_filename

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_SAFE = re.compile(r"[A-Za-z0-9._%-]+")

_LABELS = ["..", ".", "a\\b", "a:b", "CON", "con", "Con.txt", "NUL", "COM1", "lpt9", "a/b", "x.", "x ", " x", "a*b?c",
           "é" * 127 + "a", "a" * 255, "notes 50%: café", "日本語", "~tilde", "a\tb"]


@pytest.mark.parametrize("label", _LABELS, ids=lambda s: repr(s)[:20])
def test_the_name_is_one_safe_path_component(label: str) -> None:
    name = safe_label_filename(label)

    assert _SAFE.fullmatch(name), name
    assert name not in {".", ".."}
    assert not name.endswith(".")
    assert name.split(".")[0].upper() not in _WINDOWS_RESERVED
    assert len(name.encode()) <= 120
    assert Path(name).name == name  # no separator survives


def test_a_conformant_collection_name_is_unchanged() -> None:
    for name in ("code__repo-1a2b3c4d__voyage-code-3__v1", "knowledge__my_subject__bge-base-en-v15-768__v1"):
        assert safe_label_filename(name) == name


def test_the_named_cases() -> None:
    assert safe_label_filename("..") == "%2E%2E"
    assert safe_label_filename("a\\b") == "a%5Cb"
    assert safe_label_filename("a:b") == "a%3Ab"
    assert safe_label_filename("CON") == "%43ON"


def test_distinct_labels_get_distinct_names_including_the_truncated_ones() -> None:
    labels = _LABELS + ["a" * 254, "a" * 253 + "b", "é" * 127 + "b"]
    names = [safe_label_filename(label) for label in labels]
    assert len(set(names)) == len(set(labels))


def test_a_255_byte_label_is_cut_to_about_100_bytes_plus_a_hash() -> None:
    name = safe_label_filename("a" * 255)
    assert 100 <= len(name) <= 115
    assert name.startswith("a" * 100)


def test_a_cut_never_leaves_half_an_escape() -> None:
    name = safe_label_filename("é" * 127 + "a")
    body = name.rsplit("-", 1)[0]
    assert re.fullmatch(r"(?:[A-Za-z0-9._-]|%[0-9A-F]{2})+", body), body


@pytest.mark.parametrize("label", ["..", ".", "a\\b", "a:b", "CON", "a/b"])
def test_dump_csvs_writes_inside_the_out_dir(tmp_path: Path, label: str) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    paths = dump_csvs(out_dir, label, [], [], [])

    for p in paths:
        assert p.exists()
        assert out_dir in p.resolve().parents
        assert p.parent.parent == out_dir  # exactly one directory below out_dir
    assert sorted(x.name for x in tmp_path.iterdir()) == ["out"], "nothing was written outside out_dir"


class _FakeDb:
    def list_collections(self) -> list[dict]:
        return [{"name": "notes 50%: café"}, {"name": ".."}]


def test_nx_store_export_names_its_files_with_the_safe_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``nx store export`` (one collection, and ``--all``) writes ``<safe label>.nxexp`` / ``<safe label>-<date>.nxexp``."""
    from unittest.mock import patch

    from click.testing import CliRunner

    from nexus.cli import main

    seen: list[Path] = []

    def fake_export(*, db, collection_name, output_path, includes, excludes):
        seen.append(Path(output_path))
        return {"exported_count": 0, "file_bytes": 0, "elapsed_seconds": 0.0}

    monkeypatch.chdir(tmp_path)
    with patch("nexus.commands.store._t3", return_value=_FakeDb()), \
            patch("nexus.commands.store._resolve_bare_subject", side_effect=lambda c, t3=None: c), \
            patch("nexus.exporter.export_collection", side_effect=fake_export):
        one = CliRunner().invoke(main, ["store", "export", "a:b"])
        every = CliRunner().invoke(main, ["store", "export", "--all", "-o", str(tmp_path / "out")])

    assert one.exit_code == 0, one.output
    assert every.exit_code == 0, every.output
    assert seen[0] == Path("a%3Ab.nxexp")
    assert [p.parent for p in seen[1:]] == [tmp_path / "out", tmp_path / "out"]
    assert [p.name.split("-")[0] for p in seen[1:]] == [safe_label_filename("notes 50%: café"), "%2E%2E"]
