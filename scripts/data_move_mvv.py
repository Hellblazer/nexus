#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-246 Minimum Viable Validation driver (nexus-f3yqd.2.22): a registered move over nexus.chunks, run on a
live-like store and measured.

It builds on ``scripts/upgrade_boot_writes.py`` (the old-state builder, the boot measurement and the helpers) and
``scripts/upgrade_boot_seed.py`` (the seed) and adds what those do not have: a jar that carries a test-only move, a
kill in the middle of that move, a steady search load with and without it, and a boot on a PostgreSQL without
pg_background. It touches no cloud. Heavy: run it on a lab host (``lab-run -c lab-m -- uv run python
scripts/data_move_mvv.py ...``), never on the laptop.

Subcommands:

``make-jar SRC DST``
    Copies a gate jar and adds ``service/src/test/resources/mvv/mvv-chunks-move.xml`` to it, included in the master
    changelog before ``grants-nexus-svc``. The changeset adds a nullable counter column to nexus.chunks and registers
    one move over it with one INSERT. Test only: no main changelog includes it and nothing ships it.

``scenario NAME --work DIR --orig DIR --jar JAR``
    ``DIR`` is an old-state install built by ``upgrade_boot_writes.py --prepare-only --work DIR``; ``--orig`` is a
    pristine copy of it (restored over ``DIR`` before every scenario, so each starts from the same bytes). Names:

    ``upgrade``      boot the new engine once on the old state: bootstrap, libraries, time to serve, the move's
                     progress in /v1/status, completion, boot-time write bytes.
    ``kill-resume``  SIGKILL the engine and its supervisor in the middle of the move, boot again, and check the move
                     went on from the last committed chunk with every row moved exactly once.
    ``load``         a steady search load, with the move paused and then running, at one ``--sleep-ms``.
    ``no-pgbg``      boot on a bundle without pg_background.
    ``masking``      after the move, register a move that errors with the admin password in its message; the engine
                     log must carry ``<redacted>`` and never the password.

Every scenario prints one JSON object on stdout (and to ``--json``). Exit 0 when its checks pass, 1 when one fails,
2 when the run could not measure.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE))

#: Where the test changeset sits inside the jar, and the include that names it.
MVV_RESOURCE = "db/changelog/mvv-chunks-move.xml"
MASTER_ENTRY = "db/changelog/db.changelog-master.xml"
GRANTS_ANCHOR = '<include file="db/changelog/grants-nexus-svc.xml"/>'
CHANGESET = REPO / "service" / "src" / "test" / "resources" / "mvv" / "mvv-chunks-move.xml"
MOVE_LABEL = "mvv-chunks-stamp"


class MvvError(RuntimeError):
    """The run could not produce a measurement (exit 2)."""


def log(msg: str) -> None:
    sys.stderr.write(f"data_move_mvv: {msg}\n")
    sys.stderr.flush()


# ── pure parts (tests/test_data_move_mvv.py) ─────────────────────────────────


def patch_master(xml: str) -> str:
    """The master changelog with the MVV include placed just before the service grants (the position a real
    registering changeset takes: after the last schema change, before the runAlways grants)."""
    if MVV_RESOURCE in xml:
        raise MvvError("the master changelog already includes the MVV changeset")
    if GRANTS_ANCHOR not in xml:
        raise MvvError(f"the master changelog has no grants-nexus-svc include to place the MVV changeset before")
    return xml.replace(GRANTS_ANCHOR, f'<include file="{MVV_RESOURCE}"/>\n    {GRANTS_ANCHOR}', 1)


