#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CTRL_BREAK stop probe for the native Windows engine (RDR-224 P1.1, nexus-f9bgu.8).

The driver behind rows a to d of T2 ``nexus_rdr/224-p1.1-stop-probe``, committed
so they can be re-run whenever ``OrtInitGate``, ``Main`` or the native-image
configuration changes (critique S1, nexus-f9bgu.30). The release leg's smoke
(``engine_windows_smoke.py``) asserts only the serving stop (row d); the other
three phases need a stop at a chosen instant, which a smoke cannot give.

Each phase gets its own throwaway PG cluster under ``--run-dir`` (made with a
plain ``os.makedirs``: an owner-only-ACL directory makes ``initdb`` die
0xC0000135 under an elevated token), starts the engine with
CREATE_NEW_PROCESS_GROUP from this console, and sends CTRL_BREAK_EVENT to the
engine's group::

    a  a stop before the changelog lock is taken (at schema_migration_pending)
    b  a stop in the middle of a changeset (about half the changelog applied);
       the next boot is only OBSERVED, for a few seconds (the lock is left held)
    c  a stop at eight offsets around ONNX Runtime initialisation, with the
       ORT-init wait bound optionally shortened (--ort-wait-ms) so the
       post-timeout branch fires inside init (critique S2)
    d  a stop of a serving engine, then a second boot and a second stop

Every phase reports engine exit codes (149 = 128 + 21 is a clean CTRL_BREAK),
seconds to exit, the engine's own shutdown events, the changelog row counts and
the lock row, and whether Postgres logged a crash recovery. Results go to
``<run-dir>/results-<phase>.json`` and stdout, each ending with a ``verdict`` (PASS or FAIL
with the problems named). The exit status is 1 when any phase failed: it raised, its trigger did
not fire where it was meant to, no stop landed inside ORT init (phase c), an exit code was not 149,
a serving stop did not log its shutdown events, the second boot re-applied changesets, or a crash
artifact appeared (``phase_problems``).

Windows only (CTRL_BREAK_EVENT, taskkill), stdlib only. Run it from a session
that has a console. Phases a to d start and kill engines of their own; nothing
else on the machine is touched. Usage::

    python scripts/engine_windows_stop_probe.py --exe C:\\build\\x\\nexus-service.exe \\
        --pg-bin C:\\build\\x\\bundle\\bin --models C:\\build\\x\\onnx_models \\
        --changelog-dir service\\src\\main\\resources\\db --run-dir C:\\build\\x\\run  d

The pure parts (changeset counting, log reading, the exit-code summary) are
tested by tests/test_engine_windows_stop_probe.py.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

