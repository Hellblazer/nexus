#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Upgrade-boot write gate: bytes an engine upgrade writes on its first boot (nexus-f3yqd.1).

Two upgrades in a row rewrote user data for about 20 minutes on the first boot, and nothing measured
it. This builds the state a user of the previous release holds, then boots the new engine on it and
counts what PG writes from the moment the cluster starts until the engine is ready and idle.

Old state, the way users got it: ``uvx --from conexus==<prev>`` runs ``nx init`` (the real acquire
route: that release's pinned engine binary and PG bundle) under an isolated ``NEXUS_CONFIG_DIR``,
seeds it through that client (``scripts/upgrade_boot_seed.py``; large collections that pass the PCI
build threshold at its default, one small control, and a small repo through ``nx index repo``), and
waits until the old engine has built its indexes and gone idle. Then ``nx daemon service stop
--with-pg``.

New boot, the way users get it: this checkout's client runs ``nx daemon service start`` on the same
config dir with ``NEXUS_SERVICE_JAR`` naming the new engine, through the same supervisor.

What is counted. WAL bytes are an LSN difference: the old cluster's shutdown checkpoint
(``pg_controldata``) to ``pg_current_wal_lsn()``, so every byte the new boot logs counts, migrations
included, with no reset hook between the PG start and Liquibase. ``pg_stat_io`` writes and extends
are reported beside it, measured from the first sample after PG accepts connections. Phases:
``migrate`` is PG up to the engine's HTTP bind, ``ready`` is up to ``/health`` 200, ``settle`` is up to
idle. Idle means all of: ``/v1/status`` ``boot_work`` empty (absent on engines that predate it is
taken as empty), no rows in ``pg_stat_progress_create_index`` or ``pg_stat_progress_vacuum``, and WAL
growth under ``--idle-wal-bytes`` over the last ``--idle-window`` seconds; and, unless
``--no-reconcile-wait``, a completed PCI reconcile pass in this boot's engine log plus the PCI end state (a
valid ``pci_`` index for each large seeded collection, none INVALID). The ceiling applies to the total;
the phases name where a regression is (see Bounds below). The ceiling gates WAL only: data-file writes that are not
WAL-logged (an index build's own heap and index pages beyond WAL, temp files) show in the reported
``pg_stat_io`` figures, which are not gated.

Every boot is followed by a second one on the same data dir: a same-version ``reboot`` after a good
upgrade, a ``retry`` after a failed one (does the failure repeat), a ``resume`` after a mid-queue restart.
Each boot also runs ``nx doctor`` from ``/health`` 200 and records how long it took (``--doctor-capture``
adds a py-spy profile, a DEBUG log and ``pg_stat_activity`` samples).

Old-state variants. ``--prev-client`` picks the release (8.0.0 holds PCI indexes; 7.76.0, engine 0.1.156,
predates PCI, so the upgrade builds every one; 7.72.1, engine 0.1.149, predates the vectors-030 walk).
``--rot SHAPE[,..]|all`` plants named store-rot shapes (``ROT_SHAPES``) before the old stop, each with a
non-vacuity presence count. ``--interrupt-build`` kills the old engine mid ``pci_`` build and requires an
INVALID index afterwards. ``--prepare-only --work DIR`` builds and stops the old state for another gate.

Restart leg. ``--restart-mid-queue K`` stops the upgrade boot once its Kth ``pci_`` build has started
(``--restart-mode graceful``: ``nx daemon service stop``, PG up, a user's path; ``kill``: SIGKILL of the
supervisor and engine; ``with-pg``: PG down too). A build is (index, backend pid): the same pair after the
restart is a kept build, a new pid is a rebuild. With PG up the pass rule wants nothing killed, rebuilt or
built twice and every kept build adopted (``pci_anchor_adopted``); with PG down, each in-flight index
rebuilt exactly once.

Bounds: ``--ceiling-boot`` gates the upgrade boot's migrate + ready WAL (``boot_wal_bytes``; the release gate
is 64 MiB, accepted by Sam 2026-11-10), ``--ceiling`` its total through settle (on a ``--restart-mid-queue``
run, through the restart), and ``--ceiling-reboot`` a plain same-version reboot's migrate + ready
(``reboot_wal_bytes``; 64 MiB on the release gate's same-version leg). Both limits are Sam's, 2026-11-10, recorded
on nexus-f3yqd.1. Every boot's ``boot_wal_bytes`` is reported in ``boots[]``. A ``resume`` boot is never gated:
a PCI build kept across the restart writes its WAL while it migrates.

Exit: 0 within every bound given (or none given) with the restart rule met; 1 above a bound
(``boot-over-ceiling``, ``reboot-over-ceiling`` or ``over-ceiling``, with ``ceilings_tripped``), a boot that never reached ready
(``boot-failed``) or a broken restart rule (``restart-red``); 2 when the run
itself failed (a fixture that did not take, a boot that never reached idle within ``--max-wait``, a step
that errored). Prints the figures as one JSON object on stdout, and to ``--json`` when given.

Heavy: run it on a lab host (``lab-remote -c lab-m -- uv run python scripts/upgrade_boot_writes.py
...``), never on the laptop. It touches no cloud.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import re
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import signal
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
#: The PCI build threshold's default (NX_SEARCH_PCI_BUILD_MIN_ROWS, PgSession.java): a collection
#: builds its own index at or above this, so the fixture's large collections sit above it.
PCI_BUILD_MIN_ROWS = 20_000
#: Per-collection index names (PciCatalog.PREFIX); every other hnsw index is a partition leaf's.
PCI_PREFIX = "pci_"
#: The tenant a local install writes as (HttpCatalogClient logs tenant=default).
LOCAL_TENANT = "default"


class HarnessError(RuntimeError):
    """The run could not produce a measurement (exit 2)."""


def log(msg: str) -> None:
    sys.stderr.write(f"upgrade_boot_writes: {msg}\n")
    sys.stderr.flush()


# ── the install under test ───────────────────────────────────────────────────


@dataclass
class Install:
    config_dir: Path

    @property
    def creds(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in (self.config_dir / "pg_credentials").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
        return out

    @property
    def bundle(self) -> Path:
        return self.config_dir / "pg-bundle" / "bundle"

    @property
    def pgdata(self) -> Path:
        return Path(self.creds["PG_DATA"])

    @property
    def dbname(self) -> str:
        return self.creds["NX_DB_URL"].rsplit("/", 1)[-1].split("?", 1)[0]

    def _bin_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["LD_LIBRARY_PATH"] = f"{self.bundle / 'lib'}:{env.get('LD_LIBRARY_PATH', '')}".rstrip(":")
        return env

    def sql(self, query: str, *, timeout: float = 30.0) -> list[list[str]] | None:
        """Rows of ``query`` as the cluster superuser, or None when PG does not accept connections."""
        c = self.creds
        env = self._bin_env()
        # The cluster superuser where the install recorded its password; older installs (before
        # PG_SUPERUSER_PASS) record only the schema owner, nexus_admin, which owns every nexus table.
        user, env["PGPASSWORD"] = ((getpass.getuser(), c["PG_SUPERUSER_PASS"]) if "PG_SUPERUSER_PASS" in c
                                   else (c["NX_DB_ADMIN_USER"], c["NX_DB_ADMIN_PASS"]))
        env["PGCONNECT_TIMEOUT"] = "3"
        r = subprocess.run(
            [str(self.bundle / "bin" / "psql"), "-XAtq", "-F", "\t", "-h", "127.0.0.1", "-p", c["PG_PORT"],
             "-U", user, "-d", self.dbname, "-v", "ON_ERROR_STOP=1", "-c", query],
            env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            if "could not connect" in r.stderr or "Connection refused" in r.stderr or "starting up" in r.stderr \
                    or "shutting down" in r.stderr:
                return None
            raise HarnessError(f"psql failed: {r.stderr.strip()[:400]}")
        return [line.split("\t") for line in r.stdout.splitlines() if line]

    def checkpoint_lsn(self) -> str:
        """The stopped cluster's last checkpoint LSN, from pg_controldata (refuses a running cluster)."""
        r = subprocess.run([str(self.bundle / "bin" / "pg_controldata"), str(self.pgdata)],
                           env=self._bin_env(), capture_output=True, text=True, check=True)
        fields = dict(line.split(":", 1) for line in r.stdout.splitlines() if ":" in line)
        state = fields.get("Database cluster state", "").strip()
        if state != "shut down":
            raise HarnessError(f"cluster state is {state!r} after the stop, not 'shut down'")
        return fields["Latest checkpoint location"].strip()

    def pg_ctl(self, *args: str) -> None:
        subprocess.run([str(self.bundle / "bin" / "pg_ctl"), "-D", str(self.pgdata), *args],
                       env=self._bin_env(), check=True, capture_output=True, text=True, timeout=300)

    def addr_pids(self) -> tuple[int | None, int | None]:
        """(engine pid, supervisor pid) from the install's service address file."""
        for p in sorted(self.config_dir.glob("storage_service_addr.*")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ep, pl = d.get("endpoint") or {}, d.get("payload") or {}
            return ep.get("pid"), pl.get("supervisor_pid")
        return None, None

    def service_port(self) -> int | None:
        for p in sorted(self.config_dir.glob("storage_service_addr.*")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ep = d.get("endpoint") if isinstance(d, dict) else None
            port = ep.get("port") if isinstance(ep, dict) else None
            if port:
                return int(port)
        return None


def lsn_bytes(lsn: str) -> int:
    hi, lo = lsn.split("/")
    return (int(hi, 16) << 32) + int(lo, 16)


# ── real-store rot (nexus-f3yqd.7) ───────────────────────────────────────


@dataclass(frozen=True)
class RotShape:
    """One shape a long-lived local store collects, planted into the old state before its stop.

    ``plant`` and ``present`` are SQL run as the install's most privileged recorded role, on the OLD
    schema. ``present`` returns one count; a rotted run fails (exit 2) when it is 0, so a shape that did
    not land never passes as tested. A shape found in the field is one more entry in ROT_SHAPES, with a
    docstring saying where it came from and which upgrade step it exercises."""

    name: str
    doc: str
    plant: str
    present: str


def _unit_1024() -> str:
    return "[" + ",".join(["0.03125"] * 1024) + "]"  # 1024 x (1/32)^2 = 1: a unit vector


ROT_SHAPES: dict[str, RotShape] = {s.name: s for s in (
    RotShape(
        name="dimension_mismatch",
        doc=("A chunk in a collection registered for bge-base-en-v1.5 (768) whose vector sits in embedding_1024. "
             "The pre-vectors-030 chunks table allows it (CHECK num_nonnulls(embedding_*) = 1); the RDR-225 walk "
             "(vectors-030-1 step 3) moves rows into per-model partitions with a dimension CHECK. Planted on the "
             "pre-030 schema (--prev-client 7.72.1, engine 0.1.149), into the first large seeded collection. "
             "Source: wren's reading of the vectors-030-1 rollback, 2026-11-10 (nexus-f3yqd.7)."),
        plant=("INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024, metadata) "
               "SELECT r.tenant_id, r.name, sha256(convert_to('rot dimension_mismatch in ' || r.name, 'UTF8')), "
               "'rot dimension_mismatch in ' || r.name, '" + _unit_1024() + "', '{\"rot\": \"dimension_mismatch\"}' "
               "FROM nexus.catalog_collections r WHERE r.name LIKE 'knowledge\\_\\_bootw-a\\_\\_%' ESCAPE '\\' "
               "AND r.embedding_model LIKE 'bge%'"),
        present=("SELECT count(*) FROM nexus.chunks c JOIN nexus.catalog_collections r "
                 "ON r.tenant_id = c.tenant_id AND r.name = c.collection "
                 "WHERE r.name LIKE 'knowledge\\_\\_bootw-a\\_\\_%' ESCAPE '\\' AND c.embedding_1024 IS NOT NULL"),
    ),
    RotShape(
        name="pending_aspect_queue",
        doc=("Rows still waiting in nexus.aspect_extraction_queue when the upgrade starts, as on a store whose "
             "aspect worker never drained its queue. aspects-005-2 (retire the queue table) is guarded by a "
             "precondition that the table is empty, with onFail=CONTINUE and failOnError=false, so on the first "
             "upgrade boot it is skipped and NOT recorded; the new engine drains the queue, and on the next boot "
             "it runs as a real pending changeset, which makes SchemaMigrator terminate a PCI build kept across a "
             "restart (event=pci_builders_terminated reason=schema_changesets_pending). Three pending rows for "
             "real seeded documents (doc_id references catalog_documents). Pair with --restart-mid-queue K "
             "--restart-mode graceful. Source: the e90c46349 kill-leg diagnosis, 2026-11-10 (wren, "
             "nexus-f3yqd.1)."),
        plant=("INSERT INTO nexus.aspect_extraction_queue (tenant_id, collection, source_path, doc_id, content, "
               "enqueued_at) SELECT d.tenant_id, d.physical_collection, 'nx-seed://rot/aspect-queue/' || d.tumbler, "
               "d.tumbler, 'aspect extraction left pending for the upgrade gate', now() "
               "FROM nexus.catalog_documents d WHERE d.physical_collection LIKE 'knowledge\\_\\_bootw-a\\_\\_%' "
               "ESCAPE '\\' ORDER BY d.tumbler LIMIT 3"),
        present=("SELECT count(*) FROM nexus.aspect_extraction_queue "
                 "WHERE source_path LIKE 'nx-seed://rot/aspect-queue/%'"),
    ),
    RotShape(
        name="disputed_fallback_model",
        doc=("hygiene-002's attribute walk registered a collection it could not attribute under a fallback "
             "embedding_model (bge-base-en-v1.5, 768) with lifecycle_state 'disputed', while every vector the "
             "collection stores is 1024 wide. On a pre-030 store vectors-030-1 then fails on the model "
             "partition's <partition>_dimension_chk, rolls back, and repeats on every boot (the same refusal "
             "P225MigrationWalkIntegrationTest's aVectorWhoseDimensionDisagrees... pins). Planted as its own "
             "collection, its registry row copied from the first large seeded collection's (so every column "
             "holds a value the old engine wrote), with three 1024-wide chunks. Source: wren, 2026-11-10 "
             "(nexus-f3yqd.7)."),
        plant=("INSERT INTO nexus.catalog_collections SELECT (jsonb_populate_record(r, jsonb_build_object("
               "'name', 'knowledge__bootw-disputed__' || split_part(r.name, '__', 3) || '__v1', "
               "'lifecycle_state', 'disputed', 'created_at', now()))).* "
               "FROM nexus.catalog_collections r WHERE r.name LIKE 'knowledge\\_\\_bootw-a\\_\\_%' ESCAPE '\\'; "
               "INSERT INTO nexus.chunks (tenant_id, collection, chash, chunk_text, embedding_1024, metadata) "
               "SELECT r.tenant_id, r.name, sha256(convert_to('rot disputed chunk ' || g || ' in ' || r.name, 'UTF8')), "
               "'rot disputed chunk ' || g || ' in ' || r.name, '" + _unit_1024() + "', "
               "'{\"rot\": \"disputed_fallback_model\"}' "
               "FROM nexus.catalog_collections r, generate_series(1, 3) g "
               "WHERE r.name LIKE 'knowledge\\_\\_bootw-disputed\\_\\_%' ESCAPE '\\'"),
        present=("SELECT count(*) FROM nexus.chunks c JOIN nexus.catalog_collections r "
                 "ON r.tenant_id = c.tenant_id AND r.name = c.collection "
                 "WHERE r.lifecycle_state = 'disputed' AND r.embedding_model LIKE 'bge%' "
                 "AND c.embedding_1024 IS NOT NULL"),
    ),
)}


def plant_rot(inst: "Install", names: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in names:
        shape = ROT_SHAPES[name]
        # Under nexus_admin (installs that record no superuser password) the registry is behind RLS keyed on
        # nexus.tenant; a local install's tenant is 'default'. A superuser bypasses RLS and ignores it.
        inst.sql(f"SET nexus.tenant = '{LOCAL_TENANT}'; {shape.plant}")
        rows = inst.sql(f"SET nexus.tenant = '{LOCAL_TENANT}'; {shape.present}") or [["0"]]
        present = int(rows[0][0])
        if present == 0:
            raise HarnessError(f"rot shape {name} did not land: its presence check counts 0")
        out[name] = {"present_before_stop": present}
    return out


# ── sampling ────────────────────────────────────────────────────────────────


_IO_SQL_BYTES = ("select coalesce(sum(write_bytes),0)::bigint, coalesce(sum(extend_bytes),0)::bigint "
                 "from pg_stat_io")
_IO_SQL_OPS = ("select coalesce(sum(writes*op_bytes),0)::bigint, coalesce(sum(extends*op_bytes),0)::bigint "
               "from pg_stat_io")
_HNSW_SQL = ("select c.relname, c.relfilenode, i.indisvalid from pg_index i join pg_class c on c.oid = i.indexrelid "
             "join pg_am a on a.oid = c.relam where a.amname = 'hnsw' order by 1")


@dataclass
class Sample:
    t: float
    lsn: int
    io_write_bytes: int | None
    io_extend_bytes: int | None
    building: int
    vacuuming: int
    pci_building: tuple[tuple[str, int], ...] = ()  # (index name, building backend pid)


def sample(inst: Install, t0: float) -> Sample | None:
    rows = inst.sql("select pg_current_wal_lsn()::text, "
                    "(select count(*) from pg_stat_progress_create_index), "
                    "(select count(*) from pg_stat_progress_vacuum)")
    if rows is None:
        return None
    lsn, building, vacuuming = rows[0]
    pci = inst.sql("select c.relname, p.pid from pg_stat_progress_create_index p join pg_class c on c.oid = p.index_relid "
                   "where c.relname like 'pci\\_%' escape '\\'") or []
    io_w = io_e = None
    for q in (_IO_SQL_BYTES, _IO_SQL_OPS):  # PG18 has write_bytes; PG16-17 have op_bytes
        try:
            io = inst.sql(q)
        except HarnessError:
            continue
        if io:
            io_w, io_e = int(io[0][0]), int(io[0][1])
            break
    return Sample(time.monotonic() - t0, lsn_bytes(lsn), io_w, io_e, int(building), int(vacuuming),
                  tuple(sorted((r[0], int(r[1])) for r in pci)))


def build_timeline(samples: list[Sample]) -> list[dict]:
    """Each pci_ build seen in the samples: when it first and last showed in pg_stat_progress_create_index,
    and the WAL logged across that span (all writers, so concurrent work is included). ``left_progress``
    means the build left the progress view before the samples ended: it finished OR was terminated (the
    boot's terminatePciBuilders ends a surviving build the same way), so it is not a validity claim."""
    spans: dict[tuple[str, int], list[Sample]] = {}
    for x in samples:
        for key in x.pci_building:
            spans.setdefault(key, []).append(x)
    out = []
    for (name, pid), xs in sorted(spans.items(), key=lambda kv: kv[1][0].t):
        nxt = next((x for x in samples if x.t > xs[-1].t), xs[-1])
        out.append({"index": name, "pid": pid, "start_s": round(xs[0].t, 1), "end_s": round(nxt.t, 1),
                    "wal_bytes": nxt.lsn - xs[0].lsn,
                    "left_progress": nxt is not xs[-1] and (name, pid) not in nxt.pci_building})
    return out


def resume_analysis(first: list[dict], second: list[dict], adopted_lines: list[str], mode: str,
                    after_stop_pids: set[int] | None = None) -> dict:
    """What a mid-queue restart did to the PCI queue. A build is identified by (index, backend pid): the
    same pid on both sides of the restart means the CREATE INDEX CONCURRENTLY kept running through it (it
    cannot be resumed, only kept); a new pid for an index the first boot finished or lost is a rebuild.
    ``after_stop_pids`` are the CREATE INDEX backends pg_stat_activity still showed after the stop: a build
    whose pid is among them was kept even when it finished before the second boot's first sample, as long
    as no other pid built that index afterwards."""
    after_stop_pids = after_stop_pids or set()
    done = {b["index"] for b in first if b["left_progress"]}
    lost = {(b["index"], b["pid"]) for b in first if not b["left_progress"]}
    first_pids = {(b["index"], b["pid"]) for b in first}
    new_builds = [b for b in second if (b["index"], b["pid"]) not in first_pids]
    survived = sorted({n for n, pid in lost
                       if any(b["index"] == n and b["pid"] == pid for b in second)
                       or (pid in after_stop_pids and not any(b["index"] == n for b in new_builds))})
    rebuilt = sorted({b["index"] for b in new_builds if b["index"] in done or b["index"] in {n for n, _ in lost}})
    pids: dict[str, set[int]] = {}
    for b in first + second:
        pids.setdefault(b["index"], set()).add(b["pid"])
    in_flight = sorted({n for n, _ in lost})
    killed = sorted(set(in_flight) - set(survived))
    built_twice = sorted(n for n, ps in pids.items() if len(ps) > 1)
    adopted = {n for n in survived if any(f"index={n}" in ln or n in ln for ln in adopted_lines)}
    rebuild_counts = {n: sum(1 for b in new_builds if b["index"] == n) for n in in_flight}
    return {"finished_before_restart": sorted(done),
            "in_flight_at_restart": in_flight,
            "survived_restart": survived,
            "killed_at_restart": killed,
            "rebuilt_after_restart": rebuilt,
            "finished_rebuilt": sorted(set(rebuilt) & done),
            "built_twice": built_twice,
            "adopted": sorted(adopted),
            "builds_after_restart": [b["index"] for b in second]} | _resume_verdict(
                mode, survived, killed, rebuilt, sorted(set(rebuilt) & done), built_twice, in_flight, adopted,
                rebuild_counts)


def _resume_verdict(mode: str, survived: list[str], killed: list[str], rebuilt: list[str], finished_rebuilt: list[str],
                    built_twice: list[str], in_flight: list[str], adopted: set[str],
                    rebuild_counts: dict[str, int] | None = None) -> dict:
    """The restart leg's pass rule (wren, nexus-f3yqd.1). PG kept up (graceful, kill): the in-flight build is
    KEPT, so nothing is killed, rebuilt or built twice, and every survivor is adopted (adoption writes the
    anchor; an unadopted survivor reads as unanchored on the next restart). PG restarted (with-pg): no build
    survives, each in-flight index is rebuilt exactly once, and nothing that had finished is rebuilt."""
    why: list[str] = []
    if not in_flight:
        # Vacuous otherwise: with nothing in flight every rule below holds, and the leg tested nothing.
        why.append("no pci_ build was in flight at the restart, so the leg tested nothing")
    if finished_rebuilt:
        why.append(f"finished indexes rebuilt: {finished_rebuilt}")
    if mode == "with-pg":
        if survived:
            why.append(f"builds survived a PG restart: {survived}")
        if sorted(rebuilt) != sorted(in_flight):
            why.append(f"rebuilt {rebuilt}, expected exactly the in-flight {in_flight}")
        more = sorted(n for n, c in (rebuild_counts or {}).items() if c > 1)
        if more:
            why.append(f"in-flight index rebuilt more than once: {more}")
        if set(built_twice) - set(in_flight):
            why.append(f"built twice outside the in-flight set: {sorted(set(built_twice) - set(in_flight))}")
    else:
        if killed:
            why.append(f"in-flight builds killed: {killed}")
        if rebuilt:
            why.append(f"rebuilt after the restart: {rebuilt}")
        if built_twice:
            why.append(f"built twice: {built_twice}")
        if set(survived) - adopted:
            why.append(f"survived but never adopted: {sorted(set(survived) - adopted)}")
    return {"resume_ok": not why, "resume_why": why}


class MidQueueRestart(Exception):
    """Raised inside the idle wait once the Kth pci_ build of the boot has started (--restart-mid-queue)."""


def applied(inst: "Install", changeset: str) -> bool | None:
    """Whether ``changeset`` is in databasechangelog; None when PG cannot be read (never a false "not applied")."""
    rows = inst.sql(f"select count(*) from databasechangelog where id = '{changeset}'")
    return None if rows is None else int(rows[0][0]) > 0


def hnsw_indexes(inst: Install) -> list[tuple[str, str, bool]]:
    rows = inst.sql(_HNSW_SQL) or []
    return [(r[0], r[1], r[2] == "t") for r in rows]


def http_status(port: int, path: str) -> tuple[int, dict | None]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, OSError):
        return 0, None


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1.0)
        return s.connect_ex(("127.0.0.1", port)) == 0


def boot_work_empty(status: dict | None) -> bool:
    """``/v1/status`` boot_work is empty; an engine that predates the field counts as empty."""
    if not status or "boot_work" not in status:
        return True
    return not status["boot_work"]


def status_says_idle(code: int, status: dict | None) -> bool:
    """The ``/v1/status`` half of the idle rule: a 200 whose boot_work is empty (or absent, on an engine that
    predates the field), or a 404 from an engine that predates the route. Any other answer (none, a 401, a
    500) is not idle: a status that cannot be read says nothing about boot work."""
    if code == 404:
        return True
    return code == 200 and boot_work_empty(status)


@dataclass
class IdleRule:
    window_s: float
    wal_bytes: int


class LogWatch:
    """New lines in the install's logs/*.log since construction (offsets taken at boot start, so a
    previous incarnation's lines never count)."""

    def __init__(self, config_dir: Path) -> None:
        self._dir = config_dir / "logs"
        self._off: dict[Path, tuple[int, int]] = {}
        if self._dir.is_dir():
            for p in self._dir.glob("*.log"):
                st = p.stat()
                self._off[p] = (st.st_ino, st.st_size)

    def _start(self, path: Path) -> int:
        """Where this boot's lines begin. The supervisor rotates the log when it opens it at spawn, so a file
        with a new inode, or shorter than its recorded offset, is a fresh one and is read from its start."""
        ino, off = self._off.get(path, (None, 0))
        try:
            st = path.stat()
        except OSError:
            return 0
        # A different inode is a rotated (fresh) file; a shorter one is too, if rotation reused the inode.
        return 0 if st.st_ino != ino or st.st_size < off else off

    def lines(self, *needles: str) -> list[str]:
        out: list[str] = []
        for path in self._dir.glob("*.log"):
            try:
                with path.open("rb") as f:
                    f.seek(self._start(path))
                    out += [ln for raw in f if all(n in (ln := raw.decode("utf-8", "replace")) for n in needles)]
            except OSError:
                continue
        return out

    def seen(self, *needles: str) -> bool:
        for path in self._dir.glob("*.log"):
            try:
                with path.open("rb") as f:
                    f.seek(self._start(path))
                    for raw in f:
                        line = raw.decode("utf-8", "replace")
                        if all(n in line for n in needles):
                            return True
            except OSError:
                continue
        return False


#: A completed PCI reconcile pass (PciReconciler). The reconciler sleeps a boot jitter of up to 60 s before
#: its first pass, so a store with per-collection indexes is not idle until one pass has run to its end.
RECONCILE_PASS = ("event=pci_reconcile_pass", "ran_to_end=true")


def reconcile_pass_did_work(lines: list[str]) -> bool:
    """A completed pass that processed at least one leaf: ``ran_to_end=true`` is also logged when every leaf
    was skipped, which says nothing about the indexes."""
    for ln in lines:
        m = re.search(r"\bprocessed_leaves=(\d+)", ln)
        if m and int(m.group(1)) > 0:
            return True
    return False


def wait_idle(inst: Install, port_fn, t0: float, samples: list[Sample], rule: IdleRule, deadline: float,
              what: str, extra=None, on_sample=None) -> Sample:
    """Sample every second until idle (module docstring) and ``extra()``, when given, is true; raise
    HarnessError at the deadline."""
    last_note = 0.0
    while True:
        now = time.monotonic()
        if now > deadline:
            raise HarnessError(f"{what} never reached idle within the max wait")
        s = sample(inst, t0)
        if s is not None:
            samples.append(s)
            if on_sample is not None:
                on_sample(samples)
            port = port_fn()
            code, status = http_status(port, "/v1/status") if port else (0, None)
            older = [x for x in samples if x.t <= s.t - rule.window_s]
            if older and s.building == 0 and s.vacuuming == 0 and status_says_idle(code, status) \
                    and (extra is None or extra()):
                if s.lsn - older[-1].lsn < rule.wal_bytes:
                    return s
            if now - last_note > 60:
                last_note = now
                log(f"{what}: {s.t:.0f}s, wal +{s.lsn - samples[0].lsn} bytes, building={s.building} "
                    f"vacuuming={s.vacuuming} status={code} boot_work_empty={boot_work_empty(status)}")
        time.sleep(1.0)


# ── the two lives of the install ────────────────────────────────────────────


def run(cmd: list[str], env: dict[str, str], *, cwd: Path | None = None, input_text: str | None = None,
        what: str) -> str:
    log(f"{what}: {' '.join(cmd[:6])}{' ...' if len(cmd) > 6 else ''}")
    r = subprocess.run(cmd, env=env, cwd=cwd, input=input_text, capture_output=True, text=True)
    if r.returncode != 0:
        raise HarnessError(f"{what} failed (rc={r.returncode}): {(r.stderr or r.stdout).strip()[-1500:]}")
    return r.stdout


def make_synthetic_repo(root: Path) -> Path:
    repo = root / "synthetic-repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs").mkdir()
    for i in range(12):
        (repo / "src" / f"module_{i}.py").write_text(
            f'"""Module {i} of the synthetic repo."""\n\n\ndef handler_{i}(x: int) -> int:\n'
            f'    """Return x scaled by {i + 1}."""\n    return x * {i + 1}\n', encoding="utf-8")
        (repo / "docs" / f"guide_{i}.md").write_text(
            f"# Guide {i}\n\nThis guide explains handler {i} and when it scales its input.\n\n"
            f"## Usage\n\nCall handler_{i} with an integer.\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@example.invalid",
           "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@example.invalid"}
    for c in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "seed"]):
        subprocess.run(c, cwd=repo, env=env, check=True)
    return repo


@dataclass
class Report:
    prev_client: str
    new_jar: str
    rows_per_collection: int
    collections: dict[str, int] = field(default_factory=dict)
    hnsw_before: int = 0
    hnsw_after: int = 0
    pci_before: int = 0
    pci_after: int = 0
    hnsw_rebuilt: list[str] = field(default_factory=list)
    hnsw_new: list[str] = field(default_factory=list)
    rot: dict[str, dict] = field(default_factory=dict)
    interrupted_build: dict = field(default_factory=dict)
    resume: dict = field(default_factory=dict)
    boots: list[dict] = field(default_factory=list)
    vectors_030_1_applied_before: bool | None = None
    vectors_030_1_applied_after: bool | None = None
    checkpoint_lsn: str = ""
    phases: dict[str, dict] = field(default_factory=dict)
    total_wal_bytes: int = 0
    total_io_write_bytes: int | None = None
    total_io_extend_bytes: int | None = None
    elapsed_s: float = 0.0
    ceiling: int | None = None
    ceiling_boot: int | None = None
    ceiling_reboot: int | None = None
    new_jar_sha256: str | None = None
    new_jar_commit: str | None = None
    new_engine_version: dict = field(default_factory=dict)
    boot_wal_bytes: int = 0
    reboot_wal_bytes: int = 0
    ceilings_tripped: list[str] = field(default_factory=list)
    prepare_s: float = 0.0
    data_dir_bytes: int = 0
    verdict: str = ""
    error: str = ""


def _cmdline(pid: int) -> str:
    return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")


def _ppid(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return int(stat.rsplit(")", 1)[1].split()[1])


def kill_engine_and_supervisor(inst: Install) -> tuple[int, int]:
    """SIGKILL the install's supervisor, then its engine: two processes this run started. The engine is
    checked by its command line naming this install's config dir, the supervisor by being the engine's
    parent; neither is ever picked by a pattern."""
    engine, supervisor = inst.addr_pids()
    if not engine or not supervisor:
        raise HarnessError("no engine or supervisor pid in the install's address file")
    try:
        if str(inst.config_dir) not in _cmdline(engine):
            raise HarnessError(f"engine pid {engine} does not name {inst.config_dir}; not killing it")
        if _ppid(engine) != supervisor:
            raise HarnessError(f"supervisor pid {supervisor} is not engine {engine}'s parent; not killing it")
    except OSError as exc:
        raise HarnessError(f"cannot check pids {engine}/{supervisor}: {exc}") from exc
    os.kill(supervisor, signal.SIGKILL)
    os.kill(engine, signal.SIGKILL)
    return engine, supervisor


_PCI_BUILDING_SQL = ("select count(*) from pg_stat_progress_create_index p join pg_class c on c.oid = p.index_relid "
                     "where c.relname like 'pci\\_%' escape '\\'")
_PCI_INVALID_SQL = ("select count(*) from pg_index i join pg_class c on c.oid = i.indexrelid "
                    "where c.relname like 'pci\\_%' escape '\\' and not i.indisvalid")


def interrupt_pci_build(inst: Install, a: argparse.Namespace, rep: Report) -> None:
    """Old-state variant (wren, nexus-f3yqd.1): the engine dies while a per-collection index is being built,
    the way a laptop shut down mid-build leaves it. Wait for a pci_ build in pg_stat_progress_create_index,
    SIGKILL the supervisor and the engine, and stop PG fast so the in-flight build is aborted. Requires an
    INVALID pci_ index afterwards, so a run where the build finished first never passes as tested."""
    deadline = time.monotonic() + a.max_wait
    while True:
        rows = inst.sql(_PCI_BUILDING_SQL)
        if rows and int(rows[0][0]) > 0:
            break
        if time.monotonic() > deadline:
            raise HarnessError("no pci_ build appeared on the old engine within the max wait")
        time.sleep(0.5)
    engine, supervisor = kill_engine_and_supervisor(inst)
    log(f"interrupted a pci_ build: SIGKILL supervisor {supervisor} and engine {engine}, then a fast PG stop")
    inst.pg_ctl("stop", "-m", "fast", "-w")
    inst.pg_ctl("start", "-w", "-l", str(inst.pgdata / "pg.log"))
    invalid = int((inst.sql(_PCI_INVALID_SQL) or [["0"]])[0][0])
    idx = hnsw_indexes(inst)
    rep.hnsw_before = len(idx)
    rep.pci_before = sum(1 for n, _, ok in idx if n.startswith(PCI_PREFIX) and ok)
    a._before = {n: node for n, node, _ in idx}  # noqa: SLF001
    rep.interrupted_build = {"invalid_pci_before": invalid}
    rep.vectors_030_1_applied_before = applied(inst, "vectors-030-1")
    inst.pg_ctl("stop", "-m", "fast", "-w")
    if invalid == 0:
        raise HarnessError("the interrupted build left no INVALID pci_ index (it finished first?)")
    rep.checkpoint_lsn = inst.checkpoint_lsn()


def build_old_state(inst: Install, a: argparse.Namespace, env: dict[str, str], rep: Report, work: Path) -> None:
    old = ["uvx", "--quiet", "--from", f"conexus=={a.prev_client}"]
    run([*old, "nx", "init", "--no-autostart", "--no-beads-prime", "-y"], env, what="old nx init")
    subjects = [f"--subject=bootw-{chr(97 + i)}={a.rows}" for i in range(a.collections)]
    subjects.append(f"--subject=bootw-control={a.control_rows}")
    out = run([*old, "python", str(REPO / "scripts" / "upgrade_boot_seed.py"), *subjects, f"--seed={a.seed}"],
              env, what="old seed")
    rep.collections = json.loads(out.strip().splitlines()[-1])["collections"]
    run([*old, "nx", "index", "repo", str(make_synthetic_repo(work))], env, what="old nx index repo")
    if a.interrupt_build:
        interrupt_pci_build(inst, a, rep)
        return
    t0 = time.monotonic()
    log("waiting for the old engine to build its indexes and go idle")
    samples: list[Sample] = []
    deadline = t0 + a.max_wait
    while not a.pci_wait:
        wait_idle(inst, inst.service_port, t0, samples, IdleRule(a.idle_window, a.idle_wal_bytes),
                  deadline, "old engine")
        idx = hnsw_indexes(inst)
        pci = [i for i in idx if i[0].startswith(PCI_PREFIX)]
        break
    while a.pci_wait:
        wait_idle(inst, inst.service_port, t0, samples, IdleRule(a.idle_window, a.idle_wal_bytes),
                  deadline, "old engine")
        idx = hnsw_indexes(inst)
        pci = [i for i in idx if i[0].startswith(PCI_PREFIX)]
        valid = [i for i in pci if i[2]]
        if len(valid) >= a.collections and len(valid) == len(pci):
            break
        if time.monotonic() > deadline:
            raise HarnessError(f"the old engine built {len(valid)} valid per-collection index(es), fewer than "
                               f"the {a.collections} large collections; the fixture does not exercise the PCI path")
        time.sleep(5)
    rep.hnsw_before = len(idx)
    rep.pci_before = len(pci)
    if a.rot:
        rep.rot = plant_rot(inst, a.rot)
    rep.vectors_030_1_applied_before = applied(inst, "vectors-030-1")
    before = {name: node for name, node, _ in idx}
    run([*old, "nx", "daemon", "service", "stop", "--with-pg"], env, what="old stop")
    rep.checkpoint_lsn = inst.checkpoint_lsn()
    a._before = before  # noqa: SLF001 - carried to the new boot's comparison


def stop_new(env: dict[str, str]) -> None:
    subprocess.run(["uv", "run", "--project", str(REPO), "nx", "daemon", "service", "stop", "--with-pg"],
                   env=env, cwd=REPO, capture_output=True, text=True)


def _phase(start_lsn: int, start: Sample | None, end: Sample) -> dict:
    d: dict = {"wal_bytes": end.lsn - start_lsn, "end_s": round(end.t, 1)}
    if start is not None and None not in (start.io_write_bytes, end.io_write_bytes,
                                          start.io_extend_bytes, end.io_extend_bytes):
        d["io_write_bytes"] = int(end.io_write_bytes or 0) - int(start.io_write_bytes or 0)
        d["io_extend_bytes"] = int(end.io_extend_bytes or 0) - int(start.io_extend_bytes or 0)
    return d


def first_mark(marks: dict[str, Sample]) -> Sample:
    return marks["pg_up"]


_ACTIVITY_SQL = ("select now()::text, pid, backend_type, coalesce(state,''), coalesce(wait_event_type,''), "
                 "coalesce(wait_event,''), coalesce(round(extract(epoch from now() - query_start)::numeric, 1)::text,''), "
                 "left(regexp_replace(coalesce(query,''), '\\s+', ' ', 'g'), 160) from pg_stat_activity "
                 "where pid <> pg_backend_pid() and coalesce(query,'') not ilike '%create index%' "
                 "and backend_type = 'client backend'")


def _activity_sampler(inst: Install, path: Path, stop: threading.Event, d0: float) -> None:
    """Every 5 s while nx doctor runs: the client backends other than index builders, with their wait events."""
    with path.open("w", encoding="utf-8") as f:
        f.write("doctor_s\tnow\tpid\tbackend_type\tstate\twait_event_type\twait_event\tquery_age_s\tquery\n")
        while not stop.is_set():
            try:
                for row in inst.sql(_ACTIVITY_SQL) or []:
                    f.write(f"{time.monotonic() - d0:.1f}\t" + "\t".join(row) + "\n")
                f.flush()
            except HarnessError:
                pass
            stop.wait(5.0)


def run_doctor(inst: Install, a: argparse.Namespace, env: dict[str, str], label: str, t0: float) -> dict:
    """``nx doctor`` once, timed. With ``--doctor-capture`` it runs under py-spy (idle frames included, so a
    blocking call shows as its stack) with a DEBUG log and ``pg_stat_activity`` sampled every 5 s; when py-spy
    cannot run or attach, doctor is run again plain and that is recorded."""
    cmd = ["uv", "run", "--project", str(REPO), "nx", "doctor"]
    d0 = time.monotonic()
    if a.doctor_capture is None:
        r = subprocess.run(cmd, env=env, cwd=REPO, capture_output=True, text=True, timeout=a.max_wait)
        return {"started_s": round(d0 - t0, 1), "doctor_s": round(time.monotonic() - d0, 1), "rc": r.returncode}
    a.doctor_capture.mkdir(parents=True, exist_ok=True)
    stem = a.doctor_capture / f"doctor-{label}"
    env_d = {**env, "NEXUS_LOG_LEVEL": "DEBUG"}
    out_rec: dict = {"capture": str(stem)}
    stop = threading.Event()
    sampler = threading.Thread(target=_activity_sampler, args=(inst, stem.with_suffix(".activity.tsv"), stop, d0),
                               daemon=True)
    sampler.start()
    try:
        prof = ["uvx", "py-spy", "record", "--idle", "--subprocesses", "--rate", "10", "--format", "raw",
                "-o", str(stem.with_suffix(".pyspy.txt")), "--"]
        with stem.with_suffix(".log").open("w", encoding="utf-8") as out:
            r = subprocess.run([*prof, *cmd, "--verbose"], env=env_d, cwd=REPO, stdout=out,
                               stderr=subprocess.STDOUT, timeout=a.max_wait)
        if not stem.with_suffix(".pyspy.txt").exists():
            out_rec["pyspy_failed_rc"] = r.returncode
            d0 = time.monotonic()
            with stem.with_suffix(".log").open("a", encoding="utf-8") as out:
                r = subprocess.run([*cmd, "--verbose"], env=env_d, cwd=REPO, stdout=out,
                                   stderr=subprocess.STDOUT, timeout=a.max_wait)
    finally:
        stop.set()
        sampler.join(timeout=10)
    return out_rec | {"started_s": round(d0 - t0, 1), "doctor_s": round(time.monotonic() - d0, 1),
                      "rc": r.returncode}


def measure_boot(inst: Install, a: argparse.Namespace, env: dict[str, str], label: str, base_lsn: str) -> dict:
    """One boot of the new engine on the stopped install, measured from the cluster's last checkpoint
    (``base_lsn``) until idle, or until the boot fails: ``nx daemon service start`` exits non-zero, or
    ``/health`` never answers 200 within ``--max-wait``. A failed boot is a result, not a harness error:
    its bytes are counted to the moment the failure was seen. Leaves the install stopped."""
    new_env = {**env, "NEXUS_SERVICE_JAR": str(a.new_jar)}
    base = lsn_bytes(base_lsn)
    logs = LogWatch(inst.config_dir)
    t0 = time.monotonic()
    # Output to a file, never a pipe: the supervisor can inherit the descriptor, and a read of a pipe it holds
    # would block the failure path with no deadline.
    start_log = inst.config_dir / f"harness-start-{label}.log"
    start_out = start_log.open("w", encoding="utf-8")
    proc = subprocess.Popen(["uv", "run", "--project", str(REPO), "nx", "daemon", "service", "start"],
                            env=new_env, cwd=REPO, stdout=start_out, stderr=subprocess.STDOUT, text=True)
    deadline = t0 + a.max_wait
    samples: list[Sample] = []
    marks: dict[str, Sample] = {}
    rec: dict = {"label": label, "base_lsn": base_lsn, "reached_ready": False}
    failure = ""
    while "ready" not in marks:
        if time.monotonic() > deadline:
            failure = "/health never answered 200 within the max wait"
            break
        s = sample(inst, t0)
        if s is not None:
            samples.append(s)
            marks.setdefault("pg_up", s)
            port = inst.service_port()
            if port and "bind" not in marks and port_open(port):
                marks["bind"] = s
            if port and "bind" in marks and http_status(port, "/health")[0] == 200:
                # Sampled again after the 200, so WAL written during the probe counts toward the boot, not settle.
                marks["ready"] = sample(inst, t0) or s
                samples.append(marks["ready"])
                ver = http_status(port, "/version")[1] or {}
                rec["engine_version"] = {k: ver.get(k) for k in ("release_version", "app_version", "build_ref")}
        rc = proc.poll()
        if rc not in (None, 0):
            start_out.flush()
            out = start_log.read_text(encoding="utf-8", errors="replace")
            failure = f"nx daemon service start exited {rc}"
            rec["start_output_tail"] = out[-2000:]
            break
        time.sleep(0.5)
    doctor: dict = {}
    doc_thread: threading.Thread | None = None
    # A boot that will be restarted mid-queue runs no doctor: it would outlive the stop and run against an
    # install between two boots. The resume boot carries the doctor leg.
    if "ready" in marks and not (a.restart_mid_queue and label == "upgrade"):
        def _doctor() -> None:
            try:
                doctor.update(run_doctor(inst, a, new_env, label, t0))
            except Exception as exc:  # noqa: BLE001 - recorded in the report, never a silent "still running"
                doctor["error"] = f"{type(exc).__name__}: {exc}"

        doc_thread = threading.Thread(target=_doctor, daemon=True)
        doc_thread.start()
    if failure:
        last = sample(inst, t0) or (samples[-1] if samples else None)
        rec["failure"] = failure
        rec["failed_s"] = round(time.monotonic() - t0, 1)
        if last is not None:
            rec["total_wal_bytes"] = last.lsn - base
            if "pg_up" in marks:
                rec["phases"] = {"until_failure": _phase(base, marks["pg_up"], last)}
        rec["vectors_030_1_applied_after"] = applied(inst, "vectors-030-1") if last is not None else None
        stop_new(new_env)
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=60)
        start_out.close()
        return rec
    # Idle only after a reconcile pass ran to its end in THIS boot (its first pass waits a boot jitter), and
    # only once the PCI end state is reached: a valid pci_ index for every large seeded collection and no
    # INVALID one left. A build killed by a restart is dropped on one pass and rebuilt on a later one (the
    # sweep period defaults to 600 s), so a quiet window after the first pass is not the end of the work.
    def extra() -> bool:
        if not a.reconcile_wait:
            return True
        if not reconcile_pass_did_work(logs.lines(*RECONCILE_PASS)):
            return False
        idx = [i for i in hnsw_indexes(inst) if i[0].startswith(PCI_PREFIX)]
        return sum(1 for i in idx if i[2]) >= a.collections and all(i[2] for i in idx)

    def on_sample(xs: list[Sample]) -> None:
        if restart_at and len({n for x in xs for n, _ in x.pci_building}) >= restart_at:
            raise MidQueueRestart

    restart_at = a.restart_mid_queue if label == "upgrade" else 0
    try:
        idle = wait_idle(inst, inst.service_port, t0, samples, IdleRule(a.idle_window, a.idle_wal_bytes),
                         deadline, f"new engine ({label})", extra, on_sample)
    except MidQueueRestart:
        last = samples[-1]
        if a.restart_mode == "kill":
            engine, supervisor = kill_engine_and_supervisor(inst)
            how = f"SIGKILL supervisor {supervisor} and engine {engine}"
        elif a.restart_mode == "with-pg":  # case D: PG restarts too, so no build can survive
            r = subprocess.run(["uv", "run", "--project", str(REPO), "nx", "daemon", "service", "stop", "--with-pg"],
                               env=new_env, cwd=REPO, capture_output=True, text=True, timeout=300)
            how = f"nx daemon service stop --with-pg (rc {r.returncode})"
        else:  # graceful, the path a user's restarts take: the supervisor's own stop, PG left running
            r = subprocess.run(["uv", "run", "--project", str(REPO), "nx", "daemon", "service", "stop"],
                               env=new_env, cwd=REPO, capture_output=True, text=True, timeout=300)
            how = f"nx daemon service stop (rc {r.returncode})"
        def _index_backends() -> list[list[str]]:
            return [] if a.restart_mode == "with-pg" else (inst.sql(
            "select pid, state, left(regexp_replace(query, '\\s+', ' ', 'g'), 200), coalesce(wait_event_type, ''), "
            "coalesce(wait_event, ''), backend_start::text, backend_type "
            "from pg_stat_activity where pid <> pg_backend_pid() and query ilike '%create index%'") or [])

        # Read right after the stop and again 2 s later: a kept build that finishes inside the wait would
        # otherwise leave no trace and read as killed.
        seen = {x[0]: x for x in _index_backends()}
        time.sleep(2.0)
        seen.update({x[0]: x for x in _index_backends()})
        survivors = list(seen.values())
        restart_lsn = ("" if a.restart_mode == "with-pg"
                       else (inst.sql("select pg_current_wal_lsn()::text") or [[""]])[0][0])
        log(f"restarted mid-queue at {last.t:.0f}s ({a.restart_mode}): {how}; "
            f"{len(survivors)} CREATE INDEX backend(s) still running")
        rec.update({"restart_mode": a.restart_mode, "restart_how": how, "restart_lsn": restart_lsn,
                    "index_backends_after_stop": [{"pid": int(x[0]), "state": x[1], "query": x[2], "wait_event_type": x[3],
                                                   "wait_event": x[4], "backend_start": x[5], "backend_type": x[6]}
                                                  for x in survivors]})
        rec.update({"reached_ready": True, "restarted_mid_queue_s": round(last.t, 1), "total_wal_bytes": last.lsn - base,
                    "pci_builds": build_timeline(samples),
                    "phases": {"migrate": _phase(base, first_mark(marks), marks["bind"]),
                               "ready": _phase(marks["bind"].lsn, marks["bind"], marks["ready"]),
                               "until_restart": _phase(marks["ready"].lsn, marks["ready"], last)}})
        rec["doctor"] = {"doctor_s": None, "note": "not run on a boot restarted mid-queue"}
        proc.wait(timeout=60)
        start_out.close()
        return rec
    rec["pci_builds"] = build_timeline(samples)
    rec["reconcile_pass_seen"] = reconcile_pass_did_work(logs.lines(*RECONCILE_PASS))
    # The fix adopts through the existing anchor path (event=pci_anchor_adopted ... index=<name>); the
    # pci_index_adopted name is accepted too. pci_builders_kept is the boot's "left the builder running" line.
    adopted = logs.lines("event=pci_anchor_adopted") + logs.lines("event=pci_index_adopted")
    rec["adopted_logged"] = bool(adopted)
    rec["builders_kept_logged"] = logs.lines("event=pci_builders_kept")[:3]
    rec["_adopted_lines"] = adopted
    if doc_thread is not None:
        doc_thread.join(timeout=a.max_wait)
    rec["doctor"] = doctor or {"doctor_s": None, "note": "still running when the boot went idle"}
    first = marks["pg_up"]
    rec["reached_ready"] = True
    rec["phases"] = {"migrate": _phase(base, first, marks["bind"]),
                     "ready": _phase(marks["bind"].lsn, marks["bind"], marks["ready"]),
                     "settle": _phase(marks["ready"].lsn, marks["ready"], idle)}
    rec["total_wal_bytes"] = idle.lsn - base
    if None not in (first.io_write_bytes, idle.io_write_bytes, first.io_extend_bytes, idle.io_extend_bytes):
        rec["total_io_write_bytes"] = int(idle.io_write_bytes or 0) - int(first.io_write_bytes or 0)
        rec["total_io_extend_bytes"] = int(idle.io_extend_bytes or 0) - int(first.io_extend_bytes or 0)
    rec["elapsed_s"] = round(idle.t, 1)
    after = hnsw_indexes(inst)
    rec["hnsw"] = len(after)
    rec["pci"] = sum(1 for n, _, ok in after if n.startswith(PCI_PREFIX) and ok)
    rec["vectors_030_1_applied_after"] = applied(inst, "vectors-030-1")
    rec["_indexes"] = {n: node for n, node, _ in after}
    stop_new(new_env)
    proc.wait(timeout=60)
    start_out.close()
    return rec


def boot_new(inst: Install, a: argparse.Namespace, env: dict[str, str], rep: Report) -> None:
    """The gated boot, then a second boot on the same data dir: a same-version reboot when the upgrade
    reached ready (it should write next to nothing), a retry when it failed (does the failure repeat)."""
    first = measure_boot(inst, a, env, "upgrade", rep.checkpoint_lsn)
    first.pop("_adopted_lines", None)
    before = a._before  # noqa: SLF001
    idx = first.pop("_indexes", {})
    rep.hnsw_rebuilt = sorted(n for n, node in idx.items() if n in before and before[n] != node)
    rep.hnsw_new = sorted(n for n in idx if n not in before)
    rep.boots = [first]
    last = first
    rep.phases = first.get("phases", {})
    rep.total_wal_bytes = first.get("total_wal_bytes", 0)
    rep.boot_wal_bytes = boot_wal_bytes(first)
    first["boot_wal_bytes"] = rep.boot_wal_bytes
    rep.total_io_write_bytes = first.get("total_io_write_bytes")
    rep.total_io_extend_bytes = first.get("total_io_extend_bytes")
    rep.elapsed_s = first.get("elapsed_s", first.get("failed_s", 0.0))
    rep.vectors_030_1_applied_after = first.get("vectors_030_1_applied_after")
    if a.second_boot:
        label = ("resume" if "restarted_mid_queue_s" in first else
                 "reboot" if first["reached_ready"] else "retry")
        # After a graceful or kill restart PG is still up, so the second boot counts from the live LSN read at
        # the restart; after a with-pg restart, a failed boot or a clean stop, from the stopped cluster's checkpoint.
        try:
            base = first.get("restart_lsn") or inst.checkpoint_lsn()
            second = measure_boot(inst, a, env, label, base)
        except HarnessError as exc:
            # A failure of the second boot is its own result; it never overwrites the first boot's.
            second = {"label": label, "reached_ready": False, "harness_error": f"second boot could not be measured: {exc}"}
        second.pop("_indexes", None)
        if second.get("reached_ready") or "phases" in second:
            second["boot_wal_bytes"] = boot_wal_bytes(second)  # reported; gated only as a plain reboot
        if label == "reboot" and second.get("reached_ready"):
            rep.reboot_wal_bytes = second["boot_wal_bytes"]   # the figure --ceiling-reboot gates
        rep.boots.append(second)
        last = second
        if label == "resume":
            rep.resume = resume_analysis(first.get("pci_builds", []), second.get("pci_builds", []),
                                         second.get("_adopted_lines", []), a.restart_mode,
                                         {x["pid"] for x in first.get("index_backends_after_stop") or []})
        second.pop("_adopted_lines", None)
    rep.hnsw_after = last.get("hnsw", 0)
    rep.pci_after = last.get("pci", 0)


def jar_identity(jar: Path | None) -> str | None:
    """sha256 of the engine jar under test, so a verdict names exactly the bytes it ran."""
    if jar is None or not jar.is_file():
        return None
    h = hashlib.sha256()
    with jar.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def build_commit(build_ref: str | None) -> str | None:
    """The commit a gate jar was built from: scripts/build-gate-jar.sh stamps build_ref as <git short sha>+<nonce>."""
    if not build_ref:
        return None
    return build_ref.split("+", 1)[0] or None


def commit_matches(expected: str, built: str | None) -> bool:
    """A full or abbreviated expected commit against the short sha the jar carries."""
    if not built:
        return False
    n = min(len(expected), len(built))
    return n >= 7 and expected[:n].lower() == built[:n].lower()


def boot_wal_bytes(boot: dict) -> int:
    """WAL a boot wrote before it was ready: migrate + ready (PG up to /health 200). A failed boot's figure is
    its span up to the failure, or 0 when PG never answered (no sample); a failed boot is boot-failed whatever
    this says."""
    ph = boot.get("phases") or {}
    if "until_failure" in ph:
        return int(ph["until_failure"].get("wal_bytes", 0))
    if boot.get("reached_ready") and not all("wal_bytes" in (ph.get(k) or {}) for k in ("migrate", "ready")):
        # A ready boot with no measured span must not read as 0 bytes, a silent green.
        raise HarnessError(f"boot {boot.get('label')} reached ready but has no migrate/ready WAL figures: {ph}")
    return sum(int((ph.get(k) or {}).get("wal_bytes", 0)) for k in ("migrate", "ready"))


def ceiling_verdict(boot_wal: int, total_wal: int, ceiling: int | None, ceiling_boot: int | None,
                    reboot_wal: int = 0, ceiling_reboot: int | None = None) -> tuple[str, list[str]]:
    """The byte bounds, decided by Sam on 2026-11-10 (recorded on nexus-f3yqd.1). ``ceiling_boot`` gates the
    UPGRADE boot's migrate + ready (64 MiB on the release gate); ``ceiling`` gates that boot's total through
    settle; ``ceiling_reboot`` gates a plain same-version ``reboot``'s migrate + ready (64 MiB on the release
    gate's same-version leg). A ``resume`` boot is never gated, because a PCI build kept across the restart
    writes its WAL while it migrates; ``--ceiling-reboot`` is refused with ``--restart-mid-queue`` for that
    reason. Returns the verdict and the bounds that tripped."""
    tripped = [name for name, value, bound in (("boot", boot_wal, ceiling_boot), ("reboot", reboot_wal, ceiling_reboot),
                                               ("total", total_wal, ceiling))
               if bound is not None and value > bound]
    if "boot" in tripped:
        return "boot-over-ceiling", tripped
    if "reboot" in tripped:
        return "reboot-over-ceiling", tripped
    if tripped:
        return "over-ceiling", tripped
    gated = any(b is not None for b in (ceiling, ceiling_boot, ceiling_reboot))
    return ("under-ceiling" if gated else "measured"), tripped


def default_jar() -> Path | None:
    jars = sorted(glob.glob(str(REPO / "service" / "target" / "nexus-service-*.jar")))
    jars = [j for j in jars if not j.endswith(("-sources.jar", "-javadoc.jar", "-tests.jar"))]
    return Path(jars[-1]) if jars else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Upgrade-boot write gate (nexus-f3yqd.1).")
    ap.add_argument("--prev-client", default="8.0.0", help="conexus release whose pinned engine builds the old state")
    ap.add_argument("--new-jar", type=Path, default=None, help="engine jar to upgrade to (default service/target)")
    ap.add_argument("--new-jar-commit", default=None,
                    help="commit the jar must have been built from: checked against the build_ref the booted engine "
                         "serves on /version (stamped by scripts/build-gate-jar.sh); a mismatch or no stamp is exit 2")
    ap.add_argument("--collections", type=int, default=3, help="large collections, each above the PCI threshold")
    ap.add_argument("--rows", type=int, default=27_000, help="rows in each large collection")
    ap.add_argument("--control-rows", type=int, default=5_000, help="rows in the control (must stay under the threshold)")
    ap.add_argument("--seed", type=int, default=20261110)
    ap.add_argument("--ceiling", type=int, default=None,
                    help="fail (exit 1, over-ceiling) when the upgrade boot's total WAL, through settle, exceeds this")
    ap.add_argument("--ceiling-boot", type=int, default=None,
                    help="fail (exit 1, boot-over-ceiling) when the upgrade boot's migrate + ready WAL exceeds this")
    ap.add_argument("--ceiling-reboot", type=int, default=None,
                    help="fail (exit 1, reboot-over-ceiling) when the same-version reboot's migrate + ready WAL exceeds "
                         "this; refused with --restart-mid-queue (a resume boot is never gated) and --no-second-boot")
    ap.add_argument("--max-wait", type=float, default=2400.0, help="seconds each boot may take to reach idle")
    ap.add_argument("--idle-window", type=float, default=60.0)
    ap.add_argument("--idle-wal-bytes", type=int, default=1 << 20)
    ap.add_argument("--work", type=Path, default=None, help="work dir (default a new temp dir, removed after)")
    ap.add_argument("--keep", action="store_true", help="keep the work dir")
    ap.add_argument("--json", type=Path, default=None, help="also write the report here")
    ap.add_argument("--rot", default="",
                    help="comma-separated rot shapes to plant before the old stop, or 'all': "
                         + ", ".join(ROT_SHAPES))
    ap.add_argument("--no-pci-wait", dest="pci_wait", action="store_false",
                    help="do not require per-collection indexes on the old engine (engines that predate PCI)")
    ap.add_argument("--no-second-boot", dest="second_boot", action="store_false",
                    help="skip the second boot (a same-version reboot after a good upgrade, a retry after a failed one)")
    ap.add_argument("--interrupt-build", action="store_true",
                    help="old state: kill the old engine while a pci_ index is being built (needs an INVALID pci_ after)")
    ap.add_argument("--restart-mid-queue", type=int, default=0, metavar="K",
                    help="kill the upgrade boot once its Kth pci_ build has started; the second boot then resumes")
    ap.add_argument("--restart-mode", choices=("graceful", "kill", "with-pg"), default="graceful",
                    help="how --restart-mid-queue restarts: nx daemon service stop with PG left up (a user's "
                         "path), SIGKILL of the supervisor and engine, or a stop --with-pg (no build survives)")
    ap.add_argument("--no-reconcile-wait", dest="reconcile_wait", action="store_false",
                    help="do not require a completed PCI reconcile pass before idle (engines without PCI)")
    ap.add_argument("--doctor-capture", type=Path, default=None, metavar="DIR",
                    help="profile each boot's nx doctor (py-spy, DEBUG log, pg_stat_activity every 5 s) into DIR")
    ap.add_argument("--prepare-only", action="store_true",
                    help="build and stop the old state in --work/config and report it; boot nothing new")
    a = ap.parse_args(argv)
    a.rot = list(ROT_SHAPES) if a.rot == "all" else [r for r in a.rot.split(",") if r]
    unknown = [r for r in a.rot if r not in ROT_SHAPES]
    if unknown:
        ap.error(f"unknown rot shape(s) {unknown}; known: {', '.join(ROT_SHAPES)}")
    if a.rot and a.interrupt_build:
        ap.error("--rot and --interrupt-build cannot be combined: the interrupted state is taken before rot is planted")
    if a.ceiling_reboot is not None and (a.restart_mid_queue or not a.second_boot):
        ap.error("--ceiling-reboot gates the same-version reboot: it cannot be combined with --restart-mid-queue "
                 "(that second boot is a resume, which is never gated) or --no-second-boot (there is no reboot)")
    if a.restart_mid_queue and not a.second_boot:
        ap.error("--restart-mid-queue needs the second boot (drop --no-second-boot): the verdict is what it resumed")
    if a.prepare_only and a.work is None:
        ap.error("--prepare-only needs --work, the directory the old state is left in")
    if a.rows < PCI_BUILD_MIN_ROWS or a.control_rows >= PCI_BUILD_MIN_ROWS:
        ap.error(f"--rows must be >= {PCI_BUILD_MIN_ROWS} and --control-rows below it (the engine default)")
    a.new_jar = a.new_jar or default_jar()
    if not a.prepare_only and (a.new_jar is None or not a.new_jar.is_file()):
        ap.error("no engine jar: build one (scripts/mvnw-leased.sh -q -DskipTests package) or pass --new-jar")
    work = a.work or Path(tempfile.mkdtemp(prefix="nx-upgrade-boot-"))
    config = work / "config"
    config.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NX_", "NEXUS_"))}
    env.update({"NEXUS_CONFIG_DIR": str(config), "NX_LOCAL": "1"})
    inst = Install(config)
    rep = Report(prev_client=a.prev_client, new_jar=str(a.new_jar), rows_per_collection=a.rows, ceiling=a.ceiling,
                 ceiling_boot=a.ceiling_boot, ceiling_reboot=a.ceiling_reboot,
                 new_jar_sha256=None if a.prepare_only else jar_identity(a.new_jar))
    rc = 2
    try:
        t_prep = time.monotonic()
        build_old_state(inst, a, env, rep, work)
        rep.prepare_s = round(time.monotonic() - t_prep, 1)
        rep.data_dir_bytes = sum(f.stat().st_size for f in inst.pgdata.rglob("*") if f.is_file())
        if a.prepare_only:
            rep.verdict = "prepared"
            rc = 0
            return rc
        boot_new(inst, a, env, rep)
        rep.new_engine_version = rep.boots[0].get("engine_version") or {}
        rep.new_jar_commit = build_commit(rep.new_engine_version.get("build_ref"))
        if a.new_jar_commit and rep.boots[0].get("reached_ready") and not commit_matches(a.new_jar_commit,
                                                                                         rep.new_jar_commit):
            raise HarnessError(f"the engine under test reports build_ref {rep.new_engine_version.get('build_ref')!r}, "
                               f"not commit {a.new_jar_commit}: build the jar on that tree with "
                               f"scripts/build-gate-jar.sh")
        if a.restart_mid_queue and rep.boots[0].get("reached_ready") and "restarted_mid_queue_s" not in rep.boots[0]:
            raise HarnessError(f"--restart-mid-queue {a.restart_mid_queue} never fired: the upgrade boot showed "
                               f"fewer than {a.restart_mid_queue} pci_ builds, so the restart leg tested nothing")
        ceiling_name, rep.ceilings_tripped = ceiling_verdict(rep.boot_wal_bytes, rep.total_wal_bytes, a.ceiling,
                                                             a.ceiling_boot, rep.reboot_wal_bytes, a.ceiling_reboot)
        failed = [b["label"] for b in rep.boots if not b.get("reached_ready") and "harness_error" not in b]
        unmeasured = [b["harness_error"] for b in rep.boots if "harness_error" in b]
        if unmeasured and rep.boots[0].get("reached_ready"):
            # A good upgrade whose second boot could not be measured is a harness failure, not a red.
            raise HarnessError("; ".join(unmeasured))
        if failed:
            rep.verdict = "boot-failed"
            rep.error = f"boots that never reached ready: {failed}"
            rc = 1
        elif rep.resume and not rep.resume["resume_ok"]:
            rep.verdict = "restart-red"
            rc = 1
        else:
            rep.verdict = ceiling_name
            rc = 1 if rep.ceilings_tripped else 0
    except Exception as exc:  # noqa: BLE001 - every failure to measure is exit 2, never a red or a green
        rc = 2
        rep.verdict = "error"
        rep.error = f"{type(exc).__name__}: {exc}"
    finally:
        if not (a.prepare_only and rep.verdict == "prepared"):
            subprocess.run(["uv", "run", "--project", str(REPO), "nx", "daemon", "service", "stop", "--with-pg"],
                           env=env, cwd=REPO, capture_output=True, text=True)
        if not (a.prepare_only and rep.verdict == "prepared") and (inst.config_dir / "pg_credentials").exists():
            # If the stop above failed, a postmaster may still hold this pgdata: stop it there before any removal.
            try:
                if (inst.pgdata / "postmaster.pid").exists():
                    inst.pg_ctl("stop", "-m", "immediate", "-w")
            except (subprocess.SubprocessError, OSError, KeyError):
                pass
        if not (a.keep or a.work):
            shutil.rmtree(work, ignore_errors=True)
        text = json.dumps(asdict(rep), indent=1)
        sys.stdout.write(text + "\n")
        if a.json:
            a.json.write_text(text + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
