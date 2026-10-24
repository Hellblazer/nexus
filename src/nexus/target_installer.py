# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The installer of the release being installed (nexus-2k66t.4.3).

``nx self install`` builds the next generation with ``nexus/_install/`` scripts.
Which copy runs matters: the lock pin (nexus-2k66t.4.1) lives in the installer
a release ships, so an upgrade built by the RUNNING release's installer
resolves the new release fresh, and only the upgrade after it is pinned. This
module finds the TARGET's installer so the build can run it.

WHERE THE TARGET'S INSTALLER COMES FROM, by what the receipt's source names:

* registry (``conexus``): the package is fetched alone (``uv pip install
  --no-deps --target``) at the version asked for, and ``nexus/_install/`` is read
  from what landed. No version asked for means the newest the index offers.
* directory: a checkout (``<dir>/src/nexus/_install``) is used where it stands;
  a wheel file (``*.whl``) is unpacked and its ``nexus/_install/`` read.
* any other source, and a directory that does not exist: the running installer,
  which then reports the problem in its own words. The installer does not
  accept a git source: it classifies by shape, so a URL reads as a directory.

WHEN THE TARGET'S INSTALLER IS NOT USED. Never silently: a target that predates
this arrangement (no installer in the package, files missing, or an installer
that does not take an argument the running code passes) builds with the running
installer and one line says why. Usable means the files the running code relies
on are present and, on POSIX, ``install_generation.sh`` has an arm for every
flag in the argv; on Windows ``generation_core.build_generation`` takes every
keyword the call passes.

A fetch that fails is NOT a fallback. The build that follows needs the same
index, so continuing would only fail later with less to go on: the caller gets
:class:`TargetFetchError` before anything is built, flipped or reaped.

Only the BUILD runs the target's scripts. The flip, the shims and the reap
stay with the running code: they are its contract with the layout it reads
back, and they are not where the pin lives. The one exception is the Windows
legacy convergence, where the build, the flip and the PATH entry are a single
``generation_core.migrate_legacy`` call, so the target's runs whole.

WHICH HOPS THIS COVERS. An upgrade FROM a release that carries this code. The
hop out of a release that does not (7.85.0 into its successor) is built by the
old release's installer, so the successor's own pin does not apply to it; only
the PDF carry-forward (nexus-2k66t.4.2) re-pins that generation, at the
running version, on the first ``nx upgrade`` after it.

PURPOSES. ``build`` is ``nx self install`` on a generation and the uv-takeover
repair. ``migrate`` is the legacy uv-tool convergence, whose build step lives
inside ``migrate_legacy.sh`` (POSIX: handed the target's ``install_generation.sh``
through ``NX_MIGRATE_BUILD_SCRIPT``) or ``generation_core.migrate_legacy`` (Windows).

AFTER THE BUILD the caller compares the version the new generation reports with
:attr:`Staged.version`, the version the installer was read at, and warns on a
difference (the build resolves again, so an index that moved between the fetch
and the build can disagree). No ``--version`` pin is added to close it: that
would change the receipt's spec, and ``upgrade_finish`` reports a pin as a
one-shot.
"""
from __future__ import annotations

import importlib.util
import inspect
import re
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from nexus.bounded_subprocess import run_bounded
from nexus.util.nx_argv import argv_off_cwd

__all__ = ["BUILD_PYTHON", "Staged", "TargetFetchError", "staged_installer"]

#: What the POSIX build needs beside ``install_generation.sh`` (it sources
#: ``layout.sh``, which dispatches to ``layout_core.py``, and reads the overrides).
POSIX_FILES = ("install_generation.sh", "layout.sh", "layout_core.py", "overrides.txt")
#: What the Windows build loads: the core and the siblings it reaches lazily.
WINDOWS_FILES = ("generation_core.py", "layout_core.py", "gc_core.py", "winproc_core.py", "overrides.txt")

#: The Python the build runs with (``install_generation.sh --python`` and
#: ``generation_core.build_generation`` both default to it); the fetch uses the
#: same one so the wheel it picks is the wheel the build will install.
BUILD_PYTHON = "3.12"
#: A wheel is a few MB; the bound is for a stalled index, not for size.
_FETCH_TIMEOUT_S = 600.0
_CORE_KEY = "_nx_target_generation_core"
_SIBLING_KEY_PREFIX = "_nx_install_"


class TargetFetchError(Exception):
    """The target release's installer could not be read.

    ``registry`` is True when the cause is a fetch from the package index, False
    for a local checkout or wheel that could not be read, so the caller words it
    and decides on index advice accordingly."""

    def __init__(self, message: str, *, registry: bool = True) -> None:
        super().__init__(message)
        self.registry = registry


@dataclass(frozen=True)
class Staged:
    """Which installer builds the generation.

    ``install_dir`` is the target's ``_install`` directory, or ``None`` for the
    running one. ``core`` is the target's ``generation_core`` module on Windows
    (its own ``GenerationError`` class, so the caller catches the right one).
    ``note`` is the one line to show the operator, or ``""``. ``version`` is the
    release version the installer was read at when that is known (a registry
    fetch, or a package already on disk), else ``""``; the caller compares it
    with what the build installed.
    """

    install_dir: Path | None = None
    core: ModuleType | None = None
    note: str = ""
    version: str = ""

    @property
    def fallback(self) -> bool:
        """True when a note says the RUNNING installer builds instead."""
        return self.install_dir is None and self.core is None and bool(self.note)


def _running(note: str = "") -> Staged:
    return Staged(None, None, note)


def _flag_arms(script: Path) -> set[str]:
    """The ``--flag)`` arms of the script's argument ``case``."""
    text = script.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"^\s*(--[a-z][a-z-]*)\)", text, flags=re.MULTILINE))


