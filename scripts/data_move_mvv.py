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
    ``crash-resume`` stop PostgreSQL itself with ``pg_ctl stop -m immediate`` while a chunk's pg_background worker is running (the
                     engine and supervisor are killed right after), recover it, and check the aborted chunk rolled
                     back (moved rows = committed chunks x chunk size), then let the move finish: every row once.
    ``masking``      after the move, register a move that errors with the admin password in its message. Positive
                     control first: PostgreSQL's own log (``<pgdata>/pg.log``) must carry the password; then the
                     engine log must carry ``<redacted>`` and never the password, and nexus.data_moves.failure may not
                     carry it; the dml_utils run tables are scanned and the counts reported.

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


def pg_log_files(pgdata: Path) -> list[Path]:
    """The PostgreSQL server log files of an install. The daemon starts PG with ``pg_ctl -l <pgdata>/pg.log``
    (``nexus.db.pg_provision``), so the log is ``pg.log``, not a ``log/`` directory; a collector directory is read
    too when a cluster has one."""
    out = [pgdata / "pg.log"] if (pgdata / "pg.log").is_file() else []
    for d in ("log", "pg_log"):
        if (pgdata / d).is_dir():
            out += sorted(p for p in (pgdata / d).iterdir() if p.is_file())
    return out


def log_sizes(paths: list[Path]) -> dict[Path, int]:
    return {p: p.stat().st_size for p in paths if p.exists()}


def lines_with(paths: list[Path], needle: str, start: dict[Path, int]) -> list[str]:
    """Lines containing ``needle`` that each file gained after its ``start`` offset (0 for a file not in ``start``)."""
    out: list[str] = []
    for p in paths:
        try:
            with p.open("rb") as f:
                f.seek(start.get(p, 0))
                out += [ln.rstrip("\n") for raw in f if needle in (ln := raw.decode("utf-8", "replace"))]
        except OSError:
            continue
    return out


def masking_verdict(*, pg_log_lines: int, engine_log_lines: int, failure_column_hits: int, masking_installed: bool,
                    masked_line_seen: bool) -> dict:
    """The masking probe. ``pg_log_lines >= 1`` is the positive control: PostgreSQL logged the password, so an engine
    log without it shows the engine's output was masked, and a probe that read the wrong log (0 lines) is not a pass."""
    checks = {"control_pg_log_carries_the_password": pg_log_lines >= 1,
              "masking_installed_ge_1": masking_installed,
              "a_masked_line_with_redacted": masked_line_seen,
              "no_engine_log_line_carries_the_password": engine_log_lines == 0,
              "no_data_moves_failure_carries_the_password": failure_column_hits == 0}
    why = []
    if not checks["control_pg_log_carries_the_password"]:
        why.append("the control did not fire: PostgreSQL's log holds no line with the password, so the probe read "
                   "the wrong place or PostgreSQL did not log it, and 0 engine lines proves nothing")
    why += [k for k, v in checks.items() if not v and k != "control_pg_log_carries_the_password"]
    return {"ok": not why, "checks": checks, "why": "; ".join(why) or "the control fired and the engine's output was clean"}


def postmaster_pid(text: str) -> int:
    """The postmaster's pid, the first line of ``postmaster.pid``."""
    first = text.splitlines()[0].strip() if text.strip() else ""
    if not first.isdigit():
        raise MvvError(f"postmaster.pid does not start with a pid: {text[:60]!r}")
    return int(first)


def crash_verdict(*, in_flight_at_kill: int, done_before_kill: int, done_after_recovery: int, moved_after_recovery: int,
                  chunk_size: int, total_chunks: int, rows_total: int, rows_once: int, rows_twice: int, rows_missing: int,
                  completed: bool, recovery_logged: bool) -> dict:
    """Did a PostgreSQL crash in the middle of a chunk roll that chunk back, and did the move still finish once per row.
    ``moved_after_recovery`` is read right after crash recovery, before the engine runs again: it must equal the
    committed chunks x chunk_size (capped by the row count, the last chunk being partial)."""
    why: list[str] = []
    if in_flight_at_kill <= 0:
        why.append("no chunk was in flight at the kill, so a rollback was not exercised")
    if not recovery_logged:
        why.append("PostgreSQL's log shows no crash recovery after the restart")
    if done_after_recovery < done_before_kill:
        why.append(f"committed chunks went backwards ({done_before_kill} before the kill, {done_after_recovery} after recovery)")
    expected = min(done_after_recovery * chunk_size, rows_total)
    if moved_after_recovery != expected:
        why.append(f"{moved_after_recovery} rows moved after recovery but {done_after_recovery} committed chunks x "
                   f"{chunk_size} = {expected}: the aborted chunk did not roll back cleanly")
    if rows_twice:
        why.append(f"{rows_twice} row(s) moved twice")
    if rows_missing:
        why.append(f"{rows_missing} row(s) never moved")
    if rows_once + rows_twice + rows_missing != rows_total:
        why.append("the row counts do not add up")
    if not completed:
        why.append("the move did not complete")
    return {"ok": not why, "why": "; ".join(why) or
            "the aborted chunk rolled back, the move finished, every row moved once"}


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
        self.stop_rc: int | None = None

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
        """``nx daemon service stop --with-pg``, its output and exit status kept in ``mvv-stop.log``."""
        t = time.monotonic()
        r = subprocess.run(["uv", "run", "--project", str(REPO), "nx", "daemon", "service", "stop", "--with-pg"],
                           env=self.env, cwd=REPO, capture_output=True, text=True)
        with (self.config / "mvv-stop.log").open("a", encoding="utf-8") as f:
            f.write(f"rc={r.returncode} after {time.monotonic() - t:.1f}s\n{r.stdout}\n{r.stderr}\n")
        self.stop_rc = r.returncode
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