def make_mvv_jar(src: Path, dst: Path, changeset: Path = CHANGESET) -> None:
    """``dst`` is ``src`` with the test changeset added and the master changelog patched; every other entry is
    copied as it was."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        names = zin.namelist()
        if MASTER_ENTRY not in names:
            raise MvvError(f"{src} has no {MASTER_ENTRY}")
        if MVV_RESOURCE in names:
            raise MvvError(f"{src} already carries {MVV_RESOURCE}")
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == MASTER_ENTRY:
                data = patch_master(data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)
        zout.writestr(MVV_RESOURCE, changeset.read_bytes())


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    if not values:
        raise MvvError("a percentile of no samples")
    xs = sorted(values)
    rank = max(1, math.ceil(p / 100.0 * len(xs)))
    return xs[rank - 1]


def choose_sleep(rows: list[dict], baseline_p99_ms: float, bound: float) -> dict:
    """The smallest sleep_ms whose p99 during the move is within ``bound`` (0.20 = 20%) of the no-move baseline.
    When none is, the one with the lowest p99, flagged ``within_bound: False``."""
    limit = baseline_p99_ms * (1.0 + bound)
    ok = sorted((r for r in rows if r["p99_ms"] <= limit), key=lambda r: r["sleep_ms"])
    if ok:
        return {**ok[0], "within_bound": True, "limit_ms": round(limit, 2)}
    best = min(rows, key=lambda r: r["p99_ms"])
    return {**best, "within_bound": False, "limit_ms": round(limit, 2)}


def resume_verdict(*, done_at_kill: int, done_after_restart_first_seen: int, total: int, rows_total: int,
                   rows_once: int, rows_twice: int, rows_missing: int, completed: bool,
                   same_run_ids: bool = True) -> dict:
    """Did the move go on from the last committed chunk. A kill that fell before the first chunk or after the last
    proves nothing, a restart that began from zero or on a re-created run is a restart not a resume, and a row
    moved twice or not at all is a broken one."""
    why: list[str] = []
    if done_at_kill <= 0 or done_at_kill >= total:
        why.append(f"the kill was not mid-move ({done_at_kill} of {total} chunks done)")
    if done_after_restart_first_seen < done_at_kill:
        why.append(f"the move began again from the start ({done_after_restart_first_seen} chunks seen after the restart, "
                   f"{done_at_kill} done at the kill)")
    if not same_run_ids:
        why.append("the runs were archived and re-created, not resumed")
    if rows_twice:
        why.append(f"{rows_twice} row(s) moved twice")
    if rows_missing:
        why.append(f"{rows_missing} row(s) never moved")
    if rows_once + rows_twice + rows_missing != rows_total:
        why.append("the row counts do not add up")
    if not completed:
        why.append("the move did not complete")
    return {"ok": not why, "why": "; ".join(why) or "resumed from the last committed chunk, every row moved once"}


# ── the install ──────────────────────────────────────────────────────────────


def _ubw():
    import upgrade_boot_writes as ubw  # noqa: PLC0415 - heavy module, only the run needs it
    return ubw


class Engine:
    """The new engine on an old-state install: started through the supervisor the way a user's ``nx`` does."""

    def __init__(self, work: Path, jar: Path, sleep_ms: int | None = None) -> None:
        self.ubw = _ubw()
        self.work = work
        self.config = work / "config"
        self.jar = jar
        self.inst = self.ubw.Install(self.config)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("NX_", "NEXUS_"))}
        self.env.update({"NEXUS_CONFIG_DIR": str(self.config), "NX_LOCAL": "1", "NEXUS_SERVICE_JAR": str(jar)})
        if sleep_ms is not None:
            self.env["NX_DATA_MOVE_SLEEP_MS"] = str(sleep_ms)
        self.watch = None
        self.proc: subprocess.Popen | None = None
        self.marks: dict[str, float] = {}

    def start(self, label: str, max_wait: float = 900.0) -> dict:
        """Starts the engine and waits for /health 200; returns the timing marks (seconds from the start)."""
        ubw = self.ubw
        self.watch = ubw.LogWatch(self.config)
        t0 = time.monotonic()
        out = (self.config / f"mvv-start-{label}.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(["uv", "run", "--project", str(REPO), "nx", "daemon", "service", "start"],
                                     env=self.env, cwd=REPO, stdout=out, stderr=subprocess.STDOUT, text=True)
        marks: dict[str, float] = {}
        deadline = t0 + max_wait
        while True:
            if time.monotonic() > deadline:
                raise MvvError(f"{label}: /health never answered 200 within {max_wait:.0f}s")
            rows = self.inst.sql("select 1")
            if rows is not None:
                marks.setdefault("pg_up_s", round(time.monotonic() - t0, 1))
                port = self.inst.service_port()
                if port and "bind_s" not in marks and ubw.port_open(port):
                    marks["bind_s"] = round(time.monotonic() - t0, 1)
                if port and ubw.http_status(port, "/health")[0] == 200:
                    marks["ready_s"] = round(time.monotonic() - t0, 1)
                    break
            rc = self.proc.poll()
            if rc not in (None, 0) and not self.ubw.engine_alive(self.inst):
                raise MvvError(f"{label}: nx daemon service start exited {rc} and the engine is not up")
            time.sleep(0.5)
        self.marks = marks
        return marks

    def status(self) -> dict | None:
        port = self.inst.service_port()
        return self.ubw.http_status(port, "/v1/status")[1] if port else None

    def stop(self) -> None:
        self.ubw.stop_new(self.env)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                log("the start process did not exit within 120 s of the stop")

    def kill(self) -> tuple[int, int]:
        return self.ubw.kill_engine_and_supervisor(self.inst)

    def events(self, *needles: str) -> list[str]:
        return [ln.rstrip("\n") for ln in (self.watch.lines(*needles) if self.watch else [])]


