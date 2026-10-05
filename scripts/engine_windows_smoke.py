#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke for the windows-x64 engine archive (RDR-224 P1.2, bead nexus-f9bgu.9).

The Windows counterpart of service/native-smoke.sh's core. That script starts a
pgvector container with ``docker run``; a Windows runner runs no Linux container,
so this boots the engine against the Windows PostgreSQL bundle instead, inside
the same job. Pass means all of:

  * the engine archive has the P0.4 layout, and the engine started from the
    EXTRACTED archive, beside only its four VC++ DLLs, with a PATH that holds
    nothing of the build host's toolchain (windows_engine_release.verify_archive);
  * ``/health`` answers;
  * ``/version`` reports a ``schema_changeset_count`` equal to the number of
    changeSets in the changelog. The expected number is READ from the changelog
    (master + includes, XML comments removed), never hardcoded: 508 on
    2026-10-05, and it grows with every changeset;
  * a real bge embedding comes back 768-dimensional, finite and non-zero. That
    drives the whole native path the exe exists for: the DJL tokenizers JNI, the
    ONNX Runtime JNI and onnxruntime.dll (which is the library that imports
    msvcp140.dll and msvcp140_1.dll);
  * the engine process then has vcruntime140.dll and msvcp140.dll loaded, every
    VC++ runtime module from the engine directory (not System32): proof that the
    shipped copies are what the process actually used;
  * a stop, ASSERTED on Windows (nexus-f9bgu.30, critique S1): CTRL_BREAK, the P1.1
    stop channel, ends the serving engine with exit code 149 (128 + 21) within
    ``STOP_BOUND_S`` seconds, its log carries ``shutdown_signal`` and
    ``service_stopped``, a second boot of the same database reaches health with
    ``new_changesets=0`` and the right changeset count, and that boot stops the same
    way. A session that cannot deliver CTRL_BREAK (no console: a service session)
    FAILS the smoke rather than warns: run it in a session that has one. The
    driver for the other three phases (a stop before the changelog lock, in the
    middle of a changeset, during ONNX Runtime initialisation) is
    scripts/engine_windows_stop_probe.py. Off Windows the stop is reported, not
    asserted (SIGTERM, exit code 143, the cloud and Linux legs' own behaviour).

Not covered, stated rather than implied: the fused-rerank stage and the real
Python client probes of native-smoke.sh (they need the cross-encoder model and a
nexus install on the runner).

The bge model is provisioned here, verified by sha256 against the pins the
prime-bge-onnx action carries (tests pin the two together), because that action
is a bash action and this runner has no bash. A persistent runner keeps it
between runs; absent or corrupt files are fetched again.

Hazards, each from the nexus-f9bgu.8/.12 measurements: the work directory is made
with ``os.mkdir`` (``tempfile.mkdtemp`` gives an owner-only ACL on Windows that an
elevated initdb then cannot read); pg_ctl's output goes to a file; the children get
a scrubbed environment.

Usage::

    python scripts/engine_windows_smoke.py --engine-archive dist/nexus-service-windows-x64.txz \\
        --pg-archive pgdist/nexus-pg-windows-x64.txz
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

import pg_bundle_windows_smoke as sm
import windows_engine_release as wer

SmokeError = sm.SmokeError

#: sha256 pins and release tag of the bge-base-en-v1.5 export; equal to .github/actions/prime-bge-onnx.
MODEL_PINS: dict[str, str] = {
    "model.onnx": "9bc579acdba21c253c62a9bf866891355a63ffa3442b52c8a37d75b2ccb91848",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
}
MODEL_ASSET_TAG = "ci-assets-bge-768-v1"
MODEL_SUBDIR = Path("bge-base-en-v1.5") / "onnx"
EMBED_MODEL = "bge-base-en-v15-768"
EMBED_TEXT = "windows engine smoke"
EMBED_DIMS = 768
TOKEN = "smoketoken"
DB_NAME = "nexus"
DEFAULT_REPO = "Hellblazer/nexus"
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHANGELOG = REPO_ROOT / "service" / "src" / "main" / "resources" / "db" / "changelog"
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_INCLUDE_RE = re.compile(r'<include\s+file="(?:db/changelog/)?([^"]+)"')
_CHANGESET_RE = re.compile(r"<changeSet\b")


# --------------------------------------------------------------------------- #
# Expected changeset count
# --------------------------------------------------------------------------- #


def changeset_count(changelog_dir: Path) -> int:
    """changeSet elements in the master changelog and everything it includes, comments removed."""
    master = changelog_dir / "db.changelog-master.xml"
    if not master.is_file():
        raise SmokeError(f"changelog master not found: {master}")
    seen: set[Path] = set()
    total = 0
    pending = [master]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        text = _COMMENT_RE.sub("", path.read_text(encoding="utf-8"))
        total += len(_CHANGESET_RE.findall(text))
        for name in _INCLUDE_RE.findall(text):
            target = changelog_dir / name
            if not target.is_file():
                raise SmokeError(f"{path.name} includes {name}, which does not exist under {changelog_dir}")
            pending.append(target)
    if total == 0:
        raise SmokeError(f"no changeSet found under {changelog_dir}: refusing a vacuous expected count")
    return total


# --------------------------------------------------------------------------- #
# The bge model
# --------------------------------------------------------------------------- #


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch_url(url: str, dest: Path) -> None:
    """Download a public release asset. Unauthenticated on purpose: the repository is public, and
    urllib forwards every header to the CDN host the release URL redirects to, where a second
    credential is at best rejected and at worst leaked (code review m1)."""
    with urllib.request.urlopen(url, timeout=120) as resp, dest.open("wb") as out:  # noqa: S310
        shutil.copyfileobj(resp, out, 1 << 20)


def ensure_model(
    model_root: Path,
    *,
    fetch: Callable[[str, Path], None],
    repo: str,
    pins: Mapping[str, str] = MODEL_PINS,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 4,
) -> Path:
    """Make sure the bge model and tokenizer are under *model_root*, verified; return model.onnx."""
    target_dir = model_root / MODEL_SUBDIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, want in pins.items():
        dest = target_dir / name
        if dest.is_file() and _sha256(dest) == want:
            continue
        url = f"https://github.com/{repo}/releases/download/{MODEL_ASSET_TAG}/{name}"
        part = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(1, attempts + 1):
            part.unlink(missing_ok=True)
            try:
                emit(f"SMOKE model: fetching {name} (attempt {attempt})")
                fetch(url, part)
                break
            except (OSError, urllib.error.URLError) as exc:
                if attempt == attempts:
                    part.unlink(missing_ok=True)
                    raise SmokeError(f"could not download {name} from {url}: {exc}") from exc
                sleep(min(2**attempt, 30))
        got = _sha256(part)
        if got != want:
            part.unlink(missing_ok=True)
            raise SmokeError(f"{name} downloaded from {url} has sha256 {got}, expected {want}")
        os.replace(part, dest)
    return target_dir / "model.onnx"


# --------------------------------------------------------------------------- #
# Response checks
# --------------------------------------------------------------------------- #


def check_version(text: str, expected: int) -> str:
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise SmokeError(f"/version is not JSON: {text[:200]!r}") from exc
    got = body.get("schema_changeset_count") if isinstance(body, dict) else None
    if isinstance(got, bool) or not isinstance(got, int):
        raise SmokeError(f"/version carries no integer schema_changeset_count (got {got!r}): the migration did not report")
    if got != expected:
        raise SmokeError(f"schema_changeset_count is {got}, the changelog has {expected} changesets: the migration did not apply every changeset")
    release = body.get("release_version") if isinstance(body, dict) else None
    return f"changesets {got}/{expected}, release_version={release}"


def check_embedding(text: str) -> int:
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise SmokeError(f"embed response is not JSON: {text[:200]!r}") from exc
    vectors = body.get("embeddings") if isinstance(body, dict) else None
    if not isinstance(vectors, list) or len(vectors) != 1 or not isinstance(vectors[0], list):
        raise SmokeError(f"embed response does not hold exactly one vector: {text[:200]!r}")
    vec = vectors[0]
    if len(vec) != EMBED_DIMS:
        raise SmokeError(f"embedding has {len(vec)} dimensions, expected {EMBED_DIMS}")
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in vec):
        raise SmokeError("embedding holds a non-numeric or non-finite component")
    if not any(vec):
        raise SmokeError("embedding is all zeros: the model did not run")
    return len(vec)


