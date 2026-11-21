# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-254 Phase 4 (Gap 4): the name a repository registers its owner under.

The engine matches an owner by ``(name, owner_type)`` first and ``repo_hash`` second, then OVERWRITES the matched
owner's hash and root. A second clone of a repository in a folder with the same name (``~/work/nexus`` and
``~/scratch/nexus``) therefore took the first clone's owner over on its first registration, and the two swapped it on
every registration after. The engine's matching stays as it is; the client stops sending the name that triggers it.

:func:`repo_owner_name` is the rule the repo owner registrations call on an ``owner_for_repo`` miss, before their
upsert: ``HttpCatalogClient.ensure_owner_for_repo`` and the two direct ``register_owner`` sites
(``indexer._catalog_hook``, ``nx catalog backfill``), each with its own name, hash and root (the experiment-owner
registration in ``nxexp_owner`` has its own name-taken rule and no root). It returns
``<name>-<repo_hash>`` when an owner of that name and type records a root that is another live git work tree on this
machine, or a root that is not readable (a wrong split costs one duplicate index, a wrong takeover costs the first clone
its owner), and ``name`` otherwise: no such owner, an owner without a root, a root that is gone or no longer a work tree
(the Gap 3 hint picks that case up), or the same directory reached another way (``os.path.samefile``, so a symlinked
path to one clone is not split, and a differently cased one is not on a case-insensitive filesystem only; an ``st_ino``
of 0, as some Windows network shares report, would make ``samefile`` call two directories one). A root recorded by
another machine, a container path or an unmounted volume reads as gone here and keeps ``name``.

When ``<name>-<repo_hash>`` is already held by a repo owner of another hash (an owner move keeps the name and changes
the hash), the suffix grows to the full SHA-256 of the repo root instead. Each split logs ``repo_owner_split``.

No error handling of its own: an engine error from the lookup propagates the way an ``owner_for_repo`` error does at
each call site. The module sits in this subpackage because ``nexus/catalog/`` is at its module cap."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


def _is_other_live_clone(recorded_root: str, repo_root: Path | str) -> bool:
    """True when *recorded_root* is a git work tree on this machine other than *repo_root*, or is not readable.

    Only a permission error means "exists but not readable" (a split). Any other ``OSError`` on the recorded root
    (``EINVAL`` for a path another machine recorded and this OS rejects, ``ESTALE``, ``EIO``) is not a live clone here.
    A *repo_root* that cannot be reached cannot be shown to be the recorded root, so it splits too: it is never read as
    "the recorded root is gone", which would take the owner over."""
    try:
        os.stat(os.path.join(recorded_root, ".git"))  # a gone root, or one that is no longer a work tree, raises here
    except PermissionError:
        return True
    except OSError:  # FileNotFoundError, NotADirectoryError and every other error: not a live work tree on this machine
        return False
    try:
        return not os.path.samefile(recorded_root, repo_root)
    except OSError:  # this run's root is missing (or the recorded one vanished): sameness cannot be shown
        return True


def _suffixed_name_is_taken(cat: Any, candidate: str, repo_hash: str) -> bool:
    """True when a repo owner of another hash already holds *candidate*: the engine would converge on that owner by
    name and overwrite it, which is the takeover this rule exists to stop."""
    for tumbler in cat.owner_tumblers_by_name(candidate):
        owner = cat.get_owner_by_prefix(str(tumbler))
        if owner and owner.get("owner_type") == "repo" and owner.get("repo_hash") != repo_hash:
            return True
    return False


def repo_owner_name(cat: Any, *, name: str, repo_hash: str, repo_root: Path | str) -> str:
    """The owner name to register this repository under: *name*, or ``<name>-<repo_hash>`` for a second live clone.

    When that name is already held by a repo owner of another hash, the suffix is the full SHA-256 of *repo_root*
    (``<name>-<64 hex>``): deterministic for the clone, and not shared with another owner.

    *cat* needs ``owner_tumblers_by_name`` (``/owners/by_name``) and ``get_owner_by_prefix`` (``/owners/show``)."""
    if not name or not repo_hash:
        return name
    for tumbler in cat.owner_tumblers_by_name(name):
        owner = cat.get_owner_by_prefix(str(tumbler))
        if not owner or owner.get("owner_type") != "repo" or owner.get("repo_hash") == repo_hash:
            continue
        recorded_root = str(owner.get("repo_root") or "")
        if recorded_root and _is_other_live_clone(recorded_root, repo_root):
            candidate = f"{name}-{repo_hash}"
            if _suffixed_name_is_taken(cat, candidate, repo_hash):
                full = hashlib.sha256(str(repo_root).encode()).hexdigest()
                candidate = f"{name}-{full}" if full.startswith(repo_hash) else f"{name}-{repo_hash}-{full}"
            _log.info("repo_owner_split", name=name, candidate=candidate, other_root=recorded_root)
            return candidate
    return name