def restore(work: Path, orig: Path) -> None:
    """``work`` as it was when ``orig`` was copied: stopped, same paths (pg_credentials names absolute ones)."""
    log(f"restoring {work} from {orig}")
    subprocess.run(["rsync", "-a", "--delete", f"{orig}/", f"{work}/"], check=True)


def sql(eng: Engine, query: str) -> list[list[str]]:
    rows = eng.inst.sql(query)
    if rows is None:
        raise MvvError("PostgreSQL does not accept connections")
    return rows


def move_row(eng: Engine) -> dict | None:
    rows = sql(eng, "select label, completed_at is not null, failed_at is not null, coalesce(failure, ''), "
                    "transient_retries, coalesce(paused_at::text, ''), chunk_size, sleep_ms "
                    f"from nexus.data_moves where label = '{MOVE_LABEL}'")
    if not rows:
        return None
    r = rows[0]
    return {"completed": r[1] == "t", "failed": r[2] == "t", "failure": r[3], "retries": int(r[4]), "paused": bool(r[5]),
            "chunk_size": int(r[6]), "sleep_ms": int(r[7])}


def run_progress(eng: Engine) -> dict:
    rows = sql(eng, "select count(*), coalesce(sum(total_chunks), 0), coalesce(sum(total_chunks_completed), 0), "
                    "coalesce(string_agg(run_id::text, ',' order by run_id), ''), coalesce(min(sleep_ms), -1), "
                    "coalesce(max(sleep_ms), -1) from dml_utils_data.migration_run "
                    f"where label = '{MOVE_LABEL}' or starts_with(label, '{MOVE_LABEL}/')")
    n, total, done, ids, smin, smax = rows[0]
    return {"runs": int(n), "total_chunks": int(total), "done_chunks": int(done), "run_ids": ids,
            "run_sleep_ms": [int(smin), int(smax)]}


def row_counts(eng: Engine) -> dict:
    r = sql(eng, "select count(*), count(*) filter (where mvv_n = 1), count(*) filter (where mvv_n > 1), "
                 "count(*) filter (where mvv_n is null) from nexus.chunks")[0]
    return {"rows_total": int(r[0]), "rows_once": int(r[1]), "rows_twice": int(r[2]), "rows_missing": int(r[3])}