def _find_installer(root: Path) -> Path | None:
    """``nexus/_install`` under a package root, or ``src/nexus/_install`` under a checkout."""
    for rel in ("nexus/_install", "src/nexus/_install"):
        candidate = root / rel
        if candidate.is_dir():
            return candidate
    return None


def _missing(install_dir: Path, names: tuple[str, ...]) -> list[str]:
    return [n for n in names if not (install_dir / n).is_file()]


def _fetch_registry(spec: str, dest: Path, *, uv_python: str, env: Mapping[str, str]) -> None:
    argv = argv_off_cwd([
        "uv", "pip", "install", "--quiet", "--no-deps", "--target", str(dest),
        # Resolve FOR the build's Python without needing that interpreter: the wheel is pure
        # Python, and `uv pip install --python 3.12` never downloads one (a box on 3.13 alone
        # would fail here where the build, `uv venv --python 3.12`, downloads it).
        "--python", sys.executable, "--python-version", uv_python, spec,
    ])
    try:
        done = run_bounded(argv, timeout=_FETCH_TIMEOUT_S, env=dict(env))
    except FileNotFoundError as exc:
        raise TargetFetchError(f"`uv` was not found ({exc})") from exc
    except OSError as exc:
        raise TargetFetchError(f"could not run `uv` ({exc})") from exc
    except Exception as exc:  # noqa: BLE001 — a timeout is a failed fetch, with its own words
        raise TargetFetchError(f"fetching {spec} did not finish: {exc}") from exc
    if done.returncode != 0:
        raise TargetFetchError((done.stderr or done.stdout or "").strip() or f"uv exited {done.returncode}")


def _unpack_wheel(wheel: Path, dest: Path) -> None:
    """Extract ``nexus/_install/`` from *wheel* into *dest*, refusing any member
    whose resolved path leaves *dest*."""
    root = dest.resolve()
    try:
        with zipfile.ZipFile(wheel) as zf:
            for member in zf.namelist():
                if not member.startswith("nexus/_install/") or member.endswith("/"):
                    continue
                target = (root / member).resolve()
                if not target.is_relative_to(root):
                    raise TargetFetchError(
                        f"{wheel.name} holds a member outside the package: {member}", registry=False,
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))
    except (zipfile.BadZipFile, OSError) as exc:
        raise TargetFetchError(f"{wheel.name} could not be read as a wheel ({exc})", registry=False) from exc


def _version_of(package_root: Path) -> str:
    """The version a fetched package root names (its ``*.dist-info``), or ``""``."""
    for info in sorted(package_root.glob("*.dist-info")):
        stem = info.name[: -len(".dist-info")]
        if "-" in stem:
            return stem.split("-", 1)[1]
    return ""


def _load_core(install_dir: Path) -> ModuleType:
    """The target's ``generation_core``, loaded by path under a private key.

    Registered in ``sys.modules`` BEFORE it runs, as the module's own sibling
    loader does: a dataclass inside it looks its module up by name. Its
    siblings (``layout_core`` and the rest) load lazily from the same directory
    under ``_nx_install_*`` keys; :func:`staged_installer` clears those on both
    sides so a second staging in one process cannot be served a stale one.
    """
    path = install_dir / "generation_core.py"
    spec = importlib.util.spec_from_file_location(_CORE_KEY, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_KEY] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(_CORE_KEY, None)
        raise
    return module


def _clear_loaded_cores() -> None:
    for key in [k for k in sys.modules if k == _CORE_KEY or k.startswith(_SIBLING_KEY_PREFIX)]:
        sys.modules.pop(key, None)


