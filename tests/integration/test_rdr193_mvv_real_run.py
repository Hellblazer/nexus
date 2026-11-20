# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-193 Phase 2 Minimum Viable Validation (nexus-u5m5g.9): a REAL ``nx index repo`` of this repository, counted.

NOT committed: a one-shot harness for the Phase 2 review gate.

What it does, against the test substrate's own engine (a loopback engine built from this tree, a freshly minted tenant,
``NEXUS_CONFIG_DIR`` and ``HOME`` in a temp dir; it aborts before the first request if any of that is not so):

1. clones this checkout into a temp directory (the worktree's tracked files are never touched);
2. run A: ``nx index repo <clone>`` (cold), run B: the same command again (warm), counting every HTTP request at
   ``httpx.Client.send`` (what goes on the wire) and at ``RefreshableHttpStoreMixin._send`` (what the client logs as
   one logical call), tagged with the phase it was made in (``pre`` the CLI's owner pre-registration, ``index`` the
   index_repository run, ``link:<generator>`` inside one link generator, ``post`` after index_repository returned:
   taxonomy and the rest);
3. run C: one existing RDR is changed in the CLONE, committed there, and ``nx index repo --since-head`` runs
   (``walk=partial`` is read off the stage request); run D (supplementary): a NEW RDR is added the same way, so the
   linkers see a seed;
4. reads the tenant's catalog rows and links from Postgres and checks them against the rules of
   ``tests/fixtures/rdr193_reconcile_parity_expected.json`` (see ``_check_rows``), and run B against run A.

The report is printed between ``MVV-REPORT-BEGIN`` and ``MVV-REPORT-END`` and written to ``$MVV_REPORT`` (default:
under the test's tmp dir). Run: ``scripts/build-gate-jar.sh`` then
``uv run pytest -m integration tests/integration/test_rdr193_mvv_real_run.py -s -p no:randomly``.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import pytest
from click.testing import CliRunner

from nexus import indexer
from nexus.catalog import link_generator, reconcile_run
from nexus.catalog.factory import make_catalog_reader
from nexus.cli import main
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin
from nexus.repo_identity import _repo_identity
from tests._engine_substrate import ensure_engine
from tests.db import _service_fixture as _sf
from tests._tenant_pool import psql

pytestmark = [pytest.mark.integration, pytest.mark.t2_tenant]

# The substrate engine is launched with ActiveProcessorCount=2, which also sizes its bge ONNX threads: measured on
# chas, 190% CPU and about 12 files a minute, so a cold embed of this repository would take ~7 hours. This one-shot
# harness gives the engine more of its grant (the engine boots at the first fixture, after this import).
_sf.SUBSTRATE_ENGINE_CPUS = int(os.environ.get("MVV_ENGINE_CPUS", "6"))

REPO = Path(__file__).resolve().parents[2]
CATALOG = "/v1/catalog"
CHANGED_RDR = "docs/rdr/rdr-015-indexing-pipeline-rethink.md"   # carries ``supersedes: RDR-014``
NEW_RDR = "docs/rdr/rdr-998-mvv-delta.md"


# ── the counter ───────────────────────────────────────────────────────────────────────────────────────────────

_CLIENT_FILES = ("_refreshable_client.py", "http_catalog_client.py", "reconcile_client.py", "/httpx/", "/httpcore/",
                 "test_rdr193_mvv_real_run.py")


def _caller() -> str:
    """The first two nexus frames outside the HTTP client layers: who asked."""
    out = []
    f = sys._getframe(2)
    while f is not None and len(out) < 2:
        fn = f.f_code.co_filename
        if "/src/nexus/" in fn and not any(c in fn for c in _CLIENT_FILES):
            out.append(f"{fn.rsplit('/', 1)[-1]}:{f.f_code.co_name}")
        f = f.f_back
    return " < ".join(out)


class Wire:
    def __init__(self) -> None:
        self.run = "none"
        self.phase = "pre"
        self.wire: list[dict] = []
        self.logical: list[dict] = []
        self.walks: dict[str, list[str]] = defaultdict(list)
        self.lock = threading.Lock()

    def add(self, into: list[dict], method: str, path: str, params: dict, content: bytes | None) -> None:
        rec: dict[str, Any] = {"run": self.run, "phase": self.phase, "method": method, "path": path,
                               "params": {k: str(v) for k, v in params.items()}}
        if path.startswith(CATALOG) and not path.endswith(("/reconcile/stage", "/reconcile/commit")):
            rec["caller"] = _caller()
        if content:
            try:
                body = json.loads(content)
            except ValueError:
                body = None
            if isinstance(body, dict):
                if path.endswith("/reconcile/stage"):
                    rows = body.get("rows") or []
                    rec["stage"] = {"walk": body.get("walk"), "expected_rows": body.get("expected_rows"),
                                    "rows": len(rows), "kinds": dict(Counter(r.get("kind") for r in rows))}
                    rec["stage_rows"] = rows
                if path.endswith("/update_many"):
                    ups = body.get("updates") or body.get("docs") or body.get("rows") or []
                    rec["update_n"] = len(ups)
                    rec["update_keys"] = sorted({k for u in ups[:200] if isinstance(u, dict) for k in u})
                    rec["body_keys"] = sorted(body)
                if "edges" in body:
                    rec["edges"] = len(body["edges"])
                if path.endswith("/links/same_content"):
                    rec["same_content_tumblers"] = len(body.get("tumblers") or [])
        with self.lock:
            into.append(rec)


W = Wire()


def _install_spies(monkeypatch: pytest.MonkeyPatch) -> None:
    real_send = httpx.Client.send

    def wire_send(self, request, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        try:
            content = request.content if request.method != "GET" else None
        except httpx.RequestNotRead:
            content = None
        W.add(W.wire, request.method, request.url.path, dict(request.url.params), content)
        return real_send(self, request, *a, **kw)

    monkeypatch.setattr(httpx.Client, "send", wire_send)

    real_logical = RefreshableHttpStoreMixin._send

    def logical_send(self, method, path, **kw):  # noqa: ANN001, ANN003
        body = kw.get("json")
        W.add(W.logical, method, path, dict(kw.get("params") or {}),
              json.dumps(body).encode() if body is not None else None)
        return real_logical(self, method, path, **kw)

    monkeypatch.setattr(RefreshableHttpStoreMixin, "_send", logical_send)

    real_ir = indexer.index_repository

    def index_repository(*a, **kw):  # noqa: ANN002, ANN003
        W.phase = "index"
        try:
            return real_ir(*a, **kw)
        finally:
            W.phase = "post"

    monkeypatch.setattr(indexer, "index_repository", index_repository)

    def phased(name: str, real):  # noqa: ANN001
        def inside(*a, **kw):  # noqa: ANN002, ANN003
            before, W.phase = W.phase, f"link:{name}"
            try:
                return real(*a, **kw)
            finally:
                W.phase = before
        return inside

    for gen, label in (("generate_rdr_filepath_links", "rdr"), ("generate_prose_filepath_links", "prose"),
                       ("generate_pdf_corpus_links", "pdf"), ("hook_link_inputs", "inputs")):
        monkeypatch.setattr(link_generator, gen, phased(label, getattr(link_generator, gen)))

    real_bind = link_generator.bind_rdr_dependency_generator

    def bind(cat, repo):  # noqa: ANN001
        before, W.phase = W.phase, "link:bind"
        try:
            entry = real_bind(cat, repo)
        finally:
            W.phase = before
        if entry is None:
            return None
        kind, fn = entry
        return kind, phased("rdr-dependency", fn)

    monkeypatch.setattr(link_generator, "bind_rdr_dependency_generator", bind)

    real_sc = reconcile_run.stage_and_commit

    def stage_and_commit(writer, owner, rows, *, walk, page=reconcile_run.STAGE_PAGE):  # noqa: ANN001
        W.walks[W.run].append(walk)
        return real_sc(writer, owner, rows, walk=walk, page=page)

    monkeypatch.setattr(reconcile_run, "stage_and_commit", stage_and_commit)


# ── safety ────────────────────────────────────────────────────────────────────────────────────────────────────

def _assert_self_provisioned(tmp_path: Path) -> dict:
    state = ensure_engine()
    url = os.environ.get("NX_SERVICE_URL", "")
    host = urlparse(url).hostname
    assert url == state["base_url"], f"NX_SERVICE_URL {url!r} is not the substrate engine {state['base_url']!r}"
    assert host in ("127.0.0.1", "localhost", "::1"), f"engine host {host!r} is not loopback"
    cfg = Path(os.environ["NEXUS_CONFIG_DIR"]).resolve()
    assert cfg.is_relative_to(tmp_path.resolve()), f"NEXUS_CONFIG_DIR {cfg} is not under the temp dir {tmp_path}"
    assert Path(os.environ["HOME"]).resolve().is_relative_to(tmp_path.resolve()), "HOME is not the temp dir"
    assert Path.home().resolve() != Path("/Users/hal.hildebrand").resolve()
    for name in ("NX_ALLOW_PROD_WRITE", "NX_TEST_EXTERNAL_ENGINE_URL", "NX_TEST_EXTERNAL_TENANT_TOKEN"):
        assert not os.environ.get(name), f"{name} is set: refusing to run"
    assert "conexus-nexus.com" not in url
    return state


# ── database reads ────────────────────────────────────────────────────────────────────────────────────────────

def _query(state: dict, tenant: str, sql: str, **variables: str) -> Any:
    proc = psql(state, "\\set ON_ERROR_STOP on\n" + sql, tenant=tenant, **variables)
    assert proc.returncode == 0, f"psql failed: {(proc.stderr or proc.stdout)[-800:]}"
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def _dump(state: dict, tenant: str) -> dict[str, list[dict]]:
    def table(name: str, drop: str = "") -> str:
        return (f"SELECT coalesce(jsonb_agg(to_jsonb(t){drop}), '[]'::jsonb) FROM nexus.{name} t "
                f"WHERE t.tenant_id = :'tenant'")
    return {
        "docs": _query(state, tenant, table("catalog_documents", " - 'tenant_id' - 'fts_vector'")),
        "links": _query(state, tenant, table("catalog_links", " - 'tenant_id' - 'id'")),
        "owners": _query(state, tenant, table("catalog_owners", " - 'tenant_id'")),
    }


# ── counting ──────────────────────────────────────────────────────────────────────────────────────────────────

def _route(rec: dict) -> str:
    p = rec["path"]
    if p.startswith(CATALOG + "/reconcile/") and rec["method"] == "GET":
        return "GET /reconcile/{job} (poll)"
    return f"{rec['method']} {p}"


def _table(recs: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(_route(r) for r in recs).items()))


def _cat(recs: list[dict]) -> list[dict]:
    return [r for r in recs if r["path"].startswith(CATALOG)]


def _is_unscoped_typed_list(r: dict) -> bool:
    return (r["method"] == "GET" and r["path"] == CATALOG + "/list" and "content_type" in r["params"]
            and not r["params"].get("owner"))


def _bar_counts(recs: list[dict]) -> dict[str, Any]:
    cat = _cat(recs)
    by = Counter(_route(r) for r in cat)
    stage = [r for r in cat if r["path"] == CATALOG + "/reconcile/stage"]
    rows = sum(r["stage"]["rows"] for r in stage if "stage" in r)
    return {
        "commit_requests": by[f"POST {CATALOG}/reconcile/commit"],
        "stage_requests": len(stage),
        "stage_rows_total": rows,
        "stage_walks": sorted({r["stage"]["walk"] for r in stage if "stage" in r}),
        "stage_row_kinds": dict(sum((Counter(r["stage"]["kinds"]) for r in stage if "stage" in r), Counter())),
        "poll_requests": by["GET /reconcile/{job} (poll)"],
        "single_update": by[f"POST {CATALOG}/update"],
        "single_register": by[f"POST {CATALOG}/doc/register"],
        "single_delete": by[f"POST {CATALOG}/delete"] + by[f"DELETE {CATALOG}/delete"],
        "single_link": by[f"POST {CATALOG}/link"] + by[f"POST {CATALOG}/unlink"],
        "unscoped_typed_list": sum(1 for r in cat if _is_unscoped_typed_list(r)),
        "all_list_reads": by[f"GET {CATALOG}/list"],
        "list_reads_without_owner_or_prefix": sum(
            1 for r in cat if r["method"] == "GET" and r["path"] == CATALOG + "/list"
            and not (r["params"].get("owner") or r["params"].get("source_uri_prefix"))),
    }


def _by_phase(recs: list[dict]) -> dict[str, dict[str, int]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in _cat(recs):
        out[r["phase"]].append(r)
    return {ph: _table(rs) for ph, rs in sorted(out.items())}


def _callers(recs: list[dict], *, skip: tuple[str, ...] = ()) -> dict[str, int]:
    """phase | route | param keys | caller -> count, for every catalog request but the stage/commit/poll ones."""
    c: Counter = Counter()
    for r in _cat(recs):
        if r["path"].endswith(("/reconcile/stage", "/reconcile/commit")) or r["path"].startswith(CATALOG + "/reconcile/"):
            continue
        keys = ",".join(sorted(r["params"]))
        c[f"{r['phase']} | {_route(r)} | {keys} | {r.get('caller', '')}"] += 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1]))


