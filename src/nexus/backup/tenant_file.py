# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cloud tenant backup file, ``*.nxtenant`` (nexus-csayy.3, RDR-248 "Client").

One framed file holds one export session of one tenant: the engine's header, then for every table a start
frame and one frame per page the engine served, then a trailer of per-table row counts and SHA-256. A page
body is the engine's own complete PostgreSQL binary COPY stream, kept byte for byte, so the restore side
(RDR-248 Phase 2) feeds it to ``COPY ... FROM STDIN (FORMAT binary)`` without translating anything.

Layout, all integers big-endian::

    file    := MAGIC "NXTENANT" | u16 file format version | frame*
    frame   := kind (1 byte) | u64 payload length | payload
    H       the header: UTF-8 JSON, the engine's POST /v1/tenant/export answer without its session fields
    T       a table starts: JSON {"table", "table_rows"} (the count the table's first page carried)
    P       a page: u32 meta length | meta JSON {"table", "rows", "last_key", "done", "sha256"} | COPY body
    E       the trailer: JSON {"format_version", "tables": [{"name", "rows", "pages", "sha256"}],
            "total_rows", "file_sha256"}; the last frame, nothing follows

A table's SHA-256 is over its page bodies, concatenated in order. ``file_sha256`` is over every byte before
the trailer frame, so a flipped byte in a header or a page's meta, which no page hash covers, still fails.

:func:`verify_file` is ``nx backup --check``: a streaming pass, no engine. Every refusal is a
:class:`TenantFileError` whose message names the table (or says it is the header or the trailer).

Imported by ``nexus.backup.cloud_export``, ``nexus.backup.cloud_import`` and ``nexus.commands.backup_cmd`` only (``tests/test_nx_backup_pin.py``).
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

FILE_MAGIC = b"NXTENANT"
FILE_FORMAT_VERSION = 1
#: The engine's header and page format (``/version`` ``tenant_export_format_version``) this client reads.
ENGINE_FORMAT_VERSIONS = frozenset({1})

KIND_HEADER = b"H"
KIND_TABLE = b"T"
KIND_PAGE = b"P"
KIND_TRAILER = b"E"

_FRAME_HEAD = struct.Struct(">cQ")
_COPY_SIGNATURE = b"PGCOPY\n\xff\r\n\0"
_FILE_HEAD_LEN = len(FILE_MAGIC) + 2
#: A frame payload larger than this is damage, not data: a page is cut at about 16 MB and a header is KBs.
_MAX_PAYLOAD = 512 * 1024 * 1024


class TenantFileError(Exception):
    """A backup file (or a page offered to the writer) that does not verify. ``table`` names where, when known."""

    def __init__(self, message: str, table: str | None = None) -> None:
        super().__init__(message)
        self.table = table


# -- PostgreSQL binary COPY, just enough to count tuples and read a boolean ---------------------------


def _copy_body_start(body: bytes | memoryview) -> int:
    if len(body) < len(_COPY_SIGNATURE) + 8 or bytes(body[: len(_COPY_SIGNATURE)]) != _COPY_SIGNATURE:
        raise ValueError("not a PostgreSQL binary COPY stream (bad signature)")
    (ext,) = struct.unpack_from(">i", body, len(_COPY_SIGNATURE) + 4)
    pos = len(_COPY_SIGNATURE) + 8 + ext
    if ext < 0 or pos > len(body):
        raise ValueError("binary COPY header extension runs past the end")
    return pos


def iter_copy_tuples(body: bytes) -> Iterator[list[memoryview | None]]:
    """Each tuple of a binary COPY stream as its fields (a memoryview of the raw bytes, None for NULL).

    Raises ValueError on a stream that is malformed, cut short, or has bytes after its trailer."""
    view = memoryview(body)
    n = len(view)
    pos = _copy_body_start(view)
    while True:
        if pos + 2 > n:
            raise ValueError("binary COPY stream ends before its trailer")
        (nfields,) = struct.unpack_from(">h", view, pos)
        pos += 2
        if nfields == -1:
            if pos != n:
                raise ValueError(f"{n - pos} bytes follow the binary COPY trailer")
            return
        if nfields < 0:
            raise ValueError(f"binary COPY tuple with {nfields} fields")
        fields: list[memoryview | None] = []
        for _ in range(nfields):
            if pos + 4 > n:
                raise ValueError("binary COPY stream ends inside a tuple")
            (flen,) = struct.unpack_from(">i", view, pos)
            pos += 4
            if flen == -1:
                fields.append(None)
                continue
            if flen < 0 or pos + flen > n:
                raise ValueError("binary COPY field runs past the end of the stream")
            fields.append(view[pos : pos + flen])
            pos += flen
        yield fields


def count_copy_rows(body: bytes, *, fields: int | None = None) -> int:
    """The number of tuples in a binary COPY stream; with *fields*, every tuple must have exactly that many.

    The same walk as :func:`iter_copy_tuples` without building the fields, since this runs over every page of
    a multi-gigabyte backup. Raises ValueError on a malformed stream."""
    view = memoryview(body)
    n = len(view)
    unpack_h = struct.Struct(">h").unpack_from
    unpack_i = struct.Struct(">i").unpack_from
    pos = _copy_body_start(view)
    rows = 0
    while True:
        if pos + 2 > n:
            raise ValueError("binary COPY stream ends before its trailer")
        (nfields,) = unpack_h(view, pos)
        pos += 2
        if nfields == -1:
            if pos != n:
                raise ValueError(f"{n - pos} bytes follow the binary COPY trailer")
            return rows
        if nfields < 0:
            raise ValueError(f"binary COPY tuple with {nfields} fields")
        if fields is not None and nfields != fields:
            raise ValueError(f"row {rows + 1} has {nfields} fields, expected {fields}")
        for _ in range(nfields):
            if pos + 4 > n:
                raise ValueError("binary COPY stream ends inside a tuple")
            (flen,) = unpack_i(view, pos)
            pos += 4
            if flen > 0:
                pos += flen
            elif flen != -1 and flen != 0:
                raise ValueError("binary COPY field has a negative length")
        if pos > n:
            raise ValueError("binary COPY field runs past the end of the stream")
        rows += 1


def count_true_in_column(body: bytes, column: int) -> int:
    """How many tuples of *body* hold a one-byte boolean ``true`` in field *column* (a ``boolean`` column)."""
    n = 0
    for fields in iter_copy_tuples(body):
        value = fields[column] if column < len(fields) else None
        if value is not None and len(value) == 1 and value[0] == 1:
            n += 1
    return n


# -- the writer ---------------------------------------------------------------------------------------


def _frame(kind: bytes, payload: bytes) -> bytes:
    return _FRAME_HEAD.pack(kind, len(payload)) + payload


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass
class _TableState:
    name: str
    table_rows: int
    rows: int = 0
    pages: int = 0
    digest: Any = field(default_factory=hashlib.sha256)
    done: bool = False


class TenantFileWriter:
    """Writes one tenant file to *fh* (a binary file opened by the caller, who also closes and syncs it).

    The call order is ``write_header``, then per table ``begin_table``, ``add_page`` one or more times,
    ``end_table``, and finally ``finish``. Each step checks what it can see: a table out of the header's
    order, a page whose hash is not its body's, pages that do not add up to the table's count, a table left
    unfinished. A failure is a :class:`TenantFileError` naming the table; the caller discards the file.

    *check_complete* is a test seam: ``False`` lets ``finish`` write a trailer for a file that skipped tables,
    so a test can hand the verifier such a file.
    """

    def __init__(self, fh: IO[bytes], *, check_complete: bool = True) -> None:
        self._fh = fh
        self._check_complete = check_complete
        self._file_digest = hashlib.sha256()
        self._header: dict[str, Any] | None = None
        self._order: list[str] = []
        self._next = 0
        self._current: _TableState | None = None
        self._finished: list[_TableState] = []
        self.bytes_written = 0

    def _emit(self, data: bytes, *, in_file_hash: bool = True) -> None:
        self._fh.write(data)
        self.bytes_written += len(data)
        if in_file_hash:
            self._file_digest.update(data)

    def write_header(self, header: dict[str, Any]) -> None:
        if self._header is not None:
            raise TenantFileError("the header is written once")
        if header.get("schema_changeset_count") is None:
            raise TenantFileError(
                "the engine's header has no schema_changeset_count (it could not read its changelog), so the "
                "file would carry no schema marker a restore can check; refusing to write it"
            )
        if header.get("format_version") not in ENGINE_FORMAT_VERSIONS:
            raise TenantFileError(f"the engine's header has format version {header.get('format_version')!r}, which this client does not read")
        self._header = header
        self._order = [t["name"] for t in header["tables"]]
        self._emit(FILE_MAGIC + struct.pack(">H", FILE_FORMAT_VERSION))
        self._emit(_frame(KIND_HEADER, _json_bytes(header)))

    def begin_table(self, name: str, table_rows: int) -> None:
        if self._header is None:
            raise TenantFileError("a table before the header", name)
        if self._current is not None:
            raise TenantFileError(f"table {name} starts before table {self._current.name} ended", name)
        expected = self._order[self._next] if self._next < len(self._order) else None
        if name != expected:
            raise TenantFileError(f"table {name} is out of order: the header says {expected or 'no more tables'} is next", name)
        self._current = _TableState(name, table_rows)
        self._emit(_frame(KIND_TABLE, _json_bytes({"table": name, "table_rows": table_rows})))

    def add_page(
        self, table: str, body: bytes, *, rows: int, last_key: str | None, done: bool, sha256: str,
    ) -> None:
        state = self._current
        if state is None or state.name != table:
            raise TenantFileError(f"a page of table {table} outside its table frame", table)
        if state.done:
            raise TenantFileError(f"table {table} has a page after its last", table)
        actual = hashlib.sha256(body).hexdigest()
        if actual != sha256:
            raise TenantFileError(
                f"table {table} page {state.pages + 1}: its hash is {actual}, the engine said {sha256}", table
            )
        meta = _json_bytes({"table": table, "rows": rows, "last_key": last_key, "done": done, "sha256": actual})
        self._emit(_frame(KIND_PAGE, struct.pack(">I", len(meta)) + meta + body))
        state.digest.update(body)
        state.rows += rows
        state.pages += 1
        state.done = done

    def end_table(self) -> None:
        state = self._current
        if state is None:
            raise TenantFileError("end_table with no table open")
        if state.pages == 0:
            raise TenantFileError(f"table {state.name} has no pages", state.name)
        if not state.done:
            raise TenantFileError(f"table {state.name} ended without its last page", state.name)
        if state.rows != state.table_rows:
            raise TenantFileError(
                f"table {state.name}: its pages hold {state.rows} rows, its first page said {state.table_rows}", state.name
            )
        self._finished.append(state)
        self._current = None
        self._next += 1

    def finish(self) -> dict[str, Any]:
        """Write the trailer and return it."""
        if self._header is None:
            raise TenantFileError("finish before the header")
        if self._current is not None:
            raise TenantFileError(f"table {self._current.name} was not finished", self._current.name)
        if self._check_complete and self._next != len(self._order):
            missing = self._order[self._next]
            raise TenantFileError(f"table {missing} was never written", missing)
        trailer = {
            "format_version": FILE_FORMAT_VERSION,
            "tables": [
                {"name": s.name, "rows": s.rows, "pages": s.pages, "sha256": s.digest.hexdigest()}
                for s in self._finished
            ],
            "total_rows": sum(s.rows for s in self._finished),
            "file_sha256": self._file_digest.hexdigest(),
        }
        self._emit(_frame(KIND_TRAILER, _json_bytes(trailer)), in_file_hash=False)
        return trailer


# -- the verifier -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TableSummary:
    name: str
    table_class: str
    rows: int
    pages: int
    sha256: str


@dataclass(frozen=True)
class FileReport:
    header: dict[str, Any]
    tables: list[TableSummary]
    size: int
    file_sha256: str

    def rows_by_class(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for t in self.tables:
            out[t.table_class] = out.get(t.table_class, 0) + t.rows
        return out


def verify_file(path: Path) -> FileReport:
    """Check *path* end to end (``nx backup --check``): frame structure, every page's hash and row count, every
    table's count and SHA-256 against the trailer, the whole-file hash. No engine is contacted.

    Returns the header and per-table summary. Raises :class:`TenantFileError` naming the table at fault."""
    try:
        size = path.stat().st_size
        fh = path.open("rb")
    except OSError as exc:
        raise TenantFileError(f"cannot read {path}: {exc.strerror or exc}") from None
    if size == 0:
        fh.close()
        raise TenantFileError(f"{path} is empty")
    with fh:
        head = fh.read(_FILE_HEAD_LEN)
        if len(head) < _FILE_HEAD_LEN or head[: len(FILE_MAGIC)] != FILE_MAGIC:
            raise TenantFileError(f"{path} is not a nexus tenant backup (it does not start with the {FILE_MAGIC.decode()} marker)")
        (version,) = struct.unpack(">H", head[len(FILE_MAGIC):])
        if version != FILE_FORMAT_VERSION:
            raise TenantFileError(
                f"{path} has file format version {version}; this client reads version {FILE_FORMAT_VERSION}. "
                "Upgrade nx to read it."
            )
        return _verify_frames(fh, size, head)


def _verify_frames(fh: IO[bytes], size: int, head: bytes) -> FileReport:  # noqa: C901 - one linear pass, kept in one place
    file_digest = hashlib.sha256(head)
    header: dict[str, Any] | None = None
    declared: dict[str, tuple[str, int]] = {}
    order: list[str] = []
    summaries: list[TableSummary] = []
    current: _TableState | None = None
    last_table: str | None = None
    next_table = 0

    def where() -> str:
        return f"table {last_table}" if last_table else "the header"

    def close_table() -> None:
        nonlocal current, next_table
        if current is None:
            return
        if not current.done:
            raise TenantFileError(f"table {current.name} ends without its last page", current.name)
        if current.rows != current.table_rows:
            raise TenantFileError(
                f"table {current.name}: its pages hold {current.rows} rows, its first page said {current.table_rows}",
                current.name,
            )
        summaries.append(TableSummary(current.name, declared[current.name][0], current.rows, current.pages, current.digest.hexdigest()))
        current = None
        next_table += 1

    while True:
        raw_head = fh.read(_FRAME_HEAD.size)
        if not raw_head:
            raise TenantFileError(f"the file ends before its trailer (the last table read was {last_table or 'none: only the header'})", last_table)
        if len(raw_head) < _FRAME_HEAD.size:
            raise TenantFileError(f"the file is truncated inside a frame header after {where()}", last_table)
        kind, length = _FRAME_HEAD.unpack(raw_head)
        if length > _MAX_PAYLOAD:
            raise TenantFileError(f"a frame after {where()} claims {length} bytes: the file is damaged", last_table)
        payload = fh.read(length)
        if len(payload) < length:
            where_at = {
                KIND_TRAILER: "the trailer", KIND_HEADER: "the header",
                KIND_PAGE: f"table {current.name} page {current.pages + 1}" if current else where(),
            }.get(kind, f"a frame after {where()}")
            raise TenantFileError(f"the file is truncated in {where_at}: a frame needs {length} bytes and {len(payload)} remain", last_table)
        if kind == KIND_TRAILER:
            break
        file_digest.update(raw_head)
        file_digest.update(payload)
        if kind == KIND_HEADER:
            if header is not None:
                raise TenantFileError("a second header frame")
            try:
                header = json.loads(payload)
                order = [t["name"] for t in header["tables"]]
                declared = {t["name"]: (t["class"], len(t["columns"])) for t in header["tables"]}
            except (ValueError, KeyError, TypeError) as exc:
                raise TenantFileError(f"the header frame does not parse: {exc}") from None
            if header.get("format_version") not in ENGINE_FORMAT_VERSIONS:
                raise TenantFileError(f"the header says engine format version {header.get('format_version')!r}, which this client does not read")
            if header.get("schema_changeset_count") is None:
                raise TenantFileError("the header has no schema_changeset_count, so no schema marker a restore can check")
            if not isinstance(header.get("tenant_id"), str) or not header["tenant_id"]:
                raise TenantFileError("the header names no tenant")
            if not order:
                raise TenantFileError("the header lists no tables")
        elif header is None:
            raise TenantFileError("a frame before the header")
        elif kind == KIND_TABLE:
            close_table()
            try:
                meta = json.loads(payload)
                name, table_rows = str(meta["table"]), int(meta["table_rows"])
            except (ValueError, KeyError, TypeError) as exc:
                raise TenantFileError(f"a table frame after {where()} does not parse: {exc}", last_table) from None
            expected = order[next_table] if next_table < len(order) else None
            if name != expected:
                raise TenantFileError(f"table {name} is out of order: the header says {expected or 'no more tables'} is next", name)
            current = _TableState(name, table_rows)
            last_table = name
        elif kind == KIND_PAGE:
            if current is None:
                raise TenantFileError(f"a page frame outside a table frame after {where()}", last_table)
            _verify_page(current, declared[current.name][1], payload)
        else:
            raise TenantFileError(f"an unknown frame kind {kind!r} after {where()}", last_table)

    if header is None:
        raise TenantFileError("the file has no header frame")
    close_table()
    if fh.read(1):
        raise TenantFileError("bytes follow the trailer: the file is damaged")
    if next_table != len(order):
        missing = order[next_table]
        raise TenantFileError(f"table {missing} is in the header and not in the file", missing)
    try:
        trailer = json.loads(payload)
        listed = {t["name"]: t for t in trailer["tables"]}
        claimed_file_sha = str(trailer["file_sha256"])
    except (ValueError, KeyError, TypeError) as exc:
        raise TenantFileError(f"the trailer does not parse: {exc}") from None
    for s in summaries:
        entry = listed.get(s.name)
        if entry is None:
            raise TenantFileError(f"table {s.name} is in the file and not in the trailer", s.name)
        if entry.get("rows") != s.rows or entry.get("pages") != s.pages:
            raise TenantFileError(
                f"table {s.name}: the trailer says {entry.get('rows')} rows in {entry.get('pages')} pages, the file holds {s.rows} rows in {s.pages}", s.name
            )
        if entry.get("sha256") != s.sha256:
            raise TenantFileError(f"table {s.name}: its SHA-256 is {s.sha256}, the trailer says {entry.get('sha256')}", s.name)
    extra = set(listed) - {s.name for s in summaries}
    if extra:
        raise TenantFileError(f"the trailer lists table {sorted(extra)[0]}, which the file does not hold", sorted(extra)[0])
    if file_digest.hexdigest() != claimed_file_sha:
        raise TenantFileError(
            "the whole file does not match its trailer's SHA-256 (damage in a header or a page's own metadata, "
            "which no page hash covers)"
        )
    return FileReport(header=header, tables=summaries, size=size, file_sha256=claimed_file_sha)


def _verify_page(state: _TableState, columns: int, payload: bytes) -> None:
    name = state.name
    n = state.pages + 1
    if state.done:
        raise TenantFileError(f"table {name} has a page after its last (page {n})", name)
    if len(payload) < 4:
        raise TenantFileError(f"table {name} page {n} is too short to hold its metadata", name)
    (mlen,) = struct.unpack_from(">I", payload, 0)
    try:
        meta = json.loads(payload[4 : 4 + mlen])
        rows, done, claimed = int(meta["rows"]), bool(meta["done"]), str(meta["sha256"])
    except (ValueError, KeyError, TypeError) as exc:
        raise TenantFileError(f"table {name} page {n}: its metadata does not parse: {exc}", name) from None
    body = payload[4 + mlen :]
    actual = hashlib.sha256(body).hexdigest()
    if actual != claimed:
        raise TenantFileError(f"table {name} page {n}: its hash is {actual}, the page says {claimed}", name)
    try:
        counted = count_copy_rows(body, fields=columns)
    except ValueError as exc:
        raise TenantFileError(f"table {name} page {n}: {exc}", name) from None
    if counted != rows:
        raise TenantFileError(f"table {name} page {n}: it says {rows} rows and holds {counted}", name)
    state.digest.update(body)
    state.rows += rows
    state.pages += 1
    state.done = done


# -- reading a file back for a restore (nexus-csayy.6) -------------------------------------------------


@dataclass(frozen=True)
class PageRef:
    """Where one page's COPY body sits in the file, and what the page's own metadata says of it."""

    offset: int
    length: int
    rows: int
    sha256: str


@dataclass(frozen=True)
class TableIndex:
    name: str
    table_class: str
    rows: int
    pages: list[PageRef]


@dataclass(frozen=True)
class FileIndex:
    """What a restore needs from a file without holding it in memory: the header, the SHA-256 of the header
    frame's payload (the marker the engine writes names it), the sum of the page bodies' bytes, and every
    table's page positions."""

    header: dict[str, Any]
    header_sha256: str
    row_bytes: int
    tables: dict[str, TableIndex]


def index_file(path: Path) -> FileIndex:
    """Walk *path*'s frames and record each page's position. It does not verify (call :func:`verify_file`
    first): it reads only frame heads and page metadata, so it costs a few seeks per page, not the file.

    Raises :class:`TenantFileError` on a structure it cannot walk."""
    try:
        fh = path.open("rb")
    except OSError as exc:
        raise TenantFileError(f"cannot read {path}: {exc.strerror or exc}") from None
    with fh:
        head = fh.read(_FILE_HEAD_LEN)
        if len(head) < _FILE_HEAD_LEN or head[: len(FILE_MAGIC)] != FILE_MAGIC:
            raise TenantFileError(f"{path} is not a nexus tenant backup")
        header: dict[str, Any] | None = None
        header_sha = ""
        declared: dict[str, str] = {}
        tables: dict[str, TableIndex] = {}
        current: str | None = None
        pages: list[PageRef] = []
        table_rows = 0
        row_bytes = 0

        def close_current() -> None:
            nonlocal current, pages, table_rows
            if current is not None:
                tables[current] = TableIndex(current, declared[current], table_rows, pages)
            current, pages, table_rows = None, [], 0

        while True:
            raw = fh.read(_FRAME_HEAD.size)
            if len(raw) < _FRAME_HEAD.size:
                raise TenantFileError("the file ends before its trailer")
            kind, length = _FRAME_HEAD.unpack(raw)
            body_start = fh.tell()
            if kind == KIND_TRAILER:
                break
            if kind == KIND_PAGE:
                if current is None:
                    raise TenantFileError("a page frame outside a table frame")
                meta_head = fh.read(4)
                if len(meta_head) < 4:
                    raise TenantFileError(f"table {current}: a page is truncated", current)
                (mlen,) = struct.unpack(">I", meta_head)
                try:
                    meta = json.loads(fh.read(mlen))
                    rows, sha = int(meta["rows"]), str(meta["sha256"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise TenantFileError(f"table {current}: a page's metadata does not parse: {exc}", current) from None
                size = length - 4 - mlen
                if size < 0:
                    raise TenantFileError(f"table {current}: a page's metadata runs past its frame", current)
                pages.append(PageRef(body_start + 4 + mlen, size, rows, sha))
                table_rows += rows
                row_bytes += size
                fh.seek(body_start + length)
                continue
            payload = fh.read(length)
            if len(payload) < length:
                raise TenantFileError("the file is truncated inside a frame")
            try:
                if kind == KIND_HEADER:
                    header = json.loads(payload)
                    header_sha = hashlib.sha256(payload).hexdigest()
                    declared = {t["name"]: str(t["class"]) for t in header["tables"]}
                elif kind == KIND_TABLE:
                    close_current()
                    current = str(json.loads(payload)["table"])
                    if current not in declared:
                        raise TenantFileError(f"table {current} is not in the header", current)
                else:
                    raise TenantFileError(f"an unknown frame kind {kind!r}")
            except (ValueError, KeyError, TypeError) as exc:
                raise TenantFileError(f"a frame does not parse: {exc}") from None
        close_current()
        if header is None:
            raise TenantFileError("the file has no header frame")
        return FileIndex(header, header_sha, row_bytes, tables)


def read_page_body(fh: IO[bytes], ref: PageRef) -> bytes:
    """The COPY body of one page, read from *fh* (a file opened for reading)."""
    fh.seek(ref.offset)
    body = fh.read(ref.length)
    if len(body) != ref.length:
        raise TenantFileError("the file is shorter than it was when it was indexed")
    return body


def split_copy_body(body: bytes, max_part_bytes: int) -> list[tuple[bytes, int]]:
    """Cut one page's binary COPY stream at row boundaries into complete COPY streams (the page's own header,
    whole rows, the trailer) of at most *max_part_bytes* each. Returns ``(part, rows)`` pairs; a page with no
    rows yields none. Raises ValueError for a stream that is malformed or a row too big for any part."""
    view = memoryview(body)
    n = len(view)
    start = _copy_body_start(view)
    head = bytes(view[:start])
    trailer = struct.pack(">h", -1)
    budget = max_part_bytes - len(head) - len(trailer)
    if budget <= 0:
        raise ValueError(f"a part of {max_part_bytes} bytes cannot hold a COPY header")
    unpack_h = struct.Struct(">h").unpack_from
    unpack_i = struct.Struct(">i").unpack_from
    parts: list[tuple[bytes, int]] = []
    pos = chunk_start = start
    chunk_rows = 0
    while True:
        if pos + 2 > n:
            raise ValueError("binary COPY stream ends before its trailer")
        (nfields,) = unpack_h(view, pos)
        if nfields == -1:
            if pos + 2 != n:
                raise ValueError(f"{n - pos - 2} bytes follow the binary COPY trailer")
            break
        if nfields < 0:
            raise ValueError(f"binary COPY tuple with {nfields} fields")
        row_start = pos
        pos += 2
        for _ in range(nfields):
            if pos + 4 > n:
                raise ValueError("binary COPY stream ends inside a tuple")
            (flen,) = unpack_i(view, pos)
            pos += 4
            if flen > 0:
                pos += flen
            elif flen not in (-1, 0):
                raise ValueError("binary COPY field has a negative length")
        if pos > n:
            raise ValueError("binary COPY field runs past the end of the stream")
        if pos - row_start > budget:
            raise ValueError(f"a row of {pos - row_start} bytes does not fit a part of {max_part_bytes} bytes")
        if chunk_rows and pos - chunk_start > budget:
            parts.append((head + bytes(view[chunk_start:row_start]) + trailer, chunk_rows))
            chunk_start, chunk_rows = row_start, 0
        chunk_rows += 1
    if chunk_rows:
        parts.append((head + bytes(view[chunk_start:pos]) + trailer, chunk_rows))
    return parts
