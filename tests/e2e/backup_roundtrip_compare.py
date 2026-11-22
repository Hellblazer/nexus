#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compare two ``.nxtenant`` files offline: a tenant's export and an export of the tenant it was restored into
(nexus-csayy.10, the RDR-248 cloud round trip). Reads only the two files and contacts no engine.

Per table, in order:

1. Both files must hold the table, and its row counts must be equal.
2. Per column, an order-free digest of the column's values. A restore gives a table new primary keys and
   rewrites the references to them, and every reassigned id is an integer, so a differing integer column (or
   ``tenant_id``) is expected. A differing ``jsonb`` column may hold a rewritten id (``nx_answer_runs.run_summary``)
   or changed content, which only a person can tell, so it is held for REVIEW. Any other differing column FAILS.
3. Per row, an order-free digest over the columns that matched in step 2. Equal per-column digests with unequal
   rows means values moved between rows, which FAILS.

Tables a restore writes to itself (``catalog_meta``: the restore's marker row) are reported and not judged.

A restore also relabels every minted-form collection label (``c<id>``) to the label of the row's new id (RDR-229
P3.11), and reports each pair in the import marker's ``collections_relabelled``. Given that map (``relabels``, or
``--relabels MARKER.json``), the source's rows are rewritten the way the engine's ``RestoreIdFill`` rewrites the
stage before they are digested: the registry's ``name``, every name column ``db/rdr229/p31-checks.tsv`` lists, a
note's ``nx-note://<label>/`` prefix in ``source_uri``, and a chunk's ``origin_collection`` metadata tag. Only a value
that IS an old label (or that prefix, or that tag) is rewritten, so any other difference in those columns still fails.

Usage: ``uv run python tests/e2e/backup_roundtrip_compare.py SOURCE.nxtenant RESTORED.nxtenant [--relabels
MARKER.json]``, the marker being the restored tenant's ``GET /v1/tenant/import-marker`` body (or its ``marker``). It
refuses a marker whose list is shorter than its ``collections_relabelled_count``. It prints one
JSON report and exits 0 on PASS, 1 on FAIL, 3 on REVIEW. Verify both files with ``nx backup --check`` first: this
script assumes they are sound and does not re-check their hashes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

from nexus.backup.tenant_file import index_file, iter_copy_tuples, read_page_body

#: Tables the restore itself writes rows into; their counts and content are reported, not judged.
NOT_JUDGED: dict[str, str] = {
    "catalog_meta": "the restore writes its own marker row (rdr248_restore), and the tenant's first request seeds another",
}
_INTEGER_TYPES = {"smallint", "integer", "bigint", "int2", "int4", "int8"}
_REVIEW_TYPES = {"jsonb", "json"}
_MOD = 1 << 256
_CHECKS = Path(__file__).resolve().parents[2] / "service" / "src" / "main" / "resources" / "db" / "rdr229" / "p31-checks.tsv"
_NOTE = b"nx-note://"
#: The tables whose ``source_uri`` follows a relabelled collection (RestoreIdFill.NOTE_URI_TABLES).
_NOTE_URI_TABLES = {"catalog_documents", "document_aspects", "document_highlights"}
_ORIGIN_TAG = b'"origin_collection": "'

Row = list[bytes | None]


def name_columns() -> dict[str, set[str]]:
    """The name columns the restore's relabel rewrites, per table: the registry's ``name`` and every name column of
    ``p31-checks.tsv`` (RestoreIdFill.relabelRegistry and relabelNames)."""
    out: dict[str, set[str]] = {"catalog_collections": {"name"}}
    for line in _CHECKS.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or line.startswith("table\t"):
            continue
        table, column = line.split("\t")[:2]
        if column != "-":
            out.setdefault(table, set()).add(column)
    return out


def relabels_from_marker(marker: dict[str, Any]) -> dict[str, str]:
    """The old-to-new label map of an import marker (``GET /v1/tenant/import-marker``, or its ``marker``)."""
    m = marker.get("marker", marker)
    pairs = m.get("collections_relabelled") or []
    count = m.get("collections_relabelled_count", len(pairs))
    if count != len(pairs):
        raise ValueError(f"the marker lists {len(pairs)} of {count} relabels; the comparison needs them all")
    return {p["label"]: p["target_label"] for p in pairs}


def _rewriter(table: str, cols: list[tuple[str, str]], relabels: dict[str, str]) -> Callable[[Row], Row] | None:
    if not relabels:
        return None
    names = name_columns().get(table, set())
    exact = [i for i, (c, _) in enumerate(cols) if c in names]
    uri = [i for i, (c, _) in enumerate(cols) if c == "source_uri"] if table in _NOTE_URI_TABLES else []
    tag = [i for i, (c, _) in enumerate(cols) if c == "metadata"] if table == "chunks" else []
    if not (exact or uri or tag):
        return None
    m = {k.encode(): v.encode() for k, v in relabels.items()}

    def rewrite(row: Row) -> Row:
        row = list(row)
        for i in exact:
            if row[i] in m:
                row[i] = m[row[i]]  # type: ignore[index]
        for i in uri:
            v = row[i]
            if v is not None and v.startswith(_NOTE):
                label, sep, rest = v[len(_NOTE):].partition(b"/")
                if sep and label in m:
                    row[i] = _NOTE + m[label] + b"/" + rest
        for i in tag:
            v = row[i]
            if v is not None and _ORIGIN_TAG in v:
                start = v.index(_ORIGIN_TAG) + len(_ORIGIN_TAG)
                end = v.find(b'"', start)
                if end > start and v[start:end] in m:
                    row[i] = v[:start] + m[v[start:end]] + v[end:]
        return row

    return rewrite


