# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed cache for the upgrade rehearsal's preparation phase (release step 6e).

Preparing a start's old store (``upgrade_boot_writes.py --prepare-only``) takes about 13 minutes for the
86,000-row start and about 20 for the five-collection 7.76.0 start, and the result is the same bytes every
time for the same inputs. This module keeps that prepared state on the run host and puts it back in place
of the seeder. It changes no criterion: every judged step runs on the restored store as on a seeded one, and
any doubt about an entry falls back to a full seed. It is not a byte-for-byte copy of a seed, and says so:
see "What a hit changes" below.

What the prepared state is: whatever the seeder wrote under the start's run dir, except the install-prev
client (``home/.local/bin``, ``home/.local/share/uv``) and uv's cache (``home/.cache/uv``), the step logs
and the scratch cwd. In practice that is ``config/`` (the engine binary, the PG bundle, the PG data dir,
``pg_credentials``, the engine logs), ``prepare.json`` and the synthetic repo the seeder indexed.

Entry layout, under the cache root (default ``~/.cache/lab-remote/upgrade-rehearsal-seeds``, or
``$NEXUS_UPGRADE_REHEARSAL_SEED_CACHE``)::

    <from_version>-<kind>-<16 hex of the key>/manifest.json
    <from_version>-<kind>-<16 hex of the key>/payload/<the state, under its run-dir relative paths>

``kind`` is ``real`` or ``standin`` (the prepare template family), so a stand-in run never replaces the
real entry. The key covers the previous release, the engine tag that release pins, the platform, the Python
of the install, the prepare command template in use and its collection arguments, and the sha256 of every
file the seeder reads. At most one entry per (``from_version``, ``kind``) is kept: saving a new key evicts
the old one. An entry not used for ``MAX_AGE_S`` (30 days) is evicted by the next save or restore, which
bounds the entries of releases that are no longer cut. A rejection that may be passing (a lock that was not
free, a full disk, the ceiling) never evicts; one that names a defect in the entry does.

Safety rules, each tested in ``tests/test_upgrade_rehearsal_seed_cache.py``:

* an entry is written to a temp dir and renamed into place, so a reader never sees a partial one;
* a restore verifies the entry's inventory (every path, size and the sha256 of every file) against its
  manifest, refuses a destination that is not empty (or a file that differs), copies, rewrites the old run
  dir in the copy's small text files and symlinks (and fails when any trace is left in a file it scans),
  gives the store a PG port that is free on this host, checks the cluster was cleanly shut down, then starts
  PG alone with ``-o "-p <port>"``, counts the store's rows, requires the count to meet the floor and equal
  the manifest's, and rewrites the catalog's filesystem paths to the new run dir (see below);
* every restore failure scrubs exactly what the restore created and reports a reason, so the caller seeds in
  full; no exception escapes ``restore`` or ``save`` except a signal;
* a save requires the cluster to be cleanly shut down before and after its own row count, and a count at or
  above the floor; otherwise nothing is saved;
* a copy or an inventory that runs past ``ceiling_s`` stops and falls back.

What a hit changes, against a seed:

* the PG port is a fresh free one (as a seed gets), written to ``pg_credentials`` (``PG_PORT`` and the
  ``NX_DB_*URL`` lines) and the ``port =`` line of ``postgresql*.conf``; a file under ``config`` (not a log)
  that still names the old port is a refusal;
* the run dir in text files and symlinks is the new one;
* in the store, every row of a ``repo_root``, ``source_uri``, ``file_path`` or ``repo_path`` text column of
  the ``nexus`` schema that names the saved run dir is rewritten by SQL (PG started alone), the row counts
  per column must equal those recorded at save, and none may name the old run dir afterwards. Chunk
  titles, chunk metadata and the engine and PG logs keep the old run dir as text; nothing judged reads them;
