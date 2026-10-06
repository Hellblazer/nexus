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
reaches ``layout_core``, ``shims_core`` and ``gc_core`` through ``_sibling`` so
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
  renames; nothing on Windows resolves ``current`` at spawn (the shims bind to a
  generation directly, see ``shims_core``), so that span is not a spawn failure.
  This works while a process runs THROUGH the junction.
* A junction is removed with ``os.rmdir``, never ``shutil.rmtree``, which would
  empty the generation it names.
* The venv's executables are in ``Scripts\\`` with ``.exe`` suffixes.

Renaming a venv directory SUCCEEDS while its ``python.exe`` runs, so a rename is
NOT a probe for "is anything running from here"; the holder census is.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: The attempts at claiming a stamp: the bare stamp, then ``a`` .. ``h``. The
#: shell half's own list.
_STAMP_SUFFIXES = ("", "a", "b", "c", "d", "e", "f", "g", "h")

_LAYOUT: object | None = None
_SHIMS: object | None = None
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


def _shims():
    global _SHIMS
    if _SHIMS is None:
        _SHIMS = _sibling("shims_core")
    return _SHIMS


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


def _key(text: str, platform: str | None) -> str:
    return _layout().compare_key(text, platform=platform)


def _sweep_swap_litter(link: Path, ops: LinkOps) -> None:
    """Remove pointers an interrupted swap of *link* left beside it.

    Only LINKS are removed (``ops.remove`` refuses anything else), and only
    under the swap's own two names, so nothing else in the directory is at risk.
    """
    for kind in ("new", "old"):
        for litter in link.parent.glob(f".{link.name}.{kind}.*"):
            try:
                if ops.is_link(litter):
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
    ops.create(target, new)
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
    runner = run if run is not None else subprocess.run
    try:
        done = runner(argv, capture_output=True, text=True, check=False)  # noqa: S603 -- fixed argv, no shell
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
    root.mkdir(parents=True, exist_ok=True)
    clock = time.time() if now is None else now()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(clock))
    gen: Path | None = None
    for suffix in _STAMP_SUFFIXES:
        candidate = layout.generation_dir(f"{stamp}{suffix}", tools=root)
        try:
            os.mkdir(candidate)
        except FileExistsError:
            continue
        gen = candidate
        (gen / layout.BUILDING_MARKER_NAME).touch()
        break
    if gen is None:
        raise GenerationError(
            f"could not claim a generation directory for stamp {stamp} (9 collisions)"
        )

    receipt = layout.receipt_path(gen)
    try:
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
        rendered = layout._cli_render_receipt(
            version, spec, kind, source, ",".join(extras_list), python_full,
            base_interpreter, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock)),
        )
        tmp = gen / ".nexus-install.json.tmp"
        tmp.write_text(rendered + "\n", encoding="utf-8")
        os.replace(tmp, receipt)
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


def write_shims(
    generation: Path, bin_dir: Path | None = None, dist: str = "conexus", *,
    platform: str | None = None,
) -> list[str]:
    """``shims_core.write_shims`` with its diagnostics returned to the caller."""
    shims = _shims()
    try:
        return shims.write_shims(generation, bin_dir, dist, platform=platform)
    except shims.ShimsError as exc:
        raise GenerationError(str(exc)) from exc


def migrate_legacy(
    source: str, *, version: str = "", python_version: str = "",
    legacy_venv: Path | str | None = None, dist: str = "conexus",
    tools: Path | None = None, bin_dir: Path | None = None, run=None,
    ops: LinkOps | None = None, platform: str | None = None,
) -> Path | None:
    """Converge a legacy ``uv tool install conexus`` tree onto the generation layout.

    ``migrate_legacy.sh`` in Python, in its order and for its reasons: read the
    uv receipt's extras one last time (the only bridge for ``[local]``), build
    side-by-side leaving uv's tree untouched, flip, replace uv's launcher copies
    with nexus-owned shims, and register the legacy tree for a LATER, SEPARATE
    reap. This never reaps and never runs ``uv tool uninstall`` (which deletes
    the tree every live holder is running from).

    Returns the new generation, or ``None`` when uv resolves and there is no
    legacy tree to migrate (the documented clean no-op).
    """
    layout = _layout()
    legacy = Path(legacy_venv) if legacy_venv else uv_tool_dir(run) / "conexus"
    if not legacy.is_dir():
        return None
    if not source:
        raise GenerationError("--source is required (a checkout path, or a distribution name)")

    root = layout._root(tools)
    shim_dir = bin_dir if bin_dir is not None else layout.bin_dir()
    root.mkdir(parents=True, exist_ok=True)

    extras = legacy_extras(legacy)
    gen = build_generation(
        source, version=version, extras=extras,
        python_version=python_version or "3.12", tools=root, run=run, platform=platform,
    )
    flip_current(gen, root, ops=ops, platform=platform)
    _emit(write_shims(gen, shim_dir, dist, platform=platform))
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
