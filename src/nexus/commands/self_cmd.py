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

SCOPE FENCE. This replaces the MECHANISM of ``uv tool upgrade conexus``. It
does NOT merge that with ``nx upgrade``. RDR-143 CA-2 keeps them two commands
deliberately — binary upgrade versus migration ladder — and ``nx upgrade``
today never invokes uv or pip at all. Merging them is a separate RDR, and
doing it by accident here is the specific thing the fence forbids.
"""

from __future__ import annotations

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
    *, keep: int = 3, version: str | None = None, dry_run: bool = False
) -> Path | None:
    """Build a new generation from the running one's receipt, flip, reap.

    Returns the new generation, or ``None`` on a dry run.
    """
    from nexus import install_layout  # noqa: PLC0415 — deferred import

    install_dir = packaged_install_dir()
    tools = install_layout.tools_dir()
    bin_dir = install_layout.bin_dir()
    host = running_generation()

    # THIS COMMAND ONLY MEANS SOMETHING FROM A GENERATION. Run from a dev
    # checkout's .venv there is no receipt, and the first version of this died
    # with a raw InstallLayoutError naming a missing JSON file -- which tells
    # the reader nexus is broken when the truth is that they are standing
    # somewhere the command does not apply. The repo has a script for that
    # case; say so.
    if not (host.parent == tools and host.name.startswith(install_layout.GENERATION_PREFIX)):
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

    # EXTRAS ARE THREADED EXPLICITLY. This is the load-bearing reason the old
    # hook chose `uv tool upgrade` over `uv tool install`: a raw install strips
    # the [local] extra and reintroduces the 5.6.2 local-search P0 (a 768-dim
    # embedder silently replaced by a 384-dim one, against collections built
    # at 768). There is no uv receipt to re-derive them from any more, so they
    # travel from nexus-install.json or they are lost.
    build = [
        "bash", str(install_dir / "install_generation.sh"),
        "--source", receipt.source,
    ]
    if receipt.extras:
        build += ["--extras", ",".join(receipt.extras)]
    if version:
        build += ["--version", version]

    if dry_run:
        click.echo(" ".join(build))
        return None

    # `bash <script>` rather than executing it directly: the wheel ships mode
    # 755 today, but a mode bit lost in some install path would fail at exec
    # with a permissions error rather than anything self-explanatory, and
    # nothing about this command needs the bit.
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

    # RULE (d) IS PASSED HERE. --self names the generation hosting this very
    # process; without it the reap below is free to delete the tree these
    # lines are executing from.
    _sh(
        install_dir,
        f'nx_gc_generations --keep {int(keep)} --self "{host}" "{tools}"',
        check=False,
    )
    return generation


def _sh(install_dir: Path, snippet: str, *, check: bool = True) -> None:
    """Source the install library and run one statement against it."""
    r = subprocess.run(  # noqa: S603 — fixed argv, no shell interpolation of user input
        ["bash", "-c",
         f'. "{install_dir}/layout.sh"; . "{install_dir}/flip.sh"; '
         f'. "{install_dir}/shims.sh"; . "{install_dir}/census.sh"; '
         f'. "{install_dir}/gc.sh"; {snippet}'],
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
@click.option("--dry-run", is_flag=True,
              help="Print the build command and stop.")
def install_cmd(keep: int, version: str | None, dry_run: bool) -> None:
    """Install a new generation from the one this process is running from.

    Safe under live sessions: nothing is swapped underneath a running process.
    Holders keep their own tree and converge at their next spawn.

    This upgrades the BINARY only. Run `nx upgrade` separately for the
    migration ladder — they are two commands on purpose (RDR-143 CA-2).
    """
    generation = perform_self_install(keep=keep, version=version, dry_run=dry_run)
    if generation is None:
        return
    click.echo(f"installed {generation.name}")
    click.echo("run `nx upgrade` for migrations; live sessions converge at their next spawn")