# The large seeded collections, by the seed's own documents (``nx-seed://<subject>/<n>``): a pre-8.5.6 client names
# a collection ``knowledge__<subject>__...``, a later one registers it under an engine-minted ``c<id>`` label, so
# the name says nothing. The control collection is left out either way.
SEEDED_COLLECTIONS_SQL = (
    "select distinct r.name from nexus.catalog_collections r "
    "left join nexus.catalog_documents d on d.tenant_id = r.tenant_id and d.physical_collection = r.name "
    "where (r.name like 'knowledge\\_\\_bootw-%' and r.name not like '%control%') "
    "or (d.source_uri like 'nx-seed://bootw-%' and d.source_uri not like 'nx-seed://bootw-control/%') "
    "order by 1")


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


def move_in_boot_work(boot_work: object, label: str) -> bool:
    """Whether /v1/status's boot_work names this move's own entry (``data-move:<label>``), not just any entry."""
    return f"data-move:{label}" in json.dumps(boot_work)


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
    # What the install held before this boot: PG is started on its own, read, and stopped again, so nothing but the
    # new client and engine have touched it when the boot below runs.
    eng.inst.pg_ctl("start", "-w", "-l", str(eng.work / "pre-boot-pg.log"))
    try:
        before = {"pg_background_extension": sql(eng, "select count(*) from pg_extension where extname = 'pg_background'")[0][0],
                  "library_roles": [r[0] for r in sql(eng, "select rolname from pg_roles where rolname like 'dml_utils%' "
                                                           "or rolname like 'ddl_utils%' order by 1")],
                  "library_schemas": [r[0] for r in sql(eng, "select nspname from pg_namespace where nspname in "
                                                             "('dml_utils', 'ddl_utils', 'dml_utils_data', 'liquibase') order by 1")],
                  "data_moves_table": sql(eng, "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                                               "where n.nspname = 'nexus' and c.relname = 'data_moves'")[0][0]}
    finally:
        eng.inst.pg_ctl("stop", "-m", "fast", "-w")
    base = eng.inst.checkpoint_lsn()
    t_start = time.monotonic()
    marks = eng.start("upgrade")
    ready_lsn = wal_lsn(eng)
    out: dict = {"marks": marks, "state_before_boot": before, "boot_wal_bytes": ready_lsn - ubw.lsn_bytes(base)}
    out["libraries_at_ready"] = library_state(eng)
    samples: list = []
    if move_row(eng) is None:
        # A jar with no move registered (the plain gate jar): the control for the boot timing, nothing to wait for.
        out["move"] = None
        out["events"] = collect_events(eng)
        out["libraries_after"] = library_state(eng)
        return out, True
    row = wait_move(eng, a.move_timeout, samples)
    out["move"] = row
    out["move_seconds_after_ready"] = round(time.monotonic() - t_start - marks["ready_s"], 1)
    out["progress_samples"] = samples[:: max(1, len(samples) // 25)]
    out["boot_work_seen_in_status"] = any(move_in_boot_work(s.get("boot_work"), MOVE_LABEL) for s in samples)
    out["row_counts"] = row_counts(eng)
    out["libraries_after"] = library_state(eng)
    out["events"] = collect_events(eng)
    out["wal_after_move_bytes"] = wal_lsn(eng) - ubw.lsn_bytes(base)
    if a.reboot:
        out["reboot"] = reboot_after(eng, ubw)
    ok = (row["completed"] and out["boot_work_seen_in_status"] and out["row_counts"]["rows_missing"] == 0
          and out["row_counts"]["rows_twice"] == 0)
    out["checks"] = {"move_completed": row["completed"], "progress_in_v1_status": out["boot_work_seen_in_status"],
                     "every_row_moved_once": out["row_counts"]["rows_missing"] == 0 and out["row_counts"]["rows_twice"] == 0,
                     "libraries_applied_lines": len(out["events"].get("event=data_move_library_applied", []))}
    return out, ok


def reboot_after(eng: Engine, ubw) -> dict:
    """Stop the engine and PG cleanly, wait until the cluster reads 'shut down' (a stop after a large move can
    take minutes to checkpoint), then boot again and count the boot's WAL: a same-version reboot with the move done."""
    eng.stop()
    stop_log = (eng.config / "mvv-stop.log").read_text(encoding="utf-8", errors="replace")[-1500:]
    stopped_by_us = False
    deadline = time.monotonic() + 120
    while True:
        try:
            base = eng.inst.checkpoint_lsn()
            break
        except ubw.HarnessError as exc:
            if time.monotonic() > deadline and not stopped_by_us:
                # nx daemon service stop --with-pg left PostgreSQL running: say so, stop it here, go on.
                log(f"PostgreSQL still up {120}s after nx daemon service stop --with-pg (rc {eng.stop_rc}): {exc}")
                eng.inst.pg_ctl("stop", "-m", "fast", "-w")
                stopped_by_us = True
            elif stopped_by_us:
                raise MvvError(f"the cluster is not 'shut down' after a fast stop: {exc}") from exc
            time.sleep(5)
    eng.proc = None
    marks = eng.start("reboot")
    wal = wal_lsn(eng) - ubw.lsn_bytes(base)
    row = move_row(eng)
    return {"marks": marks, "boot_wal_bytes": wal, "move": row, "stop_rc": eng.stop_rc, "stop_log_tail": stop_log,
            "pg_left_running_by_the_stop": stopped_by_us,
            "library_applied_lines": len(eng.events("event=data_move_library_applied"))}


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


def in_flight_snapshot(eng: Engine) -> dict:
    """One statement: the move's committed chunks and every chunk in flight, so the two numbers come from one snapshot.
    A chunk runs in a ``pg_background`` worker whose query is ``SELECT dml_utils_lib.process_migration_chunk(run,
    chunk, 'UPDATE nexus.chunks_... SET mvv_n ...')`` (seen live on chas, 2026-11-17), beside the engine's
    ``CALL dml_utils.run_migration_chunks_committing`` backend."""
    r = sql(eng, "select d.n, a.pid, a.backend_type, a.state, regexp_replace(left(a.query, 90), '\\s+', ' ', 'g'), a.age_s, "
                 "(select count(*) from pg_stat_activity where state = 'active' and pid <> pg_backend_pid() "
                 "and query ilike 'call %run_migration_chunks%') "
                 "from (select coalesce(sum(total_chunks_completed), 0) as n from dml_utils_data.migration_run "
                 f"where label = '{MOVE_LABEL}' or starts_with(label, '{MOVE_LABEL}/')) d "
                 "left join (select pid, backend_type, state, query, extract(epoch from now() - xact_start)::int as age_s "
                 "from pg_stat_activity where state = 'active' and backend_type = 'pg_background' "
                 "and query ilike '%process_migration_chunk%' and query ilike '%mvv_n%') a on true")
    # one row per active UPDATE (or one with null pid when none)
    updates = [{"pid": x[1], "backend_type": x[2], "state": x[3], "query": x[4], "xact_age_s": x[5]} for x in r if x[1]]
    return {"done_chunks": int(r[0][0]), "updates_in_flight": updates, "calls_in_flight": int(r[0][6])}


def scenario_crash_resume(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    """PostgreSQL itself is stopped with ``pg_ctl stop -m immediate`` (no checkpoint, no clean shutdown: the next
    start is crash recovery) while a chunk's pg_background worker is running its UPDATE. The engine and supervisor are killed straight
    after, so nothing else touches the cluster. PostgreSQL is started alone and read before the engine runs again:
    the moved rows must equal the committed chunks x chunk size, the aborted chunk having rolled back."""
    inst = eng.inst
    out: dict = {"marks_first": eng.start("first")}
    c = inst.creds
    pgdata = inst.pgdata
    total_rows = row_counts(eng)["rows_total"]
    # the postmaster the stop will signal: the pid and its command line, checked now, never found by a pattern
    pid_file = pgdata / "postmaster.pid"
    pm = postmaster_pid(pid_file.read_text(encoding="utf-8"))
    try:
        cmd = Path(f"/proc/{pm}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError as exc:
        raise MvvError(f"cannot read the postmaster's command line (pid {pm}): {exc}") from exc
    if str(pgdata) not in cmd:
        raise MvvError(f"pid {pm} does not name {pgdata}; not stopping it")
    out["postmaster"] = {"pid": pm, "cmdline": cmd[:200]}
    logs0 = pg_log_files(pgdata)
    pg_start = log_sizes(logs0)
    deadline = time.monotonic() + a.move_timeout
    snap: dict
    while True:
        if (move_row(eng) or {}).get("completed") or time.monotonic() > deadline:
            raise MvvError("the move finished or stalled before a chunk was caught in flight")
        snap = in_flight_snapshot(eng)
        # a chunk that has been running a while, so the transaction holds real uncommitted work to roll back
        aged = [u for u in snap["updates_in_flight"] if int(u["xact_age_s"] or 0) >= a.min_chunk_age_s]
        if snap["done_chunks"] * 1000 >= total_rows * a.kill_at and aged:
            break
        time.sleep(0.1)
    log(f"crash-resume: {snap['done_chunks']} chunks done, {len(snap['updates_in_flight'])} in flight: {snap['updates_in_flight']}")
    t_kill = time.monotonic()
    inst.pg_ctl("stop", "-m", "immediate", "-w")
    engine, supervisor = eng.kill()
    out["killed"] = {"snapshot": snap, "pg_stop": "immediate", "engine": engine, "supervisor": supervisor,
                     "seconds_from_snapshot_to_both_dead": round(time.monotonic() - t_kill, 2)}
    time.sleep(a.settle_s)
    if inst.sql("select 1") is not None:
        raise MvvError("PostgreSQL accepts connections after an immediate stop (something restarted it); the crash "
                       "recovery below would not be the one this scenario forced")
    # Crash recovery with only PostgreSQL up: read the cluster before the engine can move anything.
    t_rec = time.monotonic()
    subprocess.run([str(inst.bundle / "bin" / "pg_ctl"), "-D", str(pgdata), "start", "-w", "-t", "900", "-l",
                    str(pgdata / "pg.log"), "-o", f"-p {c['PG_PORT']}"], env=inst._bin_env(), check=True,  # noqa: SLF001
                   capture_output=True, text=True, timeout=1000)
    recovery_s = round(time.monotonic() - t_rec, 1)
    prog = run_progress(eng)
    moved = moved_rows(eng)
    rec_lines = lines_with(pg_log_files(pgdata), "automatic recovery in progress", pg_start)
    redo = lines_with(pg_log_files(pgdata), "redo", pg_start)
    log(f"crash-resume: recovered in {recovery_s}s, {prog['done_chunks']} chunks, {moved} rows, {len(rec_lines)} recovery line(s)")
    out["recovered"] = {"seconds": recovery_s, **prog, "moved_rows": moved, "move": move_row(eng),
                        "moved_rows_mod_chunk_size": moved % max(1, (move_row(eng) or {}).get("chunk_size", 1000)),
                        "recovery_log_lines": [ln[:200] for ln in rec_lines[:3]], "redo_log_lines": [ln[:200] for ln in redo[:4]]}
    # the second boot, PostgreSQL already running; the registry and the run tables hold the state
    eng.proc = None
    out["marks_second"] = eng.start("second")
    out["first_seen_after_restart"] = run_progress(eng)
    row = wait_move(eng, a.move_timeout)
    final = run_progress(eng)
    counts = row_counts(eng)
    out |= {"move": row, "final_progress": final, "row_counts": counts, "events": collect_events(eng)}
    verdict = crash_verdict(in_flight_at_kill=len(snap["updates_in_flight"]), done_before_kill=snap["done_chunks"],
                            done_after_recovery=prog["done_chunks"], moved_after_recovery=moved, chunk_size=row["chunk_size"],
                            total_chunks=final["total_chunks"] or 1, rows_total=counts["rows_total"],
                            rows_once=counts["rows_once"], rows_twice=counts["rows_twice"], rows_missing=counts["rows_missing"],
                            completed=row["completed"], recovery_logged=bool(rec_lines))
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
    port = eng.inst.service_port()
    if port is None:
        raise MvvError("no-pgbg: the engine answered /health during start but its address file is gone")
    st = eng.ubw.http_status(port, "/health")
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


_FAILURE_COLUMN = re.compile(r"fail|error|message|sqlstate|detail|reason", re.I)


def secret_column_hits(eng: Engine, secret: str) -> dict[str, int]:
    """Rows carrying ``secret`` in each text-like column of nexus.data_moves and every dml_utils_data table, keyed
    ``schema.table.column``. Only columns holding something (a hit) are listed."""
    lit = secret.replace("'", "''")
    cols = sql(eng, "select table_schema, table_name, column_name from information_schema.columns "
                    "where (table_schema = 'dml_utils_data' or (table_schema = 'nexus' and table_name = 'data_moves')) "
                    "and data_type in ('text', 'character varying', 'json', 'jsonb') order by 1, 2, 3")
    hits: dict[str, int] = {}
    for schema, table, col in cols:
        n = int(sql(eng, f'select count(*) from "{schema}"."{table}" where position(\'{lit}\' in "{col}"::text) > 0')[0][0])
        if n:
            hits[f"{schema}.{table}.{col}"] = n
    return hits


def scenario_masking(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    eng.start("masking")
    row = wait_move(eng, a.move_timeout)
    pw = eng.inst.creds["NX_DB_ADMIN_PASS"]
    pg_logs = pg_log_files(eng.inst.pgdata)
    if not pg_logs:
        raise MvvError(f"no PostgreSQL server log under {eng.inst.pgdata}: the positive control cannot be read")
    pg_start = log_sizes(pg_logs)
    # A move whose chunk error carries the admin password: PostgreSQL echoes the literal it cannot cast.
    sql(eng, "insert into nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, sleep_ms, "
             "statement_timeout_ms, verification, changeset_id) values ('mvv-poison', "
             f"'UPDATE <driving_table> SET mvv_n = ''{pw}''::int WHERE <chunking_clause>', 'nexus', 'chunks', 1000, 0, "
             "60000, 'SELECT 1 FROM nexus.chunks t WHERE false AND (t.tenant_id = $1 OR $1 IS NULL)', 'mvv-poison')")
    deadline = time.monotonic() + 180
    carried: list[str] = []
    recorded = False
    while time.monotonic() < deadline and not (carried and recorded):
        time.sleep(3)
        carried = [ln for ln in eng.events("<redacted>") if "mvv-poison" in ln or "invalid input syntax" in ln]
        recorded = bool(sql(eng, "select 1 from nexus.data_moves where label = 'mvv-poison' and failed_at is not null"))
    raw = eng.events(pw)
    pg_lines = lines_with(pg_logs, pw, pg_start)
    # the password is in the redacted text's place everywhere the engine wrote: scan what it stored, too
    column_hits = secret_column_hits(eng, pw)
    # nexus.data_moves.failure is the engine's own record and is gated. The dml_utils_data run tables belong to the
    # library, which stores PostgreSQL's raw error message: reported, with the count, not gated.
    failure_hits = column_hits.get("nexus.data_moves.failure", 0)
    library_error_hits = {k: n for k, n in column_hits.items()
                          if k.startswith("dml_utils_data.") and _FAILURE_COLUMN.search(k.rsplit(".", 1)[-1])}
    installed = eng.events("event=log_masking_installed")
    masking = [int(m.group(1)) for ln in installed if (m := re.search(r"masking=(\d+)", ln))]
    verdict = masking_verdict(pg_log_lines=len(pg_lines), engine_log_lines=len(raw), failure_column_hits=failure_hits,
                              masking_installed=bool(masking) and max(masking) >= 1, masked_line_seen=bool(carried))
    out = {"first_move": row, "install_lines": installed[:2], "masked_lines": [ln[:300] for ln in carried[:3]],
           "failure_recorded_in_data_moves": recorded,
           "pg_log_files_read": [str(p) for p in pg_logs],
           "pg_log_lines_with_the_password": len(pg_lines),
           "pg_log_sample": [ln.replace(pw, "<PASSWORD>")[:300] for ln in pg_lines[:3]],
           "engine_log_lines_with_the_password": len(raw),
           "stored_columns_with_the_password": column_hits, "data_moves_failure_hits": failure_hits,
           "library_error_columns_with_the_password": library_error_hits,
           "checks": verdict["checks"], "verdict": verdict,
           "note": "nexus.data_moves.template and dml_utils_data.migration_run.sql_text hold the password because the "
                   "probe's own INSERT put it in the template: that is the input, not an output of the engine. "
                   "dml_utils_data.migration_error.message is PostgreSQL's raw error text, kept by the library"}
    return out, verdict["ok"]


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
            what = sql(eng, "select coalesce(string_agg(x, '; '), '-') from ("
                            "select 'index ' || relid::regclass || ' ' || phase as x from pg_stat_progress_create_index "
                            "union all select 'vacuum ' || relid::regclass || ' ' || phase from pg_stat_progress_vacuum "
                            "union all select 'active ' || left(regexp_replace(query, '[[:space:]]+', ' ', 'g'), 120) "
                            "from pg_stat_activity where state = 'active' and pid <> pg_backend_pid()) s")[0][0]
            log(f"quiet check: busy={busy} wal={lsn - prev[1]} bytes in {now - prev[0]:.0f}s; {what}")
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
    cols = [r[0] for r in sql(eng, SEEDED_COLLECTIONS_SQL)]
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


SECOND_LABEL = MOVE_LABEL + "-load"


def _label_row(eng: Engine, label: str) -> dict | None:
    rows = sql(eng, "select completed_at is not null, failed_at is not null, coalesce(failure, ''), transient_retries "
                    f"from nexus.data_moves where label = '{label}'")
    if not rows:
        return None
    r = rows[0]
    return {"completed": r[0] == "t", "failed": r[1] == "t", "failure": r[2], "retries": int(r[3])}


def _label_done_chunks(eng: Engine, label: str) -> tuple[int, int]:
    r = sql(eng, "select coalesce(sum(total_chunks), 0), coalesce(sum(total_chunks_completed), 0) "
                 f"from dml_utils_data.migration_run where label = '{label}' or starts_with(label, '{label}/')")[0]
    return int(r[0]), int(r[1])


def scenario_load2(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    """Baseline and move in one warm boot. The upgrade boot's own move runs first and completes (a paused_at set
    after boot does not stop a CALL already in flight), the cluster goes quiet, the baseline window runs under load,
    then a second move with the same template is registered by hand and measured while it runs under the same load."""
    eng.start("load2")
    first = wait_move(eng, a.move_timeout)
    if not first["completed"]:
        raise MvvError(f"the boot move did not complete: {first}")
    cols = [r[0] for r in sql(eng, SEEDED_COLLECTIONS_SQL)]
    if not cols:
        raise MvvError("no seeded collections to search")
    t_q0 = time.monotonic()
    wait_quiet(eng, timeout=a.quiet_timeout)
    quiet_s = round(time.monotonic() - t_q0, 1)
    load = Load(eng, cols, a.rate, a.threads)
    load.start()
    time.sleep(a.warmup_s)
    t_b0, w_b0 = time.monotonic(), wal_lsn(eng)
    time.sleep(a.baseline_s)
    t_b1, w_b1 = time.monotonic(), wal_lsn(eng)
    base = load.window(t_b0, t_b1)
    base["wal_bytes_per_s"] = round((w_b1 - w_b0) / (t_b1 - t_b0))
    sql(eng, "insert into nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, sleep_ms, "
             "statement_timeout_ms, verification, on_complete, changeset_id) "
             f"select '{SECOND_LABEL}', template, driving_schema, driving_table, chunk_size, sleep_ms, statement_timeout_ms, "
             "'SELECT 1 FROM nexus.chunks t WHERE coalesce(t.mvv_n, 0) < 2 AND (t.tenant_id = $1 OR $1 IS NULL)', "
             f"on_complete, 'mvv-load-second' from nexus.data_moves where label = '{MOVE_LABEL}'")
    t_m0, w_m0 = time.monotonic(), wal_lsn(eng)
    started = t_end = None
    row = None
    deadline = t_m0 + a.move_timeout
    while time.monotonic() < deadline:
        row = _label_row(eng, SECOND_LABEL)
        total, done = _label_done_chunks(eng, SECOND_LABEL)
        if started is None and done > 0:
            started = time.monotonic()
        if row and (row["completed"] or row["failed"]):
            t_end, w_m1 = time.monotonic(), wal_lsn(eng)
            break
        time.sleep(0.5)
    else:
        load.stop()
        raise MvvError(f"the second move did not finish under load: {row}")
    # the tail: the autovacuum the move's updates set off, still under the same load, until the cluster is quiet
    t_v0, w_v0 = time.monotonic(), wal_lsn(eng)
    tail_quiet = True
    try:
        wait_quiet(eng, timeout=a.quiet_timeout, wal_bytes=int(a.quiet_wal_mb * (1 << 20)))
    except MvvError:
        tail_quiet = False
    t_v1, w_v1 = time.monotonic(), wal_lsn(eng)
    load.stop()
    if started is None:
        started = t_m0
    during = load.window(started, t_end)
    during["wal_bytes_per_s"] = round((w_m1 - w_m0) / max(t_end - t_m0, 1e-9))
    after = load.window(t_v0, t_v1) if t_v1 - t_v0 >= 10 else {}
    if after:
        after["wal_bytes_per_s"] = round((w_v1 - w_v0) / (t_v1 - t_v0))
        after["quiet_reached"] = tail_quiet
    rc = sql(eng, "select count(*), count(*) filter (where mvv_n = 2), count(*) filter (where coalesce(mvv_n, 0) < 2) "
                  "from nexus.chunks")[0]
    counts = {"rows_total": int(rc[0]), "rows_at_2": int(rc[1]), "rows_short": int(rc[2])}
    sl = sql(eng, "select coalesce(min(sleep_ms), -1), coalesce(max(sleep_ms), -1), coalesce(sum(total_chunks), 0) "
                  f"from dml_utils_data.migration_run where label = '{SECOND_LABEL}' or starts_with(label, '{SECOND_LABEL}/')")[0]
    out = {"sleep_ms": a.sleep_ms, "applied_run_sleep_ms": [int(sl[0]), int(sl[1])], "chunks": int(sl[2]),
           "rate": a.rate, "threads": a.threads, "collections_searched": len(cols), "quiet_wait_s": quiet_s,
           "baseline": base, "during_move": during, "after_move_until_quiet": after, "search_errors": load.errors,
           "move_seconds": round(t_end - t_m0, 1), "move_work_seconds": round(t_end - started, 1),
           "move": row, "row_counts": counts,
           "p99_ratio": round(during["p99_ms"] / base["p99_ms"], 3),
           "p50_ratio": round(during["p50_ms"] / base["p50_ms"], 3),
           "wal_ratio": round(during["wal_bytes_per_s"] / max(base["wal_bytes_per_s"], 1), 1),
           "after_p99_ratio": round(after["p99_ms"] / base["p99_ms"], 3) if after else None}
    ok = bool(row["completed"]) and load.errors == 0 and counts["rows_short"] == 0
    return out, ok



def wait_settled(eng: Engine, timeout: float, wal_bytes: int) -> dict:
    """Quiet, and nothing left for autovacuum to start: every chunk leaf's dead tuples are under its autovacuum trigger
    (threshold + scale_factor * live, from the leaf's reloptions or the server defaults) and no vacuum runs. A quiet
    window alone is not enough, because autovacuum looks only once a naptime and can start right after it."""
    t0 = time.monotonic()
    while True:
        rows = sql(eng, "select s.relid::regclass::text, s.n_dead_tup, s.n_live_tup, "
                        "coalesce((select split_part(o, '=', 2) from unnest(c.reloptions) o "
                        "where o like 'autovacuum_vacuum_threshold=%'), current_setting('autovacuum_vacuum_threshold')), "
                        "coalesce((select split_part(o, '=', 2) from unnest(c.reloptions) o "
                        "where o like 'autovacuum_vacuum_scale_factor=%'), current_setting('autovacuum_vacuum_scale_factor')) "
                        "from pg_stat_user_tables s join pg_class c on c.oid = s.relid "
                        "where s.relid in (select inhrelid from pg_inherits where inhparent in "
                        "(select inhrelid from pg_inherits where inhparent = 'nexus.chunks'::regclass))")
        over = [(r[0], int(r[1])) for r in rows if int(r[1]) > float(r[3]) + float(r[4]) * int(r[2])]
        running = int(sql(eng, "select count(*) from pg_stat_progress_vacuum")[0][0])
        if not over and running == 0:
            wait_quiet(eng, timeout=max(timeout - (time.monotonic() - t0), 60.0), wal_bytes=wal_bytes)
            return {"settle_s": round(time.monotonic() - t0, 1)}
        if time.monotonic() - t0 > timeout:
            raise MvvError(f"autovacuum never caught up: over trigger {over}, vacuums running {running}")
        log(f"settling: over trigger {over}, vacuums running {running}")
        time.sleep(15)


def _leaf_upd(eng: Engine) -> tuple[int, int]:
    """n_tup_upd and n_tup_hot_upd summed over the leaf partitions of nexus.chunks."""
    r = sql(eng, "select coalesce(sum(s.n_tup_upd), 0), coalesce(sum(s.n_tup_hot_upd), 0) from pg_stat_user_tables s "
                 "where s.relid in (select inhrelid from pg_inherits where inhparent in "
                 "(select inhrelid from pg_inherits where inhparent = 'nexus.chunks'::regclass) "
                 "union select inhrelid from pg_inherits where inhparent = 'nexus.chunks'::regclass)")[0]
    return int(r[0]), int(r[1])


def _stop_move(eng: Engine, label: str) -> dict:
    """Pause a move and cancel its in-flight chunk (a pause alone waits for the run's end). Returns what was cancelled."""
    sql(eng, f"update nexus.data_moves set paused_at = now(), paused_by = 'mvv-sweep' where label = '{label}'")
    pids = [r[0] for r in sql(eng, "select pid from pg_stat_activity where pid <> pg_backend_pid() and state = 'active' and "
                                   "(query like '%run_migration_chunks_committing%' or query like '%process_migration_chunk%')")]
    for pid in pids:
        sql(eng, f"select pg_cancel_backend({int(pid)})")
    time.sleep(10)
    _, d0 = _label_done_chunks(eng, label)
    time.sleep(20)
    _, d1 = _label_done_chunks(eng, label)
    return {"cancelled_pids": len(pids), "chunks_after_stop": d1 - d0}


def scenario_sweep(eng: Engine, a: argparse.Namespace) -> tuple[dict, bool]:
    """One warm boot, then per (chunk_size, sleep_ms) pair: a fresh baseline after quiet, a move registered with that
    pair in its INSERT (no NX_DATA_MOVE_SLEEP_MS override), a window of a.window_s under the same steady load (or the
    whole move when it ends sooner), the move paused and its chunk cancelled, then the tail until quiet."""
    if a.sleep_ms is not None:
        raise MvvError("sweep sets sleep_ms per move in the registry; leave --sleep-ms unset")
    pairs = [tuple(int(x) for x in s.split(":")) for s in a.pairs.split(",")]
    eng.start("sweep")
    first = wait_move(eng, a.move_timeout)
    if not first["completed"]:
        raise MvvError(f"the boot move did not complete: {first}")
    upd, hot = _leaf_upd(eng)
    cols = [r[0] for r in sql(eng, SEEDED_COLLECTIONS_SQL)]
    if not cols:
        raise MvvError("no seeded collections to search")
    out: dict = {"rate": a.rate, "threads": a.threads, "collections_searched": len(cols), "window_s": a.window_s,
                 "boot_move_leaf_updates": {"n_tup_upd": upd, "n_tup_hot_upd": hot}, "pairs": []}
    t_q0 = time.monotonic()
    wait_settled(eng, a.quiet_timeout, 1 << 20)
    out["boot_quiet_wait_s"] = round(time.monotonic() - t_q0, 1)
    load = Load(eng, cols, a.rate, a.threads)
    load.start()
    time.sleep(a.warmup_s)
    ok = True
    try:
        for i, (cs, sl) in enumerate(pairs):
            label = f"{MOVE_LABEL}-c{cs}-s{sl}"
            res: dict = {"chunk_size": cs, "sleep_ms": sl, "label": label}
            out["pairs"].append(res)
            try:
                t_q = time.monotonic()
                res["settle"] = wait_settled(eng, a.quiet_timeout, int(a.quiet_wal_mb * (1 << 20)))
                res["quiet_wait_s"] = round(time.monotonic() - t_q, 1)
                t_b0, w_b0 = time.monotonic(), wal_lsn(eng)
                time.sleep(a.baseline_s)
                t_b1, w_b1 = time.monotonic(), wal_lsn(eng)
                base = load.window(t_b0, t_b1)
                base["wal_bytes_per_s"] = round((w_b1 - w_b0) / (t_b1 - t_b0))
                u0, h0 = _leaf_upd(eng)
                sql(eng, "insert into nexus.data_moves (label, template, driving_schema, driving_table, chunk_size, sleep_ms, "
                         "statement_timeout_ms, verification, on_complete, changeset_id) "
                         f"select '{label}', template, driving_schema, driving_table, {cs}, {sl}, statement_timeout_ms, "
                         f"'SELECT 1 FROM nexus.chunks t WHERE coalesce(t.mvv_n, 0) < {i + 3} AND (t.tenant_id = $1 OR $1 IS NULL)', "
                         f"on_complete, 'mvv-sweep-{i}' from nexus.data_moves where label = '{MOVE_LABEL}'")
                t_m0, w_m0 = time.monotonic(), wal_lsn(eng)
                started = None
                d_start = 0
                completed = False
                while True:
                    now = time.monotonic()
                    total, done = _label_done_chunks(eng, label)
                    if started is None and done > 0:
                        started, d_start = now, done
                    row = _label_row(eng, label)
                    if row and (row["completed"] or row["failed"]):
                        completed = bool(row["completed"])
                        res["failed"] = bool(row["failed"])
                        break
                    if started is not None and now - started >= a.window_s:
                        break
                    if now - t_m0 > a.window_s + 600:
                        raise MvvError(f"no chunk of {label} completed within {a.window_s + 600:.0f}s")
                    time.sleep(0.5)
                t_m1, w_m1 = time.monotonic(), wal_lsn(eng)
                total, done = _label_done_chunks(eng, label)
                u1, h1 = _leaf_upd(eng)
                res["stop"] = {} if completed else _stop_move(eng, label)
                during = load.window(started or t_m0, t_m1)
                during["wal_bytes_per_s"] = round((w_m1 - w_m0) / (t_m1 - t_m0))
                window_chunks = done - d_start
                res |= {"baseline": base, "during_move": during, "completed": completed, "total_chunks": total,
                        "chunks_done": done, "window_chunks": window_chunks,
                        "seconds_per_chunk": round((t_m1 - (started or t_m0)) / window_chunks, 2) if window_chunks else None,
                        "leaf_updates": {"n_tup_upd": u1 - u0, "n_tup_hot_upd": h1 - h0},
                        "p99_ratio": round(during["p99_ms"] / base["p99_ms"], 3),
                        "p50_ratio": round(during["p50_ms"] / base["p50_ms"], 3),
                        "wal_ratio": round(during["wal_bytes_per_s"] / max(base["wal_bytes_per_s"], 1), 1)}
                t_v0, w_v0 = time.monotonic(), wal_lsn(eng)
                tail_quiet = True
                try:
                    wait_settled(eng, a.quiet_timeout, int(a.quiet_wal_mb * (1 << 20)))
                except MvvError:
                    tail_quiet = False
                t_v1, w_v1 = time.monotonic(), wal_lsn(eng)
                if t_v1 - t_v0 >= 10:
                    after = load.window(t_v0, t_v1)
                    after["wal_bytes_per_s"] = round((w_v1 - w_v0) / (t_v1 - t_v0))
                    after["quiet_reached"] = tail_quiet
                    res["after_move_until_quiet"] = after
                    res["after_p99_ratio"] = round(after["p99_ms"] / base["p99_ms"], 3)
                log(f"pair c{cs} s{sl}: p99 {base['p99_ms']} -> {during['p99_ms']} ({res['p99_ratio']}x), "
                    f"{window_chunks} chunks in window, {res['seconds_per_chunk']} s/chunk")
            except Exception as exc:  # noqa: BLE001 - one pair's failure is recorded and the sweep goes on
                res["error"] = f"{type(exc).__name__}: {exc}"
                ok = False
                log(f"pair c{cs} s{sl} failed: {res['error']}")
            if a.json:
                a.json.write_text(json.dumps(out, indent=1, default=str) + "\n", encoding="utf-8")
    finally:
        load.stop()
    out["search_errors"] = load.errors
    return out, ok and load.errors == 0


SCENARIOS = {"upgrade": scenario_upgrade, "kill-resume": scenario_kill_resume, "crash-resume": scenario_crash_resume, "load": scenario_load, "load2": scenario_load2, "sweep": scenario_sweep,
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
    sc.add_argument("--kill-at", type=float, default=0.4, help="kill-resume, crash-resume: fraction of rows moved at the kill")
    sc.add_argument("--min-chunk-age-s", type=float, default=3.0,
                    help="crash-resume: the in-flight chunk's transaction must be at least this old at the stop")
    sc.add_argument("--settle-s", type=float, default=5.0)
    sc.add_argument("--move-timeout", type=float, default=1800.0)
    sc.add_argument("--rate", type=float, default=20.0, help="load: requests a second")
    sc.add_argument("--threads", type=int, default=4)
    sc.add_argument("--warmup-s", type=float, default=20.0)
    sc.add_argument("--baseline-s", type=float, default=90.0)
    sc.add_argument("--reboot", action="store_true", help="upgrade: after the move, a clean stop and a second boot, counted")
    sc.add_argument("--pairs", default="1000:20000", help="sweep: chunk_size:sleep_ms pairs, comma separated")
    sc.add_argument("--window-s", type=float, default=900.0, help="sweep: seconds measured per pair, from its first chunk")
    sc.add_argument("--quiet-wal-mb", type=float, default=1.0, help="WAL MB per 30 s under which a wait under search load counts as quiet")
    sc.add_argument("--quiet-timeout", type=float, default=900.0, help="load2: seconds to wait for quiet before the baseline")
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