* the store is as old as its entry (the table label says when it was saved).
"""

from __future__ import annotations

import calendar
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

SCHEMA: int = 2
CACHE_ENV: str = "NEXUS_UPGRADE_REHEARSAL_SEED_CACHE"
LOCK_NAME: str = ".lock"
MANIFEST: str = "manifest.json"
PAYLOAD: str = "payload"
#: Files up to this size are scanned for the old run dir and the old PG port on restore.
SMALL_FILE_BYTES: int = 1 << 20
#: Under the PG data dir these hold relation files and WAL: never text, never scanned.
PG_BULK_DIRS: tuple[str, ...] = ("base", "pg_wal")
#: Run-dir top-level entries that are not prepared state.
RUN_DIR_EXCLUDED: frozenset[str] = frozenset({"home", "logs", "cwd", "verdict.json"})
#: Paths under ``home`` that install-prev and uv write, not the seeder.
HOME_EXCLUDED: frozenset[str] = frozenset(
    {".local/bin", ".local/share/uv", ".cache/uv"}
)
STALE_TMP_S: float = 6 * 3600.0
#: An entry unused this long is evicted by the next save or restore.
MAX_AGE_S: float = 30 * 86400.0
LOCK_TIMEOUT_S: float = 120.0
#: The longest a restore or a save may spend inventorying and copying before it gives up (a seed takes
#: 13 to 20 minutes; a copy of 2.6 GB takes a small fraction of that).
CEILING_S: float = 900.0
#: Text columns of the ``nexus`` schema the rehearsal reads as filesystem paths (doctor's dead-owner check,
#: the catalog reconcile rungs). ``file_path`` is relative in a seed and is listed so a surprise shows.
PATH_COLUMNS: tuple[str, ...] = ("repo_root", "source_uri", "file_path", "repo_path")
TS_FORMAT: str = "%Y-%m-%dT%H:%M:%SZ"

Log = Callable[[str], None]


class SeedCacheError(Exception):
    """A cache operation failed; the caller falls back to a full seed."""


class TransientError(SeedCacheError):
    """A failure that says nothing about the entry (the ceiling, a lock): the entry is not evicted."""


class RelocationError(SeedCacheError):
    """The old run dir could not be removed from the restored copy."""


# --- where, and under which key ----------------------------------------------------------------


def default_cache_root(
    environ: dict[str, str] | None = None, home: Path | None = None
) -> Path:
    env = os.environ if environ is None else environ
    if env.get(CACHE_ENV):
        return Path(env[CACHE_ENV])
    base = (
        Path(env["XDG_CACHE_HOME"])
        if env.get("XDG_CACHE_HOME")
        else (home or Path.home()) / ".cache"
    )
    return base / "lab-remote" / "upgrade-rehearsal-seeds"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(4 << 20), b""):
            h.update(block)
    return h.hexdigest()


def hash_inputs(paths: Sequence[Path]) -> tuple[tuple[str, str], ...]:
    """(file name, sha256) of every file the seeder reads, in a fixed order."""
    return tuple(sorted((p.name, sha256_file(p)) for p in paths))


def platform_tag() -> str:
    return f"{sys.platform}-{platform.machine()}"


def _iso(t: float) -> str:
    return time.strftime(TS_FORMAT, time.gmtime(t))


def _parse_iso(text: str) -> float:
    return float(calendar.timegm(time.strptime(text, TS_FORMAT)))


@dataclasses.dataclass(frozen=True)
class KeyInputs:
    from_version: str
    kind: str  # the prepare template family: "real" or "standin"
    engine_pin: str  # the engine tag the previous release pins, "0.1.156"
    python: str
    prepare_template: str  # the command template in use, placeholders unexpanded
    collections: int  # the pci collection argument the template is given
    seeder: tuple[
        tuple[str, str], ...
    ]  # (file name, sha256) of the seeder and its fixture inputs
    platform: str

    def as_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["seeder"] = [list(x) for x in self.seeder]
        return d

    def digest(self) -> str:
        return hashlib.sha256(
            json.dumps(self.as_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()

    @property
    def group(self) -> str:
        """What one entry is kept per: the start and the template family."""
        return f"{self.from_version}-{self.kind}"

    @property
    def name(self) -> str:
        return f"{self.group}-{self.digest()[:16]}"


class _Deadline:
    """The ceiling on an inventory plus a copy: checked per file, so a slow disk stops it."""

    def __init__(self, ceiling_s: float, what: str) -> None:
        self.ceiling_s = ceiling_s
        self.what = what
        self.at = time.monotonic() + ceiling_s

    def check(self) -> None:
        if time.monotonic() >= self.at:
            raise TransientError(
                f"{self.what} ran past its ceiling of {self.ceiling_s:g} s"
            )


# --- what the prepared state is ----------------------------------------------------------------


def payload_relpaths(run_dir: Path) -> list[str]:
    """Run-dir relative paths of what the seeder left: top-level entries other than home, logs and cwd,
    and the entries under ``home`` other than the install-prev client and uv's cache."""
    out: list[str] = []
    for child in sorted(run_dir.iterdir()):
        if child.name not in RUN_DIR_EXCLUDED:
            out.append(child.name)
    home = run_dir / "home"

    def visit(d: Path, rel: str) -> None:
        for child in sorted(d.iterdir()):
            r = f"{rel}/{child.name}" if rel else child.name
            if r in HOME_EXCLUDED:
                continue
            is_parent = any(e.startswith(r + "/") for e in HOME_EXCLUDED)
            if is_parent and child.is_dir() and not child.is_symlink():
                visit(child, r)
            else:
                out.append(f"home/{r}")

    if home.is_dir():
        visit(home, "")
    return sorted(out)


@dataclasses.dataclass(frozen=True)
class Inventory:
    digest: str
    files: int
    bytes: int


def inventory(
    root: Path, relpaths: Sequence[str], deadline: _Deadline | None = None
) -> Inventory:
    """A digest of the tree: every path, its kind, size and the sha256 of every file. The hashes catch a
    same-size corruption of a relation file or the engine binary as well as a truncated or missing one."""
    h = hashlib.sha256()
    files = total = 0

    def line(*parts: str) -> None:
        h.update(("\t".join(parts) + "\n").encode("utf-8"))

    def walk(p: Path, rel: str) -> None:
        nonlocal files, total
        if deadline is not None:
            deadline.check()
        if p.is_symlink():
            line(rel, "L", os.readlink(p))
        elif p.is_dir():
            line(rel, "D")
            for c in sorted(p.iterdir()):
                walk(c, f"{rel}/{c.name}")
        else:
            size = p.stat().st_size
            files += 1
            total += size
            line(rel, "F", str(size), sha256_file(p))

    for rel in sorted(relpaths):
        p = root / rel
        if not (p.exists() or p.is_symlink()):
            raise SeedCacheError(f"{rel} is missing under {root}")
        walk(p, rel)
    return Inventory(h.hexdigest(), files, total)


