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

SCOPE FENCE. This replaces the MECHANISM of ``uv tool upgrade conexus``. It
does NOT merge that with ``nx upgrade``. RDR-143 CA-2 keeps them two commands
deliberately — binary upgrade versus migration ladder — and ``nx upgrade``
today never invokes uv or pip at all. Merging them is a separate RDR, and
doing it by accident here is the specific thing the fence forbids.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import click

__all__ = ["self_group", "perform_self_install", "packaged_install_dir"]


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


def perform_self_install(
    *, keep: int = 3, version: str | None = None, dry_run: bool = False,
    add_extras: tuple[str, ...] = (),
) -> Path | None:
    """Build a new generation from the running one's receipt, flip, reap.

    *add_extras* (nexus-pffc4) MERGES with the receipt's existing extras —
    never replaces them — so ``nx self install --extras local`` is the
    supported way to ADD an extra to an existing generation install (the
    legacy answer, ``uv tool install --reinstall "conexus[local]"``, rebuilds
    the uv tree over the nexus-owned shims on a migrated box). The merged
    set travels through ``install_generation.sh --extras`` and is rendered
    by ``install_layout.build_spec``, so spec and extras cannot disagree.

    Returns the new generation, or ``None`` on a dry run.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    new_extras = _normalize_extras(add_extras)

    install_dir = packaged_install_dir()
    tools = install_layout.tools_dir()
    bin_dir = install_layout.bin_dir()
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
    if host.parent == tools and host.name.startswith(install_layout.GENERATION_PREFIX):
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
    build = _build_argv(install_dir, receipt, version=version, extras=extras)
    if dry_run:
        click.echo(" ".join(build))
        return None
    generation = _build_flip_shims(build, install_dir=install_dir, tools=tools, bin_dir=bin_dir)

    # RULE (d) IS PASSED HERE. --self names the generation hosting this very
    # process; without it the reap below is free to delete the tree these
    # lines are executing from.
    _sh(
        install_dir,
        f'nx_gc_generations --keep {int(keep)} --self "{host}" "{tools}"',
        check=False,
    )

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


def _build_flip_shims(build: list[str], *, install_dir: Path, tools: Path, bin_dir: Path) -> Path:
    """Run one generation build, flip ``current`` to it, write the shims.

    No reap here: callers that reap do it afterwards and pass rule (d)
    themselves. `bash <script>` rather than executing it directly: the wheel
    ships mode 755 today, but a mode bit lost in some install path would fail
    at exec with a permissions error rather than anything self-explanatory,
    and nothing about this command needs the bit.
    """
    tools.mkdir(parents=True, exist_ok=True)
    built = subprocess.run(  # noqa: S603 — fixed argv, no shell
        build, capture_output=True, text=True, check=False,
    )
    if built.returncode != 0:
        raise click.ClickException(
            f"generation build failed:\n{built.stderr.strip()}"
        )
    generation = Path(built.stdout.strip().splitlines()[-1])
    _sh(install_dir, f'nx_flip_current "{generation}" "{tools}"')
    _sh(install_dir, f'nx_write_shims "{generation}" "{bin_dir}"')
    return generation


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
    python = venv / "bin" / "python"
    if not python.exists():
        return None
    r = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [str(python), "-c",
         "import importlib.metadata as m; print(m.version('conexus'))"],
        capture_output=True, text=True, check=False, timeout=30,
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
    from nexus.install_layout import uv_conexus_venv  # noqa: PLC0415 — deferred import

    legacy = uv_conexus_venv()
    if not (legacy / "bin").is_dir():
        return None
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
        build, capture_output=True, text=True, check=False,
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


def _sh(install_dir: Path, snippet: str, *, check: bool = True) -> None:
    """Source the install library and run one statement against it."""
    r = subprocess.run(  # noqa: S603 — fixed argv, no shell interpolation of user input
        ["bash", "-c",
         f'. "{install_dir}/layout.sh"; . "{install_dir}/flip.sh"; '
         f'. "{install_dir}/shims.sh"; . "{install_dir}/census.sh"; '
         f'. "{install_dir}/gc.sh"; . "{install_dir}/legacy.sh"; {snippet}'],
        capture_output=True, text=True, check=False,
    )
    if check and r.returncode != 0:
        raise click.ClickException(f"{snippet.split()[0]} failed:\n{r.stderr.strip()}")


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
@click.option("--dry-run", is_flag=True,
              help="Print the build command and stop.")
def install_cmd(keep: int, version: str | None, extras: tuple[str, ...], dry_run: bool) -> None:
    """Install a new generation from the one this process is running from.

    Safe under live sessions: nothing is swapped underneath a running process.
    Holders keep their own tree and converge at their next spawn.

    This upgrades the BINARY only. Run `nx upgrade` separately for the
    migration ladder — they are two commands on purpose (RDR-143 CA-2).
    """
    generation = perform_self_install(
        keep=keep, version=version, dry_run=dry_run, add_extras=extras,
    )
    if generation is None:
        return
    click.echo(f"installed {generation.name}")
    click.echo("run `nx upgrade` for migrations; live sessions converge at their next spawn")