def _list_reads(recs: list[dict]) -> list[dict]:
    seen: Counter = Counter()
    for r in _cat(recs):
        if r["method"] == "GET" and r["path"] == CATALOG + "/list":
            params = {k: (v[:60] + "…" if len(v) > 60 else v) for k, v in r["params"].items()}
            seen[json.dumps({"phase": r["phase"], "params": params, "caller": r.get("caller", "")}, sort_keys=True)] += 1
    return [{**json.loads(k), "n": n} for k, n in seen.most_common(12)]


def _updates(recs: list[dict]) -> list[dict]:
    return [{"phase": r["phase"], "route": _route(r), "caller": r.get("caller", ""), "keys": r.get("update_keys"), "n": r.get("update_n")}
            for r in _cat(recs) if r["path"].endswith(("/update_many", "/update"))]


def _linker_counts(recs: list[dict], kind: str) -> dict[str, Any]:
    rs = [r for r in _cat(recs) if r["phase"] == f"link:{kind}"]
    lists = [r for r in rs if r["method"] == "GET" and r["path"] == CATALOG + "/list"]
    writes = [r for r in rs if r["path"] == CATALOG + "/links/upsert_many"]
    same = [r for r in rs if r["path"] == CATALOG + "/links/same_content"]
    resolves = [r for r in rs if "resolve" in r["path"]]
    other = [r for r in rs if r not in lists + writes + same + resolves]
    return {
        "requests": len(rs),
        "list_reads": len(lists),
        "list_reads_scoped": sum(1 for r in lists if r["params"].get("owner") or r["params"].get("source_uri_prefix")),
        "list_read_params": [r["params"] for r in lists],
        "upsert_many_writes": len(writes),
        "edges_written": sum(r.get("edges", 0) for r in writes),
        "same_content_calls": len(same),
        "same_content_tumblers": sum(r.get("same_content_tumblers", 0) for r in same),
        "resolve_calls": len(resolves),
        "resolve_routes": dict(Counter(_route(r) for r in resolves)),
        "other": dict(Counter(_route(r) for r in other)),
    }


