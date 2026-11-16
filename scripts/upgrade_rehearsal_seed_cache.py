# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed cache for the upgrade rehearsal's preparation phase (release step 6e).

Preparing a start's old store (``upgrade_boot_writes.py --prepare-only``) takes about 13 minutes for the
86,000-row start and about 20 for the five-collection 7.76.0 start, and the result is the same bytes every
time for the same inputs. This module keeps that prepared state on the run host and puts it back in place
of the seeder. It changes nothing that is judged: a restored store is the one the seeder would have left,
every judged step runs on it as before, and any doubt about an entry falls back to a full seed.

What the prepared state is: whatever the seeder wrote under the start's run dir, except the install-prev
client (``home/.local/bin``, ``home/.local/share/uv``) and uv's cache (``home/.cache/uv``), the step logs
and the scratch cwd. In practice that is ``config/`` (the engine binary, the PG bundle, the PG data dir,
``pg_credentials``, the engine logs), ``prepare.json`` and the synthetic repo the seeder indexed.

Entry layout, under the cache root (default ``~/.cache/lab-remote/upgrade-rehearsal-seeds``, or
``$NEXUS_UPGRADE_REHEARSAL_SEED_CACHE``)::

    <from_version>-<16 hex of the key>/manifest.json
    <from_version>-<16 hex of the key>/payload/<the state, under its run-dir relative paths>

The key covers the previous release, the engine tag that release pins, the platform, the Python of the
install, the prepare command template in use and its collection arguments, and the sha256 of every file
the seeder reads. At most one entry per ``from_version`` is kept; saving a new key evicts the old one.

Safety rules, each tested in ``tests/test_upgrade_rehearsal_seed_cache.py``:

* an entry is written to a temp dir and renamed into place, so a reader never sees a partial one;
* a restore verifies the entry's inventory (paths, sizes, a sha256 of every small file) against its
  manifest, copies it, rewrites the old run dir in the copy's small text files and symlinks (and fails when
  any trace is left in a file it scans), checks the PG port is free and the cluster was cleanly shut down,
  then starts PG alone, counts the store's rows and requires the count to meet the floor and equal the
  manifest's; every failure scrubs what was restored and reports a reason, so the caller seeds in full;
* a save requires the cluster to be cleanly shut down before and after its own row count, and a count at or
  above the floor; otherwise nothing is saved.

Not covered: the data pages of the PG store can hold the old run dir as text (the synthetic repo's catalog
``source_uri``); they are not rewritten.
"""

from __future__ import annotations

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

SCHEMA: int = 1
CACHE_ENV: str = "NEXUS_UPGRADE_REHEARSAL_SEED_CACHE"
LOCK_NAME: str = ".lock"
MANIFEST: str = "manifest.json"
PAYLOAD: str = "payload"
#: Files up to this size are hashed in the inventory and scanned for the old run dir on restore.
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
LOCK_TIMEOUT_S: float = 120.0

Log = Callable[[str], None]


class SeedCacheError(Exception):
    """A cache operation failed; the caller falls back to a full seed."""


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
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def hash_inputs(paths: Sequence[Path]) -> tuple[tuple[str, str], ...]:
    """(file name, sha256) of every file the seeder reads, in a fixed order."""
    return tuple(sorted((p.name, sha256_file(p)) for p in paths))


def platform_tag() -> str:
    return f"{sys.platform}-{platform.machine()}"


@dataclasses.dataclass(frozen=True)
class KeyInputs:
    from_version: str
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
    def name(self) -> str:
        return f"{self.from_version}-{self.digest()[:16]}"


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


def inventory(root: Path, relpaths: Sequence[str]) -> Inventory:
    """A digest of the tree: every path, its kind, size, and a sha256 of each small file. Sizes catch a
    truncated or missing large file; the hashes catch a changed config or credentials file."""
    h = hashlib.sha256()
    files = total = 0

    def line(*parts: str) -> None:
        h.update(("\t".join(parts) + "\n").encode("utf-8"))

    def walk(p: Path, rel: str) -> None:
        nonlocal files, total
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
            line(
                rel, "F", str(size), sha256_file(p) if size <= SMALL_FILE_BYTES else "-"
            )

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
    then scan again: any file that still names an old run dir, or a binary file that would need a
    different length, is a RelocationError. ``skip_dirs`` (the PG relation and WAL dirs) are not scanned."""
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
        if any(len(o) != len(r) for o, r in pairs):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RelocationError(
                    f"{p} is binary and names the old run dir with a different length: {exc}"
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


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def rows_with_pg_up(config: Path, count: Callable[[], int]) -> int:
    """Start the cluster alone, ``count()`` its rows, stop it with a fast shutdown and require the cluster
    to read 'shut down' again: the count of a store that is not otherwise started."""
    creds = read_credentials(config)
    pgdata = Path(creds["PG_DATA"])
    pg_ctl = str(_bundle(config) / "bin" / "pg_ctl")
    env = _pg_env(config)
    fd, logpath = tempfile.mkstemp(prefix="seed-cache-pg-", suffix=".log")
    os.close(fd)
    try:
        r = subprocess.run(
            [pg_ctl, "-D", str(pgdata), "-w", "-t", "300", "-l", logpath, "start"],
            env=env,
            capture_output=True,
            text=True,
            timeout=330,
        )
        if r.returncode != 0:
            raise SeedCacheError(
                f"pg_ctl start failed (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:300]}"
            )
        try:
            rows = count()
        finally:
            s = subprocess.run(
                [pg_ctl, "-D", str(pgdata), "-m", "fast", "-w", "-t", "300", "stop"],
                env=env,
                capture_output=True,
                text=True,
                timeout=330,
            )
            if s.returncode != 0:
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
                raise SeedCacheError(
                    f"pg_ctl stop failed (exit {s.returncode}): {(s.stderr or s.stdout).strip()[:300]}"
                )
        why = stopped_clean(config)
        if why:
            raise SeedCacheError(f"after the row count: {why}")
        return rows
    finally:
        with contextlib.suppress(OSError):
            os.unlink(logpath)


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
                    fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
                )
                got = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        yield got
    finally:
        if got:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _copy_tree(src: Path, dst: Path) -> None:
    if src.is_symlink():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.readlink(src), dst)
    elif src.is_dir():
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _remove(p: Path) -> None:
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p, ignore_errors=True)