#: CREATE_NEW_PROCESS_GROUP: the engine gets its own group so CTRL_BREAK reaches only it.
NEW_PROCESS_GROUP = 0x200
#: 128 + SIGBREAK (21): what a clean CTRL_BREAK stop exits with.
CLEAN_STOP_EXIT_CODE = 149
PG_USER = "nxsuper"
DB_NAME = "nexus"
EVENT_KEYS: tuple[str, ...] = (
    "event=schema_migration", "event=shutdown", "event=service_stopped", "event=own_backends",
    "event=service_ready", "event=boot_aborted", "event=ort_init", "event=ort_run", "event=signal",
    "Exception", "change log lock", "Waiting for changelog lock", "event=onnx_model_root",
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_CHANGESET_RE = re.compile(r"<changeSet\b")
_NEW_CHANGESETS_RE = re.compile(r"\bnew_changesets=(\d+)")


# --------------------------------------------------------------------------- #
# Pure parts
# --------------------------------------------------------------------------- #


def count_changesets(changelog_dir: Path) -> int:
    """changeSet elements in every ``*.xml`` under *changelog_dir*, XML comments removed (a tag inside a comment is not a changeset)."""
    total = 0
    for root, _, files in os.walk(changelog_dir):
        for name in files:
            if name.endswith(".xml"):
                text = (Path(root) / name).read_text(encoding="utf-8", errors="replace")
                total += len(_CHANGESET_RE.findall(_COMMENT_RE.sub("", text)))
    return total


def event_lines(text: str, keys: Sequence[str] = EVENT_KEYS, limit: int = 18) -> list[str]:
    """The log lines that carry one of *keys*, each trimmed to what follows the logger name, first *limit*."""
    return [ln.split(" - ", 1)[-1][:200] for ln in text.splitlines() if any(k in ln for k in keys)][:limit]


def shutdown_flags(text: str) -> dict[str, object]:
    """What the engine's own log says about a stop."""
    return {
        "shutdown_signal_logged": "event=shutdown_signal" in text,
        "service_stopped_logged": "event=service_stopped" in text,
        "own_backends_terminated": [ln.split(" - ", 1)[-1][:160] for ln in text.splitlines() if "own_backends_terminated" in ln],
        "unavailable_warnings": [ln[-170:] for ln in text.splitlines() if "ort_init_signal_gate_unavailable" in ln],
    }


def new_changesets(text: str) -> int | None:
    """``new_changesets`` of the last schema_migration_complete line, or None when there is none."""
    found = _NEW_CHANGESETS_RE.findall(text)
    return int(found[-1]) if found else None


def stop_verdict(exit_code: object, *, expected: int = CLEAN_STOP_EXIT_CODE) -> str:
    """``clean`` for the CTRL_BREAK exit code, ``no-exit`` when the engine outlived the wait, else ``unexpected:<code>``."""
    if exit_code == expected:
        return "clean"
    if isinstance(exit_code, str) or exit_code is None:
        return "no-exit"
    return f"unexpected:{exit_code}"


def landed_in_init(text: str) -> bool:
    """True when the stop arrived while ONNX Runtime was initialising (the gate logged that it deferred the exit)."""
    return "event=ort_init_shutdown_wait " in text


def _get(d: object, *path: str) -> object:
    """``d[path[0]][path[1]]...`` through mappings; None when any step is missing or not a mapping."""
    for key in path:
        if not isinstance(d, Mapping):
            return None
        d = d.get(key)
    return d


def _stop_problems(label: str, exit_code: object) -> list[str]:
    verdict = stop_verdict(exit_code)
    return [] if verdict == "clean" else [f"{label}: stop was {verdict}, expected a clean exit {CLEAN_STOP_EXIT_CODE}"]


def phase_problems(phase: str, res: Mapping[str, object]) -> list[str]:
    """What a phase's result says is wrong. An empty list means the phase proved what it exists to prove.

    The probe used to record and exit 0 whatever happened. As the only recurring instrument for the
    stop-at-a-chosen-instant phases (a to c) it must fail on a phase error, a trigger that did not fire
    where it was meant to, no stop landing inside ORT init, a stop that was not a clean 149, a second
    boot that re-applied changesets, or a crash artifact."""
    if "error" in res:
        return [f"phase {phase} raised: {res['error']}"]
    problems: list[str] = []
    if phase == "a":
        seen = _get(res, "boot1_break_before_lock", "seen")
        if seen != "migration_pending":
            problems.append(f"a: the stop was to land before the changelog lock, the trigger saw {seen!r}")
        problems += _stop_problems("a boot 1", _get(res, "boot1_break_before_lock", "exit_code"))
        if _get(res, "boot2_plain", "seen") != "ready":
            problems.append(f"a: the boot after the early stop did not reach ready (saw {_get(res, 'boot2_plain', 'seen')!r})")
        problems += _stop_problems("a boot 2", _get(res, "boot2_plain", "stop_exit_code"))
    elif phase == "b":
        seen = _get(res, "boot1_break_mid_changeset", "seen")
        if not (isinstance(seen, str) and seen.startswith("changesets_running=")):
            problems.append(f"b: the stop was to land mid-changeset, the trigger saw {seen!r}")
        problems += _stop_problems("b boot 1", _get(res, "boot1_break_mid_changeset", "exit_code"))
    elif phase == "c":
        problems += _stop_problems("c warm boot", res.get("warm_stop_exit"))
        sweep = res.get("sweep")
        if not isinstance(sweep, list) or not sweep:
            problems.append("c: no sweep rows were recorded")
            sweep = []
        for i, row in enumerate(sweep):
            if not isinstance(row, Mapping):
                continue
            if row.get("seen") != "onnx_model_root":
                problems.append(f"c offset row {i}: the trigger saw {row.get('seen')!r}, not onnx_model_root (the stop was not placed)")
            problems += _stop_problems(f"c offset row {i}", row.get("exit_code"))
        landed = res.get("landed_count")
        if not isinstance(landed, int) or landed < 1:
            problems.append(f"c: no stop landed inside ORT init (landed_count={landed!r}); the sweep proved nothing about that window")
        problems += _stop_problems("c next boot", _get(res, "next_boot", "stop_exit_code"))
    elif phase == "d":
        problems += _stop_problems("d serving stop", _get(res, "serving_stop", "exit_code"))
        for flag in ("shutdown_signal_logged", "service_stopped_logged"):
            if _get(res, "serving_stop", flag) is not True:
                problems.append(f"d: the serving stop did not log {flag.removesuffix('_logged')}")
        problems += _stop_problems("d next boot", _get(res, "next_boot", "stop_exit_code"))
        if _get(res, "next_boot", "new_changesets") != 0:
            problems.append(f"d: the second boot applied changesets (new_changesets={_get(res, 'next_boot', 'new_changesets')!r}), expected 0")
    artifacts = res.get("crash_artifacts")
    if artifacts:
        problems.append(f"{phase}: crash artifacts found: {artifacts}")
    return problems


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass(frozen=True)
class Config:
    exe: Path
    pg_bin: Path
    models: Path
    changelog_dir: Path
    run_dir: Path
    ort_wait_ms: int | None = None
    prod_write_note: str = "engine_windows_stop_probe: throwaway PG"

    def pg(self, tool: str) -> str:
        return str(self.pg_bin / f"{tool}.exe")


# --------------------------------------------------------------------------- #
# Windows-only driver
# --------------------------------------------------------------------------- #


class Cluster:
    def __init__(self, cfg: Config, name: str) -> None:
        self.cfg = cfg
        self.dir = cfg.run_dir / name
        if self.dir.exists():
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.data = self.dir / "data"
        self.port = free_port()
        self.res: dict[str, object] = {}

    def run(self, args: Sequence[str], log: str) -> int:
        with (self.dir / log).open("w") as f:
            return subprocess.run(list(args), stdin=subprocess.DEVNULL, stdout=f, stderr=subprocess.STDOUT).returncode

    def pg_ctl(self, action: str, extra: Sequence[str] = ()) -> int:
        return self.run(
            [self.cfg.pg("pg_ctl"), "-D", str(self.data), "-l", str(self.dir / "pg.log"), *extra, "-w", action],
            f"pgctl-{action}.log",
        )

    def psql(self, db: str, sql: str) -> str:
        o = subprocess.run(
            [self.cfg.pg("psql"), "-h", "127.0.0.1", "-p", str(self.port), "-U", PG_USER, "-v", "ON_ERROR_STOP=1", "-qAt", "-d", db, "-c", sql],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        return (o.stdout + o.stderr).strip()[:300]

    def start(self) -> None:
        r = self.res
        listen = ["-o", f"-p {self.port} -c listen_addresses=127.0.0.1"]
        r["initdb"] = self.run([self.cfg.pg("initdb"), "-D", str(self.data), "--no-locale", "-E", "UTF8", "-U", PG_USER, "-A", "trust"], "initdb.log")
        r["pg_start"] = self.pg_ctl("start", listen)
        r["createdb"] = self.run(
            [self.cfg.pg("psql"), "-h", "127.0.0.1", "-p", str(self.port), "-U", PG_USER, "-qAt", "-d", "postgres", "-c", f"CREATE DATABASE {DB_NAME}"],
            "createdb.log",
        )
        r["ext"] = self.psql(DB_NAME, "CREATE EXTENSION vector; CREATE EXTENSION pg_trgm")
        if r["initdb"] != 0 or r["pg_start"] != 0:
            raise RuntimeError(f"cluster did not start: {r}")

    def stop(self) -> None:
        r = self.res
        listen = ["-o", f"-p {self.port} -c listen_addresses=127.0.0.1"]
        r["pg_stop"] = self.pg_ctl("stop", ["-m", "fast"])
        r["pg_restart"] = self.pg_ctl("start", listen)
        r["pg_stop2"] = self.pg_ctl("stop", ["-m", "fast"])
        log = (self.dir / "pg.log").read_text(errors="replace")
        r["pg_crash_recovery_logged"] = ("not properly shut down" in log) or ("automatic recovery" in log)

    def abort(self) -> None:
        if (self.data / "postmaster.pid").exists():
            try:
                subprocess.run([self.cfg.pg("pg_ctl"), "-D", str(self.data), "-m", "immediate", "-w", "stop"], stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
            except Exception:  # noqa: BLE001 - best-effort teardown of a throwaway cluster
                pass

    def counts(self) -> dict[str, str]:
        return {
            "changelog_rows": self.psql(DB_NAME, "select count(*) from public.databasechangelog"),
            "changelog_distinct_ids": self.psql(DB_NAME, "select count(*) from (select distinct id, author, filename from public.databasechangelog) x"),
            "lock": self.psql(DB_NAME, "select id, locked, lockedby from public.databasechangeloglock"),
        }


def crash_artifacts(cfg: Config, since: float) -> list[str]:
    found: list[str] = []
    patterns = [str(cfg.run_dir / "**" / "hs_err*"), str(cfg.run_dir / "**" / "svm_err*"), str(cfg.run_dir / "**" / "*.dmp"),
                os.path.expandvars(r"%LOCALAPPDATA%\CrashDumps\nexus-service*")]
    for pat in patterns:
        found += [p for p in glob.glob(pat, recursive=True) if os.path.getmtime(p) >= since]
    return found


def wer_events(since: float) -> list[str]:
    ps = (
        f"$s=[DateTime]::Now.AddSeconds(-{int(time.time() - since) + 5}); "
        "Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=$s} -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Message -match 'nexus-service' } | Select-Object -First 5 | "
        "ForEach-Object { $_.Id.ToString()+' '+$_.ProviderName }"
    )
    o = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return [ln for ln in o.stdout.splitlines() if ln.strip()]


def engine_env(cfg: Config, cl: Cluster, svc_port: int, base: Mapping[str, str]) -> dict[str, str]:
    env = dict(base)
    env.update(
        NX_DB_URL=f"jdbc:postgresql://127.0.0.1:{cl.port}/{DB_NAME}", NX_DB_USER=PG_USER, NX_DB_PASS="unused-trust-auth",
        NX_SERVICE_PORT=str(svc_port), NX_SERVICE_TOKEN="probetoken", NX_ONNX_MODEL_DIR=str(cfg.models),
        NX_ALLOW_PROD_WRITE=cfg.prod_write_note,
    )
    env.pop("NX_VOYAGE_API_KEY", None)
    if cfg.ort_wait_ms is not None:
        env["NX_ORT_INIT_SHUTDOWN_WAIT_MS"] = str(cfg.ort_wait_ms)
    return env


def send_break(pid: int) -> None:
    os.kill(pid, signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]  # Windows only


def boot(cfg: Config, cl: Cluster, tag: str, trigger: str | None = None, do_break: bool = False, offset: float = 0.0,
         ready_timeout: float = 400, hold: float | None = None) -> dict[str, object]:
    """Start the engine; wait for *trigger*, then (optionally) send CTRL_BREAK after *offset* seconds.

    trigger: None (wait for ready) | 'pending' | 'midchangeset:<n>' | 'onnx_model_root' | 'ready'."""
    svc = free_port()
    out = cl.dir / f"svc-{tag}.out"
    t0 = wall0 = time.time()
    fh = out.open("w")
    p = subprocess.Popen(
        [str(cfg.exe), "-Duser.timezone=UTC"], env=engine_env(cfg, cl, svc, os.environ), cwd=cl.dir,
        stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, creationflags=NEW_PROCESS_GROUP,
    )
    r: dict[str, object] = {"tag": tag, "pid": p.pid, "trigger": trigger}

    def text() -> str:
        return out.read_text(errors="replace")

    seen = None
    while time.time() < t0 + ready_timeout and p.poll() is None:
        txt = text()
        if trigger == "pending" and "event=schema_migration_pending" in txt:
            seen = "migration_pending" if "schema_migration_complete" not in txt else "missed"
            break
        if trigger and trigger.startswith("midchangeset:") and txt.count("Running Changeset") >= int(trigger.split(":")[1]):
            seen = f"changesets_running={txt.count('Running Changeset')}" if "schema_migration_complete" not in txt else "missed"
            break
        if trigger == "onnx_model_root" and "onnx_model_root" in txt:
            seen = "onnx_model_root"
            break
        if "event=service_ready" in txt:
            seen = "ready"
            break
        time.sleep(0.005)
    r["seen"] = seen
    r["t_seen_s"] = round(time.time() - t0, 2)
    if p.poll() is not None:
        r["exited_before_trigger"] = p.returncode
    if do_break and p.poll() is None:
        if offset:
            time.sleep(offset)
        r["offset_s"] = offset
        tb = time.time()
        send_break(p.pid)
        try:
            r["exit_code"] = p.wait(timeout=20)
            r["secs_to_exit"] = round(time.time() - tb, 3)
        except subprocess.TimeoutExpired:
            r["exit_code"] = "no-exit-20s"
    elif hold is not None and p.poll() is None:
        time.sleep(hold)  # observe without signalling, e.g. a boot waiting on a held changelog lock
    if p.poll() is None:
        try:
            r["health"] = urllib.request.urlopen(f"http://127.0.0.1:{svc}/health", timeout=3).status
        except Exception as e:  # noqa: BLE001
            r["health"] = repr(e)[:100]
        if hold is None and not do_break:
            r["left_running"] = True
            r["_proc"] = p
            fh.flush()
            return r
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        r["killed_after_hold"] = True
        r["exit_code_after_taskkill"] = p.wait()
    fh.flush()
    txt = text()
    r["events"] = event_lines(txt)
    r["tail"] = [ln[-200:] for ln in txt.splitlines()[-3:]]
    r["unavailable_warnings"] = shutdown_flags(txt)["unavailable_warnings"]
    r["wall_s"] = round(time.time() - wall0, 2)
    return r


def stop_with_break(r: dict[str, object]) -> None:
    """Stop a left-running engine with CTRL_BREAK and fill the exit fields."""
    p = r.pop("_proc")
    r.pop("left_running", None)
    tb = time.time()
    send_break(p.pid)  # type: ignore[attr-defined]
    try:
        r["stop_exit_code"] = p.wait(timeout=20)  # type: ignore[attr-defined]
        r["stop_secs_to_exit"] = round(time.time() - tb, 3)
    except subprocess.TimeoutExpired:
        r["stop_exit_code"] = "no-exit-20s"
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)  # type: ignore[attr-defined]
    r["stop_verdict"] = stop_verdict(r["stop_exit_code"])


def _public(r: Mapping[str, object]) -> dict[str, object]:
    return {k: v for k, v in r.items() if not k.startswith("_")}


def phase_a(cfg: Config) -> dict[str, object]:
    cl = Cluster(cfg, "a")
    out: dict[str, object] = {"changelog_tags_in_source": count_changesets(cfg.changelog_dir)}
    try:
        cl.start()
        out["boot1_break_before_lock"] = boot(cfg, cl, "1", "pending", True)
        out["after1"] = cl.counts()
        b2 = boot(cfg, cl, "2")
        out["after2"] = cl.counts()
        if b2.get("left_running"):
            stop_with_break(b2)
        out["boot2_plain"] = _public(b2)
        cl.stop()
        out["pg"] = cl.res
    finally:
        cl.abort()
    return out


def phase_b(cfg: Config) -> dict[str, object]:
    cl = Cluster(cfg, "b")
    tags = count_changesets(cfg.changelog_dir)
    out: dict[str, object] = {"changelog_tags_in_source": tags}
    try:
        cl.start()
        out["boot1_break_mid_changeset"] = boot(cfg, cl, "1", f"midchangeset:{tags // 2}", True)
        out["after1"] = cl.counts()
        out["boot2_after_midchangeset_stop"] = boot(cfg, cl, "2", None, False, hold=5, ready_timeout=45)
        out["after2"] = cl.counts()
        cl.stop()
        out["pg"] = cl.res
    finally:
        cl.abort()
    return out


def phase_c(cfg: Config) -> dict[str, object]:
    cl = Cluster(cfg, "c")
    out: dict[str, object] = {"changelog_tags_in_source": count_changesets(cfg.changelog_dir), "ort_wait_ms": cfg.ort_wait_ms}
    start = time.time()
    try:
        cl.start()
        warm = boot(cfg, cl, "warm")  # migrate once, serve, then stop on BREAK
        out["warm_boot_ready_s"] = warm.get("t_seen_s")
        out["warm_counts"] = cl.counts()
        if warm.get("left_running"):
            stop_with_break(warm)
        out["warm_stop_exit"] = warm.get("stop_exit_code")
        rows = []
        for i, off in enumerate((0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2)):
            r = boot(cfg, cl, f"c{i}", "onnx_model_root", True, offset=off)
            txt = (cl.dir / f"svc-c{i}.out").read_text(errors="replace")
            r["landed_in_init"] = landed_in_init(txt)
            r["wait_done"] = [ln[-90:] for ln in txt.splitlines() if "ort_init_shutdown_wait_done" in ln]
            r["wait_timeout"] = "ort_init_shutdown_wait_timeout" in txt
            r["shutdown_hook_ran"] = "event=shutdown_signal" in txt
            r["stop_verdict"] = stop_verdict(r.get("exit_code"))
            rows.append({k: v for k, v in r.items() if k != "tail"})
        out["sweep"] = rows
        out["landed_count"] = sum(1 for r in rows if r.get("landed_in_init"))
        out["timeout_branch_fired"] = sum(1 for r in rows if r.get("wait_timeout"))
        out["crash_artifacts"] = crash_artifacts(cfg, start)
        out["wer_events"] = wer_events(start)
        out["after_counts"] = cl.counts()
        nb = boot(cfg, cl, "next")  # the next boot after the sweep is clean
        out["next_boot_ready_s"] = nb.get("t_seen_s")
        if nb.get("left_running"):
            stop_with_break(nb)
        out["next_boot"] = _public(nb)
        cl.stop()
        out["pg"] = cl.res
    finally:
        cl.abort()
    return out


def phase_d(cfg: Config) -> dict[str, object]:
    cl = Cluster(cfg, "d")
    out: dict[str, object] = {"changelog_tags_in_source": count_changesets(cfg.changelog_dir)}
    start = time.time()
    try:
        cl.start()
        s1 = boot(cfg, cl, "serve")
        out["serve_ready_s"] = s1.get("t_seen_s")
        out["serve_health"] = s1.get("health")
        out["counts_serving"] = cl.counts()
        if s1.get("left_running"):
            stop_with_break(s1)
        txt = (cl.dir / "svc-serve.out").read_text(errors="replace")
        out["serving_stop"] = {"exit_code": s1.get("stop_exit_code"), "secs_to_exit": s1.get("stop_secs_to_exit"),
                               "verdict": s1.get("stop_verdict"), **shutdown_flags(txt),
                               "tail": [ln[-170:] for ln in txt.splitlines()[-4:]]}
        out["after_stop"] = cl.counts()
        s2 = boot(cfg, cl, "next")
        out["next_boot_ready_s"] = s2.get("t_seen_s")
        if s2.get("left_running"):
            stop_with_break(s2)
        txt2 = (cl.dir / "svc-next.out").read_text(errors="replace")
        out["next_boot"] = {"events": s2.get("events"), "stop_exit_code": s2.get("stop_exit_code"), "stop_secs": s2.get("stop_secs_to_exit"),
                            "verdict": s2.get("stop_verdict"), "new_changesets": new_changesets(txt2),
                            "migration_complete": [ln.split(" - ", 1)[-1][:200] for ln in txt2.splitlines() if "schema_migration_complete" in ln],
                            "unavailable_warnings": shutdown_flags(txt2)["unavailable_warnings"]}
        out["after_next"] = cl.counts()
        out["crash_artifacts"] = crash_artifacts(cfg, start)
        out["wer_events"] = wer_events(start)
        cl.stop()
        out["pg"] = cl.res
    finally:
        cl.abort()
    return out


PHASES = {"a": phase_a, "b": phase_b, "c": phase_c, "d": phase_d}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("phases", nargs="+", choices=sorted(PHASES), help="phases to run, in order")
    p.add_argument("--exe", type=Path, required=True, help="the native nexus-service.exe")
    p.add_argument("--pg-bin", type=Path, required=True, help="the Windows PG bundle's bin directory")
    p.add_argument("--models", type=Path, required=True, help="NX_ONNX_MODEL_DIR (holds bge-base-en-v1.5/onnx)")
    p.add_argument("--changelog-dir", type=Path, required=True, help="service/src/main/resources/db (counted for the changeset total)")
    p.add_argument("--run-dir", type=Path, required=True, help="where each phase's throwaway cluster, logs and results-<phase>.json go")
    p.add_argument("--ort-wait-ms", type=int, help="shorten the ORT-init shutdown wait bound (NX_ORT_INIT_SHUTDOWN_WAIT_MS) so phase c reaches the timeout branch")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform != "win32":
        print("engine_windows_stop_probe drives CTRL_BREAK_EVENT and taskkill: Windows only", file=sys.stderr)
        return 2
    cfg = Config(args.exe, args.pg_bin, args.models, args.changelog_dir, args.run_dir, args.ort_wait_ms)
    cfg.run_dir.mkdir(parents=True, exist_ok=True)
    verdicts: dict[str, list[str]] = {}
    for ph in args.phases:
        t = time.time()
        try:
            res = PHASES[ph](cfg)
        except Exception as e:  # noqa: BLE001 - the report records what failed
            import traceback

            res = {"error": repr(e), "tb": traceback.format_exc()[-1500:]}
        res["phase_wall_s"] = round(time.time() - t, 1)
        problems = phase_problems(ph, res)
        res["verdict"] = "FAIL" if problems else "PASS"
        res["problems"] = problems
        text = json.dumps(res, indent=1, default=str)
        (cfg.run_dir / f"results-{ph}.json").write_text(text)
        print(f"=== phase {ph}")
        print(text)
        verdicts[ph] = problems
    for ph, problems in verdicts.items():
        print(f"PROBE phase {ph}: {'FAIL' if problems else 'PASS'}")
        for line in problems:
            print(f"  - {line}")
    return 1 if any(verdicts.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
