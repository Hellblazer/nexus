# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tenant backup file (nexus-csayy.3, RDR-248): the writer and the verifier against each other, and the
verifier against hand-built damaged files.

No engine: pages here are real PostgreSQL binary COPY streams built by hand (:func:`copy_page`), the shape
``GET /v1/tenant/export/{id}/rows`` serves. Each damaged file is made by editing bytes of a good one, and each
must be refused naming the table (or the header, or the trailer) it found the damage in.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

import pytest

from nexus.backup import tenant_file as tf
from nexus.backup.tenant_file import (
    TenantFileError,
    TenantFileWriter,
    count_copy_rows,
    iter_copy_tuples,
    verify_file,
)

_SIG = b"PGCOPY\n\xff\r\n\0"


def copy_page(rows: list[list[bytes | None]]) -> bytes:
    """A complete PostgreSQL binary COPY stream of *rows* (each a list of raw field bytes, None for NULL)."""
    out = bytearray(_SIG + struct.pack(">ii", 0, 0))
    for row in rows:
        out += struct.pack(">h", len(row))
        for field in row:
            if field is None:
                out += struct.pack(">i", -1)
            else:
                out += struct.pack(">i", len(field)) + field
    out += struct.pack(">h", -1)
    return bytes(out)


def header(tables: dict[str, tuple[str, int]], **extra: object) -> dict:
    """An engine-shaped header: tables maps name to (class, column count)."""
    return {
        "format_version": 1,
        "engine_version": "0.1.191",
        "schema_changeset_count": 572,
        "tenant_id": "acme",
        "created_at": "2026-11-12T19:22:00.123Z",
        "tables": [
            {"name": n, "class": cls, "primary_key": ["id"],
             "columns": [{"name": f"c{i}", "type": "text"} for i in range(ncols)]}
            for n, (cls, ncols) in tables.items()
        ],
        "embedding_models": [],
        **extra,
    }


def page_meta(body: bytes, rows: int, *, done: bool, last_key: str | None = "k") -> dict:
    return {"rows": rows, "done": done, "last_key": last_key, "sha256": hashlib.sha256(body).hexdigest()}


def write_good(path: Path, *, pages_per_table: int = 2) -> dict[str, list[bytes]]:
    """A two-table file; returns the page bodies by table."""
    h = header({"memory": ("data", 2), "frecency": ("derived", 1)})
    bodies: dict[str, list[bytes]] = {"memory": [], "frecency": []}
    with path.open("wb") as fh:
        w = TenantFileWriter(fh)
        w.write_header(h)
        for name, ncols in (("memory", 2), ("frecency", 1)):
            total = pages_per_table * 2
            w.begin_table(name, total)
            for p in range(pages_per_table):
                rows = [[b"%d-%d-%d" % (p, r, c) for c in range(ncols)] for r in range(2)]
                body = copy_page(rows)
                bodies[name].append(body)
                w.add_page(name, body, rows=2, last_key=f"key{p}", done=(p == pages_per_table - 1),
                           sha256=hashlib.sha256(body).hexdigest())
            w.end_table()
        w.finish()
    return bodies


# -- the COPY reader ---------------------------------------------------------------------------------


def test_count_copy_rows_counts_tuples_and_checks_field_count() -> None:
    body = copy_page([[b"a", None], [b"b", b"c"], [b"", b"d"]])
    assert count_copy_rows(body) == 3
    assert count_copy_rows(body, fields=2) == 3
    with pytest.raises(ValueError, match="2 fields, expected 3"):
        count_copy_rows(body, fields=3)
    assert count_copy_rows(copy_page([])) == 0


@pytest.mark.parametrize("damage", ["signature", "truncated", "no_trailer", "trailing_bytes"])
def test_count_copy_rows_refuses_a_malformed_stream(damage: str) -> None:
    body = bytearray(copy_page([[b"abc"], [b"def"]]))
    if damage == "signature":
        body[0:4] = b"XXXX"
    elif damage == "truncated":
        body = body[:-8]
    elif damage == "no_trailer":
        body = body[:-2]
    else:
        body += b"junk"
    with pytest.raises(ValueError):
        count_copy_rows(bytes(body))


def test_iter_copy_tuples_yields_the_fields() -> None:
    rows = [[b"a", None], [b"\x01", b"zz"]]
    assert [[bytes(f) if f is not None else None for f in t] for t in iter_copy_tuples(copy_page(rows))] == rows


# -- writer and reader against each other ------------------------------------------------------------


def test_a_written_file_verifies_and_reports_its_header_and_tables(tmp_path: Path) -> None:
    path = tmp_path / "good.nxtenant"
    write_good(path)
    report = verify_file(path)
    assert report.header["tenant_id"] == "acme" and report.header["engine_version"] == "0.1.191"
    assert report.header["schema_changeset_count"] == 572
    assert [(t.name, t.table_class, t.rows, t.pages) for t in report.tables] == [
        ("memory", "data", 4, 2), ("frecency", "derived", 4, 2),
    ]
    assert report.size == path.stat().st_size
    assert report.rows_by_class() == {"data": 4, "derived": 4}