# ── the rules of the parity fixture, applied to the real tree ───────────────────────────────────────────────────

_TIME_COLS_COMPARED_EXACTLY = {"indexed_at"}


def _norm_doc(d: dict) -> dict:
    out = {}
    for col, v in d.items():
        if col.endswith("_at") and col not in _TIME_COLS_COMPARED_EXACTLY:
            v = None if v is None else "<set>"
        out[col] = v
    return out


def _owner_prefix(state: dict, tenant: str, repo_hash: str) -> str:
    owner = make_catalog_reader().owner_for_repo(repo_hash)
    assert owner is not None, "no catalog owner for the clone"
    return str(owner)


def _check_rows(dump: dict, owner: str, clone: Path, head: str, staged_present: dict[str, dict],
                staged_keep: set[str], tracked: set[str]) -> dict[str, Any]:
    """The fixture's rules (the columns ``rdr193_reconcile_parity_expected.json`` pins for a present, unchanged,
    live row): one live row per walked path, its identity, collection, content hash and source URI as the file says,
    ``head_hash`` the commit, not sensitive, no alias, no tombstone, no duplicate path, and no live row for a path the
    walk did not offer. Each check names what it examined."""
    root = str(clone.resolve())
    mine = [d for d in dump["docs"] if d["tumbler"].rpartition(".")[0] == owner]
    live = [d for d in mine if d["deleted_at"] is None]
    by_path: dict[str, list[dict]] = defaultdict(list)
    for d in live:
        by_path[d["file_path"]].append(d)
    bad: dict[str, list[str]] = defaultdict(list)
    examined = 0
    for rel, row in sorted(staged_present.items()):
        docs = by_path.get(rel, [])
        if len(docs) != 1:
            bad["not exactly one live row"].append(f"{rel}: {len(docs)}")
            continue
        d = docs[0]
        examined += 1
        disk = (clone / rel).read_bytes()
        want_hash = hashlib.sha256(disk).hexdigest()
        checks = {
            "content_hash != sha256(file)": row["content_hash"] == want_hash and (d["metadata"] or {}).get("content_hash") == want_hash,
            "content_type": d["content_type"] == row["content_type"],
            "physical_collection": d["physical_collection"] == row["physical_collection"],
            "head_hash != git HEAD": d["head_hash"] == head == row["head_hash"],
            "source_uri": d["source_uri"] == f"file://{root}/{rel}",
            "title": d["title"] == Path(rel).name,
            "sensitive": d["sensitive"] is False,
            "alias_of": not d["alias_of"],
            "source_mtime": abs(float(d["source_mtime"] or 0) - float(row["source_mtime"])) < 1e-3,
        }
        for name, ok in checks.items():
            if not ok:
                bad[name].append(rel)
    extra = sorted(set(by_path) - set(staged_present) - staged_keep)
    if extra:
        bad["live row for a path the walk did not offer"] = extra
    dup = sorted(p for p, ds in by_path.items() if len(ds) > 1)
    if dup:
        bad["duplicate live path"] = dup
    tomb = [d["file_path"] for d in mine if d["deleted_at"] is not None]
    if tomb:
        bad["tombstoned row"] = tomb
    not_tracked = sorted(set(staged_present) - tracked)
    if not_tracked:
        bad["staged path is not a tracked file"] = not_tracked
    return {
        "owner_docs_total": len(mine), "live_docs": len(live), "present_rows_examined": examined,
        "keep_rows": len(staged_keep), "tombstoned": len(tomb),
        "tracked_files_not_staged": len(tracked - set(staged_present) - staged_keep),
        "chunk_count_zero_live_docs": sum(1 for d in live if not d["chunk_count"]),
        "violations": {k: (v[:8], len(v)) for k, v in bad.items()},
        "ok": not bad and examined == len(staged_present) and examined > 0,
    }


