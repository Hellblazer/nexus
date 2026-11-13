#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-248 gate N1 (nexus-csayy.4): export a synthetic tenant of at least 500,000 chunks through the shipped routes
and the shipped client, and time every page from the shallowest to the deepest.

A measurement script, not a test: it is not collected by pytest (no ``test_`` prefix) and it boots its own cluster, so it
depends on no ambient state. It lives under ``tests/e2e`` because it runs a cloud-arm backup, which the
no-automatic-backup pin (``tests/test_nx_backup_pin.py``) refuses in ``scripts/``. It needs the engine jar (``scripts/build-gate-jar.sh``) and a PostgreSQL with pgvector
(the substrate's discovery: ``NEXUS_PG_BIN`` or the self-provisioned bundle). On chas run it through ``lab-remote``::

    lab-remote -c lab-m --fetch scale-run-out -- bash -c \\
        'scripts/build-gate-jar.sh && uv run python tests/e2e/export_scale_run.py --out-dir scale-run-out'

What it does, in order:

1. initdb and start a PostgreSQL (``max_connections`` stays 100, the server default; reported), then the engine jar
   with ``NX_POOL_SIZE`` (default 10, the engine's own default) and every ``NX_EXPORT_*`` setting at its default.
2. Create a tenant through ``POST /v1/tenants/create`` with the bootstrap root token, which returns a tenant-scoped
   token: the routes refuse the root token.
3. Seed that tenant by SQL as the superuser: N bge-768 chunks (distinct vectors, ~1 KB of text, jsonb metadata), a
   manifest row for each, the documents they belong to, and an id-keyed ``memory`` table (``--memory-rows``) of which
   the tenant owns a tenth (nine other tenants interleave by id, as they do in a shared database: a keyset scan over a
   primary key of ``id`` alone must skip their rows, which a one-tenant table would hide) and a small tenant owns
   ``--small-tenant-rows`` rows spread evenly over the id range (the expensive case for that scan). Then
   ``ANALYZE``, a checkpoint, and a PostgreSQL restart so shared buffers start empty (the OS page cache stays warm:
   this is the database's cost with the data on a local disk, not a cold cloud volume).
4. Export through the shipped client (``nexus.backup.cloud_export``) with an HTTP client that stamps each page
   request's response start (headers in) and end (body in). The client writes and verifies the real ``.nxtenant``
   file, so the file size and total time are the product's own. Then the small tenant is exported the same way and its
   ``memory`` page timed.
5. Write ``pages.csv`` (every page of every table: depth, rows, bytes, response-start, total), ``summary.json``
   (chunk count, file size, total time, p50/p95/max, deepest page, depth trend), ``explain-*.txt`` (EXPLAIN
   (ANALYZE, BUFFERS) of the first-page count and of a deep keyset page for chunks and for ``memory``, taken from the
   COPY statements the engine actually sent, read back out of the server log), and the engine's export log lines.

"Does time grow with depth" is computed, not eyeballed: the mean of the last decile of chunk pages against the first
decile, and the least-squares slope of response-start over page index. A page time that depends on depth is a
ship-blocker in EITHER direction (nexus-csayy.4): rising with depth is a page whose cost grows with what was already
sent, and FALLING with depth is a page whose cost grows with the rows left (the sorted keyset plan the engine used to
run, whose pages got faster as the table drained, 1.59 s to 0.30 s). So the script exits 3 when the first and last
deciles' mean response-starts differ by more than 2x (either way round) AND by more than 0.5 s.
"""

from __future__ import annotations

import base64
import contextlib
import csv
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import httpx

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "src"))

from nexus._install.layout_core import exe_name  # noqa: E402
from nexus.backup import cloud_export as ce  # noqa: E402
from nexus.db.pg_provision import bootstrap_superuser  # noqa: E402
from tests._child_process import integration_pg_conf, java_executable, pg_data_tempdir, stop_group  # noqa: E402
from tests._pg_ctl import pg_ctl_start  # noqa: E402
from tests.db._service_fixture import (  # noqa: E402
    SERVICE_ROLES_SQL,
    create_tenant_token,
    jar_freshness_skip_reason,
    original_tempdir,
    pg_bin_dir,
    spawn_service,
    wait_for_service,
)

_JAR = _REPO / "service" / "target" / "nexus-service-1.0-SNAPSHOT.jar"
_ROOT_TOKEN = "scale-run-root-token-0123456789"
_DB = "nexus_scale"
_TENANT = "scale-tenant"
_SMALL_TENANT = "scale-small"
_MODEL = "bge-base-en-v15-768"
_COLLECTION = "knowledge__scalerun__bge-base-en-v15-768__v1"
_MEMORY_ROWS = 2_000_000
_NOISE_TENANTS = 9
_SMALL_TENANT_ROWS = 1_000
# middle-share tenants of the id-keyed table: percent -> (modulus, residue) of the id; the residue classes are disjoint from each
# other, from the main tenant (g % 10 = 0) and from the small tenant, and spread evenly over the id range
_SHARES: dict[str, tuple[int, int]] = {"5": (20, 1), "2": (50, 2), "1": (100, 3), "0.5": (200, 4)}
_ENGINE_PIN = "SET LOCAL enable_sort = off; "   # what TenantExportReads.pinOrderedScan sets, for a table whose key leads with tenant_id
_BATCH = 50_000
_EXIT_DEEP_PAGE_REGRESSION = 3
_EXIT_PINNED_PLAN_SORTS = 4
#: Tables measured here whose primary key leads with ``tenant_id``: the engine's page transaction pins their planner to an
#: ordered index scan (``TenantExportReads.pinOrderedScan``), so a Sort in their deep-page plan is the bug coming back.
_PINNED_TABLES = ("chunks",)
_SORT_NODE = re.compile(r"^\s*(->\s*)?(Incremental )?Sort\s+\(cost=", re.M)


def plan_has_sort(plan: str) -> bool:
    """True when an EXPLAIN text has a Sort or Incremental Sort NODE (a ``Sort Key:`` line alone is not one)."""
    return _SORT_NODE.search(plan) is not None


class _StaticCredentials:
    """The tenant token the run minted; it never changes, so a 401 is final."""

    def __init__(self, token: str) -> None:
        self._token = token

    def bearer(self) -> str:
        return self._token

    def rejected(self, sent: str) -> bool:
        return False


def _free_port() -> int:
    import socket  # noqa: PLC0415

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Cluster:
    """A throwaway PostgreSQL the run owns: made and started by the constructor, then stop, restart and psql."""

    def __init__(self) -> None:
        self.bin = pg_bin_dir()
        pgdata = pg_data_tempdir("nexus_scale_pg_")   # the substrate root: tmpfs on Linux when it has room (the OS cache is warm either way)
        self.pgdata = pgdata
        self.port = _free_port()
        self.user = bootstrap_superuser()
        self.log = Path(pgdata) / "pg.log"
        subprocess.run(
            [str(self.bin / exe_name("initdb")), "-D", pgdata, "-U", self.user, "--no-locale", "-E", "UTF8", "--auth=trust"],
            check=True, capture_output=True,
        )
        # What matters for a read-only export of ~2 GB: a shared_buffers a deployed engine's database would have and the
        # server-default connection cap (reported, not tuned). The substrate's throwaway conf follows (fsync off for the
        # seeding speed, a WAL cap on tmpfs); the export is reads.
        conf = f"""
port = {self.port}
listen_addresses = '127.0.0.1'
unix_socket_directories = '{pgdata}'
max_connections = 100
shared_buffers = 2GB
effective_cache_size = 8GB
work_mem = 16MB
maintenance_work_mem = 1GB
log_line_prefix = '%m '
logging_collector = off
"""
        with open(Path(pgdata) / "postgresql.conf", "a") as f:
            f.write(conf)
            f.write(integration_pg_conf(pgdata))
        pg_ctl_start(str(self.bin / exe_name("pg_ctl")), pgdata, str(self.log), f"-p {self.port} -k {pgdata}")

    def restart(self) -> None:
        """Stop and start the server: shared buffers begin empty (the OS cache does not)."""
        subprocess.run([str(self.bin / exe_name("pg_ctl")), "-D", self.pgdata, "restart", "-m", "fast", "-w", "-l", str(self.log)],
                       check=True, capture_output=True)

    def stop(self, mode: str = "fast") -> None:
        subprocess.run([str(self.bin / exe_name("pg_ctl")), "-D", self.pgdata, "stop", "-m", mode], capture_output=True)

    def psql(self, sql: str, *, user: str | None = None, password: str | None = None, db: str = _DB,
             timeout: float | None = None, extra: list[str] | None = None) -> str:
        env = dict(os.environ)
        if password is not None:
            env["PGPASSWORD"] = password
        proc = subprocess.run(
            [str(self.bin / exe_name("psql")), "-h", "127.0.0.1", "-p", str(self.port), "-U", user or self.user, "-d", db,
             "-v", "ON_ERROR_STOP=1", "-X", "-A", "-t", *(extra or []), "-c", sql],
            capture_output=True, text=True, env=env, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"psql failed: {proc.stderr.strip()[:600]}\n  sql: {sql[:300]}")
        return proc.stdout.strip()

    def psql_script(self, script: str, *, timeout: float | None = None) -> None:
        proc = subprocess.run(
            [str(self.bin / exe_name("psql")), "-h", "127.0.0.1", "-p", str(self.port), "-U", self.user, "-d", _DB,
             "-v", "ON_ERROR_STOP=1", "-X", "-q", "-f", "-"],
            input="SET search_path TO nexus, public, pg_catalog;\n" + script, capture_output=True, text=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"psql script failed: {proc.stderr.strip()[:800]}")

    def createdb(self) -> None:
        subprocess.run([str(self.bin / exe_name("createdb")), "-h", "127.0.0.1", "-p", str(self.port), "-U", self.user, _DB],
                       check=True, capture_output=True)
        self.psql(SERVICE_ROLES_SQL, db=_DB)


class Engine:
    def __init__(self, cluster: Cluster, pool_size: int, workdir: Path) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {
            **os.environ,
            "NX_SERVICE_PORT": str(self.port),
            "NX_SERVICE_TOKEN": _ROOT_TOKEN,
            "NX_DB_URL": f"jdbc:postgresql://127.0.0.1:{cluster.port}/{_DB}",
            "NX_DB_USER": "nexus_svc",
            "NX_DB_PASS": "nexus_svc_pass",
            "NX_POOL_SIZE": str(pool_size),
            "NX_DB_ADMIN_URL": f"jdbc:postgresql://127.0.0.1:{cluster.port}/{_DB}",
            "NX_DB_ADMIN_USER": cluster.user,
            "NX_DB_ADMIN_PASS": "",
            "NX_REAPER_ENABLED": "false",   # the chunk reaper's pass would write gc_audit rows mid-run
            "NX_SEARCH_PCI": "0",           # no per-collection index builds: this run seeds by SQL and reads
        }
        for k in list(self.env):
            if k.startswith("NX_EXPORT_"):
                del self.env[k]              # every export setting at its default
        self.env.pop("NX_STORAGE_BACKEND", None)
        self.workdir = workdir
        self.proc: subprocess.Popen[bytes] | None = None
        self.log: Path | None = None

    def start(self) -> None:
        self.proc, self.log = spawn_service([str(java_executable()), "-jar", str(_JAR)], self.env, log_dir=self.workdir / "engine-log")
        wait_for_service("127.0.0.1", self.port, proc=self.proc, log_path=self.log, timeout=600.0)

    def stop(self) -> None:
        if self.proc is not None:
            stop_group(self.proc, grace_s=10)


# -- seeding ------------------------------------------------------------------------------------------------


def _share_name(percent: str) -> str:
    return "scale-share-" + percent.replace(".", "p")


def _seed(cluster: Cluster, chunks: int, memory_rows: int, small_rows: int, say: Callable[[str], None],
          shares: list[str] | None = None) -> None:
    """Chunks, their manifest and documents, and an id-keyed table, by SQL as the superuser."""
    t, coll, model = _TENANT, _COLLECTION, _MODEL
    say("seed: collection and documents")
    docs = max(1, chunks // 10)
    cluster.psql_script(f"""
INSERT INTO nexus.catalog_collections (tenant_id, name, content_type, owner_id, embedding_model, lifecycle_state)
VALUES ('{t}', '{coll}', 'knowledge', 'scalerun', '{model}', 'live') ON CONFLICT DO NOTHING;
INSERT INTO nexus.catalog_documents (tenant_id, tumbler, title, physical_collection)
SELECT '{t}', '1.1.' || d, 'document ' || d, '{coll}' FROM generate_series(1, {docs}) d;
""")
    chash = "decode(md5(g::text) || md5(g::text || 'x'), 'hex')"
    batches = [(lo, min(lo + _BATCH - 1, chunks)) for lo in range(1, chunks + 1, _BATCH)]
    say("seed: vector pools")
    # 2,000 x 250 vector sums are 500,000 distinct vectors without 384 million random() calls; the pools are scratch tables
    cluster.psql_script("""
CREATE UNLOGGED TABLE public.scale_vp1 AS SELECT i, (SELECT array_agg((random() * 2 - 1)::real) FROM generate_series(1, 768) WHERE i >= 0)::nexus.vector(768) AS v FROM generate_series(0, 1999) i;
CREATE UNLOGGED TABLE public.scale_vp2 AS SELECT i, (SELECT array_agg((random() * 2 - 1)::real) FROM generate_series(1, 768) WHERE i >= 0)::nexus.vector(768) AS v FROM generate_series(0, 249) i;
""")
    for n, (lo, hi) in enumerate(batches, 1):
        # one psql session per batch keeps a transaction's WAL bounded
        cluster.psql_script(f"""
INSERT INTO nexus.chunks (tenant_id, collection, chash, embedding_model, chunk_text, embedding_768, metadata)
SELECT '{t}', '{coll}', {chash}, '{model}', repeat(md5(g::text) || ' ', 30), p1.v + p2.v,
       jsonb_build_object('source', 'scale', 'i', g, 'tags', jsonb_build_array('a', 'b'))
FROM generate_series({lo}, {hi}) g JOIN public.scale_vp1 p1 ON p1.i = g % 2000 JOIN public.scale_vp2 p2 ON p2.i = (g / 2000) % 250;
INSERT INTO nexus.catalog_document_chunks (tenant_id, doc_id, position, chash, chunk_index, collection, embedding_model)
SELECT '{t}', '1.1.' || ((g - 1) / 10 + 1), (g - 1) % 10, {chash}, (g - 1) % 10, '{coll}', '{model}'
FROM generate_series({lo}, {hi}) g;
""", timeout=3600)
        say(f"seed: chunks {hi}/{chunks} (batch {n}/{len(batches)})")
    cluster.psql("DROP TABLE public.scale_vp1, public.scale_vp2")
    say("seed: memory rows")
    # the small tenant's rows are spread evenly over the id range: one row in every small_every, as a small tenant's are in a shared table
    small_every = memory_rows // small_rows if small_rows > 0 else 0
    share_cases = " ".join(f"WHEN g % {_SHARES[p][0]} = {_SHARES[p][1]} THEN '{_share_name(p)}'" for p in shares or [])
    cluster.psql_script(f"""
INSERT INTO nexus.memory (tenant_id, project, title, content, timestamp)
SELECT CASE WHEN {small_every} > 0 AND g % {small_every} = 1 THEN '{_SMALL_TENANT}'
            {share_cases}
            WHEN g % {_NOISE_TENANTS + 1} = 0 THEN '{t}' ELSE 'scale-noise-' || (g % {_NOISE_TENANTS + 1}) END,
       'p' || (g % 50), 'title ' || g, repeat('memory body ', 12), now() FROM generate_series(1, {memory_rows}) g;
""", timeout=3600)
    say("seed: analyze, checkpoint")
    cluster.psql("ANALYZE nexus.chunks; ANALYZE nexus.catalog_document_chunks; ANALYZE nexus.catalog_documents; ANALYZE nexus.memory;", timeout=3600)
    cluster.psql("CHECKPOINT")


# -- the timed export ---------------------------------------------------------------------------------------


class _Timer:
    """httpx event hooks that stamp response start (headers in) for each ``/rows`` request; the caller stamps the end."""

    def __init__(self) -> None:
        self.t0 = 0.0
        self.start = 0.0
        self.url = ""
        self.records: list[dict[str, Any]] = []
        self.depth: dict[str, int] = {}

    def on_request(self, request: httpx.Request) -> None:
        self.t0 = time.perf_counter()
        self.url = str(request.url)

    def on_response(self, response: httpx.Response) -> None:
        self.start = time.perf_counter() - self.t0

    def done(self, table: str, rows: int, nbytes: int, last_key: str | None, done: bool, total: float) -> None:
        depth = self.depth.get(table, 0)
        self.depth[table] = depth + 1
        self.records.append({"table": table, "page": depth, "rows": rows, "bytes": nbytes, "start_s": self.start,
                             "total_s": total, "done": done, "last_key": last_key or ""})


def _export(engine: Engine, token: str, dest: Path, say: Callable[[str], None]) -> tuple[ce.ExportResult, _Timer]:
    timer = _Timer()
    http = httpx.Client(timeout=ce._PAGE_TIMEOUT, event_hooks={"request": [timer.on_request], "response": [timer.on_response]})
    client = ce.ExportClient(engine.url, _StaticCredentials(token), http=http)
    original = client.page

    def timed(session: ce.Session, table: str, after: str | None, columns: int) -> ce.Page:
        page = original(session, table, after, columns)
        # the page call spans request -> body in; the hooks stamped the start of the LAST request of it (a retry restamps)
        timer.done(table, page.rows, len(page.body), page.last_key, page.done, time.perf_counter() - timer.t0)
        return page

    client.page = timed  # type: ignore[method-assign]
    result = ce.run_export(client, dest, on_note=say)
    client.close_http()
    return result, timer


# -- summary ------------------------------------------------------------------------------------------------


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _slope(ys: list[float]) -> float:
    """Least-squares slope of ys over their index (seconds per page)."""
    n = len(ys)
    if n < 2:
        return 0.0
    xs = list(range(n))
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den if den else 0.0


def depth_trend_fails(first: float, last: float) -> bool:
    """True when the first- and last-decile mean response starts differ by more than 2x AND by more than 0.5 s, in either direction."""
    hi, lo = max(first, last), min(first, last)
    return hi > 2 * lo and (hi - lo) > 0.5


def _table_summary(recs: list[dict[str, Any]]) -> dict[str, Any]:
    start = [r["start_s"] for r in recs]
    total = [r["total_s"] for r in recs]
    decile = max(1, len(recs) // 10)
    first, last = start[:decile], start[-decile:]
    return {
        "pages": len(recs),
        "rows": sum(r["rows"] for r in recs),
        "bytes": sum(r["bytes"] for r in recs),
        "start_s": {"p50": _pct(start, 0.5), "p95": _pct(start, 0.95), "max": max(start)},
        "pages_start_s": start,
        "pages_rows": [r["rows"] for r in recs],
        "total_s": {"p50": _pct(total, 0.5), "p95": _pct(total, 0.95), "max": max(total)},
        "deepest_page": {"page": recs[-1]["page"], "rows": recs[-1]["rows"], "bytes": recs[-1]["bytes"],
                         "start_s": recs[-1]["start_s"], "total_s": recs[-1]["total_s"]},
        "max_start_page": max(recs, key=lambda r: r["start_s"])["page"],
        "first_decile_mean_start_s": statistics.fmean(first),
        "last_decile_mean_start_s": statistics.fmean(last),
        "slope_start_s_per_page": _slope(start),
        "slope_total_s_per_page": _slope(total),
    }


def _copy_statements(log: Path, table: str, tenant: str = _TENANT) -> list[str]:
    """The COPY statements the engine sent for ``table`` and ``tenant`` (the tenant's name reaches the statement as hex)."""
    text = log.read_text(errors="replace")
    found = re.findall(r"statement: (COPY \(select [^\n]*? from \"nexus\"\.\"" + table + r"\"[^\n]*\) TO STDOUT \(FORMAT binary\))", text, re.I)
    hexed = tenant.encode().hex()
    return [s for s in found if hexed in s]


def _explain(cluster: Cluster, select: str, tenant: str, settings: str = "") -> str:
    sql = (f"BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SELECT set_config('nexus.tenant', '{tenant}', true); {settings}"
           f"EXPLAIN (ANALYZE, BUFFERS) {select}; ROLLBACK;")
    proc = subprocess.run(
        [str(cluster.bin / exe_name("psql")), "-h", "127.0.0.1", "-p", str(cluster.port), "-U", "nexus_svc", "-d", _DB,
         "-X", "-A", "-t", "-c", sql],
        capture_output=True, text=True, env={**os.environ, "PGPASSWORD": "nexus_svc_pass"}, timeout=600,
    )
    return proc.stdout + proc.stderr


def _decode_key(cursor: str) -> list[str]:
    pad = "=" * (-len(cursor) % 4)
    return list(json.loads(base64.urlsafe_b64decode(cursor + pad)))


@click.command()
@click.option("--chunks", default=500_000, show_default=True, help="Chunks to seed (the gate wants at least 500,000).")
@click.option("--memory-rows", default=_MEMORY_ROWS, show_default=True,
              help="Rows of the id-keyed table in all; the tenant owns a tenth, nine other tenants the rest.")
@click.option("--small-tenant-rows", default=_SMALL_TENANT_ROWS, show_default=True,
              help="Rows a small tenant owns in the id-keyed table, spread evenly over it (0: none). Exported and timed after the big tenant.")
@click.option("--share-tenants", default="", show_default=True,
              help=f"Comma-separated percents of the id-keyed table to give a tenant each, from {', '.join(_SHARES)}; each is exported and its memory pages timed.")
@click.option("--pool-size", default=10, show_default=True, help="NX_POOL_SIZE (the engine's default is 10).")
@click.option("--out-dir", type=click.Path(path_type=Path), required=True, help="Where the results go.")
@click.option("--keep-file/--no-keep-file", default=False, help="Keep the exported .nxtenant file (about 2 GB at 500,000 chunks).")
def main(chunks: int, memory_rows: int, small_tenant_rows: int, share_tenants: str, pool_size: int, out_dir: Path,
         keep_file: bool) -> None:
    """Seed a synthetic tenant, export it through the routes, and write the timing."""
    if reason := jar_freshness_skip_reason():
        raise click.ClickException(f"{reason}; run scripts/build-gate-jar.sh first")
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def say(msg: str) -> None:
        click.echo(f"[{time.monotonic() - started:8.1f}s] {msg}")

    work = Path(original_tempdir()) / f"nexus-scale-run-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    cluster = Cluster()
    engine: Engine | None = None
    sampler_stop = threading.Event()
    peak: dict[str, int] = {"backends": 0, "samples": 0}
    try:
        say(f"cluster: initdb and start in {cluster.pgdata}")
        cluster.createdb()
        engine = Engine(cluster, pool_size, work)
        say("engine: boot (migrates the schema)")
        engine.start()
        token = create_tenant_token(engine.url, _ROOT_TOKEN, _TENANT)
        small_token = create_tenant_token(engine.url, _ROOT_TOKEN, _SMALL_TENANT) if small_tenant_rows > 0 else None
        shares = [x.strip() for x in share_tenants.split(",") if x.strip()]
        for pct in shares:
            if pct not in _SHARES:
                raise click.ClickException(f"--share-tenants: {pct} is not one of {', '.join(_SHARES)}")
        share_tokens = {pct: create_tenant_token(engine.url, _ROOT_TOKEN, _share_name(pct)) for pct in shares}
        say("tenant: created, tenant token minted")
        _seed(cluster, chunks, memory_rows, small_tenant_rows, say, shares)
        counts = {
            "chunks": int(cluster.psql(f"SELECT count(*) FROM nexus.chunks WHERE tenant_id = '{_TENANT}'")),
            "memory_of_tenant": int(cluster.psql(f"SELECT count(*) FROM nexus.memory WHERE tenant_id = '{_TENANT}'")),
            "memory_all_tenants": int(cluster.psql("SELECT count(*) FROM nexus.memory")),
            "memory_of_small_tenant": int(cluster.psql(f"SELECT count(*) FROM nexus.memory WHERE tenant_id = '{_SMALL_TENANT}'")),
            "catalog_document_chunks": int(cluster.psql(f"SELECT count(*) FROM nexus.catalog_document_chunks WHERE tenant_id = '{_TENANT}'")),
        }
        if counts["chunks"] < chunks:
            raise click.ClickException(f"seeded {counts['chunks']} chunks, wanted {chunks}")
        indexes = cluster.psql(
            "SELECT string_agg(DISTINCT indexdef, E'\\n') FROM pg_indexes WHERE schemaname = 'nexus' AND tablename LIKE 'chunks%' "
            "AND indexdef ILIKE '%hnsw%'")
        settings = {k: cluster.psql(f"SHOW {k}") for k in ("max_connections", "shared_buffers", "server_version")}

        say("restart: engine and database (empty shared buffers); statement log on")
        engine.stop()
        cluster.restart()
        cluster.psql("ALTER SYSTEM SET log_statement = 'all'")   # a cancelled COPY (a cut page) logs no duration, so log the statement itself
        cluster.psql("SELECT pg_reload_conf()")
        engine.start()
        sessions_before = int(cluster.psql(f"SELECT sessions FROM pg_stat_database WHERE datname = '{_DB}'"))

        def sample() -> None:
            while not sampler_stop.is_set():
                with contextlib.suppress(Exception):
                    peak["samples"] += 1   # each sample is one session of its own in pg_stat_database
                    n = int(cluster.psql("SELECT count(*) FROM pg_stat_activity WHERE usename = 'nexus_svc'"))
                    peak["backends"] = max(peak["backends"], n)
                sampler_stop.wait(1.0)

        threading.Thread(target=sample, daemon=True).start()
        dest = work / "scale.nxtenant"
        say(f"export: start ({counts['chunks']} chunks)")
        result, timer = _export(engine, token, dest, say)
        sampler_stop.set()
        say(f"export: done, {result.size} bytes in {result.seconds:.1f}s")
        sessions_after = int(cluster.psql(f"SELECT sessions FROM pg_stat_database WHERE datname = '{_DB}'"))
        small_timer = small_result = None
        if small_token is not None:
            say(f"export: small tenant ({small_tenant_rows} memory rows in a table of {memory_rows})")
            small_result, small_timer = _export(engine, small_token, work / "small.nxtenant", say)
            say(f"export: small tenant done in {small_result.seconds:.1f}s")
        share_runs: dict[str, tuple[ce.ExportResult, _Timer]] = {}
        for pct, tok in share_tokens.items():
            say(f"export: {pct}% tenant ({int(cluster.psql(f'SELECT count(*) FROM nexus.memory WHERE tenant_id = {chr(39)}{_share_name(pct)}{chr(39)}'))} memory rows)")
            share_runs[pct] = _export(engine, tok, work / f"share-{pct}.nxtenant", say)
            say(f"export: {pct}% tenant done in {share_runs[pct][0].seconds:.2f}s")

        with open(out_dir / "pages.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["table", "page", "rows", "bytes", "start_s", "total_s", "done", "last_key"])
            w.writeheader()
            w.writerows(timer.records)
        by_table: dict[str, list[dict[str, Any]]] = {}
        for r in timer.records:
            by_table.setdefault(r["table"], []).append(r)
        tables = {name: _table_summary(recs) for name, recs in by_table.items() if recs}
        cut_pages = sum(1 for r in timer.records if not r["done"] and r["rows"] > 0)

        # EXPLAIN (ANALYZE) of the statements the engine really sent, replayed as the engine's role under the tenant stamp and, for
        # a table whose key leads with tenant_id, under the planner setting the engine's page transaction sets for itself
        # (TenantExportReads.pinOrderedScan; a psql replay does not inherit it). The "-pin-flipped" variant is the other choice.
        explains: dict[str, str] = {}
        inner = lambda s: re.sub(r"^COPY \((.*)\) TO STDOUT \(FORMAT binary\)$", r"\1", s, flags=re.S)  # noqa: E731
        for table in ("chunks", "memory"):
            pinned = table == "chunks"
            stmts = _copy_statements(cluster.log, table)
            if not stmts:
                continue
            picks = {"first-page": stmts[0], "mid-page": stmts[len(stmts) // 2], "deep-page": stmts[-1]}
            engine_settings = _ENGINE_PIN if pinned else ""
            for label, stmt in picks.items():
                explains[f"{table}-{label}"] = _explain(cluster, inner(stmt), _TENANT, engine_settings)
            explains[f"{table}-mid-page-pin-flipped"] = _explain(cluster, inner(picks["mid-page"]), _TENANT, "" if pinned else _ENGINE_PIN)
            explains[f"{table}-first-page-count"] = _explain(
                cluster, f"SELECT count(*) FROM \"nexus\".\"{table}\" WHERE tenant_id = '{_TENANT}'", _TENANT)
        small_summary = None
        if small_timer is not None:
            small_memory = [r for r in small_timer.records if r["table"] == "memory"]
            small_summary = {"memory_pages": _table_summary(small_memory) if small_memory else None,
                             "export_seconds": small_result.seconds if small_result else None}
            for stmt in _copy_statements(cluster.log, "memory", _SMALL_TENANT)[:1]:
                explains["memory-small-tenant-first-page"] = _explain(cluster, inner(stmt), _SMALL_TENANT)
                explains["memory-small-tenant-first-page-pin-flipped"] = _explain(cluster, inner(stmt), _SMALL_TENANT, _ENGINE_PIN)
            explains["memory-small-tenant-count"] = _explain(
                cluster, f"SELECT count(*) FROM \"nexus\".\"memory\" WHERE tenant_id = '{_SMALL_TENANT}'", _SMALL_TENANT)
        share_summary: dict[str, Any] = {}
        for pct, (res, tmr) in share_runs.items():
            name = _share_name(pct)
            mem = [r for r in tmr.records if r["table"] == "memory"]
            share_summary[pct] = {"tenant": name,
                                  "rows": int(cluster.psql(f"SELECT count(*) FROM nexus.memory WHERE tenant_id = '{name}'")),
                                  "export_seconds": res.seconds, "memory_pages": _table_summary(mem) if mem else None}
            stmts = _copy_statements(cluster.log, "memory", name)
            if stmts:
                explains[f"memory-share-{pct}-first-page"] = _explain(cluster, inner(stmts[0]), name)
                explains[f"memory-share-{pct}-deep-page"] = _explain(cluster, inner(stmts[-1]), name)
        for name, text in explains.items():
            (out_dir / f"explain-{name}.txt").write_text(text)

        # the pin is the engine's: the replay above sets it by hand, so a Sort here means the replay and the engine disagree or
        # the planner stopped honouring it; an absent plan is a failure of the check, not a pass
        deep_plans = {t: explains.get(f"{t}-deep-page") for t in _PINNED_TABLES}
        missing = [t for t, plan in deep_plans.items() if plan is None]
        if missing:
            raise click.ClickException(f"no deep-page EXPLAIN for {', '.join(missing)}: the Sort check examined nothing")
        sorted_plans = [t for t, plan in deep_plans.items() if plan_has_sort(plan or "")]
        chunks_summary = tables.get("chunks")
        growth = None
        regression = False
        if chunks_summary and chunks_summary["pages"] >= 10:
            first, last = chunks_summary["first_decile_mean_start_s"], chunks_summary["last_decile_mean_start_s"]
            regression = depth_trend_fails(first, last)
            growth = {"first_decile_mean_start_s": first, "last_decile_mean_start_s": last,
                      "ratio": last / first if first else None, "slope_start_s_per_page": chunks_summary["slope_start_s_per_page"],
                      "slope_total_s_per_page": chunks_summary["slope_total_s_per_page"], "time_depends_on_depth": regression}

        engine_lines: list[str] = []
        if engine.log is not None and engine.log.exists():
            engine_lines = [ln for ln in engine.log.read_text(errors="replace").splitlines() if "export" in ln.lower()][:40]
        summary = {
            "seeded": counts,
            "hnsw_indexes_on_chunks": indexes or "none",
            "postgres": settings,
            "engine": {"pool_size": pool_size, "export_settings": "defaults (NX_EXPORT_* unset)", "jvm": "spawn_service sizing: -Xmx2g, 2 CPUs"},
            "file_bytes": result.size,
            "total_seconds": result.seconds,
            "total_rows": sum(t[2] for t in result.tables),
            "pages_total": len(timer.records),
            "cut_pages": cut_pages,
            "tables": tables,
            "growth_with_depth": growth,
            "small_tenant": small_summary,
            "share_tenants": share_summary,
            "connections": {"peak_nexus_svc_backends": peak["backends"], "sessions_established_during_export_all": sessions_after - sessions_before,
                            "sampler_sessions": peak["samples"] + 1,
                            "sessions_established_by_engine": sessions_after - sessions_before - peak["samples"] - 1,
                            "note": "a cut page evicts its connection, so sessions established ~ cut pages (+ the session and header connections)"},
            # the engine's own page transactions, read from the statement log: the pin is set by the engine, not by this script
            "page_pin_statements_in_pg_log": cluster.log.read_text(errors="replace").count("enable_sort"),
            "engine_export_log_lines": engine_lines,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        if keep_file:
            shutil.move(str(dest), out_dir / dest.name)
        say("summary.json written")
        click.echo(json.dumps({k: summary[k] for k in ("file_bytes", "total_seconds", "pages_total", "cut_pages", "growth_with_depth")}, indent=2, default=str))
        if sorted_plans:
            click.echo(f"FAIL: the deep-page plan of {', '.join(sorted_plans)} has a Sort node: the engine's pin is not in effect", err=True)
            raise SystemExit(_EXIT_PINNED_PLAN_SORTS)
        if regression:
            raise SystemExit(_EXIT_DEEP_PAGE_REGRESSION)
    finally:
        sampler_stop.set()
        if engine is not None:
            engine.stop()
        cluster.stop("immediate")
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
