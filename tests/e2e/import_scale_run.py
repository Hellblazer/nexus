#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-248 Step 6 (nexus-csayy.7): restore a synthetic tenant of at least 500,000 chunks into an empty tenant of the
same database through the shipped routes and the shipped client, and measure the commit at scale.

The sibling of ``export_scale_run.py`` (gate N1): it reuses that script's cluster, engine and seed, and adds what an
import needs. A measurement script, not a test (no ``test_`` prefix; pytest does not collect it); it boots its own
cluster, so it depends on no ambient state. It needs the engine jar (``scripts/build-gate-jar.sh``) and a PostgreSQL
with pgvector. A restore holds the file's size three times over on the database side (the source, the staging
temp tables, the restored tenant) plus WAL, so on chas run it on ``lab-l``; ``lab-m`` (16 GB) does not hold it::

    lab-remote -c lab-l --fetch import-scale-out -- bash -c \\
        'scripts/build-gate-jar.sh && uv run python tests/e2e/import_scale_run.py --out-dir import-scale-out'

What it does, in order:

1. initdb and start a PostgreSQL (``max_connections`` 100, ``shared_buffers`` 2 GB), then the engine jar with
   ``NX_POOL_SIZE`` 10, every ``NX_EXPORT_*`` setting at its default, and ``NX_IMPORT_LOAD_MAX`` raised to ``--load-max`` (the
   run measures the load, so the default bound must not cut it).
2. Create the source tenant and seed it by SQL as the superuser: the export run's seed (chunks, manifest, documents,
   the id-keyed ``memory`` table) and then EVERY other data and derived table of the registry, with the references a
   restore rewrites: ``topics`` with parents of larger id, assignments and links onto them, centroids, summaries
   whose ``source_ids`` hold live, negative and dangling ids, plans, runs whose ``plan_id`` and ``run_summary`` hold
   live, NULL, 0 and dangling plans, and their steps. ``ANALYZE``, a checkpoint, and a restart of the database.
3. Export it with the shipped client (``nexus.backup.cloud_export``) to a real ``.nxtenant`` file.
4. Create the empty target tenant, time a tenant create on the idle database, and restore the file with the shipped
   client (``nexus.backup.cloud_import.run_restore``). While it runs, a sampler records once a second:
   the import status (state, staged bytes), the database's oldest transaction age and oldest ``xmin`` age among
   all backends, the longest transaction of the engine's role, and the engine's own running statement and what it waits on. The staged
   bytes are the engine's own count (``staged_bytes`` of the status route), not a count of temp-table files.
   While the engine reports ``loading`` or ``verifying``, a prober asks the engine to create a NEW tenant (the DDL
   path, which a load blocks) every 3 s and records the wait and the ``Retry-After``, and asks for a token of an
   EXISTING tenant (the fast path), which must not wait.
5. Compare the per-table row counts of the source and the target, and write ``summary.json`` (every number a record
   needs), ``samples.csv``, ``probes.json`` and the engine's import log lines. It prints the proposed
   ``NX_IMPORT_LOAD_MAX`` (twice the measured load, rounded up to a whole minute).

Exit codes: 0 done; 5 the load took more than 30 minutes (the default bound is 60: stop and report, do not change it);
6 the restore did not commit or the counts differ.
"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click
import httpx

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
import export_scale_run as esr  # noqa: E402  (the sibling script: its cluster, engine and seed)

from nexus.backup import cloud_import as ci  # noqa: E402
from tests.db._service_fixture import create_tenant_token, jar_freshness_skip_reason, original_tempdir  # noqa: E402

_TARGET = "scale-restore"
_EXIT_LOAD_OVER_30_MIN = 5
_EXIT_NOT_COMMITTED = 6
_LOAD_WARN_SECONDS = 30 * 60