def _links_summary(dump: dict, owner: str) -> dict[str, Any]:
    mine = {d["tumbler"] for d in dump["docs"] if d["tumbler"].rpartition(".")[0] == owner}
    links = [lk for lk in dump["links"] if lk["from_tumbler"] in mine or lk["to_tumbler"] in mine]
    return {"links": len(links), "by_type_creator": {f"{k[0]} / {k[1]}": n for k, n in sorted(
        Counter((lk["link_type"], lk["created_by"]) for lk in links).items())}}


def _identity(dump: dict, owner: str) -> tuple[dict, dict]:
    docs = {d["tumbler"]: _norm_doc(d) for d in dump["docs"] if d["tumbler"].rpartition(".")[0] == owner}
    links = {(lk["from_tumbler"], lk["to_tumbler"], lk["link_type"], lk["created_by"]): {
        k: (None if k.endswith("_at") and v is None else ("<set>" if k.endswith("_at") else v))
        for k, v in lk.items()} for lk in dump["links"]
        if lk["from_tumbler"].rpartition(".")[0] == owner or lk["to_tumbler"].rpartition(".")[0] == owner}
    return docs, links


def _diff_summary(a: dict, b: dict) -> dict[str, Any]:
    """Every differing document, by column; ``metadata`` by key. Counts the whole set, not a sample."""
    cols: Counter = Counter()
    meta_keys: Counter = Counter()
    only = 0
    differing = 0
    examples: dict[str, str] = {}
    for k in set(a) | set(b):
        if k not in a or k not in b:
            only += 1
            continue
        if a[k] == b[k]:
            continue
        differing += 1
        for c in set(a[k]) | set(b[k]):
            if a[k].get(c) == b[k].get(c):
                continue
            cols[c] += 1
            if c == "metadata":
                ma, mb = a[k].get(c) or {}, b[k].get(c) or {}
                for mk in set(ma) | set(mb):
                    if ma.get(mk) != mb.get(mk):
                        meta_keys[mk] += 1
                        examples.setdefault(f"metadata.{mk}", f"cold={ma.get(mk)!r} warm={mb.get(mk)!r}")
            else:
                examples.setdefault(c, f"cold={a[k].get(c)!r} warm={b[k].get(c)!r}")
    return {"compared": len(set(a) | set(b)), "rows_on_one_side_only": only, "docs_differing": differing,
            "differing_columns": dict(cols), "differing_metadata_keys": dict(meta_keys), "examples": examples}


