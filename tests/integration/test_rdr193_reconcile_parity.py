# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Step 2 Part D (nexus-u5m5g.3): the parity fixture for the engine's reconcile stage and commit.

The bar of Part A1 is parity: after a commit, every catalog row and link is exactly what today's client path
leaves. This module measures that. One fixture (``FILES``) is seeded TWICE, into two owners of one tenant that
differ only in their repo root. On copy ``old`` the client path runs: ``indexer._catalog_hook`` (``register_many``,
``update_many``, ``_maybe_run_housekeeping`` -> ``_run_housekeeping``) followed by
``indexer._delete_docs_for_paths``. On copy ``new`` the same files are staged and committed over the engine's
``/v1/catalog/reconcile`` routes (the staged rows are built with the hook's own helpers, so they carry what the
Step 6 client will send). Both end states are read back from Postgres, every column of every row, and compared.

What the comparison normalises, and nothing else:

* Tumblers. The two copies have different owner prefixes, so a tumbler is compared as ``<owner label>.<seq>`` (the
  main owner is ``M``, the one other owner of each copy is ``X``). Rows are keyed by ``(owner, file_path, ordinal)``
  and a tumbler-valued column (``alias_of``, a link endpoint, an audit row's tumbler) is compared through that key,
  so a difference in minting never hides a difference in another column. The tumbler literal each row got is
  compared on its own, in the ``minted`` section.
* Time. A ``*_at`` column is compared as set or null. ``indexed_at`` is compared as ``moved`` (differs from the
  value the row held just before the run, or the row is new) or ``kept``: the rule it follows is "moves only when
  head_hash changes" (nexus-927mo), not a clock value.
* The repo root, which is the only thing the two copies differ in: it reads ``@ROOT`` wherever it appears.
* Link ``id`` and the generated ``fts_vector``, which carry no information of their own.

The link generators (``generate_*_links``) are replaced by recorders for the whole test: they belong to Part A4,
and a recorder lets the test see which tumblers the hook handed them (the ``new_tumblers`` and relink lists).

Each fixture case lives under its own top-level directory; a difference is attributed to the case of its row.
``KNOWN_DIVERGENCES`` pins, per case, the difference the RDR deliberately keeps or is silent on, with its reason;
the comparison for such a case asserts exactly that difference, so it fails if the difference goes away or
changes. Every other case must match.

The old path's end state is also the golden file ``tests/fixtures/rdr193_reconcile_parity_expected.json``, which
Step 6 keeps when it deletes the client path. ``NX_PARITY_REGEN_GOLDEN=1`` rewrites it from the old path; without
it the old path is checked against the file and the commit is checked against the file.

Run: ``scripts/build-gate-jar.sh`` then ``uv run pytest -m integration tests/integration/test_rdr193_reconcile_parity.py -p no:randomly``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nexus import indexer
from nexus.catalog import link_generator
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.repo_identity import _repo_identity
from nexus.sensitive_write import SENSITIVE_REASON, sensitive_glob_matches
from tests._engine_substrate import ensure_engine
from tests._tenant_pool import psql

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

GOLDEN = Path(__file__).resolve().parent.parent / "fixtures" / "rdr193_reconcile_parity_expected.json"

SEED_HEAD = "h1"
SEED_MTIME = 1_700_000_000.0
RUN_MTIME = 1_800_000_000.0

COLLECTIONS = {
    "code": "code__rdr193parity",
    "prose": "docs__rdr193parity",
    "pdf": "docs__rdr193parity",
    "rdr": "rdr__rdr193parity",
}
STALE_COLLECTION = "docs__rdr193stale"
SENSITIVE_GLOBS = ("misc/sens_new.txt", "misc/sens_existing.txt")


class _Same:
    def __repr__(self) -> str:
        return "SAME"


class _Broken:
    def __repr__(self) -> str:
        return "BROKEN"


SAME = _Same()      # the run-2 content is the seed content, the file is left as it is (its mtime too)
BROKEN = _Broken()  # the run-2 path is a symlink to nothing: stat and read both fail (an empty file hash)


@dataclass(frozen=True)
class F:
    """One fixture path: its content type, its content at the seed (None: not there yet) and in run 2
    (None: gone from disk, SAME, BROKEN or new text)."""
    rel: str
    ctype: str
    seed: str | None
    run2: Any = SAME


def _rdr(rid: str | None, body: str) -> str:
    head = f"---\ntitle: t\nid: {rid}\n---\n" if rid else ""
    return f"{head}# {body}\n{body} body text\n"


# The order is the mint order of both runs. Eight filler files come first, so that the two shared-hash rows of
# ``ren_shared_hash`` are the 9th and the 10th document of the owner: tumblers with sequence 9 and 10, where a
# text ordering and a numeric ordering of tumblers disagree (the RDR's 1.2.9 and 1.2.10).
FILES: list[F] = [
    *[F(f"fill/f{i:02d}.py", "code", f"def f{i}():\n    return {i}\n") for i in range(1, 9)],
    # ── Rename ────────────────────────────────────────────────────────────────────────────────────────────────
    F("ren_shared_hash/p1.txt", "prose", "shared hash body Z\n"),
    F("ren_shared_hash/p2.txt", "prose", "shared hash body Z\n"),
    F("ren_shared_hash/gone.txt", "prose", "shared hash body Z\n", None),
    F("ren_single/a.txt", "prose", "single rename body\n", None),
    F("ren_single/b.txt", "prose", None, "single rename body\n"),
    F("ren_onto_earlier/a.txt", "prose", "onto earlier body\n", None),
    F("ren_onto_earlier/b.txt", "prose", "onto earlier body\n"),
    F("ren_two_onto_one/m1.txt", "prose", "two onto one body\n", None),
    F("ren_two_onto_one/m2.txt", "prose", "two onto one body\n", None),
    F("ren_two_onto_one/t.txt", "prose", None, "two onto one body\n"),
    F("ren_link_between/a.txt", "prose", "link between body A\n", None),
    F("ren_link_between/b.txt", "prose", "link between body B\n", None),
    F("ren_link_between/a2.txt", "prose", None, "link between body A\n"),
    F("ren_link_between/b2.txt", "prose", None, "link between body B\n"),
    F("ren_self_link/a.txt", "prose", "self link body\n", None),
    F("ren_self_link/b.txt", "prose", "self link body\n"),
    F("ren_link_swept/a.txt", "prose", "link swept body\n", None),
    F("ren_link_swept/a2.txt", "prose", None, "link swept body\n"),
    F("ren_link_swept/s.txt", "prose", "link swept orphan body\n", None),
    F("ren_link_tombstoned/a.txt", "prose", "link tombstoned body\n", None),
    F("ren_link_tombstoned/a2.txt", "prose", None, "link tombstoned body\n"),
    F("ren_link_tombstoned/t.txt", "prose", "link tombstoned partner body\n", None),
    F("ren_non_evid/a.txt", "prose", "non evidentiary body\n", None),
    F("ren_non_evid/a2.txt", "prose", None, "non evidentiary body\n"),
    F("ren_and_deleted/a.txt", "prose", "rename and deleted body\n", None),
    F("ren_and_deleted/b.txt", "prose", None, "rename and deleted body\n"),
    F("ren_and_deleted/other.txt", "prose", "rename and deleted partner body\n"),
    F("ren_alias/a.txt", "prose", "alias endpoint body\n", None),
    F("ren_alias/a2.txt", "prose", None, "alias endpoint body\n"),
    F("ren_alias/alias.txt", "prose", "alias row body\n"),
    F("ren_alias/target.txt", "prose", "alias target body\n"),
    F("alias_update/alias.txt", "prose", "alias update body v1\n", "alias update body v2\n"),
    F("alias_update/target.txt", "prose", "alias update target body\n"),
    # ── Stamp ─────────────────────────────────────────────────────────────────────────────────────────────────
    F("stamp_rdr/id_change.md", "rdr", _rdr("RDR-101", "id change"), _rdr("RDR-102", "id change")),
    F("stamp_rdr/null_existing.md", "rdr", _rdr("RDR-103", "null existing"), _rdr(None, "null existing")),
    F("stamp_rdr/null_new.md", "rdr", None, _rdr(None, "null new")),
    F("stamp_sf/doc.txt", "prose", "source format body\n"),
    F("stamp_tags/absent.py", "code", "def tags_absent():\n    return 1\n"),
    F("stamp_tags/null.py", "code", "def tags_null():\n    return 2\n"),
    F("stamp_tags/empty_str.py", "code", "def tags_empty_str():\n    return 3\n"),
    F("stamp_tags/empty_list.py", "code", "def tags_empty_list():\n    return 4\n"),
    F("stamp_tags/set.py", "code", "def tags_set():\n    return 5\n"),
    F("stamp_cat/cat_absent.txt", "prose", "category absent body\n"),
    F("stamp_cat/cat_set.txt", "prose", "category set body\n"),
    F("stamp_empty_hash/existing.txt", "prose", "empty hash existing body\n", BROKEN),
    F("stamp_empty_hash/new.txt", "prose", None, BROKEN),
    # ── Content hash ──────────────────────────────────────────────────────────────────────────────────────────
    F("ch_head/doc.txt", "prose", "head bump body\n"),
    F("ch_content/doc.txt", "prose", "content change body v1\n", "content change body v2\n"),
    F("ch_rdr/edit.md", "rdr", _rdr("RDR-201", "rdr edit v1"), _rdr("RDR-201", "rdr edit v2")),
    F("ch_fence/new.txt", "prose", None, "fence new body\n"),
    F("ch_fence/changed.txt", "prose", "fence changed body v1\n", "fence changed body v2\n"),
    F("fence/indexing.txt", "prose", "fence indexing body\n"),
    F("fence/failed.txt", "prose", "fence failed body\n"),
    F("fence/complete.txt", "prose", "fence complete body\n"),
    # ── Identity ──────────────────────────────────────────────────────────────────────────────────────────────
    F("id_foreign/x.txt", "prose", None, "foreign owner body\n"),
    F("id_same_owner/x.txt", "prose", "same owner by uri body\n", "same owner by uri body\n"),
    F("id_legacy/unchanged.txt", "prose", "legacy unchanged body\n"),
    F("id_legacy/changed.txt", "prose", "legacy changed body v1\n", "legacy changed body v2\n"),
    F("id_duplicate/dup.txt", "prose", "duplicate path body\n"),
    F("id_deleted_foreign/held.txt", "prose", None, None),
    # ── Miscellany: the other arms of the commit ──────────────────────────────────────────────────────────────
    F("misc/miss.txt", "prose", "miss body\n", None),
    F("misc/del.txt", "prose", "deleted body\n", None),
    F("misc/keep.txt", "prose", "keep body\n"),
    F("misc/reset_miss.txt", "prose", "reset miss body\n"),
    F("misc/sens_existing.txt", "prose", "sensitive existing body\n"),
    F("misc/sens_new.txt", "prose", None, "sensitive new body\n"),
    F("misc/plain_new.txt", "prose", None, "plain new body\n"),
]
DELETED = ["ren_and_deleted/a.txt", "misc/del.txt", "id_deleted_foreign/held.txt"]
KEEP = ["misc/keep.txt"]

#: The action each path must get from the commit. It is not parity: it proves each case reached the arm it is
#: there for, so a case that matches because nothing happened is a failure.
EXPECTED_ACTIONS = {
    "ren_single/b.txt": "created", "ren_two_onto_one/t.txt": "created", "stamp_rdr/null_new.md": "created",
    "ch_fence/new.txt": "created", "misc/plain_new.txt": "created", "misc/sens_new.txt": "created",
    "stamp_empty_hash/new.txt": "created", "ren_link_between/a2.txt": "created", "ren_alias/a2.txt": "created",
    "alias_update/alias.txt": "updated", "misc/reset_miss.txt": "unchanged", "misc/sens_existing.txt": "unchanged",
    "ch_content/doc.txt": "updated", "ch_head/doc.txt": "updated", "stamp_rdr/id_change.md": "updated",
    "stamp_rdr/null_existing.md": "updated", "stamp_sf/doc.txt": "updated", "stamp_tags/absent.py": "updated",
    "stamp_tags/null.py": "updated", "stamp_tags/empty_str.py": "updated", "stamp_tags/empty_list.py": "updated",
    "stamp_cat/cat_absent.txt": "updated", "ch_rdr/edit.md": "updated", "ch_fence/changed.txt": "updated",
    "stamp_empty_hash/existing.txt": "updated", "id_legacy/changed.txt": "updated",
    "fill/f01.py": "unchanged", "ren_shared_hash/p1.txt": "unchanged", "stamp_tags/set.py": "unchanged",
    "stamp_cat/cat_set.txt": "unchanged", "id_legacy/unchanged.txt": "unchanged", "fence/complete.txt": "unchanged",
    "id_same_owner/x.txt": "same_owner_by_uri", "id_foreign/x.txt": "foreign_owner",
    "misc/keep.txt": "keep", "ren_and_deleted/a.txt": "deleted", "misc/del.txt": "deleted",
    "id_deleted_foreign/held.txt": "deleted",
}

#: case -> (reason, the RDR line, the exact differences ``(section, key, column, old, new)``). Filled from the
#: first live run; a case that is not here must match.
KNOWN_DIVERGENCES: dict[str, tuple[str, str, list[tuple]]] = {
    "ren_alias": (
        "a link moved by a rename keeps an alias endpoint as it is; today's link_if_absent goes through POST /link, "
        "which follows alias_of and writes the edge to the canonical row (Part A+B choice 6: the commit does not "
        "resolve aliases on a move). The RDR is silent on it: A1 describes the move without alias resolution, and "
        "the declared alias difference is only the batch link route of A4",
        "863-870 (the rename move, silent); 506-510 (today's POST /link resolves alias_of); 1009-1014 and 1295 (declared, A4 only)",
        [
            ("links", "ren_alias::M:ren_alias/a2.txt#0 -> M:ren_alias/alias.txt#0 [cites]", "<row>", "<absent>", "<present>"),
            ("links", "ren_alias::M:ren_alias/a2.txt#0 -> M:ren_alias/target.txt#0 [cites]", "<row>", "<present>", "<absent>"),
        ],
    ),
}


# ── plumbing: raw SQL against the tenant ─────────────────────────────────────────────────────────────────────────

def _psql(sql: str, tenant: str, **variables: str) -> str:
    proc = psql(ensure_engine(), "\\set ON_ERROR_STOP on\n" + sql, tenant=tenant, **variables)
    assert proc.returncode == 0, f"psql failed: {(proc.stderr or proc.stdout)[-800:]}"
    return proc.stdout.strip()


def _query(sql: str, tenant: str, **variables: str) -> Any:
    out = _psql(sql, tenant, **variables)
    return json.loads(out) if out else None


def _dump(tenant: str) -> dict[str, list[dict]]:
    """Every row of the tenant's catalog, as the database holds it (superuser: row level security does not hide any)."""
    def table(name: str, drop: str = "") -> str:
        return (f"SELECT coalesce(jsonb_agg(to_jsonb(t){drop}), '[]'::jsonb) FROM nexus.{name} t "
                f"WHERE t.tenant_id = :'tenant'")
    return {
        "docs": _query(table("catalog_documents", " - 'tenant_id' - 'fts_vector'"), tenant),
        "links": _query(table("catalog_links", " - 'tenant_id' - 'id'"), tenant),
        "owners": _query(table("catalog_owners", " - 'tenant_id'"), tenant),
        "sens": _query(table("catalog_sensitivity_changes", " - 'tenant_id'"), tenant),
    }


def _update_doc(tenant: str, tumbler: str, set_clause: str) -> None:
    got = _query(
        f"WITH u AS (UPDATE nexus.catalog_documents d SET {set_clause} "
        f"WHERE d.tenant_id = :'tenant' AND d.tumbler = :'t' RETURNING 1) SELECT to_jsonb(count(*)) FROM u",
        tenant, t=tumbler)
    assert got == 1, f"expected to update exactly one row of {tumbler}, updated {got}"


# ── the two copies ───────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Copy:
    label: str
    repo: Path
    name: str = ""
    hash: str = ""
    owner: str = ""
    other: str = ""
    files: list[F] = field(default_factory=list)

    @property
    def root(self) -> str:
        return str(self.repo)

    def path(self, rel: str) -> Path:
        return self.repo / rel


def _write(path: Path, text: str, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _write_seed(c: Copy) -> None:
    for i, f in enumerate(c.files):
        if f.seed is not None:
            _write(c.path(f.rel), f.seed, SEED_MTIME + i)


def _write_run2(c: Copy) -> None:
    for i, f in enumerate(c.files):
        p = c.path(f.rel)
        if f.run2 is SAME:
            continue
        if f.run2 is None:
            if p.is_symlink() or p.exists():
                p.unlink()
        elif f.run2 is BROKEN:
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.is_symlink() or p.exists():
                p.unlink()
            p.symlink_to(c.repo / "nowhere" / f"{i}.gone")
        else:
            _write(p, f.run2, RUN_MTIME + i)


def _present(c: Copy, rels: list[str] | None = None) -> list[F]:
    """The fixture paths run 2 offers: on disk (or a dead symlink) in run 2, not held back, not staged as deleted."""
    out = []
    for f in c.files:
        if f.run2 is None or f.rel in KEEP or f.rel in DELETED:
            continue
        if rels is not None and f.rel not in rels:
            continue
        out.append(f)
    return out


# ── the link generators: recorders ───────────────────────────────────────────────────────────────────────────────

class _Recorder:
    """Stands in for the four link generators of ``_catalog_hook`` and keeps what each was handed."""

    def __init__(self) -> None:
        self.calls: dict[str, list[list[str]]] = {}

    def reset(self) -> None:
        self.calls = {}

    def gen(self, kind: str):
        def _gen(cat, *, writer, new_tumblers, new_content_types):  # noqa: ARG001 — the generators' shared shape
            self.calls.setdefault(kind, []).append(sorted(str(t) for t in new_tumblers))
            return 0
        return _gen

    def last(self, kind: str) -> list[str]:
        return (self.calls.get(kind) or [[]])[-1]


# ── the old path ─────────────────────────────────────────────────────────────────────────────────────────────────

def _run_old(c: Copy, offered: list[F], *, head: str, complete: bool, keep: list[str], deleted: list[str],
             globs: tuple[str, ...] = SENSITIVE_GLOBS) -> dict:
    """Today's client path, exactly as ``index_repository`` drives it: the catalog hook, then the deletions."""
    stale: set[str] = set()
    cdh: dict[str, str] = {}
    fence: dict[str, tuple[str, str]] = {}
    unreg: dict[Path, str] = {}
    wanted: set[Path] = set()
    confirmed: set[Path] = set()
    file_to_doc = indexer._catalog_hook(
        repo=c.repo, repo_name=c.name, repo_hash=c.hash, head_hash=head,
        indexed_files=[(c.path(f.rel), f.ctype, COLLECTIONS[f.ctype]) for f in offered],
        skip_housekeeping=not complete,
        stale_fence_doc_ids=stale, complete_doc_hashes=cdh, needs_fence=fence, unregistered=unreg,
        housekeeping_keep=[c.path(r) for r in keep] or None,
        sensitive_globs=globs, sensitive_wanted=wanted, sensitive_confirmed=confirmed,
    )
    if deleted:
        indexer._delete_docs_for_paths(c.repo, list(deleted))
    assert not unreg, f"the old path left files unregistered: {unreg}"
    return {"file_to_doc": {str(p.relative_to(c.repo)): t for p, t in file_to_doc.items()},
            "stale": stale, "cdh": cdh, "fence": fence, "confirmed": {str(p.relative_to(c.repo)) for p in confirmed}}


# ── the new path ─────────────────────────────────────────────────────────────────────────────────────────────────

def _stage_row(c: Copy, f: F, head: str, seq: int) -> dict:
    """One ``present`` landing row: what ``_catalog_hook`` computes per file before it talks to the catalog."""
    abs_path = c.path(f.rel)
    try:
        mtime = abs_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    try:
        raw = abs_path.read_bytes()
        file_hash = hashlib.sha256(raw).hexdigest()
    except OSError:
        raw, file_hash = None, ""
    stamp = {**indexer._rdr_register_stamp(f.ctype, raw), **indexer.document_register_meta(f.ctype, f.rel)}
    return {
        "kind": "present", "seq": seq, "file_path": f.rel, "content_type": f.ctype, "title": abs_path.name,
        "physical_collection": COLLECTIONS[f.ctype], "head_hash": head, "source_mtime": mtime,
        "content_hash": file_hash,
        "base_metadata": indexer._register_time_meta(f.rel, file_hash) or {},
        "stamp": stamp,
        "sensitive": bool(sensitive_glob_matches(f.rel, SENSITIVE_GLOBS)),
    }


def _run_new(cat: HttpCatalogClient, c: Copy, offered: list[F], *, head: str, complete: bool, keep: list[str],
             deleted: list[str]) -> dict:
    """Stage, commit and poll over the engine's routes."""
    rows = [_stage_row(c, f, head, i) for i, f in enumerate(offered)]
    seq = len(rows)
    for rel in keep:
        rows.append({"kind": "keep", "seq": seq, "file_path": rel})
        seq += 1
    for rel in deleted:
        rows.append({"kind": "deleted", "seq": seq, "file_path": rel})
        seq += 1
    run_id = f"parity-{uuid.uuid4()}"
    walk = "complete" if complete else "partial"
    for start in range(0, len(rows), 1000):
        staged = cat._post("/reconcile/stage", {"owner": c.owner, "run_id": run_id, "walk": walk,
                                                "expected_rows": len(rows), "rows": rows[start:start + 1000]})
        assert staged["staged"] == len(rows[start:start + 1000])
    job = cat._post("/reconcile/commit", {"owner": c.owner, "run_id": run_id})["job_id"]
    deadline = time.monotonic() + 180
    while True:
        status = cat._get(f"/reconcile/{job}")
        if status["status"] != "running":
            break
        assert time.monotonic() < deadline, f"the commit job {job} is still running after 180 s"
        time.sleep(0.2)
    assert status["status"] == "succeeded", f"the commit failed: {status}"
    outcome = status["outcome"]
    # the same run committed twice returns the stored outcome and writes nothing (guard of the function)
    again = cat._post("/reconcile/commit", {"owner": c.owner, "run_id": run_id})["job_id"]
    for _ in range(300):
        s2 = cat._get(f"/reconcile/{again}")
        if s2["status"] != "running":
            break
        time.sleep(0.2)
    assert s2["status"] == "succeeded" and s2["outcome"] == outcome, "a second commit of the run changed the outcome"
    # the upsert never flags an existing document: the client raises the pending ones through the audited route,
    # with the reason the hook uses today (RDR-193 Part A1, 'Sensitivity on existing rows')
    pending = list(outcome["sensitivity_pending"])
    if pending:
        cat.set_sensitivity(pending, True, reason=f"{SENSITIVE_REASON}: nx index repo --sensitive-glob")
    return {"outcome": outcome, "raised": set(pending), "staged": {r["file_path"]: r for r in rows}, "ctype": {f.rel: f.ctype for f in offered}}


# ── the snapshot: one copy's end state, normalised ───────────────────────────────────────────────────────────────

def _ts(v: Any) -> Any:
    return v if v in (None, "") else "<set>"


def _rel(file_path: str, root: str) -> str:
    return file_path[len(root) + 1:] if file_path.startswith(root + "/") else file_path


def _case(rel: str) -> str:
    if not rel:
        return "(no path)"
    return rel.split("/", 1)[0] if "/" in rel else "(root)"


def _k(case: str, rest: str) -> str:
    return f"{case}::{rest}"


class Snapshot:
    """The normalised end state of one copy: ``sections[section][key] -> {column: value}``."""

    def __init__(self, c: Copy, dump: dict, before_indexed_at: dict[str, Any]) -> None:
        self.c = c
        self.prefix = {c.owner: "M", c.other: "X"}
        self.sections: dict[str, dict[str, Any]] = {"documents": {}, "links": {}, "owners": {}, "sensitivity": {},
                                                    "minted": {}, "structures": {}}
        self.key_of: dict[str, str] = {}
        self.case_of_tumbler: dict[str, str] = {}
        docs = [d for d in dump["docs"] if self._owns(d["tumbler"])]
        docs.sort(key=lambda d: (self.prefix[d["tumbler"].rpartition(".")[0]], int(d["tumbler"].rpartition(".")[2])))
        ordinal: Counter = Counter()
        for d in docs:
            t = d["tumbler"]
            owner = self.prefix[t.rpartition(".")[0]]
            rel = _rel(d.get("file_path") or "", c.root)
            n = ordinal[(owner, rel)]
            ordinal[(owner, rel)] += 1
            self.key_of[t] = f"{owner}:{rel}#{n}"
            self.case_of_tumbler[t] = _case(rel)
        for d in docs:
            t = d["tumbler"]
            case = self.case_of_tumbler[t]
            cols: dict[str, Any] = {}
            for col, v in d.items():
                if col == "tumbler":
                    continue
                if col == "alias_of":
                    v = self.key_of.get(v, v) if v else v
                elif col == "indexed_at":
                    prev = before_indexed_at.get(t)
                    v = None if v is None else ("kept" if prev is not None and prev == v else "moved")
                elif col.endswith("_at"):
                    v = _ts(v)
                elif col == "index_run_id":
                    v = "<set>" if v else v
                elif isinstance(v, str):
                    v = v.replace(c.root, "@ROOT")
                cols[col] = v
            self.sections["documents"][_k(case, self.key_of[t])] = cols
            self.sections["minted"][_k(case, self.key_of[t])] = {"tumbler": self.tumbler(t)}
        for lk in dump["links"]:
            if not (self._owns(lk["from_tumbler"]) or self._owns(lk["to_tumbler"])):
                continue
            kf, kt = self.key_of.get(lk["from_tumbler"], lk["from_tumbler"]), self.key_of.get(lk["to_tumbler"], lk["to_tumbler"])
            case = self.case_of_tumbler.get(lk["from_tumbler"]) or self.case_of_tumbler.get(lk["to_tumbler"], "(links)")
            self.sections["links"][_k(case, f"{kf} -> {kt} [{lk['link_type']}]")] = {
                "from_span": lk["from_span"], "to_span": lk["to_span"], "created_by": lk["created_by"],
                "metadata": lk["metadata"], "created_at": _ts(lk["created_at"])}
        for o in dump["owners"]:
            if o["tumbler_prefix"] in self.prefix:
                self.sections["owners"][_k("(owners)", self.prefix[o["tumbler_prefix"]])] = {
                    "owner_type": o["owner_type"], "head_hash": o["head_hash"], "next_seq": o["next_seq"],
                    "repo_root": (o["repo_root"] or "").replace(c.root, "@ROOT"),
                    "deactivated_at": _ts(o.get("deactivated_at"))}
        for s in dump["sens"]:
            if not self._owns(s["tumbler"]):
                continue
            reason = re.sub(r"\b\d+(?:\.\d+)+\b", lambda m: self.key_of.get(m.group(0), m.group(0)), s["reason"])
            self.sections["sensitivity"][_k(self.case_of_tumbler[s["tumbler"]], f"{self.key_of[s['tumbler']]} | {reason}")] = {
                "old": s["old"], "new": s["new"], "actor": s["actor"], "changed_at": _ts(s.get("changed_at"))}

    def _owns(self, tumbler: str) -> bool:
        return tumbler.rpartition(".")[0] in self.prefix

    def tumbler(self, t: str) -> str:
        p, _, s = t.rpartition(".")
        return f"{self.prefix.get(p, p)}.{s}"

    def add_structure(self, name: str, entries: dict[str, Any]) -> None:
        for rel, v in entries.items():
            path = rel.split(":", 1)[1].rsplit("#", 1)[0] if re.match(r"^[MX]:", rel) else rel
            self.sections["structures"][_k(_case(path), f"{name} {rel}")] = {"value": v}


def _diff(a: dict, b: dict) -> list[tuple]:
    """``(section, key, column, a_value, b_value)`` for every column that differs, and for every row on one side only."""
    out: list[tuple] = []
    for section in sorted(set(a) | set(b)):
        ra, rb = a.get(section, {}), b.get(section, {})
        for key in sorted(set(ra) | set(rb)):
            if key not in ra:
                out.append((section, key, "<row>", "<absent>", "<present>"))
            elif key not in rb:
                out.append((section, key, "<row>", "<present>", "<absent>"))
            else:
                for col in sorted(set(ra[key]) | set(rb[key])):
                    va, vb = ra[key].get(col, "<missing column>"), rb[key].get(col, "<missing column>")
                    if va != vb:
                        out.append((section, key, col, va, vb))
    return out


def _case_of_diff(d: tuple) -> str:
    return d[1].split("::", 1)[0]


# ── the structures the hook's callers read ───────────────────────────────────────────────────────────────────────

def _old_structures(snap: Snapshot, run: dict, rec: _Recorder) -> None:
    key = snap.key_of
    snap.add_structure("file_to_doc_id", {rel: key[t] for rel, t in run["file_to_doc"].items()})
    snap.add_structure("needs_fence", {key[t]: [h, coll] for t, (h, coll) in run["fence"].items()})
    snap.add_structure("stale_fence_doc_ids", {key[t]: True for t in run["stale"]})
    snap.add_structure("complete_doc_hashes", {key[t]: h for t, h in run["cdh"].items()})
    snap.add_structure("sensitive_confirmed", {rel: True for rel in run["confirmed"]})
    snap.add_structure("new_tumblers", {key[t]: True for t in rec.last("rdr")})
    snap.add_structure("rdr_dependency_seed", {key[t]: True for t in rec.last("rdr-dependency")})


def _new_structures(snap: Snapshot, run: dict) -> None:
    """The caller-side structures, rebuilt from the outcome's rows by the rules of RDR-193 Part A2."""
    key = snap.key_of
    rows = run["outcome"]["rows"]
    staged = run["staged"]
    placed = [r for r in rows if r["action"] in ("created", "updated", "unchanged", "same_owner_by_uri", "foreign_owner")]
    snap.add_structure("file_to_doc_id", {r["file_path"]: key[r["tumbler"]] for r in placed})
    fence, stale, cdh = {}, {}, {}
    for r in rows:
        s = staged[r["file_path"]]
        if r["action"] == "created" or (r["action"] == "updated" and r["content_hash_changed"]):
            fence[key[r["tumbler"]]] = [s["content_hash"], s["physical_collection"]]
        if r["action"] in ("updated", "unchanged"):
            if r["index_state"] in ("indexing", "failed"):
                stale[key[r["tumbler"]]] = True
            if r["index_state"] == "complete" and r["index_content_hash"]:
                cdh[key[r["tumbler"]]] = r["index_content_hash"]
    snap.add_structure("needs_fence", fence)
    snap.add_structure("stale_fence_doc_ids", stale)
    snap.add_structure("complete_doc_hashes", cdh)
    snap.add_structure("sensitive_confirmed", {r["file_path"]: True for r in rows
                                               if (r["sensitive"] or r["tumbler"] in run["raised"]) and r["action"] != "deleted"})
    created = {key[r["tumbler"]]: True for r in rows if r["action"] == "created"}
    relink = {key[r["tumbler"]]: True for r in rows
              if run["ctype"].get(r["file_path"]) == "rdr" and r["content_hash_changed"]}
    snap.add_structure("new_tumblers", created)
    snap.add_structure("rdr_dependency_seed", {**created, **relink})


# ── the scenario ─────────────────────────────────────────────────────────────────────────────────────────────────

class Scenario:
    def __init__(self, tmp_path: Path, tenant: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tenant = tenant
        self.rec = _Recorder()
        self.cat = HttpCatalogClient()
        self.copies = {label: Copy(label, (tmp_path / f"parity_{label}").resolve(), files=list(FILES)) for label in ("old", "new")}
        monkeypatch.setattr(link_generator, "generate_rdr_filepath_links", self.rec.gen("rdr"))
        monkeypatch.setattr(link_generator, "generate_prose_filepath_links", self.rec.gen("prose"))
        monkeypatch.setattr(link_generator, "generate_pdf_corpus_links", self.rec.gen("pdf"))
        monkeypatch.setattr(link_generator, "bind_rdr_dependency_generator",
                            lambda cat, repo: ("rdr-dependency", self.rec.gen("rdr-dependency")))

    def close(self) -> None:
        self.cat.close()

    # -- the seed, shared by both copies ---------------------------------------------------------------------
    def seed(self, c: Copy) -> None:
        c.repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=c.repo, check=True, capture_output=True)
        c.name, c.hash = _repo_identity(c.repo)
        c.owner = str(self.cat.register_owner(
            c.name, "repo", repo_hash=c.hash, repo_root=str(c.repo.resolve()), description=f"Git repository: {c.name}"))
        c.other = str(self.cat.register_owner(f"parity_other_{c.label}", "curator"))
        _write_seed(c)
        _run_old(c, _present_at_seed(c), head=SEED_HEAD, complete=True, keep=[], deleted=[], globs=())  # no glob: the existing sensitive row is born unflagged
        for rel, seq in (("ren_shared_hash/p1.txt", "9"), ("ren_shared_hash/p2.txt", "10")):
            assert self.t(c, rel).endswith("." + seq), f"{rel} must be document {seq} of the owner, got {self.t(c, rel)}"

    def t(self, c: Copy, rel: str) -> str:
        entry = self.cat.by_file_path(c.owner, rel)
        assert entry is not None, f"{c.label}: no live document for {rel}"
        return str(entry.tumbler)

    # -- the state both copies are put in between the seed and the run ---------------------------------------
    def prepare(self, c: Copy) -> None:
        cat, t, sql = self.cat, lambda rel: self.t(c, rel), lambda rel, clause: _update_doc(self.tenant, self.t(c, rel), clause)
        # Rename
        cat.link(t("ren_link_between/a.txt"), t("ren_link_between/b.txt"), "cites", "parity",
                 from_span="1-3", to_span="4-9", note="the link's metadata is not carried")
        cat.link(t("ren_self_link/a.txt"), t("ren_self_link/b.txt"), "relates", "parity")
        cat.link(t("ren_two_onto_one/m1.txt"), t("ren_two_onto_one/m2.txt"), "relates", "parity")
        cat.update(t("ren_link_swept/s.txt"), meta={"miss_count": 1})
        cat.link(t("ren_link_swept/a.txt"), t("ren_link_swept/s.txt"), "cites", "parity")
        cat.link(t("ren_link_tombstoned/a.txt"), t("ren_link_tombstoned/t.txt"), "cites", "parity")
        cat.delete_document(t("ren_link_tombstoned/t.txt"))
        cat.update(t("ren_non_evid/a.txt"), meta={"non_evidentiary": True})
        cat.link(t("ren_and_deleted/a.txt"), t("ren_and_deleted/other.txt"), "cites", "parity", from_span="2-5")
        cat.link(t("ren_shared_hash/gone.txt"), t("fill/f02.py"), "cites", "parity", to_span="7-8")
        cat.link(t("ren_onto_earlier/a.txt"), t("fill/f03.py"), "cites", "parity", from_span="1-2")
        cat.link(t("ren_two_onto_one/m1.txt"), t("fill/f04.py"), "cites", "parity")
        cat.link(t("fill/f05.py"), t("ren_two_onto_one/m2.txt"), "relates", "parity")
        # the link is made first, to the row as it is, and the row becomes an alias afterwards: a link made to an
        # alias is resolved to its canonical when it is written, which would hide what a rename move does
        cat.link(t("ren_alias/a.txt"), t("ren_alias/alias.txt"), "cites", "parity")
        sql("ren_alias/alias.txt", f"alias_of = '{t('ren_alias/target.txt')}'")
        sql("alias_update/alias.txt", f"alias_of = '{t('alias_update/target.txt')}'")
        # Stamp
        sql("stamp_sf/doc.txt", "metadata = d.metadata || '{\"source_format\": \"pdf\"}'::jsonb")
        sql("stamp_tags/absent.py", "metadata = d.metadata - 'tags'")
        sql("stamp_tags/null.py", "metadata = d.metadata || '{\"tags\": null}'::jsonb")
        sql("stamp_tags/empty_str.py", "metadata = d.metadata || '{\"tags\": \"\"}'::jsonb")
        sql("stamp_tags/empty_list.py", "metadata = d.metadata || '{\"tags\": []}'::jsonb")
        sql("stamp_tags/set.py", "metadata = d.metadata || '{\"tags\": [\"curated\"]}'::jsonb")
        sql("stamp_cat/cat_absent.txt", "metadata = d.metadata - 'category'")
        sql("stamp_cat/cat_set.txt", "metadata = d.metadata || '{\"category\": \"curated\"}'::jsonb")
        # Content hash
        cat.update(t("ch_head/doc.txt"), head_hash="hOLD")
        cat.update(t("misc/reset_miss.txt"), meta={"miss_count": 1})
        cat.update(t("misc/keep.txt"), meta={"miss_count": 1})
        for rel, how in (("fence/indexing.txt", "indexing"), ("fence/failed.txt", "failed"), ("fence/complete.txt", "complete")):
            h = hashlib.sha256(next(f.seed for f in c.files if f.rel == rel).encode()).hexdigest()
            cat.begin_index_run(t(rel), h, f"run-{how}", COLLECTIONS["prose"])
            if how == "failed":
                cat.fail_index_run(t(rel), "boom")
            elif how == "complete":
                cat.complete_index_run(t(rel), h, 0)
        # Identity
        cat.register(c.other, "foreign holder", content_type="prose", file_path="id_foreign/x.txt",
                     physical_collection="docs__rdr193foreign", source_uri=f"file://{c.root}/id_foreign/x.txt")
        stale_t = t("id_same_owner/x.txt")
        _update_doc(self.tenant, stale_t, f"file_path = 'id_same_owner/old-name.txt', physical_collection = '{STALE_COLLECTION}'")
        sql("id_legacy/unchanged.txt", "source_uri = ''")
        sql("id_legacy/changed.txt", "source_uri = ''")
        twin = str(cat.register(c.owner, "dup twin", content_type="prose",
                                file_path="id_duplicate/alt/dup.txt", physical_collection=COLLECTIONS["prose"],
                                source_uri=f"file://{c.root}/id_duplicate/alt/dup.txt"))
        _update_doc(self.tenant, twin, "file_path = 'id_duplicate/dup.txt'")
        cat.register(c.other, "held elsewhere", content_type="prose", file_path="id_deleted_foreign/held.txt",
                     source_uri=f"file://{c.root}-elsewhere/held.txt")

    # -- one measured run -------------------------------------------------------------------------------------
    def run(self, c: Copy, *, complete: bool = True, rels: list[str] | None = None,
            keep: list[str] | None = None, deleted: list[str] | None = None) -> tuple[Snapshot, dict]:
        keep = KEEP if keep is None else keep
        deleted = DELETED if deleted is None else deleted
        offered = _present(c, rels)
        pre = _dump(self.tenant)["docs"]
        before = {d["tumbler"]: d["indexed_at"] for d in pre}
        was_trashed = {d["tumbler"] for d in pre if d["deleted_at"] is not None}
        self.rec.reset()
        if c.label == "old":
            run = _run_old(c, offered, head=SEED_HEAD, complete=complete, keep=keep, deleted=deleted)
        else:
            run = _run_new(self.cat, c, offered, head=SEED_HEAD, complete=complete, keep=keep, deleted=deleted)
        dump = _dump(self.tenant)
        snap = Snapshot(c, dump, before)
        if c.label == "old":
            _old_structures(snap, run, self.rec)
        else:
            _new_structures(snap, run)
        run["trashed_now"] = {d["tumbler"] for d in dump["docs"]
                              if d["deleted_at"] is not None and snap._owns(d["tumbler"])} - was_trashed
        return snap, run


def _present_at_seed(c: Copy) -> list[F]:
    return [f for f in c.files if f.seed is not None]


# ── the report and the assertions ────────────────────────────────────────────────────────────────────────────────

def _verdicts(old: dict, new: dict) -> tuple[dict[str, list[tuple]], dict[str, int]]:
    diffs = _diff(old, new)
    rows: Counter = Counter()
    for section in set(old) | set(new):
        for key in set(old.get(section, {})) | set(new.get(section, {})):
            rows[key.split("::", 1)[0]] += 1
    by_case: dict[str, list[tuple]] = {case: [] for case in rows}
    for d in diffs:
        by_case.setdefault(_case_of_diff(d), []).append(d)
    return by_case, dict(rows)


def _fmt(d: tuple) -> str:
    return f"{d[0]} {d[1]} [{d[2]}]: old={d[3]!r} new={d[4]!r}"


def _golden_of(snap: Snapshot) -> dict:
    return json.loads(json.dumps(snap.sections, sort_keys=True))


def test_the_commit_leaves_what_the_client_path_leaves(tmp_path: Path, t2_tenant: str,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    sc = Scenario(tmp_path, t2_tenant, monkeypatch)
    try:
        old, new = sc.copies["old"], sc.copies["new"]
        sc.seed(old)
        sc.seed(new)
        for c in (old, new):
            sc.prepare(c)
            _write_run2(c)
        snap_old, _ = sc.run(old)
        snap_new, run_new = sc.run(new)
        outcome = run_new["outcome"]

        # ── the cases reached the arms they are for ──
        actions = {r["file_path"]: r["action"] for r in outcome["rows"]}
        wrong = {rel: (want, actions.get(rel)) for rel, want in EXPECTED_ACTIONS.items() if actions.get(rel) != want}
        assert not wrong, f"fixture paths that did not reach their arm (want, got): {wrong}"
        key = snap_new.key_of
        renamed_olds = {key[r["old"]] for r in outcome["renamed"]}
        for rel in ("ren_single/a.txt", "ren_onto_earlier/a.txt", "ren_two_onto_one/m1.txt", "ren_two_onto_one/m2.txt",
                    "ren_link_between/a.txt", "ren_link_between/b.txt", "ren_self_link/a.txt", "ren_link_swept/a.txt",
                    "ren_link_tombstoned/a.txt", "ren_non_evid/a.txt", "ren_alias/a.txt", "ren_and_deleted/a.txt", "ren_shared_hash/gone.txt"):
            assert f"M:{rel}#0" in renamed_olds, f"{rel} was not renamed: {sorted(renamed_olds)}"
        assert f"M:ren_link_swept/s.txt#0" in {key[t] for t in outcome["swept"]}, outcome["swept"]
        assert "id_duplicate/dup.txt" in {d["file_path"] for d in outcome["duplicate_path"]}, outcome["duplicate_path"]
        assert "id_deleted_foreign/held.txt" in {d["file_path"] for d in outcome["deleted_skipped_foreign_owner"]}
        assert outcome["sensitivity_pending"] == [sc.t(new, "misc/sens_existing.txt")], outcome["sensitivity_pending"]
        assert set(outcome["trashed"]) == run_new["trashed_now"], (outcome["trashed"], run_new["trashed_now"])

        # ── old path against new path, and both against the golden file ──
        by_case, row_counts = _verdicts(snap_old.sections, snap_new.sections)
        golden_now = _golden_of(snap_old)
        if os.environ.get("NX_PARITY_REGEN_GOLDEN") == "1":
            GOLDEN.parent.mkdir(parents=True, exist_ok=True)
            GOLDEN.write_text(json.dumps(golden_now, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        assert GOLDEN.exists(), f"{GOLDEN} is missing: run once with NX_PARITY_REGEN_GOLDEN=1"
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        old_vs_golden = _diff(golden, golden_now)
        new_vs_golden = _diff(golden, _golden_of(snap_new))
        golden_by_case: dict[str, list[tuple]] = {}
        for d in new_vs_golden:
            golden_by_case.setdefault(_case_of_diff(d), []).append(d)

        lines = [f"{'case':<24}{'rows':>6}  verdict"]
        failures: list[str] = []
        for case in sorted(row_counts):
            got = by_case.get(case, [])
            known = KNOWN_DIVERGENCES.get(case)
            if known is None:
                verdict = "match" if not got else f"DIVERGES ({len(got)})"
                if got:
                    failures += [f"{case}: {_fmt(d)}" for d in got]
                if golden_by_case.get(case, []) != got:
                    failures.append(f"{case}: the golden file and the commit disagree differently from the old path")
            else:
                reason, rdr_line, pinned = known
                verdict = f"divergence (RDR {rdr_line}): {reason}"
                if golden_by_case.get(case, []) != pinned:
                    failures.append(f"{case}: the golden file and the commit differ other than as pinned")
                if got != pinned:
                    failures += [f"{case}: pinned divergence changed: {_fmt(d)}" for d in got if d not in pinned]
                    failures += [f"{case}: pinned divergence gone: {_fmt(d)}" for d in pinned if d not in got]
            lines.append(f"{case:<24}{row_counts[case]:>6}  {verdict}")
        known_unseen = sorted(set(KNOWN_DIVERGENCES) - set(row_counts))
        failures += [f"{c}: a known divergence for a case the fixture does not have" for c in known_unseen]
        failures += [f"old path against the golden file: {_fmt(d)}" for d in old_vs_golden]
        total = sum(row_counts.values())
        lines.append(f"cases examined {len(row_counts)}, rows compared {total}, "
                     f"documents {len(snap_old.sections['documents'])}, links {len(snap_old.sections['links'])}, "
                     f"structures {len(snap_old.sections['structures'])}, skipped 0")
        print("\n".join(lines))  # noqa: T201 — the verdict table is the first-run record; read it with -s
        assert len(snap_old.sections["links"]) >= 5 and len(snap_old.sections["documents"]) >= 60, "the fixture is vacuous"
        assert not failures, "\n".join(lines) + "\n\n" + "\n".join(failures)
    finally:
        sc.close()


def test_a_partial_walk_commit_touches_no_unvisited_row(tmp_path: Path, t2_tenant: str,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Every delta run is partial: no miss count, no rename, nothing written to a row the walk did not visit; an
    explicit deletion is trashed with no link move (RDR-193 Part A1, 'Partial walk' and 'Explicit deletions')."""
    sc = Scenario(tmp_path, t2_tenant, monkeypatch)
    try:
        old, new = sc.copies["old"], sc.copies["new"]
        sc.seed(old)
        sc.seed(new)
        for c in (old, new):
            sc.prepare(c)
            _write_run2(c)
        visited = ["fill/f01.py", "ch_content/doc.txt", "ch_fence/changed.txt", "ch_fence/new.txt", "ren_single/b.txt"]
        snap_old, _ = sc.run(old, complete=False, rels=visited, keep=[], deleted=["misc/del.txt"])
        snap_new, run_new = sc.run(new, complete=False, rels=visited, keep=[], deleted=["misc/del.txt"])
        outcome = run_new["outcome"]
        assert outcome["renamed"] == [] and outcome["swept"] == [], "a partial walk must not rename or sweep"
        assert len(outcome["trashed"]) == 1 and set(outcome["trashed"]) == run_new["trashed_now"], outcome["trashed"]
        by_case, row_counts = _verdicts(snap_old.sections, snap_new.sections)
        failures = [f"{c}: {_fmt(d)}" for c, ds in by_case.items() for d in ds if c not in KNOWN_DIVERGENCES]
        print(  # noqa: T201 — first-run record, read with -s
            f"partial walk: cases examined {len(row_counts)}, rows compared {sum(row_counts.values())}, "
              f"differences {sum(len(v) for v in by_case.values())}")
        assert not failures, "\n".join(failures)
    finally:
        sc.close()