def check_engine_modules(
    modules: Sequence[str], engine_dir: Path, *, long_path: Callable[[str], str] = sm.resolve_long_path
) -> str:
    """Every VC++ runtime module the engine loaded came from its own directory, and the two
    that matter most were loaded at all (vcruntime140 by the exe, msvcp140 by onnxruntime.dll)."""
    seen = sm.check_loaded_modules(modules, engine_dir, long_path=long_path)
    names = {Path(m.replace("\\", "/")).name.lower() for m in seen}
    absent = [n for n in ("vcruntime140.dll", "msvcp140.dll") if n not in names]
    if absent:
        raise SmokeError(
            f"the engine's module list has no {', '.join(absent)}: the check did not see the library "
            "the shipped copy exists for (did the embed run?)"
        )
    return ", ".join(sorted(names))


# --------------------------------------------------------------------------- #
# Process and HTTP plumbing (injected in tests)
# --------------------------------------------------------------------------- #


class Proc(Protocol):
    pid: int

    def poll(self) -> int | None: ...
    def interrupt(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float) -> int | None: ...


class Launcher(Protocol):
    def start(self, argv: Sequence[str], env: Mapping[str, str], log: Path) -> Proc: ...


class Http(Protocol):
    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]: ...
    def post_json(self, url: str, headers: dict[str, str], payload: dict) -> tuple[int, str]: ...