def _h(*parts: bytes | None) -> int:
    d = hashlib.sha256()
    for p in parts:
        if p is None:
            d.update(b"\x00N")
        else:
            d.update(b"\x01" + len(p).to_bytes(8, "big") + p)
    return int.from_bytes(d.digest(), "big")


def _may_differ(name: str, type_: str) -> bool:
    base = type_.removesuffix("[]")
    return name == "tenant_id" or base in _INTEGER_TYPES


def _table_rows(path: Path, idx: Any, table: str, rewrite: Callable[[Row], Row] | None = None) -> Any:
    with path.open("rb") as fh:
        for ref in idx.tables[table].pages:
            body = read_page_body(fh, ref)
            for fields in iter_copy_tuples(body):
                row: Row = [None if f is None else bytes(f) for f in fields]
                yield rewrite(row) if rewrite else row


def _column_digests(path: Path, idx: Any, table: str, ncols: int, rewrite: Callable[[Row], Row] | None = None) -> list[int]:
    sums = [0] * ncols
    for row in _table_rows(path, idx, table, rewrite):
        for i, v in enumerate(row):
            sums[i] = (sums[i] + _h(v)) % _MOD
    return sums


def _row_digest(path: Path, idx: Any, table: str, keep: list[int], rewrite: Callable[[Row], Row] | None = None) -> int:
    total = 0
    for row in _table_rows(path, idx, table, rewrite):
        total = (total + _h(*(row[i] for i in keep))) % _MOD
    return total


def compare(source: Path, restored: Path, relabels: dict[str, str] | None = None) -> dict[str, Any]:  # noqa: C901
    a, b = index_file(source), index_file(restored)
    cols_a = {t["name"]: [(c["name"], c["type"]) for c in t["columns"]] for t in a.header["tables"]}
    cols_b = {t["name"]: [(c["name"], c["type"]) for c in t["columns"]] for t in b.header["tables"]}
    failures: list[str] = []
    review: list[str] = []
    only_a, only_b = sorted(set(a.tables) - set(b.tables)), sorted(set(b.tables) - set(a.tables))
    if only_a:
        failures.append(f"tables only in the source: {', '.join(only_a)}")
    if only_b:
        failures.append(f"tables only in the restored file: {', '.join(only_b)}")
    tables: dict[str, Any] = {}
    for name in sorted(set(a.tables) & set(b.tables)):
        ra, rb = a.tables[name].rows, b.tables[name].rows
        entry: dict[str, Any] = {"rows_source": ra, "rows_restored": rb, "judged": name not in NOT_JUDGED}
        tables[name] = entry
        if name in NOT_JUDGED:
            entry["why_not_judged"] = NOT_JUDGED[name]
            continue
        if cols_a[name] != cols_b[name]:
            failures.append(f"{name}: columns differ between the files")
            continue
        if ra != rb:
            failures.append(f"{name}: row count {ra} -> {rb}")
            continue
        cols = cols_a[name]
        rewrite = _rewriter(name, cols, relabels or {})
        da = _column_digests(source, a, name, len(cols), rewrite)
        db = _column_digests(restored, b, name, len(cols))
        differing = [i for i in range(len(cols)) if da[i] != db[i]]
        entry["differing_columns"] = [cols[i][0] for i in differing]
        for i in differing:
            col, type_ = cols[i]
            if _may_differ(col, type_):
                continue
            if type_ in _REVIEW_TYPES:
                review.append(f"{name}: {col} ({type_}) differs")
            else:
                failures.append(f"{name}: column {col} ({type_}) differs")
        keep = [i for i in range(len(cols)) if i not in differing]
        if keep:
            entry["rows_equal"] = _row_digest(source, a, name, keep, rewrite) == _row_digest(restored, b, name, keep)
            if not entry["rows_equal"]:
                failures.append(f"{name}: rows differ over the columns that match ({', '.join(cols[i][0] for i in keep)})")
    verdict = "FAIL" if failures else ("REVIEW" if review else "PASS")
    return {
        "verdict": verdict,
        "source": {"path": str(source), "tenant_id": a.header.get("tenant_id"), "header_sha256": a.header_sha256},
        "restored": {"path": str(restored), "tenant_id": b.header.get("tenant_id"), "header_sha256": b.header_sha256},
        "failures": failures,
        "review": review,
        "relabels_applied": len(relabels or {}),
        "tables": tables,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    relabels: dict[str, str] = {}
    if "--relabels" in args:
        i = args.index("--relabels")
        if i + 1 >= len(args):
            sys.stderr.write(f"{__doc__}\n")
            return 2
        try:
            relabels = relabels_from_marker(json.loads(Path(args[i + 1]).read_text(encoding="utf-8")))
        except ValueError as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
        del args[i:i + 2]
    if len(args) != 2:
        sys.stderr.write(f"{__doc__}\n")
        return 2
    report = compare(Path(args[0]), Path(args[1]), relabels)
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return {"PASS": 0, "FAIL": 1, "REVIEW": 3}[report["verdict"]]


if __name__ == "__main__":
    sys.exit(main())