def test_the_table_hash_is_over_the_page_bodies_in_order(tmp_path: Path) -> None:
    path = tmp_path / "good.nxtenant"
    bodies = write_good(path)
    report = verify_file(path)
    expected = hashlib.sha256(b"".join(bodies["memory"])).hexdigest()
    assert report.tables[0].sha256 == expected


def test_an_empty_table_is_a_frame_with_one_empty_page(tmp_path: Path) -> None:
    path = tmp_path / "empty.nxtenant"
    h = header({"memory": ("data", 1)})
    body = copy_page([])
    with path.open("wb") as fh:
        w = TenantFileWriter(fh)
        w.write_header(h)
        w.begin_table("memory", 0)
        w.add_page("memory", body, rows=0, last_key=None, done=True, sha256=hashlib.sha256(body).hexdigest())
        w.end_table()
        w.finish()
    report = verify_file(path)
    assert [(t.name, t.rows, t.pages) for t in report.tables] == [("memory", 0, 1)]


def test_the_writer_refuses_a_table_out_of_order_or_unfinished(tmp_path: Path) -> None:
    h = header({"memory": ("data", 1), "plans": ("data", 1)})
    with (tmp_path / "x").open("wb") as fh:
        w = TenantFileWriter(fh)
        w.write_header(h)
        with pytest.raises(TenantFileError, match="plans"):
            w.begin_table("plans", 0)  # memory comes first
        w.begin_table("memory", 0)
        with pytest.raises(TenantFileError, match="memory"):
            w.finish()  # memory has no pages yet


def test_the_writer_refuses_a_page_whose_hash_is_not_its_bodys(tmp_path: Path) -> None:
    h = header({"memory": ("data", 1)})
    body = copy_page([[b"a"]])
    with (tmp_path / "x").open("wb") as fh:
        w = TenantFileWriter(fh)
        w.write_header(h)
        w.begin_table("memory", 1)
        with pytest.raises(TenantFileError, match="memory.*hash"):
            w.add_page("memory", body, rows=1, last_key="k", done=True, sha256="0" * 64)


def test_the_writer_refuses_a_table_whose_pages_do_not_add_up_to_its_count(tmp_path: Path) -> None:
    h = header({"memory": ("data", 1)})
    body = copy_page([[b"a"]])
    with (tmp_path / "x").open("wb") as fh:
        w = TenantFileWriter(fh)
        w.write_header(h)
        w.begin_table("memory", 5)
        w.add_page("memory", body, rows=1, last_key="k", done=True, sha256=hashlib.sha256(body).hexdigest())
        with pytest.raises(TenantFileError, match="memory.*1 rows.*5"):
            w.end_table()


# -- hand-built damaged files: each refused, the table named -----------------------------------------


def _frames(raw: bytes) -> list[tuple[int, bytes, int]]:
    """(offset, kind, length) of each frame of a good file, to aim a hand edit."""
    out = []
    pos = len(tf.FILE_MAGIC) + 2
    while pos < len(raw):
        kind = raw[pos:pos + 1]
        (length,) = struct.unpack_from(">Q", raw, pos + 1)
        out.append((pos, kind, length))
        pos += 9 + length
    return out


