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


# RDR-229 P3.11: a restore relabels every minted-form label c<old> to c<new>, the label of the row's new id, in the
# registry, every name column, a note's source_uri and a chunk's origin_collection tag; the marker lists the pairs.
_REG = [("collection_id", "bigint"), ("name", "text"), ("superseded_by", "text")]
_DOCS = [("doc_id", "bigint"), ("physical_collection", "text"), ("source_uri", "text")]
_CHK = [("chash", "bytea"), ("collection", "text"), ("metadata", "jsonb")]
_RELABELS = {"c7": "c901", "c8": "c902"}


def _relabel_source(path: Path) -> None:
    _write(path, "src", {
        "catalog_collections": (_REG, [[b"7", b"c7", b""], [b"8", b"c8", b"c7"], [b"9", b"named", b""]]),
        "catalog_documents": (_DOCS, [[b"1", b"c7", b"nx-note://c7/a"], [b"2", b"named", b"file:///c7/b"]]),
        "chunks": (_CHK, [[b"h1", b"c8", b'{"origin_collection": "c7", "k": 1}'], [b"h2", b"named", b'{"k": 2}']]),
    })


def _relabel_restored(path: Path, *, reg_name: bytes = b"c902", uri: bytes = b"nx-note://c901/a") -> None:
    _write(path, "dst", {
        "catalog_collections": (_REG, [[b"901", b"c901", b""], [b"902", reg_name, b"c901"], [b"903", b"named", b""]]),
        "catalog_documents": (_DOCS, [[b"901", b"c901", uri], [b"902", b"named", b"file:///c7/b"]]),
        "chunks": (_CHK, [[b"h1", b"c902", b'{"origin_collection": "c901", "k": 1}'], [b"h2", b"named", b'{"k": 2}']]),
    })


def test_a_restore_that_relabelled_its_minted_labels_passes_through_the_markers_map(tmp_path: Path) -> None:
    mod = _load()
    a, b = tmp_path / "a.nxtenant", tmp_path / "b.nxtenant"
    _relabel_source(a)
    _relabel_restored(b)
    assert mod.compare(a, b)["verdict"] == "FAIL"  # without the map, every relabelled column differs
    report = mod.compare(a, b, _RELABELS)
    assert report["verdict"] == "PASS", json.dumps(report, indent=1)
    assert report["relabels_applied"] == 2
    for table in ("catalog_collections", "catalog_documents", "chunks"):
        assert report["tables"][table]["rows_equal"] is True, table


@pytest.mark.parametrize(
    ("restored", "column"),
    [
        ({"reg_name": b"c903"}, "catalog_collections: column name"),           # a label the map does not name
        ({"uri": b"nx-note://c7/a"}, "catalog_documents: column source_uri"),  # a note URI left on the old label
    ],
)
def test_a_name_column_the_map_does_not_explain_still_fails(tmp_path: Path, restored, column: str) -> None:
    mod = _load()
    a, b = tmp_path / "a.nxtenant", tmp_path / "b.nxtenant"
    _relabel_source(a)
    _relabel_restored(b, **restored)
    report = mod.compare(a, b, _RELABELS)
    assert report["verdict"] == "FAIL", json.dumps(report, indent=1)
    assert any(f.startswith(column) for f in report["failures"]), report["failures"]


def test_a_value_that_only_contains_an_old_label_is_not_rewritten(tmp_path: Path) -> None:
    """file:///c7/b is not a note URI and "named" is not minted: the map rewrites only a value that IS an old label,
    the nx-note://<label>/ prefix and the origin_collection tag, as RestoreIdFill does."""
    mod = _load()
    a, b = tmp_path / "a.nxtenant", tmp_path / "b.nxtenant"
    _relabel_source(a)
    _write(b, "dst", {
        "catalog_collections": (_REG, [[b"901", b"c901", b""], [b"902", b"c902", b"c901"], [b"903", b"named", b""]]),
        "catalog_documents": (_DOCS, [[b"901", b"c901", b"nx-note://c901/a"], [b"902", b"named", b"file:///c901/b"]]),
        "chunks": (_CHK, [[b"h1", b"c902", b'{"origin_collection": "c901", "k": 1}'], [b"h2", b"named", b'{"k": 2}']]),
    })
    report = mod.compare(a, b, _RELABELS)
    assert report["verdict"] == "FAIL"
    assert any(f.startswith("catalog_documents: column source_uri") for f in report["failures"]), report["failures"]


def test_the_map_is_read_from_the_import_marker_and_a_truncated_list_is_refused(tmp_path: Path, capsys) -> None:
    mod = _load()
    pairs = [{"label": k, "target_label": v} for k, v in _RELABELS.items()]
    body = {"present": True, "marker": {"collections_relabelled": pairs, "collections_relabelled_count": 2}}
    assert mod.relabels_from_marker(body) == _RELABELS
    assert mod.relabels_from_marker(body["marker"]) == _RELABELS
    assert mod.relabels_from_marker({"present": True, "marker": {"header_sha256": "x"}}) == {}
    with pytest.raises(ValueError, match="lists 2 of 1001"):
        mod.relabels_from_marker({"marker": {"collections_relabelled": pairs, "collections_relabelled_count": 1001}})
    a, b, m = tmp_path / "a.nxtenant", tmp_path / "b.nxtenant", tmp_path / "marker.json"
    _relabel_source(a)
    _relabel_restored(b)
    m.write_text(json.dumps(body))
    assert mod.main([str(a), str(b), "--relabels", str(m)]) == 0
    assert json.loads(capsys.readouterr().out)["relabels_applied"] == 2
    assert mod.main([str(a), str(b)]) == 1


def test_the_name_columns_are_the_restores_own_list() -> None:
    cols = _load().name_columns()
    assert cols["catalog_collections"] == {"name", "superseded_by"}
    assert cols["catalog_documents"] == {"physical_collection"}
    assert cols["chash_remap"] == {"source_collection", "target_collection"}
    assert "-" not in set().union(*cols.values())
