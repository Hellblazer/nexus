# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 Phase 4 (Gap 4): the name a repository registers its owner under.

The engine matches an owner by ``(name, owner_type)`` first and ``repo_hash`` second, then OVERWRITES the matched
owner's hash and root. A second clone of a repository in a folder with the same name (``~/work/nexus`` and
``~/scratch/nexus``) therefore took the first clone's owner over on its first registration, and the two swapped it on
every registration after. The engine's matching stays as it is; the client stops sending the name that triggers it.

:func:`repo_owner_name` is the one rule every repo owner registration calls on an ``owner_for_repo`` miss, before its
upsert: ``HttpCatalogClient.ensure_owner_for_repo`` and the two direct ``register_owner`` sites
(``indexer._catalog_hook``, ``nx catalog backfill``), each with its own name, hash and root. It returns
``<name>-<repo_hash>`` when an owner of that name and type records a root that is another live git work tree on this
machine, or a root that cannot be read (a wrong split costs one duplicate index, a wrong takeover costs the first clone
its owner), and ``name`` otherwise: no such owner, an owner without a root, a root that is gone or no longer a work tree
(the Gap 3 hint picks that case up), or the same directory reached another way (``os.path.samefile``, so a symlinked or
differently cased path to one clone is not split).

No error handling of its own: an engine error from the lookup propagates the way an ``owner_for_repo`` error does at
each call site. The module sits in this subpackage because ``nexus/catalog/`` is at its module cap."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def _is_other_live_clone(recorded_root: str, repo_root: Path | str) -> bool:
    """True when *recorded_root* is a git work tree on this machine other than *repo_root*, or cannot be read."""
    try:
        os.stat(os.path.join(recorded_root, ".git"))  # a gone root, or one that is no longer a work tree, raises here
        return not os.path.samefile(recorded_root, repo_root)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:  # a permission error and the like: the root exists but is not readable
        return True


def repo_owner_name(cat: Any, *, name: str, repo_hash: str, repo_root: Path | str) -> str:
    """The owner name to register this repository under: *name*, or ``<name>-<repo_hash>`` for a second live clone.

    *cat* needs ``owner_tumblers_by_name`` (``/owners/by_name``) and ``get_owner_by_prefix`` (``/owners/show``)."""
    if not name or not repo_hash:
        return name
    for tumbler in cat.owner_tumblers_by_name(name):
        owner = cat.get_owner_by_prefix(str(tumbler))
        if not owner or owner.get("owner_type") != "repo" or owner.get("repo_hash") == repo_hash:
            continue
        if (recorded_root := owner.get("repo_root") or "") and _is_other_live_clone(str(recorded_root), repo_root):
            return f"{name}-{repo_hash}"
    return name
