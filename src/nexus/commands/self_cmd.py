# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx self install`` — install a new generation from the running one.

nexus-utpuw.14 (P6a).

THE ENABLING INSIGHT. Under side-by-side generations a SELF-install is safe by
construction: the running nx builds a NEW tree and never mutates its own. That
is impossible with an in-place swap, which is why this command could not exist
before — ``uv tool install --reinstall`` rebuilds the tree the running process
is executing from, and nexus-q3xrx is the list of ways that goes wrong.

ONE IMPLEMENTATION, NOT TWO. The machinery ships inside the package
(``nexus/_install/*.sh``, verified present in the built wheel).
``scripts/reinstall-tool.sh`` is a thin repo wrapper around the same scripts
and this command execs the packaged copy, so there is no second installer to
keep in parity and no parity test to write.

THE HAZARD. This runs FROM generation N while it builds N+1 and then reaps.
GC rule (d) — never delete the generation hosting the running installer — is
the only thing between that and deleting the tree underneath the running
process. The rule lives in ``gc.sh`` and is proved there; what has to be true
HERE is that this caller actually passes it, which is a separate fact with its
own test.

IT ALSO CREATES THE FIRST GENERATION (nexus-gu9zo). Originally this command
could only upgrade a box that was ALREADY on the generation layout, and
refused everywhere else as "a dev checkout". That made the layout unreachable
from a packaged install: a fresh `uv tool install conexus` — the documented
route, README:31 — lands on the legacy uv-owned-symlink layout, so the only
generation boxes in existence were checkout-driven, and .7's migration had no
caller. It now distinguishes three sites rather than two:

  generation      -> build the next one (the original behaviour, unchanged)
  legacy uv tool  -> converge it via the packaged migrate_legacy.sh
  dev checkout    -> refuse, and name scripts/reinstall-tool.sh

NATIVE WINDOWS (RDR-224, nexus-f9bgu.47). ``bash`` on Windows is the WSL
launcher, so every ``_sh``/``bash`` call here has a Windows twin that calls
``nexus._install.generation_core`` directly: build with uv, flip junctions,
put ``<tools>\\current\\bin`` first on the user PATH (the Windows shim: nexus
writes no launcher into uv's bin dir, because a running one cannot be
replaced), reap with Windows-aware rules, migrate a legacy uv tree. Each branch
asks ``_is_windows()``; the POSIX path below it is unchanged. Out of scope on
Windows: the dev-checkout reinstall script.

SCOPE FENCE. This replaces the MECHANISM of ``uv tool upgrade conexus``. It
does NOT merge that with ``nx upgrade``. RDR-143 CA-2 keeps them two commands
deliberately — binary upgrade versus migration ladder — and ``nx upgrade``
today never invokes uv or pip at all. Merging them is a separate RDR, and
doing it by accident here is the specific thing the fence forbids.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import click

from nexus.bounded_subprocess import run_bounded
from nexus.util.nx_argv import argv_off_cwd

if TYPE_CHECKING:
    from importlib.metadata import EntryPoints

__all__ = ["self_group", "perform_self_install", "packaged_install_dir", "index_failure_hint"]


def packaged_install_dir() -> Path:
    """The install machinery as it ships INSIDE the package.

    Resolved through ``importlib.resources`` rather than relative to this
    file, so it works from a wheel install and from an editable checkout
    alike. The repo wrapper (.8) uses its own copy by path; this is the half
    that has to keep working after a release.
    """
    from importlib.resources import files  # noqa: PLC0415 — stdlib, deferred

    return Path(str(files("nexus") / "_install"))


def running_generation() -> Path:
    """The generation THIS process is executing from.

    ``sys.prefix`` is the venv root, and under this layout a generation IS a
    venv root — the same identity ``install_layout.is_stale`` compares against
    ``current``. Deliberately NOT ``current``: the running process may have
    spawned from an older generation and still be running from it, which is
    the entire point of the design, and reaping on the strength of ``current``
    would delete exactly that tree.
    """
    return Path(sys.prefix)


def _is_windows(platform: str | None = None) -> bool:
    """True on native Windows. *platform* follows the ``nexus._winsec`` seam:
    ``"win32"`` forces the Windows reading, anything else forces POSIX, and
    ``None`` asks ``os.name``. Tests that drive the Windows branches on another
    host patch THIS function, so there is one place the reading is decided."""
    return (platform == "win32") if platform is not None else (os.name == "nt")


def _plat() -> str | None:
    """The platform argument the layout helpers take: ``"win32"`` when
    :func:`_is_windows` says so, else ``None`` (the host's own reading)."""
    return "win32" if _is_windows() else None


def _generation():
    """``nexus._install.generation_core``, imported only where it is used so a
    POSIX run never loads it."""
    from nexus._install import generation_core  # noqa: PLC0415 — Windows branches only

    return generation_core


def _same_dir(left: Path, right: Path) -> bool:
    """Whether two spellings name one directory: ``==`` on POSIX, the Windows
    folded real path (case, extended prefix, 8.3) on Windows."""
    if not _is_windows():
        return left == right
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    return install_layout.compare_key(left, platform="win32") == install_layout.compare_key(
        right, platform="win32",
    )


def _is_generation_name(name: str) -> bool:
    """``gen-*``; on Windows without regard to case, because ``sys.prefix`` can
    be spelt in any case NTFS accepts."""
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    prefix = install_layout.GENERATION_PREFIX
    return (name.lower().startswith(prefix) if _is_windows() else name.startswith(prefix))


class _WindowsBuild:
    """What a Windows generation build needs: there is no bash argv to hand over."""

    def __init__(self, source: str, extras: list[str], version: str | None) -> None:
        self.source = source
        self.extras = extras
        self.version = version

    def describe(self) -> str:
        parts = ["generation_core.build_generation", "--source", self.source]
        if self.extras:
            parts += ["--extras", ",".join(self.extras)]
        if self.version:
            parts += ["--version", self.version]
        return " ".join(parts)


def perform_self_install(
    *, keep: int = 3, version: str | None = None, dry_run: bool = False,
    add_extras: tuple[str, ...] = (), bootstrap: bool = False,
) -> Path | None:
    """Build a new generation from the running one's receipt, flip, reap.

    *add_extras* (nexus-pffc4) MERGES with the receipt's existing extras —
    never replaces them — so ``nx self install --extras local`` is the
    supported way to ADD an extra to an existing generation install (the
    legacy answer, ``uv tool install --reinstall "conexus[local]"``, rebuilds
    the uv tree over the nexus-owned shims on a migrated box). The merged
    set travels through ``install_generation.sh --extras`` and is rendered
    by ``install_layout.build_spec``, so spec and extras cannot disagree.

    *bootstrap* (RDR-228, nexus-sv2wx.14) is the explicit first-install path:
    see :func:`_bootstrap_install`. It is the only way into that path; nothing
    is inferred from where this process runs, and without it every refusal
    below is unchanged.

    Returns the new generation, or ``None`` on a dry run.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    new_extras = _normalize_extras(add_extras)

    install_dir = packaged_install_dir()
    tools = install_layout.tools_dir()
    bin_dir = install_layout.bin_dir()
    if bootstrap:
        return _bootstrap_install(
            install_dir, tools, bin_dir, version=version, extras=new_extras, dry_run=dry_run,
        )
    host = running_generation()

    # THREE SITES, NOT TWO (nexus-gu9zo). The original guard asked one
    # path-shape question -- "am I a generation?" -- and treated every No as a
    # dev checkout. That is wrong for the commonest install in existence: a
    # packaged `uv tool install conexus`, which is what README:31 tells people
    # to run. Measured 2026-08-27 in a fenced probe: a FRESH install of the
    # current release lands on the legacy uv-owned-symlink layout with no
    # gen-*, no `current`, and no <tools> directory at all. So the refusal fired
    # on the documented install route and sent the reader to a repo script they
    # have no copy of, which left commit 047dd80e7's migration -- written
    # expressly to converge that layout -- reachable only by cloning the repo.
    if _same_dir(host.parent, tools) and _is_generation_name(host.name):
        pass  # a generation: fall through to the upgrade path below
    elif new_extras:
        # nexus-pffc4: --extras is a GENERATION-install surface. The legacy
        # convergence/repair branches below derive extras from their own
        # bridging rules (see _converge_legacy_install's deliberate
        # NO --extras); silently dropping a requested extra there would be
        # the green-then-degraded shape the [local] extra exists to prevent.
        # The message covers every non-generation shape this branch
        # intercepts (round-2 review [23834]): a legacy uv tree converges, a
        # uv takeover repairs, a dev checkout uses the repo script — each
        # via the plain `nx self install` (or reinstall-tool.sh) it names.
        raise click.ClickException(
            "--extras applies to a generation install, and this nx is not "
            "running from one. Get onto the generation layout first — "
            "`nx self install` with no --extras converges a legacy uv tree "
            "or repairs a uv takeover; a dev checkout uses "
            "scripts/reinstall-tool.sh — then re-run "
            "`nx self install --extras ...` from the generation."
        )
    elif _running_from_legacy_tool_install():
        # A uv tree beside an EXISTING generation layout is a takeover, not a
        # box that never migrated: `uv tool install --force conexus` (or a
        # stray upgrade) rebuilt the tree and re-pointed the shims, so this
        # process is running from uv's tree while `current` still names the
        # generation the user actually had -- extras included. Converging
        # through migrate_legacy.sh here would bridge extras from the REBUILT
        # uv receipt, which is exactly the receipt that dropped [local].
        # Repair instead: shims back to current, tree registered for reap,
        # and a generation at uv's version from current's OWN receipt when
        # the user's intent was an upgrade (nexus-hibpr follow-on).
        if _generation_layout_present(tools):
            for line in repair_uv_takeover(dry_run=dry_run):
                click.echo(line)
            return None
        return _converge_legacy_install(
            install_dir, tools, version=version, dry_run=dry_run,
        )
    else:
        # A genuine dev checkout. Unchanged: from a checkout's .venv there is
        # no receipt, and the first version of this died with a raw
        # InstallLayoutError naming a missing JSON file -- which tells the
        # reader nexus is broken when the truth is that they are standing
        # somewhere the command does not apply. The repo has a script for that
        # case; say so.
        raise click.ClickException(
            f"this nx is running from {host}, which is not a generation under "
            f"{tools} — `nx self install` upgrades a generation install in "
            "place-safe fashion and has nothing to do from a dev checkout.\n"
            "For a checkout, use: scripts/reinstall-tool.sh"
        )

    # WHAT TO INSTALL comes from the receipt of the generation we are running
    # from, not from `current`: a self-install reproduces THIS process's
    # install, and on a box whose current has already moved those differ.
    receipt = install_layout.read_receipt(host)
    extras = _merge_extras(receipt.extras, new_extras)
    build = _build_request(install_dir, receipt, version=version, extras=extras)
    if dry_run:
        click.echo(build.describe() if isinstance(build, _WindowsBuild) else " ".join(build))
        return None
    generation = _build_flip_shims(build, install_dir=install_dir, tools=tools, bin_dir=bin_dir)

    # RULE (d) IS PASSED HERE. --self names the generation hosting this very
    # process; without it the reap below is free to delete the tree these
    # lines are executing from.
    for line in _reap_generations(install_dir, tools, keep=keep, self_generation=host):
        click.echo(line)

    # A HYBRID BOX CONVERGES HERE, NOT IN THE elif ABOVE. A generation layout
    # beside a legacy `uv tool install` tree takes the generation branch every
    # time, so `_converge_legacy_install` -- the only thing that ever put the
    # legacy tree in gc.sh's ledger -- was unreachable on exactly the boxes
    # that have one. Every checkout-driven generation box is that box
    # (nexus-hibpr; measured 2026-08-27 with 8 processes still bound to an
    # unregistered 7.19.0 tree while doctor reported nothing older than
    # current). Registration is the whole convergence: the NEXT install's reap
    # removes it the moment nothing runs from it.
    #
    # AFTER the reap above, deliberately. .7's two-pass rule -- register on
    # one pass, reap on a later, separate one -- is what keeps "zero holders
    # right now" from being read as "safe to delete right now" (the accepted
    # stray-`uv tool upgrade` window). Registering first would let this very
    # reap delete the tree in the same process that just discovered it; the
    # test for this ordering reaped a free tree exactly that way.
    _register_legacy_tree_if_present(install_dir, tools)
    return generation


@dataclass(frozen=True)
class _RegistryReceipt:
    """What a bootstrap build asks for, in the shape :func:`_build_request`
    reads from a receipt: the distribution name from the package index, and no
    extras (the documented first install records none; local mode's embedding
    model belongs to the service, which ``nx init`` provisions)."""

    source: str = "conexus"
    extras: tuple[str, ...] = ()


def _running_version() -> str:
    """The version of the conexus this process is running."""
    from importlib.metadata import PackageNotFoundError, version as dist_version  # noqa: PLC0415 — stdlib, deferred

    try:
        return dist_version("conexus")
    except PackageNotFoundError as exc:
        raise click.ClickException(
            "--bootstrap installs the version of the conexus that is running this "
            "command, and this process has no conexus distribution metadata. "
            "Nothing was changed."
        ) from exc


def _current_generation_dir(tools: Path) -> Path | None:
    """The directory ``<tools>/current`` names, when it resolves to one that
    exists; ``None`` for an absent, dangling or unreadable pointer."""
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    try:
        target = install_layout.current_generation(tools=tools, platform=_plat())
    except Exception:  # noqa: BLE001 — no pointer, unreadable root: no current generation
        return None
    return target if target.is_dir() else None


def _read_receipt_or_none(generation: Path):
    """*generation*'s receipt, or ``None`` when it has none or cannot be read."""
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    try:
        return install_layout.read_receipt(generation)
    except Exception:  # noqa: BLE001 — missing, corrupt or unknown-schema: not receipted
        return None


def _shims_are_in_place(tools: Path, bin_dir: Path) -> bool:
    """Whether the shim step's output is there: the part of an install that comes
    AFTER the flip and is what puts ``nx`` on the PATH.

    POSIX: ``<bin>/nx`` exists and is a shim this install wrote
    (``shims_core._is_ours``) or a symlink into the ``uv tool`` conexus tree.
    Windows: ``<tools>\\current\\bin\\nx.exe`` exists and that directory is on the
    user PATH store (``inspect_user_path`` also reports a directory ahead of it
    that provides ``nx.exe``; the shim step repairs that too, so it is unfinished)."""
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    if _is_windows():
        core = _generation()
        return not core.inspect_user_path(core.current_launcher_dir(tools))
    from nexus._install import shims_core  # noqa: PLC0415 — deferred import

    nx = bin_dir / "nx"
    if shims_core._is_ours(nx, tools / install_layout.CURRENT_LINK_NAME):
        return True
    return nx.is_symlink() and nx.exists() and install_layout.is_under_uv_tool_install(nx)


#: The most of a program read to find its shebang line (``_cli_lookup._SHEBANG_BYTES``).
_SHEBANG_BYTES = 8192


def _python_link_dangles(venv: Path) -> bool:
    """Whether a ``python*`` link in ``<venv>/bin`` points nowhere: what ``uv`` leaves
    when it prunes the managed CPython the venv was built on."""
    try:
        with os.scandir(venv / "bin") as entries:
            return any(
                e.name.startswith("python") and e.is_symlink() and not os.path.exists(e.path) for e in entries
            )
    except OSError:
        return False


def _shebang_interpreter_missing(program: Path) -> bool:
    """Whether *program*'s ``#!`` line names an interpreter that cannot be started: an
    absolute path that is missing or not executable, or ``/usr/bin/env <name>`` with
    no such ``<name>`` on the absolute ``PATH`` entries. Relative interpreters, ``env``
    with options or an assignment, and any file with no ``#!`` line are not judged.
    The plugin's ``_cli_lookup._shebang_problem`` states the same rule."""
    try:
        with open(program, "rb") as handle:
            head = handle.readline(_SHEBANG_BYTES)
    except OSError:
        return False
    if not head.startswith(b"#!") or (len(head) >= _SHEBANG_BYTES and not head.endswith(b"\n")):
        return False
    fields = head[2:].decode("utf-8", "replace").split()
    if not fields:
        return False
    interpreter = fields[0]
    if os.path.basename(interpreter) == "env":
        name = fields[1] if len(fields) > 1 else ""
        if not name or name.startswith("-") or "=" in name or os.path.dirname(name):
            return False
        import shutil  # noqa: PLC0415 — stdlib, deferred

        entries = [e for e in os.environ.get("PATH", "").split(os.pathsep) if os.path.isabs(e)]
        return shutil.which(name, path=os.pathsep.join(entries)) is None
    if not os.path.isabs(interpreter):
        return False
    return not os.path.exists(interpreter) or not os.access(interpreter, os.X_OK)


def _generation_interpreter_gone(generation: Path, *, windows: bool | None = None) -> bool:
    """Whether a venv (a generation, or the legacy ``uv tool`` tree) cannot start
    because the Python it was built on is gone. The plugin's
    ``_cli_lookup.cannot_start`` states the same rule for a program, and
    ``tests/hooks/test_cli_cannot_start.py`` runs the two over one matrix.

    Windows: ``pyvenv.cfg`` names a ``home`` that is not a directory, which is what
    ``uv`` leaves when it prunes a managed CPython (the pipx#146 / uv#8028 class;
    ``Receipt.base_interpreter`` records the same fact for ``nx doctor``). POSIX (Sam,
    2026-10-14): a gone ``home`` alone says nothing, because the venv's ``bin/python``
    is a link and the interpreter can find its way back through it; it counts beside
    a ``python*`` link that dangles, and a console script whose own shebang names a
    missing interpreter counts alone. A venv with no ``pyvenv.cfg``, or one with no
    ``home`` line, gives no evidence from the cfg."""
    windows = _is_windows() if windows is None else windows
    try:
        text = (generation / "pyvenv.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    home = _generation()._pyvenv_value(text, "home").strip() if text else ""
    if home and not Path(home).is_dir() and (windows or _python_link_dangles(generation)):
        return True
    if windows:
        return False
    # The two console scripts a generation always has (order is the plugin's; not an argv).
    return any(_shebang_interpreter_missing(generation / "bin" / name) for name in ("nx-mcp", "nx"))


def _existing_install(tools: Path, bin_dir: Path) -> str | None:
    """Words naming a COMPLETE install on this box, or ``None``.

    Complete means ``current`` resolves to a receipted generation that can run
    (its Python is not gone, :func:`_generation_interpreter_gone`) AND the shim
    step's output is in place (:func:`_shims_are_in_place`), or a
    ``uv tool install conexus`` tree exists that can run. A tree or generation whose
    interpreter was pruned is not complete: the plugin's launcher serves the stand-in
    for it and its ``setup`` runs ``--bootstrap`` to repair it (RDR-228 Phase 2 Step 4,
    nexus-sv2wx.17; Sam, 2026-10-14), and a refusal here would leave the repair with
    nothing to run. That holds for the legacy tree (the README's own install path) as
    much as for a generation, and a broken ``current`` generation is repaired even
    when a working legacy tree sits beside it: the plugin's lookup is layout-first, so
    the broken generation is the one it runs and the one to replace.
    Neither half alone will do: the
    receipt is written when the build ends, before ``current``, and ``current``
    is flipped before the shims, so a run that died at either point leaves state
    that nothing runs and that the plain command cannot be run from; see
    :func:`_unfinished_generation`. A uv tree is complete by uv's own standard
    and carries its own ``nx``."""
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    current = _current_generation_dir(tools)
    if current is not None and _read_receipt_or_none(current) is not None:
        if _generation_interpreter_gone(current):
            return None
        if _shims_are_in_place(tools, bin_dir):
            return f"generation {current.name} under {tools}"
    legacy = install_layout.uv_conexus_venv(platform=_plat())
    if install_layout.venv_bin(legacy, platform=_plat()).is_dir() and not _generation_interpreter_gone(legacy):
        return f"a `uv tool` install at {legacy}"
    return None


def _generation_answers_for_its_shims(generation: Path) -> bool:
    """Whether *generation*'s own interpreter answers the console-script lookup
    the shim step makes, and the answer includes ``nx``. A generation that does
    not (its interpreter is gone, its distribution declares nothing) would
    flip and then shim nothing."""
    try:
        return "nx" in _shim_names_of(generation)
    except Exception:  # noqa: BLE001 — LayoutError, OSError: the generation cannot say
        return False


def _shim_names_of(generation: Path) -> list[str]:
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    return sorted(install_layout.owned_shim_names(generation, platform=_plat()))


def _unfinished_generation(tools: Path, running: str) -> Path | None:
    """The generation a bootstrap that died after its build can finish, or ``None``.

    Reached only when no complete install exists. The candidates are the
    generation ``current`` names (a run that died after the flip) or, when it
    names none, every receipted generation, newest first. A candidate is
    finished only when it is what a fresh build would produce: its receipt says
    *running* and its interpreter answers the shim step's lookup. Otherwise the
    caller builds afresh, which is safe because nothing complete is replaced."""
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    current = _current_generation_dir(tools)
    candidates = [current] if current is not None else list(reversed(install_layout.list_generations(tools=tools)))
    for generation in candidates:
        receipt = _read_receipt_or_none(generation)
        if receipt is not None and receipt.version == running and _generation_answers_for_its_shims(generation):
            return generation
    return None


def _conexus_entry_points() -> EntryPoints:
    """The running program's conexus entry points; raises
    ``importlib.metadata.PackageNotFoundError`` when it has no metadata. A seam,
    so a test fakes this module's answer without patching the stdlib."""
    from importlib.metadata import distribution  # noqa: PLC0415 — stdlib, deferred

    return distribution("conexus").entry_points


def _shim_names_to_write(generation: Path | None = None) -> list[str]:
    """The command names the shim step would write. For a generation that
    already exists, that generation's own answer. Otherwise asked of the running
    program, because the generation that will carry them does not exist yet: this
    distribution's console scripts plus the dependency scripts it also shims,
    minus the names nexus never shims."""
    if generation is not None:
        return _shim_names_of(generation)
    from importlib.metadata import PackageNotFoundError  # noqa: PLC0415 — stdlib, deferred

    from nexus import install_layout  # noqa: PLC0415 — deferred import

    try:
        entry_points = _conexus_entry_points()
    except PackageNotFoundError as exc:
        raise click.ClickException(
            "--bootstrap cannot tell which command names it would write, because this "
            "process has no conexus distribution metadata. Nothing was changed."
        ) from exc
    declared = {entry.name for entry in entry_points if entry.group == "console_scripts"}
    return sorted((declared | install_layout.DEPENDENCY_SCRIPTS) - install_layout.NEVER_SHIM)


def _refuse_a_foreign_program_in_the_bin_directory(
    tools: Path, bin_dir: Path, generation: Path | None = None,
) -> None:
    """Refuse, in one line, when a file the shim step would replace is not ours.

    The shim step ``os.replace``s whatever is at ``<bin>/nx`` and the other shim
    names. A plain ``nx self install`` runs where nexus already owns them; the
    bootstrap runs where nothing of nexus's exists, which is where a pipx or
    npm ``nx`` is most likely to be. A file is ours to replace when it is a
    shim this install wrote (``shims_core._is_ours``) or a symlink into the
    ``uv tool`` conexus tree (what ``uv tool install conexus`` leaves, and what
    the migration replaces by design). Windows writes no file into the bin
    directory, so there is nothing to protect there."""
    if _is_windows():
        return
    from nexus import install_layout  # noqa: PLC0415 — deferred import
    from nexus._install import shims_core  # noqa: PLC0415 — deferred import

    pointer = tools / install_layout.CURRENT_LINK_NAME
    for name in _shim_names_to_write(generation):
        path = bin_dir / name
        if not (path.exists() or path.is_symlink()):
            continue
        if shims_core._is_ours(path, pointer):
            continue
        if path.is_symlink() and install_layout.is_under_uv_tool_install(path):
            continue
        raise click.ClickException(
            f"--bootstrap would replace {path}, which is not a nexus shim. Move it "
            "aside or remove it, then run the same command again. Nothing was changed."
        )


def _bootstrap_install(
    install_dir: Path, tools: Path, bin_dir: Path, *, version: str | None,
    extras: list[str], dry_run: bool,
) -> Path | None:
    """The first install of the CLI, in one command and one generation (RDR-228
    Phase 2 Step 1, nexus-sv2wx.14).

    Reached ONLY through ``--bootstrap``. Installs the released package, at the
    running program's own version and with no extras, from the package index
    into a new ``gen-<stamp>``, points ``current`` at it and writes the command
    shims. It is what ``uv tool run --python 3.12 --from conexus==<v> nx self
    install --bootstrap`` runs, from a throwaway ``uv`` environment that
    ``perform_self_install`` otherwise refuses (CA-3).

    NOT DONE HERE, on purpose:

    * No cache prune. The plain command prunes ``uv``'s shared cache at its end
      (23.4 GiB from a developer's cache in the CA-3 test); this path never
      calls :func:`prune_uv_cache`.
    * No reap and no legacy-tree registration: with an existing install it
      refuses, so there is nothing older to reap.
    * No fallback to ``uv tool install`` then ``nx self install``: that runs the
      prune and leaves two copies on disk.

    With a complete install present it refuses, in one line naming the plain
    command. Complete means ``current`` resolving to a receipted generation AND
    the shim step's output in place, or a ``uv tool`` install. The record states
    the flag for a first install and nothing else: refusing is the reading that
    cannot overwrite a generation (checked by P2.R2, nexus-sv2wx.22). It also
    refuses, before building, when ``current`` names an existing directory whose
    receipt is missing or unreadable, and when a shim name in the bin directory
    holds a program that is not nexus's. Anything short of complete is finished
    rather than refused, because there is no ``nx`` on that machine to run the
    plain command: a receipted generation with no ``current`` (a run that died
    after its build) or with ``current`` and no shims (a run that died after the
    flip) is finished without rebuilding, but only when its receipt names the
    running version and its interpreter answers the shim step's lookup; any
    other leftover falls through to a fresh build.
    Two bootstraps at once are P2.2's lock, not this command's.
    """
    if version or extras:
        raise click.ClickException(
            "--bootstrap installs the version of the running nx with no extras, so it "
            "takes neither --version nor --extras. Nothing was changed."
        )
    existing = _existing_install(tools, bin_dir)
    if existing is not None:
        raise click.ClickException(
            f"--bootstrap is for a first install and {existing} is already installed. "
            "Run `nx self install` to update it. Nothing was changed."
        )
    current = _current_generation_dir(tools)
    if current is not None and _read_receipt_or_none(current) is None:
        # `current` names a directory that exists and whose receipt is missing or
        # unreadable. Whatever runs from it is not ours to move.
        from nexus import install_layout  # noqa: PLC0415 — deferred import

        nx = install_layout.venv_script(current, "nx", platform=_plat())
        raise click.ClickException(
            f"--bootstrap will not move `current`: it names {current}, whose receipt is missing "
            f"or unreadable. Repair it with `{nx} self install`. Nothing was changed."
        )
    running = _running_version()
    unfinished = _unfinished_generation(tools, running)
    _refuse_a_foreign_program_in_the_bin_directory(tools, bin_dir, unfinished)
    if unfinished is not None:
        # An earlier bootstrap built this generation and died before `current`
        # and the shims, or after `current` and before the shims. There is no
        # `nx` here to run, so the same command finishes the job, with no
        # rebuild. Only a generation at the running version that answers the
        # shim lookup is finished; any other leftover falls through to a build.
        what = f"{unfinished.name} (conexus {running}) is already built"
        if dry_run:
            click.echo(f"would finish the interrupted install: {what} (no rebuild); "
                       "would set `current` and write the command shims")
            return None
        click.echo(f"finishing the interrupted install: {what} (no rebuild); "
                   "setting `current` and the command shims")
        _flip_and_shim(unfinished, install_dir=install_dir, tools=tools, bin_dir=bin_dir, bootstrap=True)
        return unfinished
    build = _build_request(install_dir, _RegistryReceipt(), version=running)
    if dry_run:
        click.echo(build.describe() if isinstance(build, _WindowsBuild) else " ".join(build))
        return None
    return _build_flip_shims(
        build, install_dir=install_dir, tools=tools, bin_dir=bin_dir, bootstrap=True,
    )


_EXTRA_NAME_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?")


def _normalize_extras(raw: tuple[str, ...]) -> list[str]:
    """Flatten repeatable/comma-separated ``--extras`` values, fail loud on
    junk (nexus-pffc4). An invalid name would otherwise surface as an opaque
    uv resolution error deep inside the generation build.

    Names are PEP 685-normalized (lowercase, runs of ``-_.`` collapse to
    ``-``) so ``--extras Local`` cannot land beside an existing ``local`` as
    two "different" extras forever (round-2 review [23834]: nothing
    downstream normalizes, so the dedupe here is the only one)."""
    names: list[str] = []
    for chunk in raw:
        for name in chunk.split(","):
            name = name.strip()
            if not name:
                continue
            if not _EXTRA_NAME_RE.fullmatch(name):
                raise click.ClickException(
                    f"--extras: {name!r} is not a valid extra name"
                )
            name = re.sub(r"[-_.]+", "-", name.lower())
            if name not in names:
                names.append(name)
    return names


def _merge_extras(existing: list[str], new: list[str]) -> list[str]:
    """Receipt extras first (order preserved), requested ones appended —
    a MERGE, never a replace, so adding [local] cannot drop an extra the
    box already has. Dedupe compares PEP 685-normalized forms so a
    differently-spelled receipt entry still suppresses the duplicate."""
    def _norm(n: str) -> str:
        return re.sub(r"[-_.]+", "-", n.lower())

    have = {_norm(n) for n in existing}
    return list(existing) + [n for n in new if _norm(n) not in have]


def _build_argv(
    install_dir: Path, receipt, *, version: str | None,
    extras: list[str] | None = None,
) -> list[str]:
    """The install_generation.sh argv that reproduces *receipt*'s install.

    EXTRAS ARE THREADED EXPLICITLY. This is the load-bearing reason the old
    hook chose `uv tool upgrade` over `uv tool install`: a raw install strips
    the [local] extra and reintroduces the 5.6.2 local-search P0 (a 768-dim
    embedder silently replaced by a 384-dim one, against collections built
    at 768). There is no uv receipt to re-derive them from any more, so they
    travel from nexus-install.json or they are lost. *extras*, when given,
    is the caller's already-merged set (receipt + requested, nexus-pffc4);
    ``None`` means the receipt's own.
    """
    effective = receipt.extras if extras is None else extras
    build = [
        "bash", str(install_dir / "install_generation.sh"),
        "--source", receipt.source,
    ]
    if effective:
        build += ["--extras", ",".join(effective)]
    if version:
        build += ["--version", version]
    return build


def _build_request(
    install_dir: Path, receipt, *, version: str | None, extras: list[str] | None = None,
):
    """What one generation build is asked to do: the bash argv on POSIX, a
    :class:`_WindowsBuild` on Windows, where the build is a Python call."""
    if _is_windows():
        effective = receipt.extras if extras is None else extras
        return _WindowsBuild(receipt.source, list(effective), version)
    return _build_argv(install_dir, receipt, version=version, extras=extras)


# ── Package-index failure diagnosis (nexus-12pyx) ───────────────────────────

#: What uv prints when it cannot reach an index: DNS, connect, timeout and retry
#: exhaustion. A resolver failure ("No solution found") is deliberately absent:
#: blaming the network there would send the user after the wrong cause.
_NETWORK_FAILURE_RE = re.compile(
    r"dns error|failed to lookup address|nodename nor servname|"
    r"temporary failure in name resolution|name or service not known|"
    r"request failed after \d+ retries|error sending request|"
    r"tcp connect error|connection refused|connection reset|"
    r"timed out|failed to connect",
    re.IGNORECASE,
)
#: A certificate or TLS failure: reachability is not the problem, trust is.
_TLS_FAILURE_RE = re.compile(
    r"invalid peer certificate|unknownissuer|unknown issuer|certificate verify failed|"
    r"self[- ]signed|certificate (?:has )?expired|invalid certificate|"
    r"tls handshake|ssl error|certificate_verify_failed",
    re.IGNORECASE,
)
# Backtick excluded: uv prints ``Failed to fetch: `https://...` ``.
_URL_RE = re.compile(r"https?://[^\s'\"`)<>]+")

#: Environment variables uv reads for the index, and the pip one (which uv does
#: not read, but which a wrapper may forward).
_UV_INDEX_ENV = ("UV_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX")
_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


def _index_target(url: str) -> tuple[str, str] | None:
    """``(display, host)`` for *url*, or ``None`` when it does not parse.

    *display* is scheme, host, port and path only: userinfo, query and
    fragment are dropped, because an index URL can carry a token in any of
    them. urllib raises ValueError on a bad port (``host:abc``) and on a broken
    IPv6 literal (``[abc``); either returns ``None`` so the caller degrades to
    the generic line instead of turning a ClickException into a traceback.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host or parts.scheme not in ("http", "https"):
        return None
    shown = f"[{host}]" if ":" in host else host
    if port:
        shown = f"{shown}:{port}"
    return f"{parts.scheme}://{shown}{parts.path}", host


def _failed_index_url(stderr: str) -> str | None:
    """The URL uv failed to fetch: the one on its ``Failed to fetch`` line,
    else the first URL in the output."""
    for line in stderr.splitlines():
        if "failed to fetch" in line.lower():
            match = _URL_RE.search(line)
            if match:
                return match.group(0).rstrip(".,;:")
    match = _URL_RE.search(stderr)
    return match.group(0).rstrip(".,;:") if match else None


def _names_host(text: str, host: str) -> bool:
    """True when *text* contains *host* as a whole host name: not as the tail
    of ``test.pypi.org`` nor the head of ``pypi.org.evil``."""
    if not host:
        return False
    return re.search(rf"(?<![\w.-]){re.escape(host)}(?![\w.-])", text, re.IGNORECASE) is not None


def _where_index_is_set(host: str, *, env: Mapping[str, str], home: Path) -> list[str]:
    """Human-readable places that name *host*: environment variables (by name,
    never value) and uv/pip config files (by path). Empty when none does."""
    found: list[str] = []
    for name in (*_UV_INDEX_ENV, "PIP_INDEX_URL"):
        if _names_host(env.get(name, ""), host):
            note = (
                " (pip's variable; uv does not read it, so something else forwarded it)"
                if name == "PIP_INDEX_URL" else ""
            )
            found.append(f"the {name} environment variable{note}")
    xdg = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    uv_files = [xdg / "uv" / "uv.toml", Path("/etc/uv/uv.toml")]
    if env.get("UV_CONFIG_FILE"):
        uv_files.insert(0, Path(env["UV_CONFIG_FILE"]))
    pip_files = [xdg / "pip" / "pip.conf", home / ".pip" / "pip.conf", Path("/etc/pip.conf")]
    if env.get("PIP_CONFIG_FILE"):
        pip_files.insert(0, Path(env["PIP_CONFIG_FILE"]))
    for path in uv_files:
        if _file_names_host(path, host):
            found.append(str(path))
    for path in pip_files:
        if _file_names_host(path, host):
            found.append(f"{path} (a pip config; uv does not read it, so a UV_* variable or wrapper may forward it)")
    return found


def _file_names_host(path: Path, host: str) -> bool:
    try:
        return _names_host(path.read_text(encoding="utf-8", errors="replace"), host)
    except OSError:
        return False


def index_failure_hint(
    stderr: str, *, env: Mapping[str, str] | None = None, home: Path | None = None,
    bootstrap: bool = False,
) -> str | None:
    """Explain a network-shaped generation-build failure, or ``None``.

    ``nx self install`` used to print uv's raw output and nothing else, so an
    unreachable corporate index (off VPN) read as a nexus fault. This names the
    index uv was trying, where that setting most likely comes from, that the
    running install was not changed, and the remedy (nexus-12pyx). A
    certificate failure, or a configured proxy, is a trust or routing problem
    rather than reachability, so those get the proxy and certificate variables
    instead of the VPN and PyPI advice. *env* and *home* are injectable for
    tests; they default to the process's.

    *bootstrap* is the ``nx self install --bootstrap`` variant: there is no
    install to have been left unchanged and no ``nx`` to run, so the text says
    nothing of either and names re-running the same bootstrap command.
    """
    tls = _TLS_FAILURE_RE.search(stderr) is not None
    if not tls and not _NETWORK_FAILURE_RE.search(stderr):
        return None
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    proxies = [name for name in _PROXY_ENV if env.get(name)]
    url = _failed_index_url(stderr)
    target = _index_target(url) if url else None
    lines: list[str] = []
    host = ""
    if target is None:
        lines.append("uv could not reach the package index it was using.")
    else:
        shown, host = target
        lines.append(f"uv could not reach the package index at {shown}")
        sources = _where_index_is_set(host, env=env, home=home)
        if sources:
            lines.append("That host is named by: " + "; ".join(sources) + ".")
        else:
            lines.append(
                f"No setting naming {host} was found (checked the UV_INDEX_URL, "
                "UV_DEFAULT_INDEX and UV_INDEX variables, uv.toml and pip.conf)."
            )
    if not bootstrap:
        lines.append("The install you have was not changed.")
    if tls or proxies:
        cause = "a certificate or TLS error" if tls else "a configured proxy"
        set_now = f" ({', '.join(proxies)} is set)" if proxies else ""
        lines += [
            f"This looks like {cause}{set_now}, not an unreachable host. Check that the proxy "
            "(HTTPS_PROXY / ALL_PROXY) is reachable and correct. If the proxy or index uses a "
            "private certificate authority, point SSL_CERT_FILE at its CA bundle, or set "
            "UV_NATIVE_TLS=1 to use the system certificate store.",
        ]
    else:
        lines.append("Reach that index (connect to the VPN it needs, or check your network).")
        if host.lower() != "pypi.org":
            if bootstrap:
                lines.append(
                    "Or re-run the same bootstrap command with "
                    "UV_INDEX_URL=https://pypi.org/simple set."
                )
            else:
                lines += [
                    "Or build this once from PyPI:",
                    "    UV_INDEX_URL=https://pypi.org/simple nx self install",
                ]
    if not bootstrap:
        lines.append(
            "A local-mode upgrade also downloads the pinned engine from GitHub releases, "
            "which a corporate network may block too."
        )
    return "\n".join(lines)


def _build_flip_shims(
    build, *, install_dir: Path, tools: Path, bin_dir: Path, bootstrap: bool = False,
) -> Path:
    """Run one generation build, flip ``current`` to it, write the shims.

    No reap here: callers that reap do it afterwards and pass rule (d)
    themselves. `bash <script>` rather than executing it directly: the wheel
    ships mode 755 today, but a mode bit lost in some install path would fail
    at exec with a permissions error rather than anything self-explanatory,
    and nothing about this command needs the bit.

    *bootstrap* only changes the words a failure uses (see
    :func:`index_failure_hint`).
    """
    tools.mkdir(parents=True, exist_ok=True)
    if isinstance(build, _WindowsBuild):
        return _windows_build_flip_shims(build, tools=tools, bin_dir=bin_dir, bootstrap=bootstrap)
    built = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv_off_cwd(build), capture_output=True, text=True, encoding="utf-8", check=False, env=_script_env(),
    )
    if built.returncode != 0:
        detail = built.stderr.strip()
        hint = index_failure_hint(detail, bootstrap=bootstrap)
        if hint is None:
            raise click.ClickException(f"generation build failed:\n{detail}")
        raise click.ClickException(
            f"generation build failed:\n{hint}\n\nuv output:\n{detail}"
        )
    generation = Path(built.stdout.strip().splitlines()[-1])
    _flip_and_shim(generation, install_dir=install_dir, tools=tools, bin_dir=bin_dir, bootstrap=bootstrap)
    return generation


def _flip_and_shim(
    generation: Path, *, install_dir: Path, tools: Path, bin_dir: Path, bootstrap: bool = False,
) -> None:
    """Point ``current`` at an already-built *generation* and write the shims
    (on Windows: ``current`` and the user PATH entry). The two steps after a
    build, on their own, so a bootstrap that died between the build and them can
    finish without building again."""
    tools.mkdir(parents=True, exist_ok=True)
    if _is_windows():
        _windows_flip_and_path(generation, tools=tools, bootstrap=bootstrap)
        return
    _sh(install_dir, f'nx_flip_current "{generation}" "{tools}"')
    _sh(install_dir, f'nx_write_shims "{generation}" "{bin_dir}"')


def _echo_path_result(result) -> None:
    """Tell the operator when the user PATH changed: the one-time step whose
    effect reaches only programs started AFTER it."""
    if result.changed:
        click.echo(_generation().restart_notice(result.entry))


def _windows_build_flip_shims(
    build: _WindowsBuild, *, tools: Path, bin_dir: Path, bootstrap: bool = False,
) -> Path:
    """The Windows twin of the build, flip and shim steps, through
    ``generation_core``: the shim step is the user PATH entry. A build failure
    keeps the POSIX path's wording and its package-index diagnosis; nothing is
    flipped unless the build finished."""
    core = _generation()
    try:
        generation = core.build_generation(
            build.source, version=build.version or "", extras=build.extras, tools=tools,
            platform=_plat(),
        )
    except core.GenerationError as exc:
        detail = str(exc)
        hint = index_failure_hint(detail, bootstrap=bootstrap)
        if hint is None:
            raise click.ClickException(f"generation build failed:\n{detail}") from exc
        raise click.ClickException(
            f"generation build failed:\n{hint}\n\nuv output:\n{detail}"
        ) from exc
    _windows_flip_and_path(generation, tools=tools, bootstrap=bootstrap)
    return generation


def _windows_flip_and_path(generation: Path, *, tools: Path, bootstrap: bool = False) -> None:
    """Flip ``current`` to *generation*, put its launcher directory on the user
    PATH, and repoint the logon task. On the bootstrap path the failure names
    the same ``--bootstrap`` command, because the plain one has no ``nx`` to
    run there."""
    core = _generation()
    try:
        core.flip_current(generation, tools, platform=_plat())
        _echo_path_result(core.ensure_user_path(core.current_launcher_dir(tools)))
    except core.GenerationError as exc:
        rerun = (
            "re-run the same `--bootstrap` command to finish the install"
            if bootstrap else "`nx self install` re-runs the missing steps"
        )
        raise click.ClickException(f"flip or user PATH update failed ({rerun}):\n{exc}") from exc
    _retarget_autostart_task(generation)


def _retarget_autostart_task(generation: Path) -> None:
    """Point the storage-service logon task at *generation* (Windows only).

    The Windows task names an interpreter path, not a shim, so a flip alone
    leaves it starting the old tree (``installer.retarget_windows_task`` has the
    measurement). Best effort after a successful flip: a failure here is a line
    for the operator, never a failed install."""
    from nexus.daemon import installer  # noqa: PLC0415 — deferred import

    try:
        lines = installer.retarget_windows_task(installer.generation_pythonw(generation))
    except Exception as exc:  # noqa: BLE001 — the install already succeeded
        lines = [
            f"the storage-service logon task could not be repointed at {generation.name} "
            f"({exc}); run `nx daemon restart-stale` to repoint it"
        ]
    for line in lines:
        click.echo(line)


def _generation_layout_present(tools: Path) -> bool:
    """True when ``<tools>/current`` names a generation -- the layout exists."""
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    try:
        install_layout.current_generation(tools=tools)
    except Exception:  # noqa: BLE001 — no pointer, dangling pointer, unreadable root: no layout
        return False
    return True


def _installed_version(venv: Path) -> str | None:
    """The conexus version installed in *venv*, read from its own metadata.

    Asks the venv's interpreter rather than parsing dist-info paths, so the
    answer is what that tree would report about itself. ``None`` when the
    tree has no usable python or no conexus.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    python = install_layout.venv_python(venv, platform=_plat())
    if not python.exists():
        return None
    r = run_bounded(  # noqa: S603 — fixed argv, no shell
        [str(python), "-c",
         "import importlib.metadata as m; print(m.version('conexus'))"],
        timeout=30,
    )
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _newer(candidate: str | None, than: str | None) -> bool:
    if not candidate or not than:
        return False
    from packaging.version import InvalidVersion, Version  # noqa: PLC0415 — deferred import

    try:
        return Version(candidate) > Version(than)
    except InvalidVersion:
        return False


def repair_uv_takeover(*, dry_run: bool = False) -> list[str]:
    """Undo what a stray ``uv tool install --force conexus`` / upgrade did.

    Measured against uv 0.8 in a sandbox (2026-08-28): a plain
    ``uv tool install conexus`` on a generation box REBUILDS uv's tree (a
    [local]-less copy, wasting disk) but refuses to overwrite a nexus-owned
    shim ("Executable already exists"); ``--force`` takes the shims, and then
    every spawn resolves through uv's tree instead of ``current`` -- the box
    is silently on the wrong install, possibly at the wrong version, with the
    wrong extras. ``uv tool uninstall conexus`` DELETES the shims at those
    paths, so it is never the remedy; a reaped tree (``rm -rf``) is what
    makes uv say "not installed" and refuse to rebuild.

    Three steps, each only when needed, returned as lines for the caller to
    print (``dry_run`` describes without doing):

    1. uv's tree is NEWER than ``current`` -> the user meant to upgrade.
       Build a generation at that version from ``current``'s OWN receipt
       (its source, its extras -- never the rebuilt uv receipt), flip, shims.
    2. shims at ``bin_dir`` are symlinks (uv's) -> rewrite them to the
       generation (the new one from step 1, else ``current``).
    3. uv's tree exists -> register it for reap (idempotent); the next
       ``nx self install`` reaps it once nothing runs from it.

    Returns ``[]`` when there is no generation layout (a pure uv box is not
    a takeover; ``nx self install`` converges it) or nothing is wrong.
    """
    if _is_windows():
        # uv rewriting its own launchers is harmless on Windows (current\\bin is
        # ahead of them on the PATH), so the repair is to re-ensure the PATH
        # entry, the launcher directory and the legacy ledger. The same call
        # finishes a migration that stopped after the flip.
        core = _generation()
        try:
            return core.repair_layout(dry_run=dry_run, platform="win32")
        except core.GenerationError as exc:
            raise click.ClickException(f"repairing the Windows layout failed:\n{exc}") from exc
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    tools = install_layout.tools_dir()
    bin_dir = install_layout.bin_dir()
    if not _generation_layout_present(tools):
        return []
    current = install_layout.current_generation(tools=tools)
    install_dir = packaged_install_dir()
    legacy = install_layout.uv_conexus_venv()
    legacy_present = (legacy / "bin").is_dir()

    lines: list[str] = []
    try:
        # Owned = what the distribution DECLARES (the installer's own query),
        # never a listing of <current>/bin: a uv-managed python3.12 link in the
        # shared bin dir shares its name with the venv's interpreter and must
        # never be rewritten into a nexus shim (GH #1487, nexus-50hm9).
        taken = install_layout.reclaimed_shims(current, bin_dir)
    except install_layout.InstallLayoutError as exc:
        taken = []
        lines.append(
            f"could not ask {current.name} which console scripts it declares "
            f"({exc}): shims left untouched; run `nx doctor`"
        )
    if not taken and not legacy_present:
        return lines
    target = current
    if legacy_present:
        uv_version = _installed_version(legacy)
        cur_version = _installed_version(current)
        if _newer(uv_version, cur_version):
            receipt = install_layout.read_receipt(current)
            lines.append(
                f"uv's tree is at {uv_version}, newer than current ({cur_version}): "
                f"building a generation at {uv_version} from current's receipt "
                f"(source {receipt.source}, extras {','.join(receipt.extras) or 'none'})"
            )
            if not dry_run:
                target = _build_flip_shims(
                    _build_argv(install_dir, receipt, version=uv_version),
                    install_dir=install_dir, tools=tools, bin_dir=bin_dir,
                )
                lines.append(f"installed {target.name}")
                taken = []  # _build_flip_shims wrote the shims
    if taken:
        lines.append(
            f"shims {', '.join(taken)} in {bin_dir} were uv symlinks: rewriting "
            f"them to {target.name}"
        )
        if not dry_run:
            _sh(install_dir, f'nx_write_shims "{target}" "{bin_dir}"')
    if legacy_present:
        lines.append(
            f"uv's tree at {legacy} is registered for reap; the next `nx self "
            "install` removes it once nothing runs from it"
        )
        if not dry_run:
            _register_legacy_tree_if_present(install_dir, tools)
    return lines


def _register_legacy_tree_if_present(install_dir: Path, tools: Path) -> Path | None:
    """Put an existing legacy uv tree in the GC ledger. Returns it, or None.

    Idempotent: ``nx_register_legacy_generation`` is a no-op when the pointer
    already names this tree. Resolves uv's tool root through
    ``install_layout.uv_conexus_venv`` (UV_TOOL_DIR > XDG > default,
    nexus-orhp5) rather than shelling ``uv tool dir``, so it answers the same
    way ``nx doctor`` does and needs no uv on PATH.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    legacy = install_layout.uv_conexus_venv(platform=_plat())
    if not install_layout.venv_bin(legacy, platform=_plat()).is_dir():
        return None
    if _is_windows():
        core = _generation()
        try:
            core.register_legacy(legacy, tools, platform="win32")
        except core.GenerationError as exc:
            raise click.ClickException(f"registering the legacy uv tree failed:\n{exc}") from exc
        return legacy
    _sh(install_dir, f'nx_register_legacy_generation "{legacy}" "{tools}"')
    return legacy


def _running_from_legacy_tool_install() -> bool:
    """True when this nx is a PACKAGED install that is not a generation.

    Delegates the packaged-vs-dev-checkout question to
    ``upgrade_finish.running_from_tool_install``, which already owns that rule
    and answers True for BOTH shapes of managed install -- a generation and the
    legacy uv tool tree. Callers here have already excluded the generation, so
    a True means legacy.

    NOT a second copy of that rule's ``"uv/tools/conexus"`` path test. The
    question "is this a packaged install" had an answer in the tree the whole
    time and the refusal simply never asked it; re-deriving it here would leave
    two copies to drift apart, and the stale one eventually wins an argument.

    The gap this used to carry is CLOSED (nexus-orhp5). It read as a dev
    checkout on a box with ``UV_TOOL_DIR`` pointed elsewhere, because the
    delegated rule was a substring against the default uv path. That rule now
    does a CONTAINMENT check against ``install_layout.uv_tool_root()``, which
    resolves the way uv itself does — UV_TOOL_DIR > $XDG_DATA_HOME/uv/tools >
    ~/.local/share/uv/tools, measured against uv 0.8.0 rather than inferred.
    The four rules that separately answered "where is uv's tool dir" are one.
    """
    from nexus.upgrade_finish import running_from_tool_install  # noqa: PLC0415 — deferred; avoids import cycle and lets tests patch at call time

    return running_from_tool_install()


def _converge_legacy_install(
    install_dir: Path, tools: Path, *, version: str | None, dry_run: bool,
) -> Path | None:
    """Converge a legacy uv-tool layout onto the generation layout.

    Execs the PACKAGED ``migrate_legacy.sh`` -- complete, 15-test-covered, and
    until now called from nowhere in the tree. This wiring is the whole fix;
    the migration itself is not rewritten here.

    NO ``--extras``, deliberately. The upgrade path threads extras from the
    generation receipt, but a legacy box has no receipt -- that is what makes
    it legacy. ``migrate_legacy.sh`` reads the legacy ``uv-receipt.toml`` one
    last time and bridges the extras itself, which is the only path by which
    ``[local]`` survives the move.

    NO GC EITHER, and that is load-bearing rather than an omission. The legacy
    tree is registered as a pseudo-generation and reaped by a LATER, SEPARATE
    pass once nothing holds it; ``migrate_legacy.sh`` never sources ``gc.sh``
    precisely so a reap cannot fire in the same process that just built the
    replacement. Live holders keep running from the old tree and converge at
    their next spawn.
    """
    if _is_windows():
        return _windows_converge_legacy_install(tools, version=version, dry_run=dry_run)
    build = [
        "bash", str(install_dir / "migrate_legacy.sh"),
        # A packaged install's source is the distribution, not a checkout path.
        "--source", "conexus",
    ]
    if version:
        build += ["--version", version]

    if dry_run:
        click.echo(" ".join(build))
        return None

    tools.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv_off_cwd(build), capture_output=True, text=True, encoding="utf-8", check=False, env=_script_env(),
    )
    if r.returncode != 0:
        raise click.ClickException(
            f"legacy migration failed:\n{r.stderr.strip()}"
        )

    out = r.stdout.strip()
    if not out:
        # migrate_legacy.sh's documented clean no-op: uv resolved and found no
        # legacy tree. Reaching it HERE is a contradiction -- we only got here
        # because the running interpreter looked like a legacy tool install --
        # so report it rather than returning a success that migrated nothing.
        raise click.ClickException(
            "this nx looks like a packaged uv-tool install, but "
            "migrate_legacy.sh found no legacy tree to converge. Since "
            "nexus-orhp5 both sides resolve the uv tools directory by the "
            "same rule, so the likeliest causes are that `uv` is absent or "
            "unresolvable here, or the tree was removed between the two "
            "checks. Nothing was changed."
        )

    generation = Path(out.splitlines()[-1])
    click.echo(
        "converged the legacy uv-tool install onto the generation layout; "
        "the old tree is retained for live holders and reaped by a later "
        "`nx self install` once nothing is running from it"
    )
    return generation


def _windows_converge_legacy_install(
    tools: Path, *, version: str | None, dry_run: bool,
) -> Path | None:
    """The Windows twin of the legacy convergence, ``generation_core.migrate_legacy``:
    extras read from uv's receipt one last time, a generation built beside uv's
    tree, ``current`` flipped, launcher shims written, the legacy tree
    registered in the ledger as a junction. Never reaps, never uninstalls."""
    core = _generation()
    if dry_run:
        click.echo(
            "generation_core.migrate_legacy --source conexus"
            + (f" --version {version}" if version else "")
        )
        return None
    try:
        generation = core.migrate_legacy(
            "conexus", version=version or "", tools=tools, platform=_plat(),
            on_path=_echo_path_result,
        )
    except core.GenerationError as exc:
        detail = str(exc)
        hint = index_failure_hint(detail)
        message = f"{hint}\n\nuv output:\n{detail}" if hint else detail
        raise click.ClickException(f"legacy migration failed:\n{message}") from exc
    if generation is None:
        raise click.ClickException(
            "this nx looks like a packaged uv-tool install, but the migration "
            "found no legacy tree to converge. Since nexus-orhp5 both sides "
            "resolve the uv tools directory by the same rule, so the likeliest "
            "causes are that `uv` is absent or unresolvable here, or the tree "
            "was removed between the two checks. Nothing was changed."
        )
    _retarget_autostart_task(generation)
    click.echo(
        "converged the legacy uv-tool install onto the generation layout; "
        "the old tree is retained for live holders and reaped by a later "
        "`nx self install` once nothing is running from it"
    )
    return generation


def _script_env() -> dict[str, str]:
    """The environment for the POSIX install scripts.

    layout.sh, gc.sh, census.sh, shims.sh and legacy.sh run their logic as
    bare ``python3 <x>_core.py``, so a box with no ``python3`` on PATH failed
    the first ``nx self install`` with "python3: command not found". Measured
    2026-10-07 in a fresh ubuntu:24.04 container after the documented
    ``uv tool install conexus``: uv brings its own Python, so nothing else
    installs one. The running interpreter's directory is a venv ``bin``, and
    every venv puts a ``python3`` there. Appending it, not prepending, leaves
    every command an existing PATH resolves unchanged; it only fills the gap.
    """
    env = dict(os.environ)
    venv_bin = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    if venv_bin not in path.split(os.pathsep):
        env["PATH"] = f"{path}{os.pathsep}{venv_bin}" if path else venv_bin
    return env


def _sh(install_dir: Path, snippet: str, *, check: bool = True) -> str:
    """Source the install library and run one statement against it.
    Returns the statement's stdout."""
    r = subprocess.run(  # noqa: S603 — fixed argv, no shell interpolation of user input
        argv_off_cwd(["bash", "-c",
         # layout.sh dispatches to layout_core.py beside it and cannot find its
         # own directory when sourced; the sourcing script names it.
         f'NX_LAYOUT_HOME="{install_dir}"; '
         f'. "{install_dir}/layout.sh"; . "{install_dir}/flip.sh"; '
         f'. "{install_dir}/shims.sh"; . "{install_dir}/census.sh"; '
         f'. "{install_dir}/gc.sh"; . "{install_dir}/legacy.sh"; {snippet}']),
        capture_output=True, text=True, encoding="utf-8", check=False, env=_script_env(),
    )
    if check and r.returncode != 0:
        raise click.ClickException(f"{snippet.split()[0]} failed:\n{r.stderr.strip()}")
    return r.stdout


def _reap_generations(
    install_dir: Path, tools: Path, *, keep: int,
    self_generation: Path | None, dry_run: bool = False,
) -> list[str]:
    """Run gc.sh's reap once and return its report lines (``reaped``,
    ``would reap``, ``kept ...: held by ...``). The four never-delete rules
    live in gc.sh; this passes rule (d) when the caller runs from a
    generation and never raises: a reap that cannot run leaves the trees
    where they are, which is the safe direction."""
    if _is_windows():
        return _windows_reap(tools, keep=keep, self_generation=self_generation, dry_run=dry_run)
    self_arg = f' --self "{self_generation}"' if self_generation is not None else ""
    dry_arg = " --dry-run" if dry_run else ""
    out = _sh(
        install_dir,
        f'nx_gc_generations --keep {int(keep)}{dry_arg}{self_arg} "{tools}"',
        check=False,
    )
    return [line for line in out.splitlines() if line.strip()]


def _windows_reap(
    tools: Path, *, keep: int, self_generation: Path | None, dry_run: bool,
) -> list[str]:
    """The Windows reap: ``generation_core.reap``, then uv's old launchers.

    Once the legacy uv tree is gone (reaped just now, or long ago), uv's
    conexus launchers in uv's bin dir are dead weight that ``current\\bin``
    already precedes; they are removed best-effort, a locked one is ``kept (in
    use)`` for the next run. The names come from the legacy ``uv-receipt.toml``,
    read BEFORE the reap deletes it, else the distribution's declared console
    scripts. Never anything else, and never when ``current\\bin`` has no
    replacement.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    core = _generation()
    legacy = install_layout.uv_conexus_venv(platform="win32")
    names = core.legacy_launcher_names(legacy) if legacy.is_dir() else []
    try:
        lines = core.reap(
            tools, keep=keep, self_generation=self_generation, dry_run=dry_run,
            platform="win32",
        )
    except (OSError, core.GenerationError) as exc:
        # A reap that cannot run leaves the trees where they are.
        click.echo(f"nexus: generation reap did not run: {exc}", err=True)
        return []
    if legacy.is_dir():
        return lines  # uv's tree is still there (held, or registered for a later pass)
    try:
        current = install_layout.current_generation(tools=tools, platform="win32")
    except install_layout.InstallLayoutError:
        return lines
    names = names or core.declared_launcher_names(current, platform="win32")
    return lines + core.remove_uv_launchers(
        core.uv_bin_dir(), names, core.current_launcher_dir(tools),
        dry_run=dry_run, platform="win32",
    )


def perform_self_gc(*, keep: int = 3, dry_run: bool = False) -> list[str] | None:
    """Reap generations WITHOUT installing one (nexus-xn84f).

    ``nx self install`` was the only caller of the reap, so a generation
    held by a long-lived ``nx-mcp`` at install time stayed on disk until the
    NEXT install, and on a box whose sessions live for days every generation
    since those sessions started was held at every install: 1.7 GB per
    upgrade, never reclaimed. This is the same reap, callable on its own (the
    SessionStart hook runs it), so a tree goes the moment its holders are
    gone. Returns the report lines, or ``None`` when this box has no
    generation layout (nothing to reap; not an error, so a hook on a dev
    checkout or a legacy uv box stays silent).
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    tools = install_layout.tools_dir()
    if not _generation_layout_present(tools):
        return None
    install_dir = packaged_install_dir()
    host = running_generation()
    self_generation = (
        host if _same_dir(host.parent, tools) and _is_generation_name(host.name)
        else None
    )
    return _reap_generations(
        install_dir, tools, keep=keep, self_generation=self_generation, dry_run=dry_run,
    )


def prune_uv_cache() -> str:
    """``uv cache prune`` after a successful flip (nexus-xn84f): every
    generation build unpacks its wheels into uv's archive cache and nothing
    ever removed them (82 GB measured on a box that had been upgrading for
    months). Best-effort and never raises; returns one line for the
    operator. ``uv`` is looked up on PATH exactly as the generation build
    does."""
    try:
        r = run_bounded(  # noqa: S603 — fixed argv
            ["uv", "cache", "prune"], timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"uv cache prune skipped: {exc}"
    if r.returncode != 0:
        return f"uv cache prune failed (rc={r.returncode}): {r.stderr.strip()[:200]}"
    tail = (r.stderr.strip() or r.stdout.strip()).splitlines()
    return "uv cache prune: " + (tail[-1] if tail else "done")


@click.group("self")
def self_group() -> None:
    """Manage this installation of nx itself."""


@self_group.command("install")
@click.option("--keep", default=3, show_default=True,
              help="Generations to retain. The four never-delete rules still apply.")
@click.option("--version", default=None,
              help="Install this version instead of whatever the source resolves to.")
@click.option("--extras", "extras", multiple=True,
              help="Extra(s) to ADD, e.g. --extras local (repeatable, or "
                   "comma-separated). MERGED with the extras this install "
                   "already has — never replaces them (nexus-pffc4).")
@click.option("--bootstrap", is_flag=True,
              help="First install from nothing: build the released package, at the version "
                   "of the nx running this command and with no extras, into a new "
                   "generation. Takes neither --version nor --extras, does not prune "
                   "uv's package cache, and refuses when an install already exists.")
@click.option("--dry-run", is_flag=True,
              help="Print the build command and stop.")
def install_cmd(
    keep: int, version: str | None, extras: tuple[str, ...], bootstrap: bool, dry_run: bool,
) -> None:
    """Install a new generation from the one this process is running from.

    Safe under live sessions: nothing is swapped underneath a running process.
    Holders keep their own tree and converge at their next spawn.

    This upgrades the BINARY only. Run `nx upgrade` separately for the
    migration ladder — they are two commands on purpose (RDR-143 CA-2). A
    fresh install needs no `nx upgrade`: `nx init` converges the ladder.
    """
    generation = perform_self_install(
        keep=keep, version=version, dry_run=dry_run, add_extras=extras, bootstrap=bootstrap,
    )
    if generation is None:
        return
    click.echo(f"installed {generation.name}")
    if bootstrap:
        # A first install: no cache prune (RDR-228), and nothing to migrate.
        click.echo("a fresh install is set up by `nx init`")
        return
    click.echo(prune_uv_cache())
    click.echo(
        "if this upgraded an existing install, run `nx upgrade` for migrations "
        "(a fresh install is set up by `nx init` instead); "
        "live sessions converge at their next spawn"
    )


@self_group.command("gc")
@click.option("--keep", default=3, show_default=True,
              help="Generations to retain. The four never-delete rules still apply.")
@click.option("--dry-run", is_flag=True, help="Report what would go; delete nothing.")
@click.option("--prune-uv-cache", "prune_cache", is_flag=True,
              help="Also run `uv cache prune` (the wheel archive every build feeds and nothing else empties).")
def gc_cmd(keep: int, dry_run: bool, prune_cache: bool) -> None:
    """Reap old generations without installing one.

    The reap `nx self install` runs at the end, on its own: a generation a
    long-lived session was holding at install time goes here once that
    session has ended. The SessionStart hook runs this. Silent on a box with
    no generation layout.
    """
    lines = perform_self_gc(keep=keep, dry_run=dry_run)
    if lines is None:
        return
    for line in lines:
        click.echo(line)
    if not lines:
        click.echo("nothing to reap")
    if prune_cache and not dry_run:
        click.echo(prune_uv_cache())
