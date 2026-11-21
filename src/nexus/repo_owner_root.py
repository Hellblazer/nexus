# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Give an existing repo owner that has no ``repo_root`` the one the caller knows (RDR-229).

An owner an ``.nxexp`` import registered from a collection name has no root (the source machine's path is
not this one's, :mod:`nexus.nxexp_owner`). The first ``nx index repo`` of that repo knows the real one.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


def backfill_owner_root(client: Any, row: dict, repo_hash: str, main_repo: Path | str) -> bool:
    """Set the root of the owner *row* (a ``/owners/by_repo`` answer) when it has none; True when it did.

    One upsert that names the owner by its identity (``name``, ``owner_type``, ``repo_hash``) and carries the
    root, so the engine converges on the existing row and PRESERVES the fields the payload omits
    (``description``, ``head_hash``): an upsert naming the row by ``tumbler_prefix`` assigns every column
    from the payload (``CatalogRepository.ownerUpdateSet``), which would blank them. Best effort: a failed
    backfill leaves the owner as it was and never fails the caller."""
    if row.get("repo_root") or not str(main_repo):
        return False
    try:
        client.register_owner(
            str(row.get("name") or ""), str(row.get("owner_type") or "repo"),
            repo_hash=repo_hash, repo_root=main_repo,
        )
    except Exception as exc:  # noqa: BLE001 — best effort: the owner is usable without a root
        _log.warning("owner_repo_root_backfill_failed", tumbler_prefix=row.get("tumbler_prefix"), error=str(exc))
        return False
    return True