class ProcessLauncher:
    """The real launcher. On Windows the engine gets its own process group so CTRL_BREAK reaches only it."""

    WINDOWS_FLAGS_ATTR = "CREATE_NEW_PROCESS_GROUP"

    def start(self, argv: Sequence[str], env: Mapping[str, str], log: Path) -> Proc:
        flags = getattr(subprocess, self.WINDOWS_FLAGS_ATTR, 0) if sys.platform == "win32" else 0
        fh = log.open("ab")
        popen = subprocess.Popen(  # noqa: S603
            list(argv), env=dict(env), stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        return _PopenProc(popen)


class _PopenProc:
    def __init__(self, popen: subprocess.Popen) -> None:
        self._p = popen
        self.pid = popen.pid

    def poll(self) -> int | None:
        return self._p.poll()

    def interrupt(self) -> None:
        sig = getattr(signal, "CTRL_BREAK_EVENT", None) if sys.platform == "win32" else signal.SIGTERM
        os.kill(self.pid, sig)  # type: ignore[arg-type]

    def kill(self) -> None:
        self._p.kill()

    def wait(self, timeout: float) -> int | None:
        try:
            return self._p.wait(timeout)
        except subprocess.TimeoutExpired:
            return None


class UrllibHttp:
    def _do(self, req: urllib.request.Request) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError):
            return 0, ""

    def get(self, url: str, headers: dict[str, str]) -> tuple[int, str]:
        return self._do(urllib.request.Request(url, headers=headers))

    def post_json(self, url: str, headers: dict[str, str], payload: dict) -> tuple[int, str]:
        data = json.dumps(payload).encode()
        return self._do(urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST"))


def engine_env(
    *, engine_dir: Path, platform: sm.Platform, base: Mapping[str, str], db_url: str, db_user: str,
    port: int, model_root: Path,
) -> dict[str, str]:
    """Scrubbed OS basics, PATH = engine directory + OS directories, plus the engine's own settings."""
    env = sm.scrubbed_env(engine_dir, platform, base)
    env.update(
        NX_DB_URL=db_url, NX_DB_USER=db_user, NX_DB_PASS="smoke", NX_SERVICE_PORT=str(port),
        NX_SERVICE_TOKEN=TOKEN, NX_EMBED_MODE="onnx", NX_ONNX_MODEL_DIR=str(model_root),
        # as service/native-smoke.sh: the posture the local launch ships
        NX_OWNERLESS_WRITE_MODE="enforce",
    )
    return env


def extract_engine(archive: Path, dest: Path) -> Path:
    """Extract the flat engine archive into *dest* (a directory that inherits its parent's ACL).

    Streams regular members by name; nothing but a verified flat layout reaches the file system."""
    problems = wer.verify_archive(archive)
    if problems:
        raise SmokeError(f"engine archive {archive.name} fails the layout check: " + "; ".join(problems))
    os.mkdir(dest)
    with tarfile.open(archive, "r:xz") as tf:
        for member in tf:
            # verify_archive already refused these; a second, local refusal keeps this function safe
            # on its own, because `dest / "C:x"` leaves dest on Windows (code review m3).
            bad = wer.bare_name_problem(member.name)
            if bad is not None:
                raise SmokeError(f"engine archive {archive.name}: {bad}")
            src = tf.extractfile(member)
            assert src is not None  # verify_archive proved every member is a regular file
            with src, (dest / member.name).open("wb") as out:
                shutil.copyfileobj(src, out)
    return dest / wer.ENGINE_EXE


def wait_healthy(
    http: Http, base: str, proc: Proc, log: Path, *, timeout_s: int, sleep: Callable[[float], None]
) -> None:
    waited = 0
    while waited < timeout_s:
        rc = proc.poll()
        if rc is not None:
            raise SmokeError(f"the engine exited with code {rc} during startup; log tail:\n{_tail(log)}")
        status, _ = http.get(f"{base}/health", {})
        if status == 200:
            return
        sleep(1)
        waited += 1
    raise SmokeError(f"the engine never became healthy in {timeout_s}s; log tail:\n{_tail(log)}")


def _tail(log: Path, chars: int = 3000) -> str:
    return log.read_text(encoding="utf-8", errors="replace")[-chars:] if log.exists() else "(no log)"


def stop_engine(proc: Proc, platform: sm.Platform, emit: Callable[[str], None], *, grace_s: float = 30) -> None:
    """Graceful first (CTRL_BREAK on Windows, SIGTERM elsewhere), hard kill if it does not exit."""
    signame = "CTRL_BREAK" if platform.windows else "SIGTERM"
    if proc.poll() is not None:
        emit(f"SMOKE stop: engine had already exited with code {proc.poll()}")
        return
    try:
        proc.interrupt()
        rc = proc.wait(grace_s)
    except OSError as exc:
        emit(f"SMOKE stop WARNING: {signame} could not be delivered ({exc})")
        rc = None
    if rc is None:
        proc.kill()
        proc.wait(10)
        emit(f"SMOKE stop WARNING: the engine did not exit on {signame}; stopped hard (this session may have no console)")
    else:
        emit(f"SMOKE stop: stopped by {signame}, exit code {rc}")


#: CTRL_BREAK is signal 21 on Windows; the engine exits 128 + signal (OrtInitGate, Main).
WINDOWS_STOP_EXIT_CODE = 128 + 21
#: A serving engine stops in well under a second (76 ms measured, nexus-f9bgu.8); ten seconds is slack, not a target.
STOP_BOUND_S = 10.0
_SHUTDOWN_EVENTS = ("shutdown_signal", "service_stopped")
_NEW_CHANGESETS_RE = re.compile(r"\bnew_changesets=(\d+)")


def read_log(log: Path) -> str:
    return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""


def check_shutdown_log(text: str) -> None:
    """The engine's own account of a clean stop: it saw the signal and ran its stop path to the end."""
    missing = [ev for ev in _SHUTDOWN_EVENTS if not re.search(rf"\bevent={ev}\b", text)]
    if missing:
        raise SmokeError(
            f"the engine log lacks event={', event='.join(missing)}: the stop did not run the engine's own "
            f"shutdown path; log tail:\n{text[-1500:]}"
        )


def check_reboot_log(text: str) -> None:
    """A second boot of an already-migrated database applies nothing new."""
    match = _NEW_CHANGESETS_RE.search(text)
    if match is None:
        raise SmokeError(
            "the second boot's log carries no new_changesets=<n> (schema_migration_complete): the check "
            f"cannot tell whether the migration was clean; log tail:\n{text[-1500:]}"
        )
    if int(match.group(1)) != 0:
        raise SmokeError(f"the second boot applied {match.group(1)} new changesets: the first stop left the migration unfinished")


def assert_serving_stop(
    proc: Proc, log: Path, emit: Callable[[str], None], *,
    bound_s: float = STOP_BOUND_S, clock: Callable[[], float] = time.monotonic,
) -> float:
    """CTRL_BREAK a serving engine and assert the stop (Windows). Returns the seconds it took.

    Exit code 149, inside *bound_s*, with the shutdown events in the log. A session that cannot
    deliver the signal is a failure, not a warning (nexus-f9bgu.30): a leg that passes through a
    hard kill proves nothing about the stop."""
    t0 = clock()
    try:
        proc.interrupt()
    except OSError as exc:
        proc.kill()
        proc.wait(10)
        raise SmokeError(
            f"CTRL_BREAK could not be delivered ({exc}): this session has no console. The Windows stop is "
            "asserted, not skipped; run the smoke in a session that has one (an interactive or ssh session, "
            "not a Windows service)."
        ) from exc
    rc = proc.wait(bound_s)
    if rc is None:
        proc.kill()
        proc.wait(10)
        raise SmokeError(f"the engine did not exit within {bound_s:.0f}s of CTRL_BREAK; stopped hard. log tail:\n{_tail(log, 1500)}")
    elapsed = clock() - t0
    if rc != WINDOWS_STOP_EXIT_CODE:
        raise SmokeError(f"the engine exited with code {rc} on CTRL_BREAK, expected {WINDOWS_STOP_EXIT_CODE}; log tail:\n{_tail(log, 1500)}")
    if elapsed > bound_s:
        raise SmokeError(f"the engine took {elapsed:.1f}s to stop, the bound is {bound_s:.0f}s")
    check_shutdown_log(read_log(log))
    emit(f"SMOKE stop: CTRL_BREAK, exit code {rc} in {elapsed:.2f}s, shutdown_signal and service_stopped logged")
    return elapsed


# --------------------------------------------------------------------------- #
# The run
# --------------------------------------------------------------------------- #


def run(
    *,
    engine_archive: Path,
    pg_archive: Path,
    model_root: Path,
    changelog_dir: Path,
    workdir: Path | None,
    platform: sm.Platform,
    runner: sm.Runner,
    launcher: Launcher,
    http: Http,
    base_env: Mapping[str, str],
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    ports: tuple[int, int] | None = None,
    keep: bool = False,
    health_timeout_s: int = 120,
) -> None:
    expected = changeset_count(changelog_dir)
    for name in MODEL_PINS:
        if not (model_root / MODEL_SUBDIR / name).is_file():
            raise SmokeError(f"{model_root / MODEL_SUBDIR / name} is missing: provision it first (--ensure-model)")
    if workdir is not None:
        workdir.mkdir(parents=True, exist_ok=True)
    work = sm.make_workdir(workdir, platform)
    proc: Proc | None = None
    pg_started = False
    pg_bin: Path | None = None
    pg_env: dict[str, str] = {}
    data = work / "pgdata"
    ctl_out = work / "pg_ctl.out"
    exe = platform.exe
    try:
        engine_exe = extract_engine(engine_archive, work / "engine")
        emit(f"SMOKE engine: extracted {engine_archive.name}: {', '.join(sorted(p.name for p in engine_exe.parent.iterdir()))}")
        pg_root = sm.materialise(pg_archive, archive=True, dest=work / "pg")
        pg_bin = pg_root / "bin"
        pg_env = sm.scrubbed_env(pg_bin, platform, base_env)
        pg_port, svc_port = ports or (sm.free_port(), sm.free_port())
        user = getpass.getuser()

        def tool(name: str) -> str:
            return str(pg_bin / f"{name}{exe}")  # type: ignore[operator]

        rc, out = runner.capture(
            [tool("initdb"), "--no-locale", "-E", "UTF8", "-A", "trust", "-U", user, "-D", str(data)], env=pg_env
        )
        if rc != 0:
            raise SmokeError(f"initdb failed ({rc}): {out.strip()}")
        rc = runner.to_file(
            [tool("pg_ctl"), "-D", str(data), "-l", str(work / "postgres.log"), "-w", "-t", "60",
             "-o", f"-p {pg_port} -c listen_addresses=127.0.0.1", "start"],
            env=pg_env, out=ctl_out,
        )
        if rc != 0:
            raise SmokeError(f"pg_ctl start failed ({rc}); see {work / 'postgres.log'}")
        pg_started = True
        rc, out = runner.capture(
            [tool("createdb"), "-h", "127.0.0.1", "-p", str(pg_port), "-U", user, DB_NAME], env=pg_env
        )
        if rc != 0:
            raise SmokeError(f"createdb failed ({rc}): {out.strip()}")
        emit(f"SMOKE postgres: up on 127.0.0.1:{pg_port}, database {DB_NAME}")

        env = engine_env(
            engine_dir=engine_exe.parent, platform=platform, base=base_env,
            db_url=f"jdbc:postgresql://127.0.0.1:{pg_port}/{DB_NAME}", db_user=user, port=svc_port, model_root=model_root,
        )
        log = work / "engine.log"
        proc = launcher.start([str(engine_exe), "-Duser.timezone=UTC"], env, log)
        base = f"http://127.0.0.1:{svc_port}"
        auth = {"Authorization": f"Bearer {TOKEN}"}
        t0 = time.monotonic()
        wait_healthy(http, base, proc, log, timeout_s=health_timeout_s, sleep=sleep)
        emit(f"SMOKE health: ok (migration included) after {time.monotonic() - t0:.1f}s")

        status, text = http.get(f"{base}/version", auth)
        if status != 200:
            raise SmokeError(f"/version returned {status}: {text[:200]!r}")
        emit(f"SMOKE version: {check_version(text, expected)}")

        status, text = http.post_json(f"{base}/v1/vectors/embed", auth, {"model": EMBED_MODEL, "texts": [EMBED_TEXT]})
        if status != 200:
            raise SmokeError(f"/v1/vectors/embed returned {status}: {text[:300]!r}; log tail:\n{_tail(log, 1500)}")
        emit(f"SMOKE embed: {check_embedding(text)}-dimensional bge vector (DJL tokenizers JNI + ONNX Runtime JNI ran)")

        if platform.windows:
            rc, listing = runner.capture(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-Process -Id {proc.pid}).Modules | ForEach-Object {{ $_.FileName }}"],
                env=pg_env,
            )
            if rc != 0:
                raise SmokeError(f"module listing failed ({rc}): {listing.strip()}")
            emit(f"SMOKE runtime modules: {check_engine_modules(listing.splitlines(), engine_exe.parent)}, all from the engine directory")

            # The stop is part of the leg (nexus-f9bgu.30): stop the serving engine, then boot the
            # same database again and stop that too.
            assert_serving_stop(proc, log, emit)
            log2 = work / "engine-reboot.log"
            proc = launcher.start([str(engine_exe), "-Duser.timezone=UTC"], env, log2)
            t0 = time.monotonic()
            wait_healthy(http, base, proc, log2, timeout_s=health_timeout_s, sleep=sleep)
            status, text = http.get(f"{base}/version", auth)
            if status != 200:
                raise SmokeError(f"/version returned {status} after the reboot: {text[:200]!r}")
            emit(f"SMOKE reboot: healthy after {time.monotonic() - t0:.1f}s, {check_version(text, expected)}")
            assert_serving_stop(proc, log2, emit)
            check_reboot_log(read_log(log2))
            emit("SMOKE reboot: second boot applied 0 new changesets")
    finally:
        if proc is not None:
            stop_engine(proc, platform, emit)
        if pg_started and pg_bin is not None:
            rc = runner.to_file(
                [str(pg_bin / f"pg_ctl{exe}"), "-D", str(data), "-m", "fast", "-w", "-t", "60", "stop"],
                env=pg_env, out=ctl_out,
            )
            if rc != 0:
                emit(f"SMOKE WARNING: pg_ctl stop exited {rc}")
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
    emit("SMOKE postgres: stopped")
    emit("SMOKE PASSED")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--engine-archive", type=Path, required=True)
    p.add_argument("--pg-archive", type=Path, required=True)
    p.add_argument("--model-root", type=Path, help="default: ~/.cache/nexus/onnx_models, the provisioner's own")
    p.add_argument("--changelog-dir", type=Path, default=DEFAULT_CHANGELOG)
    p.add_argument("--workdir", type=Path)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--health-timeout", type=int, default=120)
    p.add_argument("--no-model-download", action="store_true", help="fail instead of fetching a missing model")
    args = p.parse_args(argv)
    model_root = args.model_root or (Path.home() / ".cache" / "nexus" / "onnx_models")
    try:
        if not args.no_model_download:
            ensure_model(
                model_root, fetch=fetch_url,
                repo=os.environ.get("GITHUB_REPOSITORY", DEFAULT_REPO),
            )
        run(
            engine_archive=args.engine_archive, pg_archive=args.pg_archive, model_root=model_root,
            changelog_dir=args.changelog_dir, workdir=args.workdir, platform=sm.Platform.current(),
            runner=sm.Runner(), launcher=ProcessLauncher(), http=UrllibHttp(), base_env=os.environ,
            keep=args.keep, health_timeout_s=args.health_timeout,
        )
    except SmokeError as exc:
        print(f"SMOKE FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
