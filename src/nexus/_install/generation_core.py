# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The generation installer for native Windows: build, flip, register, migrate, reap.

RDR-224, nexus-f9bgu.47. The POSIX installer is bash (``install_generation.sh``,
``flip.sh``, ``legacy.sh``, ``migrate_legacy.sh``) dispatching into the Python
cores beside it. On Windows ``bash`` is the WSL launcher, not a shell this
process can rely on, so this module states the same orchestration in Python and
``nx self install`` calls it directly. The POSIX files are untouched and stay
the statement of the POSIX contract; this is the statement of the Windows one.

Stdlib-only and importing nothing from nexus, like its siblings, and pinned the
same way (``tests/test_install_generation_core_is_bootstrap_safe.py``). It
reaches ``layout_core``, ``gc_core`` and ``winproc_core`` through ``_sibling`` so
there is ONE module object per process.

WHAT STAYS THE SAME. A generation is a ``gen-<stamp>`` venv built AT its final
path (console-script launchers bake absolute paths), claimed with an atomic
``os.mkdir`` and a ``.nx-building`` marker, and made real by writing its
receipt LAST. ``current`` and ``previous`` are the pointers a flip moves; the
legacy ``uv tool install`` tree is registered as the ledger pointer
``gen-legacy-uv-tool`` and reaped by a LATER pass, never by the one that
discovered it.

WHAT DIFFERS ON WINDOWS, ALL MEASURED (Python 3.13, 2026-10-06)

* A pointer is a directory JUNCTION (``_winapi.CreateJunction``), not a
  symlink: creating a symlink needs a privilege a normal account lacks.
  ``is_symlink()`` is False for a junction, ``os.path.isjunction()`` is True,
  and ``os.readlink`` returns ``\\\\?\\C:\\...``; ``layout_core.is_link`` and
  ``read_link`` carry that.
* A junction is swapped like the POSIX symlink: create ``X.new``, rename ``X``
  aside, rename ``X.new`` to ``X``, ``os.rmdir`` the old one. Unlike POSIX there
  is no single atomic rename-over, so ``X`` is absent for the span of two
  renames, and a spawn through ``<tools>\\current\\bin`` (the user PATH entry)
  inside that span does not resolve. A crash there leaves the old pointer as
  ``.<link>.old.<pid>``, which the next swap RESTORES. The swap works while a
  process runs THROUGH the junction.
* A junction is removed with ``os.rmdir``, never ``shutil.rmtree``, which would
  empty the generation it names.
* The venv's executables are in ``Scripts\\`` with ``.exe`` suffixes.