def moved_rows(eng: Engine) -> int:
    return int(sql(eng, "select count(*) from nexus.chunks where mvv_n is not null")[0][0])


def wait_move(eng: Engine, timeout: float, sample: list | None = None) -> dict:
    """Until the move completes or fails; samples /v1/status boot_work and the progress as it goes."""
    t0 = time.monotonic()
    while True:
        row = move_row(eng)
        if sample is not None:
            st = eng.status() or {}
            sample.append({"t": round(time.monotonic() - t0, 1), "boot_work": st.get("boot_work"),
                           **run_progress(eng), "moved_rows": moved_rows(eng)})
        if row and (row["completed"] or row["failed"]):
            return row
        if time.monotonic() - t0 > timeout:
            raise MvvError(f"the move did not finish within {timeout:.0f}s: {row}")
        time.sleep(1.0)


def library_state(eng: Engine) -> dict:
    ext = sql(eng, "select extname || ' ' || extversion from pg_extension where extname = 'pg_background'")
    roles = sql(eng, "select rolname from pg_roles where rolname like 'dml_utils%' or rolname like 'ddl_utils%' "
                     "or rolname like 'nexus_admin_%' order by 1")
    procs = sql(eng, "select n.nspname || '.' || p.proname from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                     "where n.nspname in ('dml_utils', 'ddl_utils') and p.proname in "
                     "('run_migration_chunks_committing', 'run_migration_chunks') order by 1")
    return {"pg_background": ext[0][0] if ext else None, "roles": [r[0] for r in roles],
            "library_entry_points": [r[0] for r in procs]}


def collect_events(eng: Engine) -> dict:
    keep = ("event=data_move_library_applied", "event=data_move_libraries_skipped", "event=log_masking_installed",
            "event=data_move_started", "event=data_move_completed", "event=data_move_failed", "event=data_move_blocked",
            "event=schema_migration", "event=data_move_boot_work")
    out: dict[str, list[str]] = {}
    for ln in eng.events("event=data_move") + eng.events("event=log_masking") + eng.events("event=schema_migration"):
        for k in keep:
            if k in ln:
                out.setdefault(k, [])
                if ln not in out[k] and len(out[k]) < 6:
                    out[k].append(ln[:400])
    return out


def wal_lsn(eng: Engine) -> int:
    return eng.ubw.lsn_bytes(sql(eng, "select pg_current_wal_lsn()::text")[0][0])


# ── scenarios ────────────────────────────────────────────────────────────────