def _seed_other_tables(cluster: esr.Cluster, chunks: int, say: Callable[[str], None]) -> dict[str, int]:
    """Every data and derived table the export seed leaves empty, with the references a restore rewrites. Returns the plan."""
    t, coll, model = esr._TENANT, esr._COLLECTION, esr._MODEL
    docs = max(1, chunks // 10)
    n_topics = max(20, chunks // 100)
    n_summaries = max(10, chunks // 25)
    n_plans = max(10, chunks // 25)
    n_runs = max(10, chunks // 10)
    chash = "decode(md5(g::text) || md5(g::text || 'x'), 'hex')"
    plan = {"topics": n_topics, "memory_summaries": n_summaries, "plans": n_plans, "nx_answer_runs": n_runs}
    say("seed: owners, profile, meta, sensitivity, links, aspects, highlights")
    cluster.psql_script(f"""
INSERT INTO catalog_owners (tenant_id, tumbler_prefix, name, owner_type)
VALUES ('{t}', '1.1', 'owner-1', 'curator'), ('{t}', '1.2', 'owner-2', 'repo');
INSERT INTO embedding_profile (tenant_id, content_type, embedding_model, dimension) VALUES ('{t}', 'knowledge', '{model}', 768);
INSERT INTO catalog_meta (tenant_id, key, value) SELECT '{t}', 'k' || g, 'v' || g FROM generate_series(1, {max(10, chunks // 500)}) g;
INSERT INTO catalog_sensitivity_changes (tenant_id, tumbler, old, new, reason, actor)
SELECT '{t}', '1.1.' || g, false, true, 'r', 'a' FROM generate_series(1, {max(10, chunks // 50)}) g;
INSERT INTO catalog_links (tenant_id, from_tumbler, to_tumbler, link_type, created_by)
SELECT '{t}', '1.1.' || g, '1.1.' || (g % {docs} + 1), 'cites', 'a' FROM generate_series(1, {docs}) g;
INSERT INTO document_aspects (tenant_id, collection, source_path, extracted_at, model_version, extractor_name, source_uri, doc_id)
SELECT '{t}', '{coll}', 'p' || g, now(), 'm', 'e', 'u' || g, '1.1.' || g FROM generate_series(1, {docs}) g;
INSERT INTO document_highlights (tenant_id, doc_id, source_uri, collection, ingested_at)
SELECT '{t}', '1.1.' || g, 'u' || g, '{coll}', now() FROM generate_series(1, {max(10, docs // 2)}) g;
INSERT INTO chunk_orphaned_at (tenant_id, collection, chash, orphaned_at, embedding_model)
SELECT '{t}', '{coll}', {chash}, now(), '{model}' FROM generate_series(1, {max(10, chunks // 50)}) g;
INSERT INTO taxonomy_meta (tenant_id, collection) VALUES ('{t}', '{coll}');
INSERT INTO claude_assisted_remediation_consents (tenant_id, scope, ts, granted)
SELECT '{t}', 's' || g, now(), g % 2 = 0 FROM generate_series(1, 10) g;
""")
    say("seed: topics (parents of larger id), assignments, links, centroids")
    cluster.psql_script(f"""
INSERT INTO topics (tenant_id, label, collection, created_at)
SELECT '{t}', 'topic ' || lpad(g::text, 7, '0'), '{coll}', now() FROM generate_series(1, {n_topics}) g;
WITH ordered AS (SELECT id, lead(id) OVER (ORDER BY id) AS nxt, row_number() OVER (ORDER BY id) AS rn FROM topics WHERE tenant_id = '{t}')
UPDATE topics x SET parent_id = o.nxt FROM ordered o WHERE x.id = o.id AND o.rn % 2 = 1 AND o.nxt IS NOT NULL;
CREATE UNLOGGED TABLE public.scale_ids AS SELECT row_number() OVER (ORDER BY id) AS rn, id FROM topics WHERE tenant_id = '{t}';
INSERT INTO topic_assignments (tenant_id, doc_id, topic_id, source_collection, embedding_model)
SELECT '{t}', {chash}, i.id, '{coll}', '{model}' FROM generate_series(1, {max(10, chunks // 5)}) g JOIN public.scale_ids i ON i.rn = g % {n_topics} + 1;
INSERT INTO topic_links (tenant_id, from_topic_id, to_topic_id)
SELECT '{t}', a.id, b.id FROM public.scale_ids a JOIN public.scale_ids b ON b.rn = a.rn % {n_topics} + 1;
INSERT INTO taxonomy_centroids (tenant_id, collection, topic_id, label, embedding_model, embedding_768)
SELECT '{t}', '{coll}', i.id, 'centroid ' || i.rn, '{model}', v.v
FROM public.scale_ids i, (SELECT array_agg((random() * 2 - 1)::real)::nexus.vector(768) AS v FROM generate_series(1, 768)) v;
DROP TABLE public.scale_ids;
""")
    say("seed: memory_summaries (live, negative and dangling sources), plans, runs, steps")
    cluster.psql_script(f"""
CREATE UNLOGGED TABLE public.scale_mem AS SELECT row_number() OVER (ORDER BY id) AS rn, id FROM memory WHERE tenant_id = '{t}';
INSERT INTO plans (tenant_id, query, plan_json, created_at, verb)
SELECT '{t}', 'q' || g, '{{"steps": []}}'::jsonb, now(), 'research' FROM generate_series(1, {n_plans}) g;
CREATE UNLOGGED TABLE public.scale_plan AS SELECT row_number() OVER (ORDER BY id) AS rn, id FROM plans WHERE tenant_id = '{t}';
INSERT INTO memory_summaries (tenant_id, project, content, source_ids, model)
SELECT '{t}', 'p', 'summary ' || g,
       CASE WHEN g % 50 = 1 THEN ARRAY[a.id, -7::bigint, 999999999 + g]   -- live, already negative, dangling positive
            ELSE ARRAY[a.id, b.id] END, 'm'
FROM generate_series(1, {n_summaries}) g
JOIN public.scale_mem a ON a.rn = (g * 2) % (SELECT count(*) FROM public.scale_mem) + 1
JOIN public.scale_mem b ON b.rn = (g * 2 + 1) % (SELECT count(*) FROM public.scale_mem) + 1;
INSERT INTO nx_answer_runs (tenant_id, question, created_at, plan_id, run_summary)
SELECT '{t}', 'question ' || g, now(), pid, CASE WHEN pid IS NULL THEN NULL ELSE jsonb_build_object('run', jsonb_build_object('plan_id', pid)) END
FROM (SELECT g, CASE WHEN g % 20 = 0 THEN NULL WHEN g % 20 = 1 THEN 0::bigint WHEN g % 20 = 2 THEN 888888888 + g
                     ELSE p.id END AS pid
      FROM generate_series(1, {n_runs}) g JOIN public.scale_plan p ON p.rn = g % {n_plans} + 1) s;
INSERT INTO nx_answer_steps (run_id, tenant_id, step_index, operator, source, elapsed_ms, ok)
SELECT r.id, '{t}', k, CASE k WHEN 0 THEN 'extract' ELSE 'rank' END, 'llm', 5, k = 0
FROM nx_answer_runs r, generate_series(0, 1) k WHERE r.tenant_id = '{t}';
DROP TABLE public.scale_mem, public.scale_plan;
""", timeout=3600)
    say("seed: frecency, search_telemetry, relevance_log")
    cluster.psql_script(f"""
INSERT INTO frecency (tenant_id, chunk_id) SELECT '{t}', md5(g::text) || md5('f' || g) FROM generate_series(1, {max(10, chunks // 5)}) g;
INSERT INTO search_telemetry (tenant_id, ts, query_hash, collection, raw_count, kept_count)
SELECT '{t}', now() - (g || ' seconds')::interval, md5(g::text), '{coll}', 10, 5 FROM generate_series(1, {max(10, chunks // 2)}) g;
INSERT INTO relevance_log (tenant_id, query, chunk_id, action, timestamp)
SELECT '{t}', 'q' || (g % 100), md5(g::text) || md5('r' || g), CASE WHEN g % 2 = 0 THEN 'hit' ELSE 'miss' END, now()
FROM generate_series(1, {max(10, chunks // 5)}) g;
""", timeout=3600)
    return plan


class _Sampler(threading.Thread):
    """Once a second: the import status, the database's transaction ages and the engine's running statement."""

    _SQL = (
        "SELECT coalesce(max(age(backend_xmin)), 0), coalesce(max(age(backend_xid)), 0), "
        "coalesce(max(extract(epoch FROM now() - xact_start)) FILTER (WHERE usename = 'nexus_svc'), 0)::float8, "
        "coalesce(max(extract(epoch FROM now() - xact_start)), 0)::float8, "
        # the engine's oldest running statement and what it waits on (a load that stalls shows here)
        "coalesce((SELECT coalesce(wait_event_type || ':' || wait_event, 'cpu') || ' ' || left(regexp_replace(query, '\\s+', ' ', 'g'), 70) "
        "FROM pg_stat_activity WHERE usename = 'nexus_svc' AND state = 'active' AND xact_start IS NOT NULL ORDER BY xact_start LIMIT 1), '') "
        "FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND datname = '" + esr._DB + "'"
    )

    def __init__(self, cluster: esr.Cluster, engine_url: str, token: str) -> None:
        super().__init__(daemon=True)
        self.cluster, self.url, self.token = cluster, engine_url, token
        self.session_id: str | None = None
        self.stop_event = threading.Event()
        self.rows: list[dict[str, Any]] = []
        self.state = ""
        self.t0 = time.monotonic()
        self._http = httpx.Client(timeout=10.0)

    def run(self) -> None:
        while not self.stop_event.is_set():
            row: dict[str, Any] = {"t": round(time.monotonic() - self.t0, 2)}
            try:
                xmin_age, xid_age, svc_xact_s, any_xact_s, engine_stmt = self.cluster.psql(self._SQL).split("|", 4)
                row.update(xmin_age=int(xmin_age), xid_age=int(xid_age), engine_xact_s=float(svc_xact_s),
                           any_xact_s=float(any_xact_s), engine_stmt=engine_stmt)
            except Exception as exc:  # noqa: BLE001 - a sample is advisory; a failed one is a gap, not a failure of the run
                row["error"] = str(exc)[:80]
            if self.session_id:
                try:
                    body = self._http.get(f"{self.url}/v1/tenant/import/{self.session_id}",
                                          headers={"Authorization": f"Bearer {self.token}"}).json()
                    self.state = str(body.get("state", ""))
                    row.update(state=self.state, staged_bytes=body.get("staged_bytes"))
                except Exception:  # noqa: BLE001 - the same
                    pass
            self.rows.append(row)
            self.stop_event.wait(1.0)

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=10)
        self._http.close()


class _Prober(threading.Thread):
    """While the engine loads: a NEW tenant create (the DDL path a load blocks) and a token of an EXISTING tenant, every 3 s."""

    def __init__(self, sampler: _Sampler, engine_url: str, root: str, existing: str) -> None:
        super().__init__(daemon=True)
        self.sampler, self.url, self.root, self.existing = sampler, engine_url, root, existing
        self.stop_event = threading.Event()
        self.probes: list[dict[str, Any]] = []

    def probe(self, label: str, name: str) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            r = httpx.post(f"{self.url}/v1/tenants/create", json={"name": name}, headers={"Authorization": f"Bearer {self.root}"}, timeout=60.0)
            out: dict[str, Any] = {"status": r.status_code, "retry_after": r.headers.get("Retry-After"), "body": r.text[:200] if r.status_code != 200 else "ok"}
        except httpx.HTTPError as exc:
            out = {"status": None, "error": type(exc).__name__}
        out.update(label=label, tenant=name, seconds=round(time.perf_counter() - started, 3), at=round(time.monotonic() - self.sampler.t0, 1))
        self.probes.append(out)
        return out

    def run(self) -> None:
        n = 0
        while not self.stop_event.is_set():
            if self.sampler.state in ("loading", "verifying"):
                n += 1
                self.probe("new-tenant-during-load", f"scale-during-{n}")
                self.probe("existing-tenant-during-load", self.existing)
                self.stop_event.wait(3.0)
            else:
                self.stop_event.wait(0.2)

    def stop(self) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout=70)


def _count(cluster: esr.Cluster, table: str, tenant: str) -> int:
    return int(cluster.psql(f"SELECT count(*) FROM nexus.{table} WHERE tenant_id = '{tenant}'", timeout=1800))


@click.command()
@click.option("--chunks", default=500_000, show_default=True, help="Chunks to seed (the gate wants at least 500,000).")
@click.option("--memory-rows", default=esr._MEMORY_ROWS, show_default=True, help="Rows of the id-keyed table in all; the tenant owns a tenth.")
@click.option("--pool-size", default=10, show_default=True, help="NX_POOL_SIZE (the engine's default is 10).")
@click.option("--load-max", default=21_600, show_default=True,
              help="NX_IMPORT_LOAD_MAX in seconds for THIS run (the default is 3960). A run that must learn how long the load takes "
                   "cannot be cut at the bound it is meant to set; the summary records the value used.")
@click.option("--probe/--no-probe", default=True, show_default=True,
              help="Probe tenant create during the load. A probe is a real request that takes locks, so a run without it shows what the probes cost.")
@click.option("--out-dir", type=click.Path(path_type=Path), required=True, help="Where the results go.")
def main(chunks: int, memory_rows: int, pool_size: int, load_max: int, probe: bool, out_dir: Path) -> None:
    """Seed every table of a synthetic tenant, export it, restore it into an empty tenant, and write the numbers."""
    if reason := jar_freshness_skip_reason():
        raise click.ClickException(f"{reason}; run scripts/build-gate-jar.sh first")
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()

    def say(msg: str) -> None:
        click.echo(f"[{time.monotonic() - started:8.1f}s] {msg}")

    work = Path(original_tempdir()) / f"nexus-import-scale-run-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    cluster = esr.Cluster()
    engine: esr.Engine | None = None
    sampler: _Sampler | None = None
    prober: _Prober | None = None
    exit_code = 0
    try:
        say(f"cluster: initdb and start in {cluster.pgdata}")
        cluster.createdb()
        engine = esr.Engine(cluster, pool_size, work)
        for k in list(engine.env):
            if k.startswith("NX_IMPORT_"):
                del engine.env[k]   # every import setting at its default, except the load bound, which the run raises (above)
        engine.env["NX_IMPORT_LOAD_MAX"] = str(load_max)
        say("engine: boot (migrates the schema)")
        engine.start()
        src_token = create_tenant_token(engine.url, esr._ROOT_TOKEN, esr._TENANT)
        esr._seed(cluster, chunks, memory_rows, 0, say)
        plan = _seed_other_tables(cluster, chunks, say)
        say("seed: analyze, checkpoint")
        cluster.psql("ANALYZE", timeout=3600)
        cluster.psql("CHECKPOINT")
        db_bytes = int(cluster.psql(f"SELECT pg_database_size('{esr._DB}')"))
        settings = {k: cluster.psql(f"SHOW {k}") for k in ("max_connections", "shared_buffers", "server_version", "max_wal_size", "wal_level")}

        say("restart: engine and database (empty shared buffers)")
        engine.stop()
        cluster.restart()
        engine.start()

        dest = work / "scale.nxtenant"
        say("export: start")
        result, _timer = esr._export(engine, src_token, dest, say)
        say(f"export: done, {result.size} bytes in {result.seconds:.1f}s")

        tgt_token = create_tenant_token(engine.url, esr._ROOT_TOKEN, _TARGET)
        idle_create = _Prober(_Sampler(cluster, engine.url, tgt_token), engine.url, esr._ROOT_TOKEN, esr._TENANT).probe("new-tenant-idle", "scale-idle")
        say(f"tenant create on an idle database: {idle_create['seconds']}s (status {idle_create['status']})")
        baseline = cluster.psql(_Sampler._SQL).split("|", 4)[:4]

        sampler = _Sampler(cluster, engine.url, tgt_token)
        prober = _Prober(sampler, engine.url, esr._ROOT_TOKEN, esr._TENANT)
        client = ci.ImportClient(engine.url, esr._StaticCredentials(tgt_token), remote=True)
        marks: dict[str, float] = {}
        opened = client.open_session

        def open_session(index: Any) -> Any:
            marks["open"] = time.monotonic()
            session = opened(index)
            sampler.session_id = session.id   # type: ignore[union-attr]
            marks["opened"] = time.monotonic()
            return session

        client.open_session = open_session  # type: ignore[method-assign]
        sent = {"parts": 0, "bytes": 0}
        put = client.put_part

        def put_part(session: Any, table: str, part: int, data: bytes) -> None:
            put(session, table, part, data)
            sent["parts"] += 1
            sent["bytes"] += len(data)

        client.put_part = put_part  # type: ignore[method-assign]

        def note(msg: str) -> None:
            say(f"restore: {msg}")
            if msg.startswith("Uploaded"):
                marks["uploaded"] = time.monotonic()

        sampler.start()
        if probe:
            prober.start()
        say("restore: start")
        marks["start"] = time.monotonic()
        restored = ci.run_restore(client, dest, on_note=note, poll_interval_s=1.0)
        marks["end"] = time.monotonic()
        sampler.state = "done"
        prober.stop()
        sampler.stop()
        after_create = prober.probe("new-tenant-after-commit", "scale-after") if probe else {"seconds": None, "status": None}
        say(f"restore: committed in {marks['end'] - marks['start']:.1f}s; create after commit {after_create['seconds']}s (status {after_create['status']})")

        # what the sampler saw, by phase of the restore
        committed_at = sampler.rows and next((r["t"] for r in sampler.rows if r.get("state") == "committed"), None)
        loading_at = next((r["t"] for r in sampler.rows if r.get("state") in ("loading", "verifying")), None)
        staging = [r for r in sampler.rows if r.get("state") == "staging" or (loading_at is not None and r["t"] < loading_at)]
        loading = [r for r in sampler.rows if loading_at is not None and r["t"] >= loading_at and (committed_at is None or r["t"] <= committed_at)]

        def peak(rows: list[dict[str, Any]], key: str) -> Any:
            vals = [r[key] for r in rows if r.get(key) is not None]
            return max(vals) if vals else None

        probes = [idle_create, *prober.probes]   # the after-commit probe is in prober.probes
        counts = {}
        mismatches = []
        for t in restored.header["tables"]:
            name = t["name"]
            a, b = _count(cluster, name, esr._TENANT), _count(cluster, name, _TARGET)
            counts[name] = {"class": t["class"], "source": a, "restored": b}
            expect = a + 1 if name == "catalog_meta" else a   # the restore's own marker
            if b != expect:
                mismatches.append(f"{name}: source {a}, restored {b}")
        outcome = restored.outcome
        load_seconds = (marks["end"] - marks["uploaded"]) if "uploaded" in marks else None
        proposed = int(math.ceil(2 * load_seconds / 60.0) * 60) if load_seconds else None
        summary = {
            "seeded": {"chunks": chunks, "memory_rows_all_tenants": memory_rows, "other_tables": plan},
            "database_bytes_after_seed": db_bytes,
            "postgres": settings,
            "engine": {"pool_size": pool_size, "export_settings": "defaults (NX_EXPORT_* unset)",
                       "import_settings": f"NX_IMPORT_LOAD_MAX={load_max} (default 3960), the rest at their defaults"},
            "file_bytes": result.size,
            "export_seconds": result.seconds,
            "restore": {
                "total_seconds": marks["end"] - marks["start"],
                "open_seconds": marks["opened"] - marks["open"],
                "upload_seconds": (marks["uploaded"] - marks["opened"]) if "uploaded" in marks else None,
                "load_seconds": load_seconds,
                "outcome_source": outcome.source,
                "parts_sent": sent["parts"], "bytes_sent": sent["bytes"], "row_bytes": restored.bytes_sent,
                "engine_report": outcome.report,
                "notes": outcome.notes,
            },
            "hold": {
                "note": "the import session holds one transaction on one connection from the first part to the commit; "
                        "engine_xact_s is the longest transaction age of the engine's role, any_xact_s of any backend, "
                        "xmin_age/xid_age the largest age() of any backend's xmin/xid, in transactions",
                "baseline_xmin_xid_engine_any": baseline,
                "peak_staging": {k: peak(staging, k) for k in ("xmin_age", "xid_age", "engine_xact_s", "any_xact_s", "staged_bytes")},
                "peak_loading": {k: peak(loading, k) for k in ("xmin_age", "xid_age", "engine_xact_s", "any_xact_s", "staged_bytes")},
                "peak_all": {k: peak(sampler.rows, k) for k in ("xmin_age", "xid_age", "engine_xact_s", "any_xact_s", "staged_bytes")},
                "samples": len(sampler.rows),
                "loading_first_seen_at_s": loading_at, "committed_seen_at_s": committed_at,
            },
            "tenant_create": probes,
            "counts": counts,
            "count_mismatches": mismatches,
            "proposed_NX_IMPORT_LOAD_MAX_seconds": proposed,
            "default_NX_IMPORT_LOAD_MAX_seconds": 3960,
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
        (out_dir / "probes.json").write_text(json.dumps(probes, indent=2))
        with open(out_dir / "samples.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["t", "state", "staged_bytes", "xmin_age", "xid_age", "engine_xact_s", "any_xact_s", "engine_stmt", "error"], extrasaction="ignore")
            w.writeheader()
            w.writerows(sampler.rows)
        if engine.log is not None and engine.log.exists():
            (out_dir / "engine-import-log.txt").write_text("\n".join(
                ln for ln in engine.log.read_text(errors="replace").splitlines() if "import" in ln.lower() or "restore" in ln.lower())[:200_000])
        say("summary.json written")
        click.echo(json.dumps({k: summary[k] for k in ("file_bytes", "export_seconds", "proposed_NX_IMPORT_LOAD_MAX_seconds", "count_mismatches")}, indent=2, default=str))
        click.echo(json.dumps(summary["restore"] | {"engine_report": "(see summary.json)", "notes": "(see summary.json)"}, indent=2, default=str))
        click.echo(json.dumps(summary["hold"], indent=2, default=str))
        click.echo(json.dumps(probes, indent=2))
        if mismatches:
            click.echo(f"FAIL: counts differ: {mismatches}", err=True)
            exit_code = _EXIT_NOT_COMMITTED
        elif load_seconds is not None and load_seconds > _LOAD_WARN_SECONDS:
            click.echo(f"STOP: the load took {load_seconds:.0f}s, over 30 minutes; report it, do not change NX_IMPORT_LOAD_MAX", err=True)
            exit_code = _EXIT_LOAD_OVER_30_MIN
    finally:
        if prober is not None:
            prober.stop_event.set()
        if sampler is not None:
            sampler.stop_event.set()
        if engine is not None:
            engine.stop()
        cluster.stop("immediate")
        shutil.rmtree(work, ignore_errors=True)
    if exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