Renaming a venv directory SUCCEEDS while its ``python.exe`` runs, so a rename is
NOT a probe for "is anything running from here"; the holder census is.
"""
from __future__ import annotations

import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

#: The attempts at claiming a stamp: the bare stamp, then ``a`` .. ``h``. The
#: shell half's own list.
_STAMP_SUFFIXES = ("", "a", "b", "c", "d", "e", "f", "g", "h")

_LAYOUT: object | None = None
_GC: object | None = None


def _sibling(name: str):
    """A neighbour module in ``_install/``, as ONE object per process.

    Two worlds, and the rule differs between them:

    * Imported as part of the package, ``__package__`` is ``nexus._install``
      and the package sibling IS the adjacent file, so it is imported
      normally. That keeps ONE module object per process, which matters for
      more than tidiness: ``LayoutError`` must be one class or
      ``except InstallLayoutError`` stops catching errors raised in here, and
      a test patching ``nexus._install.layout_core`` must reach this caller.
    * Run as a script at bootstrap there is no package, so the file is loaded
      by path under a stable key and REUSED from ``sys.modules`` on the next
      ask.

    Pinned by ``tests/test_install_cores_share_one_module_identity.py``.
    """
    if __package__:
        from importlib import import_module  # noqa: PLC0415 -- package world only

        return import_module(f"{__package__}.{name}")

    key = f"_nx_install_{name}"
    cached = sys.modules.get(key)
    if cached is not None:
        return cached

    import importlib.util  # noqa: PLC0415 -- bootstrap path only

    path = Path(__file__).resolve().parent / f"{name}.py"
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- unreachable
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec, so a sibling that reaches back mid-import finds
    # the partially-built module rather than starting a second load of it.
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def _layout():
    global _LAYOUT
    if _LAYOUT is None:
        _LAYOUT = _sibling("layout_core")
    return _LAYOUT


def _gc():
    global _GC
    if _GC is None:
        _GC = _sibling("gc_core")
    return _GC


class GenerationError(Exception):
    """An install step refused or failed. The message is what the operator reads."""


# ---------------------------------------------------------------------------
# Pointers
# ---------------------------------------------------------------------------


class LinkOps:
    """Directory-pointer operations. The default is a Windows junction.

    A seam, not an abstraction for its own sake: ``_winapi.CreateJunction``
    exists only on Windows, so the swap, flip and register logic is tested on
    other hosts with a subclass whose ``create`` makes a symlink, and on a real
    Windows host with this class (``TestRealWindows``).
    """

    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform

    def create(self, target: str, link: Path) -> None:
        import _winapi  # noqa: PLC0415 -- exists only on Windows

        _winapi.CreateJunction(target, str(link))

    def is_link(self, path: Path) -> bool:
        return _layout().is_link(path, platform=self.platform)

    def read(self, path: Path) -> str:
        return _layout().read_link(path, platform=self.platform)

    def remove(self, path: Path) -> None:
        _layout().remove_link(path)

    def pid_alive(self, pid: int) -> bool:
        """Whether *pid* is a live process. Used only to tell another swap's
        in-flight litter from a crashed one's.

        ``os.kill(pid, 0)`` is NOT a probe on Windows (it sends CTRL_C_EVENT to
        the target's console group), and the one place that may spell it is
        ``service_registry.pid_alive``, which this bootstrap-safe module cannot
        import (``tests/test_pid_alive_single_probe_lint.py``). The Windows
        reading asks the process table (``winproc_core``). Anywhere else the
        answer is "cannot tell: alive", which leaves another process's litter
        alone; this installer only runs on Windows, and tests override the probe.
        """
        if not _layout()._is_nt(self.platform):
            return True
        core = _sibling("winproc_core")
        try:
            return core.process_age_seconds(pid, core.ctypes_win_info_api()) is not None
        except (RuntimeError, OSError):
            return True  # cannot tell: leave it alone


def _key(text: str, platform: str | None) -> str:
    return _layout().compare_key(text, platform=platform)


def _litter_pid(litter: Path) -> int | None:
    tail = litter.name.rsplit(".", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _sweep_swap_litter(link: Path, ops: LinkOps) -> None:
    """Settle what an interrupted swap of *link* left beside it.

    A crash between the swap's two renames leaves ``.<link>.old.<pid>`` holding
    the pointer that WAS live and *link* itself missing. Deleting that litter
    (the first version did) turns a recoverable crash into a box with no
    ``current``, so the old pointer is put BACK when *link* is absent. A
    ``.new`` pointer is just unfinished work and is removed.

    Another process's litter is left alone while that process is alive (it may
    be between its renames right now). Only LINKS are touched (``ops.remove``
    refuses anything else), and only under the swap's own two names.
    """
    for kind in ("old", "new"):
        for litter in sorted(link.parent.glob(f".{link.name}.{kind}.*")):
            pid = _litter_pid(litter)
            if pid is not None and pid != os.getpid() and ops.pid_alive(pid):
                continue  # in flight
            try:
                if not ops.is_link(litter):
                    continue
                if kind == "old" and not os.path.lexists(link):
                    os.rename(litter, link)  # the crash window: restore, never delete
                else:
                    ops.remove(litter)
            except OSError:
                pass  # in use; the next swap retries


def swap_link(target: str, link: Path | str, ops: LinkOps | None = None) -> None:
    """Point the directory link *link* at *target*, replacing what it pointed at.

    Create ``.<link>.new.<pid>``, rename *link* aside, rename the new one into
    place, ``rmdir`` the old one. A failure after the first rename puts the
    original back. A *link* that exists and is NOT a link is refused rather than
    replaced: the one thing this must never do is delete a real directory.
    """
    ops = ops or LinkOps()
    link = Path(link)
    _sweep_swap_litter(link, ops)
    tag = os.getpid()
    new = link.with_name(f".{link.name}.new.{tag}")
    aside = link.with_name(f".{link.name}.old.{tag}")

    present = ops.is_link(link)
    if not present and os.path.lexists(link):
        raise GenerationError(
            f"{link} exists and is not a link; refusing to replace it"
        )
    try:
        ops.create(target, new)
    except OSError as exc:
        raise GenerationError(f"could not create a pointer to {target} beside {link}: {exc}") from exc
    try:
        if present:
            os.rename(link, aside)
        try:
            os.rename(new, link)
        except OSError:
            if present and not os.path.lexists(link):
                os.rename(aside, link)
            raise
    except OSError as exc:
        try:
            if ops.is_link(new):
                ops.remove(new)
        except OSError:
            pass
        raise GenerationError(f"could not repoint {link}: {exc}") from exc
    if present:
        try:
            ops.remove(aside)
        except OSError:
            pass  # swept by the next swap


def flip_current(
    generation: Path | str, tools: Path | None = None, *,
    ops: LinkOps | None = None, platform: str | None = None,
) -> None:
    """Move ``<tools>/current`` to *generation*, recording the outgoing one.

    ``previous`` is recorded BEFORE ``current`` moves (``flip.sh``'s ordering,
    and for its reason): a crash between the two leaves ``previous`` naming what
    is still ``current``, so a rollback is a harmless no-op rather than a trip
    to a generation two steps back.
    """
    layout = _layout()
    ops = ops or LinkOps(platform)
    gen = Path(generation)
    if not gen.is_absolute():
        raise GenerationError(f"flip target must be an absolute path, got '{generation}'")
    if not gen.is_dir():
        raise GenerationError(f"flip target is not a directory: {gen}")
    current = layout.current_link(tools=tools)
    previous = layout.previous_link(tools=tools)
    # Settle an interrupted earlier swap BEFORE reading ``current``: after a
    # crash between its renames ``current`` is absent and only the litter knows
    # what it named, so reading first would record no outgoing generation.
    _sweep_swap_litter(current, ops)
    _sweep_swap_litter(previous, ops)
    if ops.is_link(current):
        outgoing = ops.read(current)
        if outgoing and _key(outgoing, platform) != _key(str(gen), platform):
            swap_link(outgoing, previous, ops)
    swap_link(str(gen), current, ops)


def rollback_current(
    tools: Path | None = None, *, ops: LinkOps | None = None, platform: str | None = None,
) -> None:
    """Return ``current`` to what ``previous`` names; the rollback is reversible
    because the flip records the outgoing generation as the new ``previous``."""
    layout = _layout()
    ops = ops or LinkOps(platform)
    previous = layout.previous_link(tools=tools)
    _sweep_swap_litter(previous, ops)
    if not ops.is_link(previous):
        raise GenerationError("no previous generation recorded; nothing to roll back to")
    target = ops.read(previous)
    if not target or not Path(target).is_dir():
        raise GenerationError(
            f"previous generation is gone, refusing to roll back to: {target}"
        )
    flip_current(target, tools, ops=ops, platform=platform)


def register_legacy(
    legacy: Path | str, tools: Path | None = None, *,
    ops: LinkOps | None = None, platform: str | None = None,
) -> Path:
    """Put the legacy uv tree in the GC ledger: ``gen-legacy-uv-tool`` -> *legacy*.

    Idempotent (a no-op when the pointer already names it, a refresh when it
    names something else). The pointer is a junction on Windows, which GC's
    ``is_link`` reads and removes with ``rmdir``. Returns the pointer's path.
    """
    layout = _layout()
    ops = ops or LinkOps(platform)
    tree = Path(legacy)
    if not tree.is_absolute():
        raise GenerationError(f"legacy venv dir must be an absolute path, got '{legacy}'")
    root = layout._root(tools)
    root.mkdir(parents=True, exist_ok=True)
    pointer = layout.legacy_generation_link(tools=root)
    if ops.is_link(pointer) and _key(ops.read(pointer), platform) == _key(str(tree), platform):
        return pointer
    swap_link(str(tree), pointer, ops)
    return pointer


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def _run(argv: list[str], run, what: str) -> str:
    """Run one command; return its stdout, or raise with what it said."""
    # utf-8 with replacement, not ``text=True``: that decodes with the console
    # code page on Windows, and uv prints non-ASCII progress glyphs; a
    # UnicodeDecodeError out of the reader thread would replace the build's own
    # diagnostic with a decoding traceback.
    try:
        # ``run`` is the test seam. subprocess.run is NOT bound to a name here
        # and its keywords are spelt out, not unpacked: an alias or a ``**``
        # hides the call from the bounded-subprocess lint's AST scan.
        if run is not None:
            done = run(argv, capture_output=True, encoding="utf-8", errors="replace", check=False)
        else:
            done = subprocess.run(  # noqa: S603 -- fixed argv, no shell
                argv, capture_output=True, encoding="utf-8", errors="replace", check=False,
            )
    except FileNotFoundError as exc:
        raise GenerationError(
            f"{what}: {argv[0]} was not found on PATH. nx self install needs uv "
            "to build a generation."
        ) from exc
    except OSError as exc:
        raise GenerationError(f"{what}: could not run {argv[0]}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or "").strip() or (done.stdout or "").strip()
        raise GenerationError(detail or f"{what} exited {done.returncode}")
    return done.stdout or ""


def _pyvenv_value(text: str, key: str) -> str:
    """The first ``key = value`` line of a ``pyvenv.cfg``, as ``sed -n 's/^key *= *//p'``."""
    for line in text.splitlines():
        match = re.match(rf"{key} *= *(.*)$", line)
        if match:
            return match.group(1)
    return ""


def _absolute_directory_source(source: str, platform: str | None) -> str:
    """A directory source made ABSOLUTE before anything records it.

    The receipt is read back from whatever cwd a later ``nx self install`` has,
    so ``"."`` would describe the reader's directory, not the checkout built.
    """
    layout = _layout()
    path = Path(source)
    if not path.exists():
        raise GenerationError(f"directory source does not exist: {source}")
    resolved = path.resolve() if path.is_dir() else path.resolve().parent / path.name
    text = str(resolved)
    return layout.strip_extended_prefix(text) if layout._is_nt(platform) else text


def build_generation(
    source: str, *, version: str = "", extras=(), python_version: str = "3.12",
    constraints: str = "", tools: Path | None = None, run=None,
    platform: str | None = None, now=None,
) -> Path:
    """Build ONE generation at ``<tools>/gen-<stamp>`` and write its receipt.

    ``install_generation.sh`` in Python. Never writes into an existing
    generation: the stamp is claimed with a bare ``os.mkdir`` whose
    ``FileExistsError`` is the collision detector, so a tree a live process runs
    from stays byte-identical. The receipt is written last, through a temporary
    name and ``os.replace``, and is the completion marker; the ``finally`` that
    removes an unfinished tree is tidiness, not the guarantee.

    *run* replaces ``subprocess.run`` (a test seam, called with capture options)
    and *now* the clock. Returns the generation directory.
    """
    layout = _layout()
    if not source:
        raise GenerationError("--source is required (a checkout path, or a distribution name)")
    if constraints and not Path(constraints).is_file():
        raise GenerationError(f"--constraints file does not exist: {constraints}")
    overrides = Path(__file__).resolve().parent / "overrides.txt"
    if not overrides.is_file():
        raise GenerationError(f"packaged overrides file is missing: {overrides}")

    kind = layout.source_kind(source, platform=platform)
    if kind == "directory":
        source = _absolute_directory_source(source, platform)
    extras_list = [e for e in (extras.split(",") if isinstance(extras, str) else extras) if e]
    spec = layout.build_spec(source, extras_list, version)

    root = layout._root(tools)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GenerationError(f"could not create the generation root {root}: {exc}") from exc
    clock = time.time() if now is None else now()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock))
    gen: Path | None = None
    for suffix in _STAMP_SUFFIXES:
        candidate = layout.generation_dir(f"{stamp}{suffix}", tools=root)
        try:
            os.mkdir(candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise GenerationError(f"could not create the generation directory {candidate}: {exc}") from exc
        gen = candidate
        break
    if gen is None:
        raise GenerationError(
            f"could not claim a generation directory for stamp {stamp} (9 collisions)"
        )

    receipt = layout.receipt_path(gen)
    try:
        (gen / layout.BUILDING_MARKER_NAME).touch()
        _run(
            ["uv", "venv", "--allow-existing", "--python", python_version, str(gen)],
            run, "uv venv",
        )
        (gen / layout.BUILDING_MARKER_NAME).touch()  # each phase gets the full claim window
        install = [
            "uv", "pip", "install", "--python", str(layout.venv_python(gen, platform=platform)),
            "--overrides", str(overrides),
        ]
        if constraints:
            install += ["--constraints", constraints]
        _run([*install, spec], run, "uv pip install")

        cfg = (gen / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
        base_interpreter = _pyvenv_value(cfg, "home")
        python_full = _pyvenv_value(cfg, "version") or python_version
        if not base_interpreter:
            raise GenerationError(
                "pyvenv.cfg has no 'home =' line; refusing to write a receipt with an "
                f"empty base_interpreter: {gen / 'pyvenv.cfg'}"
            )
        if layout._is_nt(platform):
            # The launcher directory ``current\\bin`` resolves to: copies of this
            # generation's own launchers, so the user PATH can name one stable
            # directory and an upgrade only has to flip the junction.
            _emit(populate_launchers(gen, platform=platform))
        rendered = layout._cli_render_receipt(
            version, spec, kind, source, ",".join(extras_list), python_full,
            base_interpreter, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock)),
        )
        tmp = gen / ".nexus-install.json.tmp"
        tmp.write_text(rendered + "\n", encoding="utf-8")
        os.replace(tmp, receipt)
    except OSError as exc:
        raise GenerationError(f"building {gen} failed: {exc}") from exc
    finally:
        if not receipt.is_file():
            shutil.rmtree(gen, ignore_errors=True)
    return gen


# ---------------------------------------------------------------------------
# The legacy bridge
# ---------------------------------------------------------------------------


def legacy_extras(legacy: Path | str) -> list[str]:
    """Extras recorded in a legacy ``uv-receipt.toml``, sorted. Empty when the
    receipt is absent, unreadable, or names none.

    ``legacy.sh``'s ``nx_legacy_extras`` verbatim: a bracketed ``extras = [...]``
    block of quoted names, with ``mineru`` dropped (a default dependency now, not
    an extra, nexus-2fyb). This is the ONLY bridge for the ``[local]`` extra, so
    a failure to read it degrades to "none" the way the shell does.
    """
    receipt = Path(legacy) / "uv-receipt.toml"
    try:
        text = receipt.read_text(encoding="utf-8")
    except OSError:
        return []
    match = re.search(r"extras\s*=\s*\[([^\]]*)\]", text, re.DOTALL)
    if not match:
        return []
    names = re.findall(r'"([^"]+)"', match.group(1))
    return sorted({n for n in names if n != "mineru"})


def uv_tool_dir(run=None) -> Path:
    """``uv tool dir``, asked of uv itself (``%APPDATA%\\uv\\tools`` on Windows)."""
    out = _run(["uv", "tool", "dir"], run, "uv tool dir").strip()
    if not out:
        raise GenerationError("could not resolve 'uv tool dir'")
    return Path(out.splitlines()[-1].strip())


def _emit(lines: list[str]) -> None:
    for line in lines:
        sys.stderr.write(line + "\n")


# ---------------------------------------------------------------------------
# Launchers and the user PATH
#
# DECISION OF RECORD (Sam, 2026-10-06; T2 nexus_rdr/224-f9bgu47-windows-shims-
# decision, which supersedes the "copy launchers into ~\.local\bin" design).
# MEASURED on native Windows: a RUNNING launcher exe cannot be renamed
# (WinError 32), replaced (WinError 5) or deleted, so a shim written into the
# shared bin dir fails exactly when `nx self install` runs from that shim. The
# Windows shim is therefore a PATH ENTRY, never a file nexus writes there:
#
#   <tools>\gen-<stamp>\bin\<name>.exe   copies of the generation's own
#                                        Scripts\<name>.exe launchers. Each
#                                        embeds that generation's absolute
#                                        python path, so a process started
#                                        through <tools>\current\bin\nx.exe has
#                                        sys.prefix = the real generation.
#   HKCU\Environment Path                <tools>\current\bin, ahead of uv's bin
#
# An upgrade only flips the ``current`` junction; no launcher is replaced.
# ---------------------------------------------------------------------------

#: ``<gen>\bin``, the directory ``<tools>\current\bin`` resolves to.
LAUNCHER_DIR_NAME = "bin"

#: Overrides the user-PATH store: a file whose single line is the PATH value,
#: read and written INSTEAD of ``HKCU\Environment`` (no broadcast). It exists so
#: a sandboxed end-to-end run never touches the real registry.
USER_PATH_STORE_ENV = "NX_USER_PATH_STORE"

_REG_SZ = 1
_REG_EXPAND_SZ = 2


def launcher_dir(generation: Path | str) -> Path:
    """``<generation>\\bin``."""
    return Path(generation) / LAUNCHER_DIR_NAME


def current_launcher_dir(tools: Path | None = None) -> Path:
    """``<tools>\\current\\bin``: the one directory the user PATH names."""
    return _layout().current_link(tools=tools) / LAUNCHER_DIR_NAME


def populate_launchers(
    generation: Path | str, dist: str = "conexus", *, platform: str | None = None,
) -> list[str]:
    """Fill ``<generation>\\bin`` with copies of its own owned launchers.

    The set is exactly what the POSIX shims cover, derived the same way: the
    distribution's declared console scripts plus ``DEPENDENCY_SCRIPTS`` that
    exist, minus ``NEVER_SHIM`` (``layout_core.owned_from_declared``). Raises
    :class:`GenerationError` before copying anything when the generation cannot
    say what it declares: a partial set is worse than none. An existing copy
    with identical bytes is left alone, so this is safe on a generation whose
    launchers are running. Returns diagnostics for the caller to show.
    """
    layout = _layout()
    gen = Path(generation)
    try:
        declared, refused = layout.declared_console_scripts_detail(gen, dist, platform=platform)
    except Exception as exc:  # noqa: BLE001 -- LayoutError, OSError: all one refusal
        raise GenerationError(
            f"could not read console scripts from distribution '{dist}' in {gen} "
            f"-- refusing to write a partial launcher set: {exc}"
        ) from exc
    owned = layout.owned_from_declared(declared, gen, platform=platform)
    dest = launcher_dir(gen)
    try:
        dest.mkdir(exist_ok=True)
    except OSError as exc:
        raise GenerationError(f"could not create the launcher directory {dest}: {exc}") from exc
    lines = [
        f"nexus: refused console script {name!r} from distribution '{dist}' "
        "-- its name is not a valid launcher command, so none was written"
        for name in refused
    ]
    for name in sorted(owned):
        src = layout.venv_script(gen, name, platform=platform)
        dst = dest / layout.exe_name(name, platform=platform)
        try:
            if dst.is_file() and dst.read_bytes() == src.read_bytes():
                continue
            shutil.copyfile(src, dst)
        except OSError as exc:
            raise GenerationError(f"could not copy the launcher {src} to {dst}: {exc}") from exc
    return lines


class UserPathStore:
    """Where the persistent user PATH lives. The seam for the registry read,
    write and broadcast; subclasses below are the registry and a file."""

    kind = "abstract"

    def read(self) -> tuple[str, int]:
        """``(value, registry type)``; an absent value is ``("", REG_EXPAND_SZ)``."""
        raise NotImplementedError

    def write(self, value: str, reg_type: int) -> None:
        raise NotImplementedError

    def broadcast(self) -> None:
        """Tell running programs the environment changed. Best effort."""


class RegistryUserPath(UserPathStore):
    """``HKCU\\Environment`` ``Path``, preserving its registry type. Never HKLM."""

    kind = "HKCU\\Environment"

    def read(self) -> tuple[str, int]:
        import winreg  # noqa: PLC0415 -- exists only on Windows

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            try:
                value, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return "", _REG_EXPAND_SZ
        return str(value), int(kind)

    def write(self, value: str, reg_type: int) -> None:
        import winreg  # noqa: PLC0415 -- exists only on Windows

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "Path", 0, reg_type, value)

    def broadcast(self) -> None:
        import ctypes  # noqa: PLC0415 -- Windows only
        from ctypes import wintypes  # noqa: PLC0415

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        send = user32.SendMessageTimeoutW
        send.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_wchar_p,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        send.restype = ctypes.c_ssize_t
        result = ctypes.c_size_t()
        # HWND_BROADCAST, WM_SETTINGCHANGE, "Environment", SMTO_ABORTIFHUNG, 5 s.
        send(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))


class FileUserPath(UserPathStore):
    """The sandbox store: one line in a file. No registry, no broadcast."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.kind = str(self.path)

    def read(self) -> tuple[str, int]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "", _REG_EXPAND_SZ
        return text.split("\n", 1)[0].rstrip("\r"), _REG_EXPAND_SZ

    def write(self, value: str, reg_type: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp.{os.getpid()}")
        tmp.write_text(value + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def default_user_path_store(environ=None) -> UserPathStore:
    """The registry, unless ``NX_USER_PATH_STORE`` names a file."""
    env = os.environ if environ is None else environ
    override = (env.get(USER_PATH_STORE_ENV) or "").strip()
    return FileUserPath(override) if override else RegistryUserPath()


def uv_bin_dir(*, run=None, environ=None) -> Path:
    """Where uv puts tool launchers: ``uv tool dir --bin``, else
    ``UV_TOOL_BIN_DIR``, else ``~\\.local\\bin`` (uv's default)."""
    env = os.environ if environ is None else environ
    try:
        out = _run(["uv", "tool", "dir", "--bin"], run, "uv tool dir --bin").strip()
        if out:
            return Path(out.splitlines()[-1].strip())
    except GenerationError:
        pass
    override = (env.get("UV_TOOL_BIN_DIR") or "").strip()
    return Path(override) if override else Path.home() / ".local" / "bin"


def _expand_percent(text: str, environ) -> str:
    """``%NAME%`` expanded from *environ*, case-insensitively; unknown names stay."""
    lookup = {str(k).upper(): str(v) for k, v in environ.items()}
    return re.sub(r"%([^%]+)%", lambda m: lookup.get(m.group(1).upper(), m.group(0)), text)


def _path_key(entry: str, environ) -> str:
    """One spelling for one directory: unquoted, expanded, normalised, case-folded."""
    text = _expand_percent(entry.strip().strip('"'), environ)
    return ntpath.normcase(ntpath.normpath(text)) if text else ""


def _provides(entry: str, environ, name: str = "nx.exe") -> bool:
    text = _expand_percent(entry.strip().strip('"'), environ)
    return bool(text) and (Path(text) / name).is_file()


def _promote(entries: list[str], entry: str, uv_key: str, environ) -> list[str] | None:
    """*entries* with *entry* first, or ``None`` when it is already well placed.

    Well placed: present, ahead of uv's bin dir when that is present, and with
    no earlier directory that provides ``nx.exe`` (the last clause goes beyond
    "ahead of uv's bin" because a pip-installed ``nx.exe`` elsewhere would
    otherwise shadow it and the doctor row would fail forever). Every other
    entry is returned byte-for-byte, in order.
    """
    ekey = _path_key(entry, environ)
    keys = [_path_key(e, environ) for e in entries]
    here = [i for i, k in enumerate(keys) if k == ekey]
    if here:
        first = here[0]
        uv_at = next((i for i, k in enumerate(keys) if k == uv_key), None)
        shadowed = any(_provides(entries[i], environ) for i in range(first) if keys[i] != ekey)
        if (uv_at is None or first < uv_at) and not shadowed:
            return None
    return [entry] + [e for i, e in enumerate(entries) if i not in here]


@dataclass(frozen=True)
class PathResult:
    entry: str
    changed: bool  #: the persistent user PATH was rewritten (a restart is needed)
    process_changed: bool  #: this process's own PATH was prepended
    store: str


def restart_notice(entry: str) -> str:
    return (
        f"added {entry} to your user PATH, ahead of uv's bin directory. Terminals and "
        "Claude Code sessions started before now keep running the old nx until they "
        "are restarted; restart them to pick this up."
    )


def ensure_user_path(
    entry: Path | str, *, uv_bin: Path | str | None = None, store: UserPathStore | None = None,
    environ=None, run=None,
) -> PathResult:
    """Put *entry* first on the user PATH, and on this process's PATH.

    Reads the store, keeps its registry type and every other entry untouched,
    writes back ONLY if the PATH changed, then broadcasts ``WM_SETTINGCHANGE``
    (best effort: a hung window must not fail an install). Entries compare by
    ``normcase`` after ``%VAR%`` expansion. Never touches HKLM.
    """
    env = os.environ if environ is None else environ
    entry = str(entry)
    chosen = store if store is not None else default_user_path_store(env)
    uv_key = _path_key(str(uv_bin if uv_bin is not None else uv_bin_dir(run=run, environ=env)), env)
    try:
        value, reg_type = chosen.read()
    except OSError as exc:
        raise GenerationError(f"could not read the user PATH ({chosen.kind}): {exc}") from exc
    promoted = _promote(value.split(";") if value else [], entry, uv_key, env)
    changed = promoted is not None
    if promoted is not None:
        try:
            chosen.write(";".join(promoted), reg_type)
        except OSError as exc:
            raise GenerationError(f"could not write the user PATH ({chosen.kind}): {exc}") from exc
        try:
            chosen.broadcast()
        except Exception:  # noqa: BLE001 -- best effort by contract
            pass
    process = env.get("PATH", "")
    process_new = _promote(process.split(";") if process else [], entry, uv_key, env)
    if process_new is not None:
        env["PATH"] = ";".join(process_new)
    return PathResult(entry, changed, process_new is not None, chosen.kind)


@dataclass(frozen=True)
class PathRemoval:
    entry: str
    removed: int  #: entries naming *entry* that were taken off the user PATH
    store: str


def remove_user_path(
    entry: Path | str, *, store: UserPathStore | None = None, environ=None,
    dry_run: bool = False,
) -> PathRemoval:
    """Take *entry* off the persistent user PATH: the inverse of
    :func:`ensure_user_path`, for ``nx uninstall`` (RDR-224, nexus-7xzc1).

    Every entry naming the same directory goes (compared as
    :func:`ensure_user_path` compares them: unquoted, ``%VAR%`` expanded,
    ``normcase``). Every other entry is written back byte-for-byte, unexpanded,
    in order, empty ones included, under the value's own registry type. Writes
    and broadcasts ``WM_SETTINGCHANGE`` only when something was removed; the
    broadcast is best effort. This process's PATH is left alone. ``dry_run``
    counts what would go and writes nothing.
    """
    env = os.environ if environ is None else environ
    entry = str(entry)
    chosen = store if store is not None else default_user_path_store(env)
    try:
        value, reg_type = chosen.read()
    except OSError as exc:
        raise GenerationError(f"could not read the user PATH ({chosen.kind}): {exc}") from exc
    entries = value.split(";") if value else []
    ekey = _path_key(entry, env)
    kept = [e for e in entries if _path_key(e, env) != ekey]
    removed = len(entries) - len(kept)
    if removed and not dry_run:
        try:
            chosen.write(";".join(kept), reg_type)
        except OSError as exc:
            raise GenerationError(f"could not write the user PATH ({chosen.kind}): {exc}") from exc
        try:
            chosen.broadcast()
        except Exception:  # noqa: BLE001 -- best effort by contract
            pass
    return PathRemoval(entry, removed, chosen.kind)


def inspect_user_path(
    entry: Path | str, *, store: UserPathStore | None = None, environ=None,
) -> list[str]:
    """What is wrong with the user PATH entry for *entry*; empty means healthy.

    Healthy: ``<entry>\\nx.exe`` exists, the entry is on the PERSISTED user PATH
    (not this process's, which may predate it), and no directory ahead of it
    provides ``nx.exe``.
    """
    env = os.environ if environ is None else environ
    entry = str(entry)
    chosen = store if store is not None else default_user_path_store(env)
    problems: list[str] = []
    if not (Path(entry) / "nx.exe").is_file():
        problems.append(f"{Path(entry) / 'nx.exe'} does not exist")
    try:
        value, _ = chosen.read()
    except OSError as exc:
        return [*problems, f"the user PATH could not be read ({chosen.kind}): {exc}"]
    entries = value.split(";") if value else []
    ekey = _path_key(entry, env)
    keys = [_path_key(e, env) for e in entries]
    if ekey not in keys:
        problems.append(f"{entry} is not on the user PATH")
        return problems
    for i in range(keys.index(ekey)):
        if keys[i] != ekey and _provides(entries[i], env):
            problems.append(f"{entries[i]} is ahead of it on the user PATH and provides nx.exe")
    return problems


def legacy_launcher_names(legacy: Path | str) -> list[str]:
    """The launcher names uv recorded for the legacy conexus tree, from its
    ``uv-receipt.toml`` ``entrypoints`` block. Empty when unreadable. Names pass
    the layout's allowlist, so none can reach outside a directory."""
    layout = _layout()
    try:
        text = (Path(legacy) / "uv-receipt.toml").read_text(encoding="utf-8")
    except OSError:
        return []
    block = re.search(r"entrypoints\s*=\s*\[([^\]]*)\]", text, re.DOTALL)
    if not block:
        return []
    names = re.findall(r'name\s*=\s*"([^"]+)"', block.group(1))
    return sorted({n for n in names if layout._COMPONENT_RE.match(n)})


def declared_launcher_names(generation: Path | str, *, platform: str | None = None) -> list[str]:
    """The distribution's declared console scripts (no dependency scripts: a
    separately installed ``mineru`` in uv's bin dir is not ours to delete)."""
    try:
        declared, _ = _layout().declared_console_scripts_detail(Path(generation), platform=platform)
    except Exception:  # noqa: BLE001 -- cannot ask: delete nothing
        return []
    return sorted(declared)


def remove_uv_launchers(
    uv_bin: Path | str, names, current_bin: Path | str, *, dry_run: bool = False,
    platform: str | None = None,
) -> list[str]:
    """Best-effort removal of uv's conexus launchers from uv's bin dir.

    Only files named ``<name>.exe`` for a NAME in *names*, only inside *uv_bin*,
    and only when ``<current_bin>\\<name>.exe`` exists to take over, so a box
    whose replacement is missing is never left without an ``nx``. A launcher
    that is locked (running) is ``kept (in use)``: the next run retries.
    """
    layout = _layout()
    lines: list[str] = []
    for name in sorted(set(names)):
        target = Path(uv_bin) / layout.exe_name(name, platform=platform)
        if not target.is_file():
            continue
        if not (Path(current_bin) / layout.exe_name(name, platform=platform)).is_file():
            lines.append(f"kept {target}: no replacement in {current_bin}")
            continue
        if dry_run:
            lines.append(f"would remove {target}")
            continue
        try:
            target.unlink()
        except OSError:
            lines.append(f"kept {target}: in use")
        else:
            lines.append(f"removed {target}")
    return lines


def repair_layout(
    tools: Path | None = None, *, dry_run: bool = False, ops: LinkOps | None = None,
    platform: str | None = None, store: UserPathStore | None = None,
    uv_bin: Path | str | None = None, run=None, environ=None,
) -> list[str]:
    """Finish or re-establish what a generation layout needs on Windows.

    The resumable half of a migration, and the Windows meaning of "repair a uv
    takeover": uv rewriting its own launchers is harmless because ``current\\bin``
    precedes them, so the repair is to RE-ENSURE the pieces. In order, each only
    when needed: the current generation's launcher directory (a generation built
    before launcher directories existed has none), the user PATH entry, and the
    legacy uv tree's ledger junction. A migration that built a generation and
    flipped ``current`` but failed before the rest is completed by the next
    ``nx self install`` from the legacy launcher, through here. ``[]`` when there
    is no generation layout or nothing is missing.
    """
    layout = _layout()
    root = layout._root(tools)
    ops = ops or LinkOps(platform)
    try:
        current = layout.current_generation(tools=root, platform=platform)
    except layout.LayoutError:
        return []
    if not current.is_dir():
        return []
    entry = current_launcher_dir(root)
    lines: list[str] = []
    if not (launcher_dir(current) / layout.exe_name("nx", platform=platform)).is_file():
        lines.append(f"building the launcher directory {launcher_dir(current)}")
        if not dry_run:
            lines.extend(populate_launchers(current, platform=platform))
    problems = inspect_user_path(entry, store=store, environ=environ)
    if problems and not dry_run:
        # The launcher was just built; anything still reported is the PATH.
        result = ensure_user_path(entry, uv_bin=uv_bin, store=store, environ=environ, run=run)
        problems = inspect_user_path(entry, store=store, environ=environ)
        lines.append(f"user PATH repaired: {entry} first")
        if result.changed:
            lines.append(restart_notice(str(entry)))
    elif problems:
        lines.append("user PATH: " + "; ".join(problems) + f"; would put {entry} first")
    legacy = layout.uv_conexus_venv(platform=platform)
    if layout.venv_bin(legacy, platform=platform).is_dir():
        pointer = layout.legacy_generation_link(tools=root)
        registered = ops.is_link(pointer) and _key(ops.read(pointer), platform) == _key(
            str(legacy), platform,
        )
        if not registered:
            lines.append(f"registering uv's tree at {legacy} for reap")
            if not dry_run:
                register_legacy(legacy, root, ops=ops, platform=platform)
    return lines


def migrate_legacy(
    source: str, *, version: str = "", python_version: str = "",
    legacy_venv: Path | str | None = None, tools: Path | None = None, run=None,
    ops: LinkOps | None = None, platform: str | None = None,
    store: UserPathStore | None = None, uv_bin: Path | str | None = None,
    environ=None, on_path=None,
) -> Path | None:
    """Converge a legacy ``uv tool install conexus`` tree onto the generation layout.

    ``migrate_legacy.sh`` in Python, in its order and for its reasons: read the
    uv receipt's extras one last time (the only bridge for ``[local]``), build
    side-by-side leaving uv's tree untouched, flip, put ``current\\bin`` first on
    the user PATH (the Windows shim step), and register the legacy tree for a
    LATER, SEPARATE reap. This never reaps and never runs ``uv tool uninstall``
    (which deletes the tree every live holder is running from). A failure after
    the flip is finished by :func:`repair_layout`, not rebuilt.

    *on_path* receives the :class:`PathResult`. Returns the new generation, or
    ``None`` when uv resolves and there is no legacy tree to migrate.
    """
    layout = _layout()
    legacy = Path(legacy_venv) if legacy_venv else uv_tool_dir(run) / "conexus"
    if not legacy.is_dir():
        return None
    if not source:
        raise GenerationError("--source is required (a checkout path, or a distribution name)")

    root = layout._root(tools)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise GenerationError(f"could not create the generation root {root}: {exc}") from exc

    extras = legacy_extras(legacy)
    gen = build_generation(
        source, version=version, extras=extras,
        python_version=python_version or "3.12", tools=root, run=run, platform=platform,
    )
    flip_current(gen, root, ops=ops, platform=platform)
    result = ensure_user_path(
        current_launcher_dir(root), uv_bin=uv_bin, store=store, environ=environ, run=run,
    )
    if on_path is not None:
        on_path(result)
    register_legacy(legacy, root, ops=ops, platform=platform)
    return gen


# ---------------------------------------------------------------------------
# Reaping
# ---------------------------------------------------------------------------


def reap(
    tools: Path | None = None, *, keep: int = 3, self_generation: Path | str | None = None,
    dry_run: bool = False, platform: str | None = None,
) -> list[str]:
    """``gc_core.gc_generations`` with the operator knobs the CLI face reads.

    Never raises for a refusal: a ``--keep`` the sweep will not honour is said on
    stderr and nothing is deleted, which is the safe direction.
    """
    gc = _gc()
    try:
        return gc.gc_generations(
            tools, keep=keep, self_generation=str(self_generation) if self_generation else "",
            dry_run=dry_run, platform=platform,
            grace_minutes=gc._env_minutes(
                "NX_GC_BUILD_GRACE_MINUTES", gc.DEFAULT_BUILD_GRACE_MINUTES),
            claim_minutes=gc._env_minutes(
                "NX_GC_BUILD_CLAIM_MINUTES", gc.DEFAULT_BUILD_CLAIM_MINUTES),
        )
    except gc.GCError as exc:
        sys.stderr.write(f"nexus: {exc}\n")
        return []
