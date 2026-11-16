# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/e2e/backup_roundtrip_compare.py (nexus-csayy.10): two ``.nxtenant`` files, the source tenant's export and
an export of the tenant it was restored into, compared offline: per-table row counts, then per-column and per-row
digests over the columns a restore keeps. A restore gives new primary keys and rewrites the references to them, so
an integer column, or ``tenant_id``, may differ; any other differing column is a finding."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from nexus.backup.tenant_file import TenantFileWriter
from tests.test_tenant_file import copy_page

_SCRIPT = Path(__file__).resolve().parents[1] / "e2e" / "backup_roundtrip_compare.py"


def _load():
    spec = importlib.util.spec_from_file_location("backup_roundtrip_compare", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, tenant: str, tables: dict[str, tuple[list[tuple[str, str]], list[list[bytes | None]]]]) -> None:
    head = {
        "format_version": 1, "engine_version": "0.1.192", "schema_changeset_count": 600, "tenant_id": tenant,
        "created_at": "2026-11-15T12:00:00Z", "embedding_models": [],
        "tables": [
            {"name": n, "class": "data", "primary_key": [cols[0][0]], "columns": [{"name": c, "type": t} for c, t in cols]}
            for n, (cols, _) in tables.items()
        ],
    }
    with path.open("wb") as fh:
        w = TenantFileWriter(fh)
        w.write_header(head)
        for name, (_, rows) in tables.items():
            body = copy_page(rows)
            w.begin_table(name, len(rows))
            w.add_page(name, body, rows=len(rows), last_key=None, done=True, sha256=hashlib.sha256(body).hexdigest())
            w.end_table()
        w.finish()


_MEM_COLS = [("id", "bigint"), ("title", "text"), ("content", "text")]
_CHUNK_COLS = [("chash", "bytea"), ("text", "text"), ("doc_id", "bigint")]


def _source(path: Path) -> None:
    _write(path, "src", {
        "memory": (_MEM_COLS, [[b"1", b"a", b"x"], [b"2", b"b", b"y"]]),
        "chunks": (_CHUNK_COLS, [[b"h1", b"t1", b"1"], [b"h2", b"t2", b"2"]]),
        "catalog_meta": ([("k", "text"), ("v", "text")], [[b"k1", b"v1"]]),
    })


def test_a_restore_with_new_ids_and_its_own_marker_row_passes(tmp_path: Path) -> None:
    mod = _load()
    a, b = tmp_path / "a.nxtenant", tmp_path / "b.nxtenant"
    _source(a)
    _write(b, "dst", {
        "memory": (_MEM_COLS, [[b"902", b"b", b"y"], [b"901", b"a", b"x"]]),   # ids reassigned, order changed
        "chunks": (_CHUNK_COLS, [[b"h2", b"t2", b"902"], [b"h1", b"t1", b"901"]]),   # reference rewritten
        "catalog_meta": ([("k", "text"), ("v", "text")], [[b"k1", b"v1"], [b"rdr248_restore", b"m"]]),
    })
    report = mod.compare(a, b)
    assert report["verdict"] == "PASS", json.dumps(report, indent=1)
    assert report["tables"]["memory"]["differing_columns"] == ["id"]
    assert report["tables"]["memory"]["rows_equal"] is True
    assert report["tables"]["catalog_meta"]["judged"] is False


@pytest.mark.parametrize(
    ("memory_rows", "reason"),
    [
        ([[b"901", b"a", b"x"]], "count"),                                   # a row lost
        ([[b"901", b"a", b"x"], [b"902", b"b", b"CHANGED"]], "content"),      # a text column changed
        ([[b"901", b"a", b"y"], [b"902", b"b", b"x"]], "rows"),               # every column's values kept, rows mixed
    ],
)
def test_a_lost_row_or_changed_content_fails_naming_the_table(tmp_path: Path, memory_rows, reason: str) -> None:
    mod = _load()
    a, b = tmp_path / "a.nxtenant", tmp_path / "b.nxtenant"
    _source(a)
    _write(b, "dst", {
        "memory": (_MEM_COLS, memory_rows),
        "chunks": (_CHUNK_COLS, [[b"h1", b"t1", b"901"], [b"h2", b"t2", b"902"]]),
        "catalog_meta": ([("k", "text"), ("v", "text")], [[b"k1", b"v1"]]),
    })
    report = mod.compare(a, b)
    assert report["verdict"] == "FAIL", json.dumps(report, indent=1)
    assert any(f.startswith("memory:") and reason in f for f in report["failures"]), report["failures"]
    assert not any(f.startswith("chunks:") for f in report["failures"]), report["failures"]


def test_a_differing_jsonb_column_is_held_for_review_not_passed(tmp_path: Path) -> None:
    mod = _load()
    cols = [("id", "bigint"), ("summary", "jsonb")]
    a, b = tmp_path / "a.nxtenant", tmp_path / "b.nxtenant"
    _write(a, "src", {"runs": (cols, [[b"1", b'{"plan_id": 1}']])})
    _write(b, "dst", {"runs": (cols, [[b"901", b'{"plan_id": 901}']])})
    report = mod.compare(a, b)
    assert report["verdict"] == "REVIEW", json.dumps(report, indent=1)
    assert report["review"] == ["runs: summary (jsonb) differs"]


def test_a_table_missing_from_one_file_fails(tmp_path: Path) -> None:
    mod = _load()
    a, b = tmp_path / "a.nxtenant", tmp_path / "b.nxtenant"
    _source(a)
    _write(b, "dst", {"memory": (_MEM_COLS, [[b"901", b"a", b"x"], [b"902", b"b", b"y"]])})
    report = mod.compare(a, b)
    assert report["verdict"] == "FAIL"
    assert "tables only in the source: catalog_meta, chunks" in report["failures"]


def test_the_cli_prints_json_and_exits_by_verdict(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mod = _load()
    a = tmp_path / "a.nxtenant"
    _source(a)
    assert mod.main([str(a), str(a)]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "PASS"