@dataclasses.dataclass
class Restore:
    ok: bool
    reason: str = ""  # why the entry was not used (a miss says "no entry")
    prepare: dict | None = None
    rows: int | None = None
    relocated: Relocated | None = None
    seconds: float = 0.0


@dataclasses.dataclass
class Save:
    saved: bool
    reason: str = ""
    name: str = ""
    rows: int | None = None
    seconds: float = 0.0


class SeedCache:
    """The entries under ``root``. ``rows_fn(config)`` counts the store's rows with its PG started alone
    (``rows_with_pg_up`` in production); ``log`` reports each decision."""

    def __init__(
        self,
        root: Path,
        *,
        rows_fn: Callable[[Path], int],
        log: Log = lambda _m: None,
        is_port_free: Callable[[int], bool] = port_free,
        clean_check: Callable[[Path], str | None] = stopped_clean,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
    ) -> None:
        self.root = Path(root)
        self.rows_fn = rows_fn
        self.log = log
        self.is_port_free = is_port_free
        self.clean_check = clean_check
        self.lock_timeout_s = lock_timeout_s

    def entry(self, inputs: KeyInputs) -> Path:
        return self.root / inputs.name

    # -- hit ---------------------------------------------------------------------------------

    def restore(self, inputs: KeyInputs, run_dir: Path, *, floor: int) -> Restore:
        t0 = time.monotonic()
        entry = self.entry(inputs)
        if not entry.is_dir():
            return Restore(False, "no entry", seconds=time.monotonic() - t0)
        restored: list[Path] = []
        try:
            with _locked(
                self.root, exclusive=False, timeout_s=self.lock_timeout_s
            ) as got:
                if not got:
                    return Restore(False, "the cache lock was not free in time")
                res = self._restore_locked(inputs, entry, run_dir, floor, restored)
        except (
            SeedCacheError,
            OSError,
            ValueError,
            KeyError,
            subprocess.SubprocessError,
        ) as exc:
            res = Restore(False, f"{type(exc).__name__}: {exc}")
        if not res.ok:
            self._scrub(run_dir, restored)
        res.seconds = time.monotonic() - t0
        return res

    def _restore_locked(
        self,
        inputs: KeyInputs,
        entry: Path,
        run_dir: Path,
        floor: int,
        restored: list[Path],
    ) -> Restore:
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
            inv = inventory(payload, relpaths)
        except (SeedCacheError, OSError) as exc:
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
        port = man.get("pg_port")
        if not isinstance(port, int) or not self.is_port_free(port):
            return Restore(
                False, f"the store's PG port {port!r} is in use on this host"
            )

        # copy, and make every path that names the old run dir name this one. What is not there yet is
        # what a failed restore removes again; a dir the run already made (config) stays, emptied of
        # the payload's children.
        for rel in relpaths:
            dst = run_dir / rel
            if not (dst.exists() or dst.is_symlink()):
                restored.append(dst)
            elif dst.is_dir() and (payload / rel).is_dir():
                restored.extend(dst / c.name for c in (payload / rel).iterdir())
        for rel in relpaths:
            _copy_tree(payload / rel, run_dir / rel)
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
        why = self.clean_check(config)
        if why:
            return Restore(
                False, f"the restored cluster is not cleanly shut down: {why}"
            )
        rows = self.rows_fn(config)
        if rows < floor:
            return Restore(
                False,
                f"the restored store holds {rows} rows, below the floor of {floor}",
                rows=rows,
            )
        if rows != man.get("rows"):
            return Restore(
                False,
                f"the restored store holds {rows} rows, the manifest says {man.get('rows')}",
                rows=rows,
            )
        why = self.clean_check(config)
        if why:
            return Restore(
                False,
                f"the restored cluster is not cleanly shut down after the count: {why}",
            )
        return Restore(True, prepare=prepare, rows=rows, relocated=relocated)

    def _scrub(self, run_dir: Path, restored: Sequence[Path]) -> None:
        """Remove what a failed restore put under the run dir, so the seeder starts from the state it
        always starts from (``config`` an empty dir)."""
        for p in restored:
            _remove(p)
        (run_dir / "config").mkdir(parents=True, exist_ok=True)

    # -- miss --------------------------------------------------------------------------------

    def save(
        self, inputs: KeyInputs, run_dir: Path, *, floor: int, replace: bool = False
    ) -> Save:
        """Copy the prepared state in ``run_dir`` into the cache. Never raises: a state that cannot be
        saved is reported and the run goes on with the store it has."""
        t0 = time.monotonic()
        try:
            res = self._save(inputs, run_dir, floor, replace)
        except (
            SeedCacheError,
            OSError,
            ValueError,
            KeyError,
            subprocess.SubprocessError,
        ) as exc:
            res = Save(False, f"{type(exc).__name__}: {exc}", name=inputs.name)
        res.seconds = time.monotonic() - t0
        return res

    def _save(
        self, inputs: KeyInputs, run_dir: Path, floor: int, replace: bool
    ) -> Save:
        name = inputs.name
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
        rows = self.rows_fn(config)
        if rows < floor:
            return Save(
                False,
                f"the store holds {rows} rows, below the floor of {floor}",
                name=name,
                rows=rows,
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
        src_inv = inventory(run_dir, relpaths)

        self.root.mkdir(parents=True, exist_ok=True)
        self._drop_stale_tmp()
        tmp = Path(tempfile.mkdtemp(prefix=f".tmp-{name}-", dir=self.root))
        try:
            payload = tmp / PAYLOAD
            payload.mkdir()
            for rel in relpaths:
                _copy_tree(run_dir / rel, payload / rel)
            copy_inv = inventory(payload, relpaths)
            if copy_inv != src_inv:
                return Save(
                    False,
                    "the copy does not match the source (the run dir changed while copying)",
                    name=name,
                )
            manifest = {
                "schema": SCHEMA,
                "key": name,
                "key_inputs": inputs.as_dict(),
                "inputs_digest": inputs.digest(),
                "source_run_dir": str(run_dir),
                "source_run_dir_resolved": str(run_dir.resolve()),
                "paths": relpaths,
                "pg_data_rel": pg_rel,
                "pg_port": pg_port,
                "rows": rows,
                "collections": prepare.get("collections") or {},
                "inventory_sha256": copy_inv.digest,
                "files": copy_inv.files,
                "bytes": copy_inv.bytes,
                "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
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
                        rows=rows,
                    )
                final = self.root / name
                if final.exists():
                    if not replace:
                        return Save(
                            False,
                            "another run saved this key first; kept its entry",
                            name=name,
                            rows=rows,
                        )
                    self._evict(final)
                os.rename(tmp, final)
                tmp = None  # type: ignore[assignment]
                self._evict_others(inputs.from_version, name)
        finally:
            if tmp is not None:
                shutil.rmtree(tmp, ignore_errors=True)
        return Save(True, name=name, rows=rows)

    # -- bounded size --------------------------------------------------------------------------

    def _evict(self, d: Path) -> None:
        gone = self.root / f".evict-{d.name}-{os.getpid()}-{time.monotonic_ns()}"
        os.rename(d, gone)
        shutil.rmtree(gone, ignore_errors=True)

    def _evict_others(self, from_version: str, keep: str) -> list[str]:
        """At most one entry per ``from_version``: remove every other key's. Caller holds the exclusive lock."""
        pat = re.compile(rf"^{re.escape(from_version)}-[0-9a-f]{{16}}$")
        gone: list[str] = []
        for d in sorted(self.root.iterdir()):
            if d.is_dir() and d.name != keep and pat.match(d.name):
                self._evict(d)
                gone.append(d.name)
                self.log(f"seed cache: evicted {d.name} (one entry per start)")
        return gone

    def _drop_stale_tmp(self) -> None:
        now = time.time()
        for d in self.root.iterdir():
            if d.is_dir() and d.name.startswith((".tmp-", ".evict-")):
                with contextlib.suppress(OSError):
                    if now - d.stat().st_mtime > STALE_TMP_S:
                        shutil.rmtree(d, ignore_errors=True)