def scenario_upgrade(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    """One boot on the old state with the move registered. The WAL phases come from the harness's own run; this
    records what the install looks like at each step."""
    ubw = eng.ubw
    before = None
    try:
        before = sql(eng, "select count(*) from pg_extension where extname = 'pg_background'")[0][0]
    except (MvvError, ubw.HarnessError):
        before = "pg down"  # the old state is stopped; PG comes up with the engine start
    base = eng.inst.checkpoint_lsn()
    t_start = time.monotonic()
    marks = eng.start("upgrade")
    ready_lsn = wal_lsn(eng)
    out: dict = {"marks": marks, "pg_background_before_boot": before, "boot_wal_bytes": ready_lsn - ubw.lsn_bytes(base)}
    out["libraries_at_ready"] = library_state(eng)
    samples: list = []
    row = wait_move(eng, a.move_timeout, samples)
    out["move"] = row
    out["move_seconds_after_ready"] = round(time.monotonic() - t_start - marks["ready_s"], 1)
    out["progress_samples"] = samples[:: max(1, len(samples) // 25)]
    out["boot_work_seen_in_status"] = any(s.get("boot_work") for s in samples)
    out["row_counts"] = row_counts(eng)
    out["libraries_after"] = library_state(eng)
    out["events"] = collect_events(eng)
    out["wal_after_move_bytes"] = wal_lsn(eng) - ubw.lsn_bytes(base)
    ok = (row["completed"] and out["boot_work_seen_in_status"] and out["row_counts"]["rows_missing"] == 0
          and out["row_counts"]["rows_twice"] == 0)
    out["checks"] = {"move_completed": row["completed"], "progress_in_v1_status": out["boot_work_seen_in_status"],
                     "every_row_moved_once": out["row_counts"]["rows_missing"] == 0 and out["row_counts"]["rows_twice"] == 0,
                     "libraries_applied_lines": len(out["events"].get("event=data_move_library_applied", []))}
    return out, ok


def scenario_kill_resume(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    ubw = eng.ubw
    marks = eng.start("first")
    out: dict = {"marks_first": marks}
    total_rows = row_counts(eng)["rows_total"]
    deadline = time.monotonic() + a.move_timeout
    while True:
        moved = moved_rows(eng)
        if moved >= total_rows * a.kill_at:
            break
        if (move_row(eng) or {}).get("completed") or time.monotonic() > deadline:
            raise MvvError(f"the move finished or stalled before the kill point ({moved} of {total_rows} rows)")
        time.sleep(0.2)
    engine, supervisor = eng.kill()
    out["killed"] = {"engine": engine, "supervisor": supervisor, "moved_rows_at_kill": moved,
                     "progress_at_kill": run_progress(eng)}
    time.sleep(a.settle_s)  # a pg_background worker mid-chunk may finish or die with its coordinator
    before = run_progress(eng)
    out["after_kill_settled"] = {**before, "moved_rows": moved_rows(eng), "move": move_row(eng)}
    # the second boot: counted from the moment of the restart; the registry and the run tables hold the state
    eng.proc = None
    marks2 = eng.start("second")
    out["marks_second"] = marks2
    first_seen = run_progress(eng)
    out["first_seen_after_restart"] = first_seen
    row = wait_move(eng, a.move_timeout)
    final = run_progress(eng)
    counts = row_counts(eng)
    out |= {"move": row, "final_progress": final, "row_counts": counts, "events": collect_events(eng)}
    verdict = resume_verdict(done_at_kill=before["done_chunks"], done_after_restart_first_seen=first_seen["done_chunks"],
                             total=final["total_chunks"] or 1, rows_total=counts["rows_total"], rows_once=counts["rows_once"],
                             rows_twice=counts["rows_twice"], rows_missing=counts["rows_missing"],
                             completed=row["completed"], same_run_ids=before["run_ids"] == "" or
                             set(before["run_ids"].split(",")) <= set(final["run_ids"].split(",")))
    out["verdict"] = verdict
    return out, verdict["ok"]


def scenario_no_pgbg(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    """A bundle that cannot create pg_background: its control and script files are moved aside. That is a PG
    without the extension, the cloud's posture under Sam's 8.4 ruling."""
    ext_dir = eng.inst.bundle / "share" / "postgresql" / "extension"
    if not ext_dir.is_dir():
        found = list(eng.inst.bundle.rglob("pg_background.control"))
        if not found:
            raise MvvError("the bundle holds no pg_background.control to remove")
        ext_dir = found[0].parent
    aside = eng.work / "pg_background-aside"
    aside.mkdir(exist_ok=True)
    moved = []
    for p in sorted(ext_dir.glob("pg_background*")):
        shutil.move(str(p), str(aside / p.name))
        moved.append(p.name)
    if not moved:
        raise MvvError(f"no pg_background files under {ext_dir}: the scenario would test nothing")
    marks = eng.start("no-pgbg")
    st = eng.ubw.http_status(eng.inst.service_port(), "/health")
    out = {"files_moved_aside": moved, "marks": marks, "health": st[0], "libraries": library_state(eng),
           "events": collect_events(eng)}
    skipped = [ln for ln in eng.events("event=data_move_libraries_skipped")]
    walked = [ln for ln in eng.events("event=schema_migration")]
    out["skip_lines"] = skipped[:3]
    out["schema_walk_lines"] = [ln[:300] for ln in walked[-3:]]
    out["checks"] = {"health_200": st[0] == 200,
                     "skip_line_reason": any('reason="pg_background is not installed"' in ln for ln in skipped),
                     "schema_walk_completed": any("complete" in ln or "done" in ln or "applied" in ln for ln in walked),
                     "no_pg_background_extension": out["libraries"]["pg_background"] is None}
    return out, all(out["checks"].values())


def scenario_masking(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    eng.start("masking")
    row = wait_move(eng, a.move_timeout)
    pw = eng.inst.creds["NX_DB_ADMIN_PASS"]
    # A move whose chunk error carries the admin password: PostgreSQL echoes the literal it cannot cast.
    sql(eng, "insert into nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, sleep_ms, "
             "statement_timeout_ms, verification, changeset_id) values ('mvv-poison', "
             f"'UPDATE <driving_table> SET mvv_n = ''{pw}''::int WHERE <chunking_clause>', 'nexus', 'chunks', 1000, 0, "
             "60000, 'SELECT 1 FROM nexus.chunks t WHERE false AND (t.tenant_id = $1 OR $1 IS NULL)', 'mvv-poison')")
    deadline = time.monotonic() + 180
    carried: list[str] = []
    while time.monotonic() < deadline and not carried:
        time.sleep(3)
        carried = [ln for ln in eng.events("<redacted>") if "mvv-poison" in ln or "invalid input syntax" in ln]
    raw = eng.events(pw)
    pg_lines = []
    for p in sorted((eng.inst.pgdata / "log").glob("*")) if (eng.inst.pgdata / "log").is_dir() else []:
        try:
            pg_lines += [ln for ln in p.read_text(errors="replace").splitlines() if pw in ln]
        except OSError:
            pass
    installed = eng.events("event=log_masking_installed")
    masking = [int(m.group(1)) for ln in installed if (m := re.search(r"masking=(\d+)", ln))]
    out = {"first_move": row, "install_lines": installed[:2], "masked_lines": [ln[:300] for ln in carried[:3]],
           "engine_log_lines_with_the_password": len(raw), "pg_server_log_lines_with_the_password": len(pg_lines),
           "checks": {"masking_installed_ge_1": bool(masking) and max(masking) >= 1,
                      "a_masked_line_with_redacted": bool(carried),
                      "no_engine_log_line_carries_the_password": not raw}}
    # the line existed (PostgreSQL logged the raw text) or the proof says nothing about the encoder
    out["note"] = ("the PostgreSQL server log carries the password in those lines" if pg_lines else
                   "the PostgreSQL server log does not hold the password either (log_min_error_statement), so the "
                   "masked line shows the engine wrote <redacted> where the driver's message held the literal")
    return out, all(out["checks"].values())


# ── the load ─────────────────────────────────────────────────────────────────

_WORDS = ("index vector query cluster partition replica schema tenant manifest chunk catalog document collection "
          "embedding search rank filter merge segment shard ledger commit boundary migration rebuild planner").split()


class Load:
    """A steady open-loop search load: ``rate`` requests a second from ``threads`` workers through the client's own
    search path, each request's start time and latency recorded."""

    def __init__(self, eng: Engine, collections: list[str], rate: float, threads: int) -> None:
        self.eng, self.collections, self.rate, self.threads = eng, collections, rate, threads
        self.samples: list[tuple[float, float]] = []   # (monotonic start, latency ms)
        self.errors = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    def _worker(self, i: int, client) -> None:
        import random  # noqa: PLC0415
        rng = random.Random(20261117 + i)
        period = self.threads / self.rate
        nxt = time.monotonic() + i * period / self.threads
        while not self._stop.is_set():
            now = time.monotonic()
            if now < nxt:
                self._stop.wait(nxt - now)
                continue
            q = " ".join(rng.choice(_WORDS) for _ in range(4))
            t = time.monotonic()
            try:
                client.search(q, [rng.choice(self.collections)], n_results=10, sensitivity="include")
                ms = (time.monotonic() - t) * 1000.0
                with self._lock:
                    self.samples.append((t, ms))
            except Exception as exc:  # noqa: BLE001 - counted, never hidden
                with self._lock:
                    self.errors += 1
                log(f"search error: {type(exc).__name__}: {str(exc)[:160]}")
            nxt += period

    def start(self) -> None:
        os.environ.update({"NEXUS_CONFIG_DIR": str(self.eng.config), "NX_LOCAL": "1"})
        from nexus.db import make_t3  # noqa: PLC0415
        client = make_t3()
        for i in range(self.threads):
            th = threading.Thread(target=self._worker, args=(i, client), daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._stop.set()
        for th in self._threads:
            th.join(timeout=30)

    def window(self, t_from: float, t_to: float) -> dict:
        with self._lock:
            xs = [ms for t, ms in self.samples if t_from <= t < t_to]
        if len(xs) < 50:
            raise MvvError(f"only {len(xs)} requests in a {t_to - t_from:.0f}s window: too few for a p99")
        return {"requests": len(xs), "seconds": round(t_to - t_from, 1), "p50_ms": round(percentile(xs, 50), 2),
                "p95_ms": round(percentile(xs, 95), 2), "p99_ms": round(percentile(xs, 99), 2),
                "max_ms": round(max(xs), 2), "mean_ms": round(statistics.fmean(xs), 2)}


def wait_quiet(eng: Engine, timeout: float = 900.0, window: float = 30.0, wal_bytes: int = 1 << 20) -> None:
    """Until the cluster has written under ``wal_bytes`` over ``window`` s with no index build or vacuum running."""
    t0 = time.monotonic()
    prev = None
    while True:
        busy = int(sql(eng, "select (select count(*) from pg_stat_progress_create_index) + "
                            "(select count(*) from pg_stat_progress_vacuum)")[0][0])
        lsn = wal_lsn(eng)
        now = time.monotonic()
        if prev and now - prev[0] >= window:
            if busy == 0 and lsn - prev[1] < wal_bytes:
                return
            prev = (now, lsn)
        elif prev is None:
            prev = (now, lsn)
        if now - t0 > timeout:
            raise MvvError("the cluster never went quiet before the baseline window")
        time.sleep(5)


def scenario_load(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    ubw = eng.ubw
    # Pause the move at once: the registry row exists from the migration, the worker starts after the listener.
    marks: dict = {}
    eng.start("load")
    sql(eng, f"update nexus.data_moves set paused_at = now(), paused_by = 'mvv-load' where label = '{MOVE_LABEL}'")
    moved_at_pause = moved_rows(eng)
    cols = [r[0] for r in sql(eng, "select name from nexus.catalog_collections where name like 'knowledge\\_\\_bootw-%' "
                                   "and name not like '%control%' order by 1")]
    if not cols:
        raise MvvError("no seeded collections to search")
    wait_quiet(eng)
    load = Load(eng, cols, a.rate, a.threads)
    load.start()
    time.sleep(a.warmup_s)
    t_b0, w_b0 = time.monotonic(), wal_lsn(eng)
    time.sleep(a.baseline_s)
    t_b1, w_b1 = time.monotonic(), wal_lsn(eng)
    base = load.window(t_b0, t_b1)
    base["wal_bytes_per_s"] = round((w_b1 - w_b0) / (t_b1 - t_b0))
    # unpause: the move runs under the same load
    sql(eng, f"update nexus.data_moves set paused_at = null, paused_by = null where label = '{MOVE_LABEL}'")
    t_m0, w_m0 = time.monotonic(), wal_lsn(eng)
    started = None
    t_end = None
    deadline = t_m0 + a.move_timeout
    while time.monotonic() < deadline:
        row = move_row(eng)
        prog = run_progress(eng)
        if started is None and prog["done_chunks"] > 0:
            started = time.monotonic()
        if row and (row["completed"] or row["failed"]):
            t_end, w_m1 = time.monotonic(), wal_lsn(eng)
            break
        time.sleep(1.0)
    else:
        load.stop()
        raise MvvError("the move did not finish under load")
    time.sleep(2)
    load.stop()
    if started is None:
        started = t_m0
    during = load.window(started, t_end)
    during["wal_bytes_per_s"] = round((w_m1 - w_m0) / max(t_end - t_m0, 1e-9))
    out = {"sleep_ms": a.sleep_ms, "chunk_size": row["chunk_size"], "applied_run_sleep_ms": run_progress(eng)["run_sleep_ms"],
           "rate": a.rate, "threads": a.threads, "collections_searched": len(cols), "moved_rows_at_pause": moved_at_pause,
           "baseline": base, "during_move": during, "search_errors": load.errors,
           "move_seconds": round(t_end - t_m0, 1), "move": row, "row_counts": row_counts(eng),
           "p99_ratio": round(during["p99_ms"] / base["p99_ms"], 3),
           "wal_ratio": round(during["wal_bytes_per_s"] / max(base["wal_bytes_per_s"], 1), 1)}
    ok = row["completed"] and load.errors == 0 and out["row_counts"]["rows_missing"] == 0
    return out, ok


SCENARIOS = {"upgrade": scenario_upgrade, "kill-resume": scenario_kill_resume, "load": scenario_load,
             "no-pgbg": scenario_no_pgbg, "masking": scenario_masking}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    mj = sub.add_parser("make-jar", help="a gate jar plus the test-only move")
    mj.add_argument("src", type=Path)
    mj.add_argument("dst", type=Path)
    sc = sub.add_parser("scenario")
    sc.add_argument("name", choices=sorted(SCENARIOS))
    sc.add_argument("--work", type=Path, required=True)
    sc.add_argument("--orig", type=Path, required=True)
    sc.add_argument("--jar", type=Path, required=True)
    sc.add_argument("--sleep-ms", type=int, default=None, help="NX_DATA_MOVE_SLEEP_MS for the engine (default: the registered 0)")
    sc.add_argument("--kill-at", type=float, default=0.4, help="kill-resume: fraction of rows moved at the kill")
    sc.add_argument("--settle-s", type=float, default=5.0)
    sc.add_argument("--move-timeout", type=float, default=1800.0)
    sc.add_argument("--rate", type=float, default=20.0, help="load: requests a second")
    sc.add_argument("--threads", type=int, default=4)
    sc.add_argument("--warmup-s", type=float, default=20.0)
    sc.add_argument("--baseline-s", type=float, default=90.0)
    sc.add_argument("--json", type=Path, default=None)
    a = ap.parse_args(argv)
    if a.cmd == "make-jar":
        make_mvv_jar(a.src, a.dst)
        log(f"wrote {a.dst}")
        return 0
    rc = 2
    out: dict = {"scenario": a.name, "jar": str(a.jar)}
    eng = None
    try:
        restore(a.work, a.orig)
        eng = Engine(a.work, a.jar, a.sleep_ms)
        res, ok = SCENARIOS[a.name](eng, a)
        out |= res
        out["ok"] = ok
        rc = 0 if ok else 1
    except Exception as exc:  # noqa: BLE001 - a failure to measure is exit 2, never a green
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["ok"] = False
        rc = 2
    finally:
        if eng is not None:
            try:
                eng.stop()
            except Exception as exc:  # noqa: BLE001
                out["stop_error"] = f"{type(exc).__name__}: {exc}"
            try:
                if (eng.inst.pgdata / "postmaster.pid").exists():
                    eng.inst.pg_ctl("stop", "-m", "fast", "-w")
            except Exception:  # noqa: BLE001
                pass
        text = json.dumps(out, indent=1, default=str)
        sys.stdout.write(text + "\n")
        if a.json:
            a.json.write_text(text + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