def test_a_truncated_page_frame_is_refused_naming_its_table(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    raw = path.read_bytes()
    pages = [f for f in _frames(raw) if f[1] == b"P"]
    cut = pages[1][0] + 9 + 20  # inside the second page of memory
    path.write_bytes(raw[:cut])
    with pytest.raises(TenantFileError, match="memory") as err:
        verify_file(path)
    assert "truncated" in str(err.value)


def test_a_file_that_stops_between_frames_names_the_missing_trailer(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    raw = path.read_bytes()
    trailer = [f for f in _frames(raw) if f[1] == b"E"][0]
    path.write_bytes(raw[: trailer[0]])
    with pytest.raises(TenantFileError, match="trailer") as err:
        verify_file(path)
    assert "frecency" in str(err.value), "names the last table it read"


def test_a_page_with_a_bad_hash_is_refused_naming_its_table_and_page(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    raw = bytearray(path.read_bytes())
    pages = [f for f in _frames(bytes(raw)) if f[1] == b"P"]
    off, _, length = pages[2]  # the first page of frecency
    raw[off + 9 + length - 3] ^= 0xFF  # flip a byte of the body
    path.write_bytes(bytes(raw))
    with pytest.raises(TenantFileError, match="frecency") as err:
        verify_file(path)
    assert "page 1" in str(err.value) and "hash" in str(err.value)


def _rewrite_page_meta(raw: bytes, which: int, change: dict) -> bytes:
    off, _, length = [f for f in _frames(raw) if f[1] == b"P"][which]
    payload = raw[off + 9: off + 9 + length]
    (mlen,) = struct.unpack_from(">I", payload, 0)
    meta = json.loads(payload[4:4 + mlen])
    meta.update(change)
    mb = json.dumps(meta, separators=(",", ":")).encode()
    new_payload = struct.pack(">I", len(mb)) + mb + payload[4 + mlen:]
    return raw[:off] + b"P" + struct.pack(">Q", len(new_payload)) + new_payload + raw[off + 9 + length:]


def test_a_page_whose_claimed_rows_differ_from_its_tuples_is_a_count_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    path.write_bytes(_rewrite_page_meta(path.read_bytes(), 0, {"rows": 3}))
    with pytest.raises(TenantFileError, match="memory") as err:
        verify_file(path)
    assert "3 rows" in str(err.value) and "2" in str(err.value)


def test_a_table_whose_pages_do_not_add_up_to_its_count_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    raw = path.read_bytes()
    t_frames = [f for f in _frames(raw) if f[1] == b"T"]
    off, _, length = t_frames[0]
    meta = json.loads(raw[off + 9: off + 9 + length])
    meta["table_rows"] = 7
    mb = json.dumps(meta, separators=(",", ":")).encode()
    path.write_bytes(raw[:off] + b"T" + struct.pack(">Q", len(mb)) + mb + raw[off + 9 + length:])
    with pytest.raises(TenantFileError, match="memory") as err:
        verify_file(path)
    assert "7" in str(err.value) and "4" in str(err.value)


def test_a_trailer_that_disagrees_with_a_table_is_refused_naming_it(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    raw = path.read_bytes()
    off, _, length = [f for f in _frames(raw) if f[1] == b"E"][0]
    trailer = json.loads(raw[off + 9: off + 9 + length])
    trailer["tables"][1]["rows"] += 1
    tb = json.dumps(trailer, separators=(",", ":")).encode()
    path.write_bytes(raw[:off] + b"E" + struct.pack(">Q", len(tb)) + tb)
    with pytest.raises(TenantFileError, match="frecency") as err:
        verify_file(path)
    assert "trailer" in str(err.value)


def test_a_trailer_table_sha_that_differs_is_refused_naming_the_table(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    raw = path.read_bytes()
    off, _, length = [f for f in _frames(raw) if f[1] == b"E"][0]
    trailer = json.loads(raw[off + 9: off + 9 + length])
    trailer["tables"][0]["sha256"] = "ab" * 32
    tb = json.dumps(trailer, separators=(",", ":")).encode()
    path.write_bytes(raw[:off] + b"E" + struct.pack(">Q", len(tb)) + tb)
    with pytest.raises(TenantFileError, match="memory") as err:
        verify_file(path)
    assert "SHA-256" in str(err.value)


def test_damage_to_the_header_alone_is_caught_by_the_whole_file_hash(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    raw = bytearray(path.read_bytes())
    off, _, _ = _frames(bytes(raw))[0]
    i = raw.index(b"acme", off)
    raw[i:i + 4] = b"evil"  # same length: every frame still parses
    path.write_bytes(bytes(raw))
    with pytest.raises(TenantFileError, match="whole file"):
        verify_file(path)


def test_a_file_that_is_not_one_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    path.write_bytes(b"PGDMP this is a pg_dump file")
    with pytest.raises(TenantFileError, match="not a nexus tenant backup"):
        verify_file(path)
    path.write_bytes(b"")
    with pytest.raises(TenantFileError, match="empty"):
        verify_file(path)


def test_a_newer_file_format_is_refused_by_version(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    write_good(path)
    raw = bytearray(path.read_bytes())
    raw[len(tf.FILE_MAGIC):len(tf.FILE_MAGIC) + 2] = struct.pack(">H", 9)
    path.write_bytes(bytes(raw))
    with pytest.raises(TenantFileError, match="format version 9"):
        verify_file(path)


def test_a_header_table_missing_from_the_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    h = header({"memory": ("data", 1), "plans": ("data", 1)})
    body = copy_page([])
    # build with the writer's own pieces, skipping the second table, so only the verifier can notice
    with path.open("wb") as fh:
        w = TenantFileWriter(fh, check_complete=False)
        w.write_header(h)
        w.begin_table("memory", 0)
        w.add_page("memory", body, rows=0, last_key=None, done=True, sha256=hashlib.sha256(body).hexdigest())
        w.end_table()
        w.finish()
    with pytest.raises(TenantFileError, match="plans"):
        verify_file(path)


def test_a_header_with_no_changeset_count_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    h = header({"memory": ("data", 1)}, schema_changeset_count=None)
    with path.open("wb") as fh:
        w = TenantFileWriter(fh)
        with pytest.raises(TenantFileError, match="schema_changeset_count"):
            w.write_header(h)


def test_page_rows_with_the_wrong_field_count_are_refused_naming_the_table(tmp_path: Path) -> None:
    path = tmp_path / "t.nxtenant"
    h = header({"memory": ("data", 2)})  # two columns declared
    body = copy_page([[b"only-one-field"]])
    with path.open("wb") as fh:
        w = TenantFileWriter(fh)
        w.write_header(h)
        w.begin_table("memory", 1)
        w.add_page("memory", body, rows=1, last_key="k", done=True, sha256=hashlib.sha256(body).hexdigest())
        w.end_table()
        w.finish()
    with pytest.raises(TenantFileError, match="memory") as err:
        verify_file(path)
    assert "1 fields, expected 2" in str(err.value)
