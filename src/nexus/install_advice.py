# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""How to upgrade THIS box, in one place. nexus-utpuw.13.

Every user-facing remediation string that named a uv-tool command became wrong
advice under the generation layout (nexus-utpuw): ``uv tool upgrade conexus``,
``uv tool install --reinstall conexus`` and ``uv tool install conexus==<pin>``
do not touch a generation install, and the middle one actively triggers .7's
accepted risk by rebuilding the uv tree over the shims.

They are NOT simply wrong, which is why this is a module and not a
find-and-replace (contract 12): a box that has not migrated still upgrades
through uv, and .7 leaves boxes in that state until their legacy tree has zero
holders. Telling such a user to run the generation installer is a different
wrong answer. So the advice follows the layout the box actually has.

WHY THIS IS A LEAF MODULE. The rule was born inside ``health.py`` (.11), which
is fine for ``health`` and ``doctor`` and useless to everyone else:
``stranded_install.py`` records at its own line 102 that it cannot import
``upgrade_finish`` and duplicates a constant under a test-enforced parity
check instead, so routing it through a 5000-line ``health`` is worse, not
better. This module imports only :mod:`nexus.install_layout` (stdlib plus
``nexus.errors``), so every caller can use it and there is ONE answer to "how
do I upgrade this box" rather than one implementation and three hardcoded
copies of a different one.

WHY ``nx self install`` AND NOT ``scripts/reinstall-tool.sh``. .11 chose the
script because .14 had not landed yet. ``nx self install`` is the packaged
installer, ``scripts/reinstall-tool.sh`` is "a thin repo wrapper around the
same scripts" (``commands/self_cmd.py``), and .15's rewired SessionStart hook
already runs the packaged form. It also needs no checkout: a generation box
has ``nx`` on PATH by construction, via the shims. A reader whose ``nx`` is a
dev checkout gets a refusal that names ``scripts/reinstall-tool.sh``, so that
case corrects itself rather than dead-ending.

NOT EVERY uv MENTION IS ADVICE, and the ones that are not must stay:

* ``health.py``'s legacy-tree detection names uv commands in order to DESCRIBE
  what a stray one does to the shims. Prose, not remediation.
* ``health.py``'s ``uv tool uninstall conexus  # once nothing is running from
  <legacy>`` is .7's legacy-REAP step. Naming uv is the whole point -- it is
  uv's tree being removed.
* ``command_context.py``'s ``uv tool install conexus`` is FIRST-install
  bootstrap. ``nx self install`` presupposes an installed ``nx``, so it cannot
  serve a box that has none; the uv form is how a box gets its first tree,
  which .7 then migrates.
"""
from __future__ import annotations

from nexus import install_layout

#: The packaged installer, and what .15's hook runs.
GENERATION_INSTALLER = "nx self install"


def has_generation_layout() -> bool:
    """True when this box has a generation install that actually resolves.

    A DANGLING ``current`` (pointer present, target reaped) is deliberately
    False: the generation advice would send the reader at a hole, and the uv
    fallback is the better of two imperfect answers. Never raises -- an
    unreadable layout is not a box we can claim has one.
    """
    try:
        return install_layout.current_generation().is_dir()
    except Exception:  # noqa: BLE001 — no resolvable layout: the uv advice stands
        return False


def upgrade_command(legacy: str) -> str:
    """The bare command that upgrades this box; *legacy* is the uv form.

    *legacy* is returned VERBATIM when there is no generation layout, never
    paraphrased -- each caller's fallback carries its own flags and the helper
    has no business editing them.
    """
    return GENERATION_INSTALLER if has_generation_layout() else legacy


def upgrade_advice(legacy: str, *, note: str = "") -> str:
    """:func:`upgrade_command` plus an optional trailing ``# note``.

    The note is appended to WHICHEVER command is chosen, so information the
    command does not carry (which version is available, say) survives on both
    layouts. .11's single-blob form dropped it on the generation branch, and
    left ``doctor.py`` splitting on ``"    #"`` to recover the bare command --
    a caller that wants the command now asks for the command.
    """
    command = upgrade_command(legacy)
    return f"{command}    # {note}" if note else command


def pinned_install_command(pin: str, *, legacy: str) -> str:
    """Install a SPECIFIC version -- the first hop of the stranded-install
    two-hop recovery (pin to the last migration-capable release, run the
    ladder, upgrade back).

    Deliberately not :func:`upgrade_command`: that would drop the pin and
    install the newest release, which is the exact hop the procedure exists to
    avoid. ``nx self install --version`` installs "this version instead of
    whatever the source resolves to", and under side-by-side a downgrade is
    safe by construction -- it builds a new generation and flips, leaving the
    old tree for its holders.
    """
    if has_generation_layout():
        return f"{GENERATION_INSTALLER} --version {pin}"
    return legacy


def local_extra_advice(
    *, legacy: str = 'uv tool install --reinstall "conexus[local]"',
) -> list[str]:
    """How to get the ``[local]`` extra (bge-768) onto THIS box (nexus-hbgso).

    The generation branch names the real command since nexus-pffc4:
    ``nx self install --extras local`` builds a NEW generation whose extras
    are the receipt's MERGED with the requested one (never replaced), flips,
    and leaves holders on their own trees -- the "rebuild with an extra"
    path this function used to have to say did not exist. Before pffc4 the
    branch returned an honest limitation sentence, because ``nx init`` and
    bare ``pip install`` both looked like remedies on a generation box and
    neither did anything.

    The legacy branch stays: on an unmigrated uv box the uv form is still
    the correct answer (and running the generation installer there is the
    different wrong answer -- module docstring).
    """
    if has_generation_layout():
        return [
            f"{GENERATION_INSTALLER} --extras local    # rebuilds this "
            "install with the extra added (merges with existing extras)",
        ]
    return [legacy]