# --- relocation --------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Relocated:
    files: int
    links: int


def relocate(
    paths: Sequence[Path],
    pairs: Sequence[tuple[str, str]],
    *,
    skip_dirs: Sequence[Path] = (),
) -> Relocated:
    """Replace each old run-dir string by the new one in the small files and symlinks under ``paths``,
    then scan again: any file that still names an old run dir, or a binary file that names one (whatever
    the lengths), is a RelocationError. ``skip_dirs`` (the PG relation and WAL dirs) are not scanned."""
    pairs = sorted({p for p in pairs if p[0] != p[1]}, key=lambda p: -len(p[0]))
    skips = {d.resolve() if d.exists() else d for d in skip_dirs}
    files = links = 0
    if not pairs:
        return Relocated(0, 0)

    def rewrite_file(p: Path) -> bool:
        data = p.read_bytes()
        new = data
        for old, repl in pairs:
            new = new.replace(old.encode(), repl.encode())
        if new == data:
            return False
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RelocationError(
                f"{p} is binary and names the old run dir: only a text file is rewritten ({exc})"
            ) from exc
        st = p.stat()
        tmp = p.with_name(f".{p.name}.reloc")
        tmp.write_bytes(new)
        os.chmod(tmp, st.st_mode & 0o7777)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, p)
        return True

    def walk(p: Path) -> None:
        nonlocal files, links
        if p.is_symlink():
            target = os.readlink(p)
            new = target
            for old, repl in pairs:
                new = new.replace(old, repl)
            if new != target:
                p.unlink()
                os.symlink(new, p)
                links += 1
            return
        if p.is_dir():
            if p.resolve() in skips:
                return
            for c in sorted(p.iterdir()):
                walk(c)
            return
        if p.stat().st_size <= SMALL_FILE_BYTES and rewrite_file(p):
            files += 1

    def scan(p: Path) -> None:
        if p.is_symlink():
            target = os.readlink(p)
            if any(old in target for old, _ in pairs):
                raise RelocationError(
                    f"symlink {p} still points into the old run dir: {target}"
                )
            return
        if p.is_dir():
            if p.resolve() in skips:
                return
            for c in sorted(p.iterdir()):
                scan(c)
            return
        if p.stat().st_size <= SMALL_FILE_BYTES:
            data = p.read_bytes()
            if any(old.encode() in data for old, _ in pairs):
                raise RelocationError(
                    f"{p} still names the old run dir after the rewrite"
                )

    for p in paths:
        walk(p)
    for p in paths:
        scan(p)
    return Relocated(files, links)


# --- the PG port ---------------------------------------------------------------------------------


