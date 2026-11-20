# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wire calls for the RDR-254 move routes, ``POST /v1/catalog/documents/move`` and ``/owners/move``.

Each takes the catalog client (for its ``_post``) and returns the route's JSON object: the counts ``repointed``,
``absorbed``, ``trashed`` (fold orphans only), ``skipped``, ``unchanged``, ``aspect_rows_rewritten`` and
``_deleted``, ``highlight_rows_rewritten`` and ``_deleted``, ``links_remapped``, ``_collapsed`` and ``_dropped``,
``absorbed_tumblers``, and ``owners_folded`` on the owner route only.

``dry_run`` has no default: the engine answers 400 to a request without a JSON boolean, so a caller never writes (or
never fails to write) by omission. A refusal (a missing or trashed document, a missing owner, a live row already at
the new URI) is a 409 and reaches the caller as :class:`httpx.HTTPStatusError`. A write is not idempotent on the wire
(a resend after a lost reply would report different counts), so the transport never resends it; a dry run is a read
and rides the production-write guard as one.

Nothing here checks the engine's capability: :func:`nexus.catalog.engine_writes.floors.require_catalog_move` is the
caller's job and runs before any call to these."""
from __future__ import annotations

from typing import Any

#: An owner fold re-writes thousands of rows in one transaction; the client-wide 30 s default would cut it short.
MOVE_TIMEOUT_S: float = 300.0


def _need_text(**fields: str) -> None:
    for name, value in fields.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name!r} must be a non-empty string, got {value!r}")


def _need_bool(dry_run: Any) -> None:
    if not isinstance(dry_run, bool):
        raise TypeError(f"'dry_run' must be a bool, got {type(dry_run).__name__}")


def post_document_move(client: Any, tumbler: Any, *, new_source_uri: str, new_file_path: str, dry_run: bool) -> dict:
    """``POST /documents/move``: re-point the curator document *tumbler* at *new_file_path* / *new_source_uri*,
    absorbing a live row already registered there. See the module docstring for the answer and the refusals."""
    _need_bool(dry_run)
    _need_text(tumbler=str(tumbler), new_source_uri=new_source_uri, new_file_path=new_file_path)
    body = {"tumbler": str(tumbler), "new_source_uri": new_source_uri, "new_file_path": new_file_path,
            "dry_run": dry_run}
    return _post(client, "/documents/move", body, dry_run)


def post_owner_move(client: Any, owner: Any, *, new_root: str, new_repo_hash: str, dry_run: bool) -> dict:
    """``POST /owners/move``: keep the repo owner *owner* when its repository moves to *new_root* (the main
    repository's path) and hash *new_repo_hash*, folding any owner already registered there into it."""
    _need_bool(dry_run)
    _need_text(owner=str(owner), new_root=new_root, new_repo_hash=new_repo_hash)
    body = {"owner": str(owner), "new_root": new_root, "new_repo_hash": new_repo_hash, "dry_run": dry_run}
    return _post(client, "/owners/move", body, dry_run)


def _post(client: Any, path: str, body: dict[str, Any], dry_run: bool) -> dict:
    result = client._post(path, body, idempotent=False, timeout=MOVE_TIMEOUT_S, retry_read_timeout=False,
                          mutates=not dry_run)
    if not isinstance(result, dict):
        raise RuntimeError(f"{path} answered {result!r}, not a JSON object; refusing to guess what moved")
    return result