def _unusable_reason(
    install_dir: Path, *, windows: bool, with_extras: bool, with_version: bool, migrate: bool = False,
) -> str | None:
    """Why *install_dir* cannot build with the arguments the running code passes,
    or ``None``. Loads nothing on POSIX; on Windows it loads the core to read the
    signature, and leaves it loaded for the build. *migrate* checks the Windows
    ``migrate_legacy`` entry point instead of ``build_generation``."""
    missing = _missing(install_dir, WINDOWS_FILES if windows else POSIX_FILES)
    if missing:
        return f"its installer lacks {', '.join(missing)}"
    if not windows:
        needed = {"--source"} | ({"--extras"} if with_extras else set()) | ({"--version"} if with_version else set())
        absent = sorted(needed - _flag_arms(install_dir / "install_generation.sh"))
        if absent:
            return f"its installer does not accept {', '.join(absent)}"
        return None
    try:
        core = _load_core(install_dir)
    except Exception as exc:  # noqa: BLE001 — any load failure means "cannot use it"
        return f"its installer could not be loaded ({type(exc).__name__}: {exc})"
    entry = "migrate_legacy" if migrate else "build_generation"
    build = getattr(core, entry, None)
    if build is None:
        return f"its installer has no {entry}"
    params = inspect.signature(build).parameters
    if migrate:
        wanted = ["version", "tools", "platform", "on_path"]
    else:
        wanted = ["tools", "platform"] + (["extras"] if with_extras else []) + (["version"] if with_version else [])
    absent = [p for p in wanted if p not in params]
    if absent:
        return f"its {entry} does not accept {', '.join(absent)}"
    return None


@contextmanager
def staged_installer(
    source: str, *, version: str | None, running_version: str, windows: bool,
    with_extras: bool, env: Mapping[str, str], platform: str | None = None,
    uv_python: str | None = None, purpose: str = "build", package_root: Path | None = None,
) -> Iterator[Staged]:
    """Yield the installer that should build *source* at *version*.

    See the module docstring. The scratch directory (a fetched package, an
    unpacked wheel) lives for the ``with`` body, which must include the build.
    *purpose* is ``"build"`` or ``"migrate"``. *package_root* is a directory
    that already holds the registry target (a venv's ``site-packages``): the
    installer is read there and nothing is fetched.
    Raises :class:`TargetFetchError` when a registry fetch fails.
    """
    from nexus._install import layout_core  # noqa: PLC0415 — deferred; shape rule shared with the installer

    kind = layout_core.source_kind(source, platform=platform)
    scratch: Path | None = None
    fetched_version = ""
    try:
        if kind == "registry" and package_root is not None:
            found = _find_installer(package_root)
            fetched_version = version or ""
            where = f"the installed {source} {fetched_version}".rstrip() + " package"
        elif kind == "registry":
            if version and version == running_version:
                yield _running()  # the running package IS the target
                return
            scratch = Path(tempfile.mkdtemp(prefix="nx-target-installer-"))
            spec = layout_core.build_spec(source, [], version or "")
            _fetch_registry(spec, scratch / "pkg", uv_python=uv_python or BUILD_PYTHON, env=env)
            found = _find_installer(scratch / "pkg")
            fetched_version = _version_of(scratch / "pkg")
            where = f"the {source} {fetched_version}".rstrip() + " package"
        else:
            path = Path(source).expanduser()
            if not path.exists():
                yield _running()  # the running installer reports a missing source in its own words
                return
            if path.is_dir():
                found = _find_installer(path)
                where = f"the checkout {path}"
            elif path.suffix == ".whl":
                scratch = Path(tempfile.mkdtemp(prefix="nx-target-installer-"))
                _unpack_wheel(path, scratch)
                found = _find_installer(scratch)
                where = f"the wheel {path.name}"
            else:
                yield _running(
                    f"building with the running installer: {path.name} is not a checkout or a wheel, "
                    "so its own installer cannot be read"
                )
                return
        if found is None:
            yield _running(f"building with the running installer: {where} ships no installer (it predates one)")
            return
        _clear_loaded_cores()
        reason = _unusable_reason(
            found, windows=windows, with_extras=bool(with_extras), with_version=bool(version),
            migrate=purpose == "migrate",
        )
        if reason is not None:
            yield _running(f"building with the running installer: {where}: {reason}")
            return
        core = sys.modules.get(_CORE_KEY) if windows else None
        yield Staged(found, core, f"building with the installer {where} ships", fetched_version)
    finally:
        if windows:
            _clear_loaded_cores()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