def free_port() -> int:
    """A port nothing holds right now, from the kernel (as the provisioner picks one)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def rewrite_port(config: Path, pgdata: Path, old: int, new: int) -> None:
    """Give the restored store a new PG port: ``PG_PORT`` and the host:port of the ``NX_DB_*URL`` lines of
    ``pg_credentials``, and the ``port =`` line of ``postgresql*.conf``. The seeded state records the port
    nowhere else (``pg_provision`` writes those two); a file under ``config`` that is not a log and still
    names the old port as ``127.0.0.1:<port>``, ``localhost:<port>``, ``PG_PORT=<port>`` or ``port = <port>``
    is a SeedCacheError, so a place this list missed refuses the restore instead of splitting the store
    from its port."""
    if old == new:
        return
    creds_path = config / "pg_credentials"
    text = creds_path.read_text(encoding="utf-8")
    text = re.sub(rf"(?m)^(PG_PORT\s*=\s*){old}\s*$", rf"\g<1>{new}", text)
    text = re.sub(rf"(127\.0\.0\.1:|localhost:){old}\b", rf"\g<1>{new}", text)
    mode = creds_path.stat().st_mode & 0o7777
    creds_path.write_text(text, encoding="utf-8")
    os.chmod(creds_path, mode)
    if parse_credentials(text).get("PG_PORT") != str(new):
        raise SeedCacheError(
            f"pg_credentials does not carry PG_PORT={new} after the rewrite"
        )
    for conf in sorted(pgdata.glob("postgresql*.conf")):
        body = conf.read_text(encoding="utf-8")
        fixed = re.sub(rf"(?m)^(\s*port\s*=\s*){old}\b", rf"\g<1>{new}", body)
        if fixed != body:
            conf_mode = conf.stat().st_mode & 0o7777
            conf.write_text(fixed, encoding="utf-8")
            os.chmod(conf, conf_mode)
    leftover = re.compile(
        rf"(?:127\.0\.0\.1|localhost):{old}\b|PG_PORT\s*=\s*{old}\b|(?m:^\s*port\s*=\s*{old}\b)"
    )
    bulk = {pgdata / d for d in PG_BULK_DIRS}
    for dirpath, dirnames, filenames in os.walk(config):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if (here / d) not in bulk and d != "logs"]
        for name in filenames:
            p = here / name
            if name.endswith(".log") or p.is_symlink():
                continue
            if p.stat().st_size <= SMALL_FILE_BYTES and leftover.search(
                p.read_text(encoding="utf-8", errors="ignore")
            ):
                raise SeedCacheError(f"{p} still names the old PG port {old}")


# --- the PG cluster, stopped ---------------------------------------------------------------------


def parse_credentials(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, v = ln.partition("=")
            out[k.strip()] = v.strip()
    return out


def read_credentials(config: Path) -> dict[str, str]:
    try:
        return parse_credentials(
            (config / "pg_credentials").read_text(encoding="utf-8")
        )
    except OSError as exc:
        raise SeedCacheError(
            f"cannot read pg_credentials under {config}: {exc}"
        ) from exc


def _bundle(config: Path) -> Path:
    return config / "pg-bundle" / "bundle"


def _pg_env(config: Path) -> dict[str, str]:
    lib = _bundle(config) / "lib"
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "LD_LIBRARY_PATH": f"{lib}:{os.environ.get('LD_LIBRARY_PATH', '')}".rstrip(":"),
    }


def stopped_clean(config: Path) -> str | None:
    """None when the cluster is cleanly shut down (pg_controldata says so and no postmaster.pid is left),
    else why not."""
    creds = read_credentials(config)
    try:
        pgdata = Path(creds["PG_DATA"])
    except KeyError as exc:
        raise SeedCacheError("pg_credentials has no PG_DATA") from exc
    if (pgdata / "postmaster.pid").exists():
        return f"{pgdata / 'postmaster.pid'} exists: the cluster is running or was not stopped"
    try:
        r = subprocess.run(
            [str(_bundle(config) / "bin" / "pg_controldata"), str(pgdata)],
            env=_pg_env(config),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"pg_controldata did not run: {exc}"
    if r.returncode != 0:
        return f"pg_controldata exit {r.returncode}: {r.stderr.strip()[:200]}"
    state = ""
    for ln in r.stdout.splitlines():
        if ln.startswith("Database cluster state:"):
            state = ln.split(":", 1)[1].strip()
    return (
        None if state == "shut down" else f"cluster state is {state!r}, not 'shut down'"
    )


def rows_with_pg_up(config: Path, work: Callable[[], object]):
    """Start the cluster alone (as the engine does, ``-o "-p <PG_PORT>"``), run ``work()`` against it, stop
    it with a fast shutdown and require the cluster to read 'shut down' again. A start that fails or times
    out is followed by a forced stop, so a postmaster is never left over a data dir the caller scrubs."""
    creds = read_credentials(config)
    pgdata = Path(creds["PG_DATA"])
    try:
        port = int(creds["PG_PORT"])
    except (KeyError, ValueError) as exc:
        raise SeedCacheError("pg_credentials has no usable PG_PORT") from exc
    pg_ctl = str(_bundle(config) / "bin" / "pg_ctl")
    env = _pg_env(config)

    def force_stop() -> None:
        with contextlib.suppress(Exception):
            subprocess.run(
                [
                    pg_ctl,
                    "-D",
                    str(pgdata),
                    "-m",
                    "immediate",
                    "-w",
                    "-t",
                    "60",
                    "stop",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )

    fd, logpath = tempfile.mkstemp(prefix="seed-cache-pg-", suffix=".log")
    os.close(fd)
    try:
        try:
            r = subprocess.run(
                [
                    pg_ctl,
                    "-D",
                    str(pgdata),
                    "-w",
                    "-t",
                    "300",
                    "-l",
                    logpath,
                    "-o",
                    f"-p {port}",
                    "start",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=330,
            )
        except BaseException:
            force_stop()
            raise
        if r.returncode != 0:
            force_stop()
            raise SeedCacheError(
                f"pg_ctl start failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:300]}"
            )
        try:
            result = work()
        finally:
            s = subprocess.run(
                [pg_ctl, "-D", str(pgdata), "-m", "fast", "-w", "-t", "300", "stop"],
                env=env,
                capture_output=True,
                text=True,
                timeout=330,
            )
            if s.returncode != 0:
                force_stop()
                raise SeedCacheError(
                    f"pg_ctl stop failed (exit {s.returncode}): {(s.stderr or s.stdout).strip()[:300]}"
                )
        why = stopped_clean(config)
        if why:
            raise SeedCacheError(f"after the row count: {why}")
        return result
    finally:
        with contextlib.suppress(OSError):
            os.unlink(logpath)


# --- the store, through the install's own PG ---------------------------------------------------------

SqlFn = Callable[[str], "list[list[str]]"]


@dataclasses.dataclass(frozen=True)
class StoreRead:
    rows: int  # nexus.chunks
    path_rows: dict[
        str, int
    ]  # "<table>.<column>" -> rows whose value names a saved run dir


@dataclasses.dataclass(frozen=True)
class StoreRewrite:
    rows: int
    before: dict[
        str, int
    ]  # rows naming an old run dir, per path column, before the rewrite
    after: dict[str, int]  # the same afterwards: every value must be 0


def _lit(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _ident(name: str) -> str:
    if '"' in name:
        raise SeedCacheError(f"unusable identifier {name!r}")
    return f'"{name}"'


def _rows_of(sql: SqlFn) -> int:
    out = sql("select count(*)::bigint from nexus.chunks")
    return int(out[0][0])


def path_columns(sql: SqlFn) -> list[tuple[str, str]]:
    cols = ", ".join(_lit(c) for c in PATH_COLUMNS)
    out = sql(
        "select table_name, column_name from information_schema.columns "
        "where table_schema = 'nexus' and data_type in ('text', 'character varying') "
        f"and column_name in ({cols}) order by 1, 2"
    )
    return [(r[0], r[1]) for r in out]


def _naming(sql: SqlFn, table: str, column: str, olds: Sequence[str]) -> int:
    cond = " or ".join(f"position({_lit(o)} in {_ident(column)}) > 0" for o in olds)
    out = sql(f"select count(*)::bigint from nexus.{_ident(table)} where {cond}")
    return int(out[0][0])


def read_store(sql: SqlFn, olds: Sequence[str]) -> StoreRead:
    """The rows and, per path column, how many values name ``olds`` (the run dir the store was seeded in)."""
    return StoreRead(
        _rows_of(sql),
        {f"{t}.{c}": _naming(sql, t, c, olds) for t, c in path_columns(sql)},
    )


def rewrite_store(sql: SqlFn, pairs: Sequence[tuple[str, str]]) -> StoreRewrite:
    """Replace each old run dir by the new one in every path column that names one, counting before and
    after. Longest old string first, as the file rewrite does."""
    all_olds = sorted({p[0] for p in pairs})
    changing = sorted({p for p in pairs if p[0] != p[1]}, key=lambda p: -len(p[0]))
    olds = [p[0] for p in changing]
    cols = path_columns(sql)
    before = (
        {f"{t}.{c}": _naming(sql, t, c, all_olds) for t, c in cols} if all_olds else {}
    )
    for t, c in cols:
        if not before.get(f"{t}.{c}"):
            continue
        for old, new in changing:
            sql(
                f"update nexus.{_ident(t)} set {_ident(c)} = replace({_ident(c)}, {_lit(old)}, {_lit(new)}) "
                f"where position({_lit(old)} in {_ident(c)}) > 0"
            )
    # a reused run dir names itself: nothing to rewrite and nothing left that should not be there
    after = {f"{t}.{c}": (_naming(sql, t, c, olds) if olds else 0) for t, c in cols}
    return StoreRewrite(_rows_of(sql), before, after)


# --- the cache -----------------------------------------------------------------------------------


@contextlib.contextmanager
def _locked(root: Path, *, exclusive: bool, timeout_s: float) -> Iterator[bool]:
    """flock on ``<root>/.lock``: shared for a restore, exclusive for a rename-in and an eviction.
    Yields False when the lock was not got within ``timeout_s``."""
    fd = os.open(root / LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    got = False
    try:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(
                    fd,
                    (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB,
                )
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield got
    finally:
        if got:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _copy_tree(src: Path, dst: Path, deadline: _Deadline | None = None) -> None:
    if deadline is not None:
        deadline.check()
    if src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.readlink(src), dst)
    elif src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for c in sorted(src.iterdir()):
            _copy_tree(c, dst / c.name, deadline)
        shutil.copystat(src, dst)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _remove(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p, ignore_errors=True)


def _same_file(a: Path, b: Path) -> bool:
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    if a.is_dir() or b.is_dir():
        return False
    return sha256_file(a) == sha256_file(b)


@dataclasses.dataclass
class _Undo:
    """What a restore created, and so all a failed restore may remove."""

    created: list[Path] = dataclasses.field(default_factory=list)
    emptied: list[Path] = dataclasses.field(
        default_factory=list
    )  # empty dirs the run had; children are ours

    def scrub(self) -> None:
        for p in self.created:
            _remove(p)
        for d in self.emptied:
            if d.is_dir() and not d.is_symlink():
                for c in d.iterdir():
                    _remove(c)


@dataclasses.dataclass
class Restore:
    ok: bool
    reason: str = ""  # why the entry was not used (a miss says "no entry")
    prepare: dict | None = None
    rows: int | None = None
    relocated: Relocated | None = None
    saved_at: str = ""  # when the entry was saved, ISO UTC
    path_rows: dict[str, int] = dataclasses.field(
        default_factory=dict
    )  # rows rewritten, per column
    port: int | None = None  # the PG port the restored store now has
    seconds: float = 0.0
    transient: bool = (
        False  # the rejection says nothing about the entry: it is not evicted
    )


@dataclasses.dataclass
class Save:
    saved: bool
    reason: str = ""
    name: str = ""
    rows: int | None = None
    seconds: float = 0.0


class SeedCache:
    """The entries under ``root``.

    ``read_fn(config, olds)`` returns a ``StoreRead`` and ``rewrite_fn(config, pairs)`` a ``StoreRewrite``,
    each with the store's PG started alone (``rows_with_pg_up`` around ``read_store`` / ``rewrite_store`` in
    production). ``pick_port()`` gives the restored store its PG port; ``clock`` is the time source for the
    manifest's ``saved_at`` and ``last_used_at`` and for the 30-day eviction.
    """

    def __init__(
        self,
        root: Path,
        *,
        read_fn: Callable[[Path, Sequence[str]], StoreRead],
        rewrite_fn: Callable[[Path, Sequence[tuple[str, str]]], StoreRewrite],
        log: Log = lambda _m: None,
        pick_port: Callable[[], int] = free_port,
        clean_check: Callable[[Path], str | None] = stopped_clean,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        ceiling_s: float = CEILING_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root)
        self.read_fn = read_fn
        self.rewrite_fn = rewrite_fn
        self.log = log
        self.pick_port = pick_port
        self.clean_check = clean_check
        self.lock_timeout_s = lock_timeout_s
        self.ceiling_s = ceiling_s
        self.clock = clock

    def entry(self, inputs: KeyInputs) -> Path:
        return self.root / inputs.name

    # -- hit ---------------------------------------------------------------------------------

    def restore(self, inputs: KeyInputs, run_dir: Path, *, floor: int) -> Restore:
        """Put the entry's prepared state under ``run_dir``. Never raises (a signal apart): any failure
        scrubs what was restored and returns ``ok=False`` with a reason; an entry that is defective, as
        opposed to a restore that could not finish (a lock, a full disk, the ceiling), is evicted."""
        t0 = time.monotonic()
        entry = self.entry(inputs)
        if not entry.is_dir():
            return Restore(False, "no entry", seconds=time.monotonic() - t0)
        ident = (entry.stat().st_ino, entry.stat().st_mtime_ns)
        undo = _Undo()
        try:
            with _locked(
                self.root, exclusive=False, timeout_s=self.lock_timeout_s
            ) as got:
                if not got:
                    res = Restore(
                        False, "the cache lock was not free in time", transient=True
                    )
                else:
                    res = self._restore_locked(inputs, entry, run_dir, floor, undo)
        except (TransientError, OSError) as exc:
            res = Restore(False, f"{type(exc).__name__}: {exc}", transient=True)
        except Exception as exc:  # noqa: BLE001 - ProbeError and every other failure of the PG phase
            res = Restore(False, f"{type(exc).__name__}: {exc}")
        if not res.ok:
            undo.scrub()
            if not res.transient:
                self._evict_entry(entry, ident)
        else:
            self._sweep(keep=entry.name)
        res.seconds = time.monotonic() - t0
        return res

    def _restore_locked(
        self,
        inputs: KeyInputs,
        entry: Path,
        run_dir: Path,
        floor: int,
        undo: _Undo,
    ) -> Restore:
        deadline = _Deadline(self.ceiling_s, "the restore")
        try:
            man = json.loads((entry / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return Restore(False, f"manifest unreadable: {exc}")
        if man.get("schema") != SCHEMA:
            return Restore(
                False, f"manifest schema {man.get('schema')!r}, expected {SCHEMA}"
            )
        if (
            man.get("inputs_digest") != inputs.digest()
            or man.get("key_inputs") != inputs.as_dict()
        ):
            return Restore(False, "the entry's key inputs differ from this run's")
        payload = entry / PAYLOAD
        relpaths = man.get("paths")
        if not isinstance(relpaths, list) or not relpaths or not payload.is_dir():
            return Restore(False, "the entry has no payload")
        try:
            inv = inventory(payload, relpaths, deadline)
        except SeedCacheError as exc:
            if isinstance(exc, TransientError):
                raise
            return Restore(False, f"payload unreadable or incomplete: {exc}")
        if (inv.digest, inv.files, inv.bytes) != (
            man.get("inventory_sha256"),
            man.get("files"),
            man.get("bytes"),
        ):
            return Restore(
                False, "payload does not match its manifest (corrupt or partial entry)"
            )
        try:
            prepare = json.loads((payload / "prepare.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return Restore(False, f"prepare.json unreadable: {exc}")
        if prepare.get("verdict") != "prepared":
            return Restore(False, f"prepare.json verdict is {prepare.get('verdict')!r}")
        old_port = man.get("pg_port")
        if not isinstance(old_port, int):
            return Restore(False, f"the manifest's PG port is {old_port!r}")

        # The destination must be what the seeder starts from: absent, or an empty dir, or a file that
        # is already what the payload holds (install-prev writes a few). Nothing else is merged into.
        skip: set[str] = set()
        for rel in relpaths:
            dst, src = run_dir / rel, payload / rel
            if not (dst.exists() or dst.is_symlink()):
                top = dst
                while (
                    not (top.parent.exists() or top.parent.is_symlink())
                    and top.parent != run_dir
                ):
                    top = top.parent
                undo.created.append(top)
            elif dst.is_dir() and not dst.is_symlink():
                if not src.is_dir() or src.is_symlink():
                    return Restore(
                        False,
                        f"{dst} is a dir the payload holds as a file",
                        transient=True,
                    )
                if any(dst.iterdir()):
                    return Restore(
                        False,
                        f"{dst} is not empty: a restore is not merged into an existing tree",
                        transient=True,
                    )
                undo.emptied.append(dst)
            elif _same_file(dst, src):
                skip.add(rel)
            else:
                return Restore(
                    False,
                    f"{dst} already exists and is not what the entry holds",
                    transient=True,
                )

        for rel in relpaths:
            if rel not in skip:
                _copy_tree(payload / rel, run_dir / rel, deadline)
        pairs = [
            (str(man["source_run_dir"]), str(run_dir)),
            (str(man["source_run_dir_resolved"]), str(run_dir.resolve())),
        ]
        config = run_dir / "config"
        pgdata = run_dir / str(man["pg_data_rel"])
        relocated = relocate(
            [run_dir / rel for rel in relpaths],
            pairs,
            skip_dirs=[pgdata / d for d in PG_BULK_DIRS],
        )
        creds = read_credentials(config)
        if Path(creds.get("PG_DATA", "")).resolve() != pgdata.resolve():
            return Restore(
                False,
                f"PG_DATA is {creds.get('PG_DATA')!r} after relocation, not {pgdata}",
            )
        if creds.get("PG_PORT") != str(old_port):
            return Restore(
                False,
                f"PG_PORT is {creds.get('PG_PORT')!r}, the manifest says {old_port}",
            )
        new_port = self.pick_port()
        rewrite_port(config, pgdata, old_port, new_port)
        why = self.clean_check(config)
        if why:
            return Restore(
                False, f"the restored cluster is not cleanly shut down: {why}"
            )
        try:
            got = self.rewrite_fn(config, pairs)
        except Exception as exc:  # noqa: BLE001 - psql, ProbeError, pg_ctl: the store could not be read
            return Restore(
                False,
                f"the store could not be read with its PG started alone: {type(exc).__name__}: {exc}",
            )
        if got.rows < floor:
            return Restore(
                False,
                f"the restored store holds {got.rows} rows, below the floor of {floor}",
                rows=got.rows,
            )
        if got.rows != man.get("rows"):
            return Restore(
                False,
                f"the restored store holds {got.rows} rows, the manifest says {man.get('rows')}",
                rows=got.rows,
            )
        if got.before != man.get("path_rows"):
            return Restore(
                False,
                f"the path columns name the saved run dir in {got.before}, the manifest says {man.get('path_rows')}",
            )
        left = {k: v for k, v in got.after.items() if v}
        if left:
            return Restore(
                False, f"rows still name the saved run dir after the rewrite: {left}"
            )
        why = self.clean_check(config)
        if why:
            return Restore(
                False,
                f"the restored cluster is not cleanly shut down after the count: {why}",
            )
        self._touch(entry, man)
        return Restore(
            True,
            prepare=prepare,
            rows=got.rows,
            relocated=relocated,
            saved_at=str(man.get("saved_at", "")),
            path_rows=dict(got.before),
            port=new_port,
        )

    def _touch(self, entry: Path, man: dict) -> None:
        """Record this use, atomically, so the 30-day eviction counts from it."""
        man = dict(man, last_used_at=_iso(self.clock()))
        tmp = entry / f".{MANIFEST}.tmp"
        with contextlib.suppress(OSError):
            tmp.write_text(json.dumps(man, indent=1) + "\n", encoding="utf-8")
            os.replace(tmp, entry / MANIFEST)

    # -- miss --------------------------------------------------------------------------------

    def save(self, inputs: KeyInputs, run_dir: Path, *, floor: int) -> Save:
        """Copy the prepared state in ``run_dir`` into the cache. Never raises (a signal apart): a state
        that cannot be saved is reported and the run goes on with the store it has."""
        t0 = time.monotonic()
        try:
            res = self._save(inputs, run_dir, floor)
        except Exception as exc:  # noqa: BLE001 - a failed save must never fail a seeded run
            res = Save(False, f"{type(exc).__name__}: {exc}", name=inputs.name)
        res.seconds = time.monotonic() - t0
        return res

    def _save(self, inputs: KeyInputs, run_dir: Path, floor: int) -> Save:
        name = inputs.name
        deadline = _Deadline(self.ceiling_s, "the save")
        config = run_dir / "config"
        creds = read_credentials(config)
        pgdata = Path(creds["PG_DATA"]).resolve()
        if not (str(pgdata) + os.sep).startswith(str(run_dir.resolve()) + os.sep):
            return Save(False, f"the store at {pgdata} is outside {run_dir}", name=name)
        try:
            pg_port = int(creds["PG_PORT"])
        except (KeyError, ValueError):
            return Save(False, "pg_credentials has no usable PG_PORT", name=name)
        why = self.clean_check(config)
        if why:
            return Save(
                False, f"the cluster was not cleanly shut down: {why}", name=name
            )
        prepare_path = run_dir / "prepare.json"
        prepare = json.loads(prepare_path.read_text(encoding="utf-8"))
        if prepare.get("verdict") != "prepared":
            return Save(
                False, f"prepare verdict is {prepare.get('verdict')!r}", name=name
            )
        olds = sorted({str(run_dir), str(run_dir.resolve())})
        read = self.read_fn(config, olds)
        if read.rows < floor:
            return Save(
                False,
                f"the store holds {read.rows} rows, below the floor of {floor}",
                name=name,
                rows=read.rows,
            )
        relpaths = payload_relpaths(run_dir)
        if "prepare.json" not in relpaths or "config" not in relpaths:
            return Save(
                False,
                f"the run dir holds no config or prepare.json to save ({relpaths})",
                name=name,
            )
        pg_rel = pgdata.relative_to(run_dir.resolve()).as_posix()
        if not any(pg_rel == r or pg_rel.startswith(r + "/") for r in relpaths):
            return Save(
                False,
                f"the PG data dir {pg_rel} is not in the prepared state",
                name=name,
            )
        src_inv = inventory(run_dir, relpaths, deadline)

        self.root.mkdir(parents=True, exist_ok=True)
        self._drop_stale_tmp()
        tmp: Path | None = Path(tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=self.root))
        try:
            payload = tmp / PAYLOAD
            payload.mkdir()
            for rel in relpaths:
                _copy_tree(run_dir / rel, payload / rel, deadline)
            copy_inv = inventory(payload, relpaths, deadline)
            if copy_inv != src_inv:
                return Save(
                    False,
                    "the copy does not match the source (the run dir changed while copying)",
                    name=name,
                )
            now = _iso(self.clock())
            manifest = {
                "schema": SCHEMA,
                "key": name,
                "kind": inputs.kind,
                "key_inputs": inputs.as_dict(),
                "inputs_digest": inputs.digest(),
                "source_run_dir": str(run_dir),
                "source_run_dir_resolved": str(run_dir.resolve()),
                "paths": relpaths,
                "pg_data_rel": pg_rel,
                "pg_port": pg_port,
                "rows": read.rows,
                "path_rows": read.path_rows,
                "collections": prepare.get("collections") or {},
                "inventory_sha256": copy_inv.digest,
                "files": copy_inv.files,
                "bytes": copy_inv.bytes,
                "saved_at": now,
                "last_used_at": now,
            }
            (tmp / MANIFEST).write_text(
                json.dumps(manifest, indent=1) + "\n", encoding="utf-8"
            )
            with _locked(
                self.root, exclusive=True, timeout_s=self.lock_timeout_s
            ) as got:
                if not got:
                    return Save(
                        False,
                        "the cache lock was not free in time",
                        name=name,
                        rows=read.rows,
                    )
                final = self.root / name
                if final.exists():
                    return Save(
                        False,
                        "another run saved this key first; kept its entry",
                        name=name,
                        rows=read.rows,
                    )
                os.rename(tmp, final)
                tmp = None
                self._evict_others(inputs.group, name)
                self._evict_stale(keep=name)
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        return Save(True, name=name, rows=read.rows)

    # -- bounded size --------------------------------------------------------------------------

    def _evict(self, d: Path) -> None:
        gone = self.root / f".evict-{d.name}-{os.getpid()}-{time.monotonic_ns()}"
        os.rename(d, gone)
        shutil.rmtree(gone, ignore_errors=True)

    def _evict_entry(self, entry: Path, ident: tuple[int, int]) -> None:
        """Remove a defective entry, unless it was replaced since this run read it. Best effort: a lock
        that is not free leaves it for the next run to reject again."""
        try:
            with _locked(
                self.root, exclusive=True, timeout_s=min(self.lock_timeout_s, 30.0)
            ) as got:
                if not got or not entry.is_dir():
                    return
                st = entry.stat()
                if (st.st_ino, st.st_mtime_ns) != ident:
                    return
                self._evict(entry)
                self.log(f"seed cache: evicted {entry.name} (rejected as defective)")
        except OSError:
            return

    def _evict_others(self, group: str, keep: str) -> list[str]:
        """At most one entry per start and template family: remove every other key's. Caller holds the
        exclusive lock."""
        pat = re.compile(rf"^{re.escape(group)}-[0-9a-f]{{16}}$")
        gone: list[str] = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and d.name != keep and pat.match(d.name):
                self._evict(d)
                gone.append(d.name)
                self.log(f"seed cache: evicted {d.name} (one entry per start)")
        return gone

    def _last_used(self, d: Path) -> float:
        try:
            man = json.loads((d / MANIFEST).read_text(encoding="utf-8"))
            return _parse_iso(str(man.get("last_used_at") or man["saved_at"]))
        except (OSError, ValueError, KeyError):
            return d.stat().st_mtime

    def _evict_stale(self, *, keep: str) -> list[str]:
        """Remove every entry unused for ``MAX_AGE_S``. Caller holds the exclusive lock."""
        gone: list[str] = []
        now = self.clock()
        for d in sorted(self.root.iterdir()):
            if not d.is_dir() or d.name.startswith(".") or d.name == keep:
                continue
            if now - self._last_used(d) > MAX_AGE_S:
                self._evict(d)
                gone.append(d.name)
                self.log(f"seed cache: evicted {d.name} (unused for 30 days)")
        return gone

    def _sweep(self, *, keep: str) -> None:
        """The age eviction for a run that only restores: when no other run holds the cache."""
        with contextlib.suppress(OSError):
            with _locked(self.root, exclusive=True, timeout_s=0.0) as got:
                if got:
                    self._evict_stale(keep=keep)

    def _drop_stale_tmp(self) -> None:
        now = time.time()
        for d in self.root.iterdir():
            if d.is_dir() and d.name.startswith((".tmp-", ".evict-")):
                with contextlib.suppress(OSError):
                    if now - d.stat().st_mtime > STALE_TMP_S:
                        shutil.rmtree(d, ignore_errors=True)