def _diff(a: dict, b: dict) -> list[str]:
    out = []
    for k in sorted(set(a) | set(b), key=str):
        if k not in a:
            out.append(f"only in warm: {k}")
        elif k not in b:
            out.append(f"only in cold: {k}")
        elif a[k] != b[k]:
            cols = sorted(c for c in set(a[k]) | set(b[k]) if a[k].get(c) != b[k].get(c))
            out.append(f"{k}: " + ", ".join(f"{c} cold={a[k].get(c)!r} warm={b[k].get(c)!r}" for c in cols))
    return out


# ── the run ───────────────────────────────────────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _say(text: str) -> None:
    """To the process's real stdout: ``CliRunner.invoke`` swaps ``sys.stdout`` for its buffer while a run is in
    flight, and this line is what keeps scripts/watched_pytest.py's stall clock fed through the silent embed."""
    sys.__stdout__.write(text + "\n")  # noqa: T201
    sys.__stdout__.flush()


def _heartbeat(stop: threading.Event) -> None:
    t0 = time.monotonic()
    while not stop.wait(30):
        _say(f"[mvv] {time.monotonic() - t0:6.0f}s run={W.run} phase={W.phase} wire={len(W.wire)} logical={len(W.logical)}")


def _invoke(runner: CliRunner, run: str, args: list[str]) -> dict[str, Any]:
    W.run, W.phase = run, "pre"
    t0 = time.monotonic()
    res = runner.invoke(main, args)
    secs = time.monotonic() - t0
    W.phase = "post"
    tail = "\n".join((res.output or "").strip().splitlines()[-12:])
    _say(f"[mvv] run {run} {' '.join(args[:3])} exit={res.exit_code} {secs:.0f}s\n{tail}")
    assert res.exit_code == 0, f"run {run} exited {res.exit_code}: {res.output[-2000:]} {res.exception!r}"
    return {"exit": res.exit_code, "seconds": round(secs), "tail": tail}


def test_rdr193_phase2_mvv_real_run(t2_service_env: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant = t2_service_env
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    state = _assert_self_provisioned(tmp_path)
    _install_spies(monkeypatch)
    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(stop,), daemon=True).start()
    report: dict[str, Any] = {"tenant": tenant, "engine": state["base_url"], "worktree_head": _git(REPO, "rev-parse", "HEAD")}
    try:
        clone = tmp_path / "nexus-copy"
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", str(REPO), str(clone)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "mvv@test.invalid"], cwd=clone, check=True)
        subprocess.run(["git", "config", "user.name", "mvv"], cwd=clone, check=True)
        assert _git(clone, "rev-parse", "HEAD") == report["worktree_head"], "the clone is not the worktree's commit"
        cap_kb = int(os.environ.get("MVV_TRUNCATE_KB", "0"))
        drop_pdfs = os.environ.get("MVV_DROP_PDFS", "0") == "1"
        report["truncate_kb"], report["drop_pdfs"] = cap_kb, drop_pdfs
        if cap_kb or drop_pdfs:
            # Cost levers, NOT the faithful run. The engine's bge embed and MinerU's PDF extraction are the run's cost
            # (hours: about 20 files a minute on 7 cores, and MinerU timed out on chas's 7 PDFs, which ends the run
            # with exit 1); the catalog traffic being counted does not depend on either. cap_kb keeps the same file set
            # (so the same N) and cuts each text file to its first cap_kb KB in the clone; drop_pdfs removes the PDFs
            # from the clone. Both are committed there.
            cut = dropped = 0
            for rel in _git(clone, "ls-files").splitlines():
                f = clone / rel
                if drop_pdfs and f.suffix == ".pdf":
                    f.unlink()
                    dropped += 1
                    continue
                if (not cap_kb or f.is_symlink() or not f.is_file() or f.suffix == ".pdf"
                        or f.stat().st_size <= cap_kb * 1024):
                    continue
                try:
                    text = f.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    continue
                f.write_text(text[: cap_kb * 1024], encoding="utf-8")
                cut += 1
            _git(clone, "add", "-A")
            _git(clone, "commit", "-qm", f"mvv: cut {cut} files to {cap_kb} KB, dropped {dropped} PDFs")
            report["files_truncated"], report["pdfs_dropped"] = cut, dropped
        head = _git(clone, "rev-parse", "HEAD")
        tracked = set(_git(clone, "ls-files").splitlines())
        name, repo_hash = _repo_identity(clone)
        runner = CliRunner()
        report["clone"] = str(clone)
        report["tracked_files"] = len(tracked)

        # ── run A (cold) and run B (warm) ──
        report["run_A"] = _invoke(runner, "A", ["index", "repo", str(clone)])
        owner = _owner_prefix(state, tenant, repo_hash)
        dump_a = _dump(state, tenant)
        report["run_B"] = _invoke(runner, "B", ["index", "repo", str(clone)])
        dump_b = _dump(state, tenant)

        stage_a = [r for r in _cat(W.wire) if r["run"] == "A" and r["path"] == CATALOG + "/reconcile/stage"]
        staged_rows = [row for r in stage_a for row in r.get("stage_rows", [])]
        staged_present = {r["file_path"]: r for r in staged_rows if r["kind"] == "present"}
        staged_keep = {r["file_path"] for r in staged_rows if r["kind"] == "keep"}
        report["N_present_rows"] = len(staged_present)
        report["N_staged_rows"] = len(staged_rows)

        for run in ("A", "B"):
            wire = [r for r in W.wire if r["run"] == run]
            logical = [r for r in W.logical if r["run"] == run]
            idx = [r for r in wire if r["phase"] in ("pre", "index") or r["phase"].startswith("link:")]
            report[f"{run}_counts_whole_command"] = _bar_counts(wire)
            report[f"{run}_counts_index_repository_only"] = _bar_counts(idx)
            report[f"{run}_logical_counts_whole_command"] = _bar_counts(logical)
            report[f"{run}_catalog_routes_by_phase"] = _by_phase(wire)
            report[f"{run}_walks"] = W.walks[run]
            report[f"{run}_requests_by_phase_route_params_caller"] = dict(list(_callers(wire).items())[:40])
            report[f"{run}_list_reads"] = _list_reads(wire)
            report[f"{run}_updates"] = _updates(wire)[:12]
        report["rows_A"] = _check_rows(dump_a, owner, clone, head, staged_present, staged_keep, tracked)
        report["rows_B"] = _check_rows(dump_b, owner, clone, head, staged_present, staged_keep, tracked)
        report["links_A"] = _links_summary(dump_a, owner)
        report["links_B"] = _links_summary(dump_b, owner)
        docs_a, links_a = _identity(dump_a, owner)
        docs_b, links_b = _identity(dump_b, owner)
        report["warm_vs_cold_doc_summary"] = _diff_summary(docs_a, docs_b)
        report["warm_vs_cold_doc_diffs"] = []
        ds = report["warm_vs_cold_doc_summary"]
        # frecency_* are written by the frecency pass on every run (a score that decays with time, and its
        # timestamp): not catalog hook state, so they are named and excluded from the identity verdict.
        non_frecency = {k: n for k, n in ds["differing_metadata_keys"].items() if not k.startswith("frecency_")}
        other_cols = {c: n for c, n in ds["differing_columns"].items() if c != "metadata"}
        report["warm_vs_cold_non_frecency_differences"] = {"columns": other_cols, "metadata_keys": non_frecency,
                                                           "rows_on_one_side_only": ds["rows_on_one_side_only"]}
        report["warm_vs_cold_link_diffs"] = _diff(links_a, links_b)[:20]
        report["warm_vs_cold_docs_compared"] = len(docs_a)
        report["warm_vs_cold_links_compared"] = len(links_a)

        # ── run C: one changed RDR, --since-head ──
        p = clone / CHANGED_RDR
        p.write_text(p.read_text(encoding="utf-8")
                     + "\n\nMVV delta note: the index path lives in `src/nexus/indexer.py` and "
                       "`src/nexus/catalog/link_generator.py`.\n", encoding="utf-8")
        _git(clone, "add", CHANGED_RDR)
        _git(clone, "commit", "-q", "-m", "mvv: change one RDR")
        report["run_C"] = _invoke(runner, "C", ["index", "repo", "--since-head", str(clone)])
        # ── run D (supplementary): a NEW RDR ──
        (clone / NEW_RDR).write_text(
            "---\nid: RDR-998\ntitle: \"MVV delta\"\ntype: process\nstatus: draft\nrelated_rdrs: [RDR-014]\n---\n\n"
            "# RDR-998: MVV delta\n\nThe change lands in `src/nexus/indexer.py`.\n", encoding="utf-8")
        _git(clone, "add", NEW_RDR)
        _git(clone, "commit", "-q", "-m", "mvv: add one RDR")
        report["run_D"] = _invoke(runner, "D", ["index", "repo", "--since-head", str(clone)])
        for run in ("C", "D"):
            wire = [r for r in W.wire if r["run"] == run]
            logical = [r for r in W.logical if r["run"] == run]
            report[f"{run}_counts_whole_command"] = _bar_counts(wire)
            report[f"{run}_catalog_routes_by_phase"] = _by_phase(wire)
            report[f"{run}_walks"] = W.walks[run]
            report[f"{run}_requests_by_phase_route_params_caller"] = dict(list(_callers(wire).items())[:40])
            report[f"{run}_list_reads"] = _list_reads(wire)
            report[f"{run}_linkers"] = {k: _linker_counts(wire, k) for k in ("inputs", "bind", "rdr", "prose", "pdf", "rdr-dependency")}
            report[f"{run}_linkers_logical"] = {k: _linker_counts(logical, k) for k in ("rdr", "prose", "pdf", "rdr-dependency")}
            stage = [r for r in _cat(wire) if r["path"] == CATALOG + "/reconcile/stage"]
            report[f"{run}_staged_rows"] = [(row["kind"], row["file_path"]) for r in stage for row in r.get("stage_rows", [])][:20]
        dump_c = _dump(state, tenant)
        report["links_after_D"] = _links_summary(dump_c, owner)

        # ── the verdicts ──
        v: dict[str, Any] = {}
        for run in ("B",):
            c = report[f"{run}_counts_whole_command"]
            n = report["N_staged_rows"]
            v[f"{run}: one commit request"] = (c["commit_requests"], c["commit_requests"] == 1)
            v[f"{run}: ceil(N/1000) stage requests"] = (f"{c['stage_requests']} stage, N(run B staged rows)={c['stage_rows_total']}, "
                                                          f"N(run A)={n}, ceil={math.ceil(c['stage_rows_total'] / 1000)}",
                                                          c["stage_requests"] == math.ceil(c["stage_rows_total"] / 1000))
            for key in ("single_update", "single_register", "single_delete", "single_link", "unscoped_typed_list"):
                v[f"{run}: zero {key}"] = (c[key], c[key] == 0)
        v["parity rules, run A rows"] = (report["rows_A"]["present_rows_examined"], report["rows_A"]["ok"])
        v["parity rules, run B rows"] = (report["rows_B"]["present_rows_examined"], report["rows_B"]["ok"])
        nf = report["warm_vs_cold_non_frecency_differences"]
        v["warm == cold (rows and links), frecency_* aside"] = (
            f"{ds['docs_differing']} of {ds['compared']} docs differ (columns {ds['differing_columns']}, metadata keys "
            f"{ds['differing_metadata_keys']}); non-frecency differences {nf}; {len(report['warm_vs_cold_link_diffs'])} link diffs",
            not nf["columns"] and not nf["metadata_keys"] and not nf["rows_on_one_side_only"] and not report["warm_vs_cold_link_diffs"])
        for run in ("C", "D"):
            v[f"{run}: walk=partial"] = (report[f"{run}_walks"], report[f"{run}_walks"] == ["partial"])
        report["verdicts"] = {k: {"value": val, "holds": ok} for k, (val, ok) in v.items()}
    finally:
        stop.set()
        text = json.dumps(report, indent=1, sort_keys=True, default=str)
        out = Path(os.environ.get("MVV_REPORT") or (tmp_path / "mvv_report.json"))
        out.write_text(text, encoding="utf-8")
        _say("MVV-REPORT-BEGIN\n" + text + "\nMVV-REPORT-END")
    failed = [k for k, x in report["verdicts"].items() if not x["holds"]]
    assert not failed, f"MVV bars that do not hold: {failed}"
