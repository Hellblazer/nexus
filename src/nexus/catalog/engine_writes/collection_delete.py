# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx collection delete`` by label or by ``collection_id``: the client half of engine 0.1.202's
``POST /v1/catalog/collections/delete`` (RDR-229 P3.12, nexus-p2m1x.32; the engine half is P3.5a's ``CollectionDeleteRoute``).

The wire, as the route's Java answers it:

* request: ``{"name": label}`` (unchanged) or ``{"collection_id": <JSON integer>}``;
* 200 ``{deleted: {...counts}, collection_id, name}``: ``name`` is the label the id resolved to, the key the pipeline purge uses;
* 404 ``unknown_collection_id`` + ``collection_id``: no row of the tenant has the id;
* 409 ``collection_superseded`` + ``collection``, ``collection_id``, and (when a live successor was found) ``superseded_by`` (its
  label) and ``successor_collection_id``: the id names a retired row; nothing is deleted;
* 409 ``collection_label_ambiguous`` + ``collection``, ``collection_ids``: two live rows hold the label;
* the ``GET /version`` flag :data:`FLAG` says the engine reads ``collection_id`` on this route. An engine without it would ignore
  the key and answer for a body with no label, so the flag is read BEFORE the id is sent, and an unknown answer refuses.

A delete by a retired LABEL is answered 200 by the engine (it removes the tombstone; the empty-tombstone sweep depends on that), so
the refusal that names the successor is the client's: :func:`check_label_deletable` reads the registry listing and refuses before
anything is sent. The same check computes a label clash on the client with the census's ``_label_clash``.

There is no lookup from an id to a label anywhere here: a rename between the lookup and the delete would delete whatever then holds
the old label. Pure (no ``click``): the CLI turns the errors into its own."""
from __future__ import annotations

from typing import Any

import structlog

from nexus.catalog.rdr229.collection_row_census import _label_clash

_log = structlog.get_logger(__name__)

#: The ``GET /version`` flag of the engine's delete by ``collection_id``.
FLAG: str = "catalog_collection_delete_by_id_supported"
#: The first engine-service release that carries it (engine-service-v0.1.202, P3.5a's write half). HAND-KEPT; not a gate: the probe is.
MIN_ENGINE_VERSION: tuple[int, int, int] = (0, 1, 202)
_FLOOR = ".".join(map(str, MIN_ENGINE_VERSION))
ROUTE: str = "/collections/delete"


class CollectionDeleteRefusedError(RuntimeError):
    """The base of the typed refusals of a collection delete: nothing was removed. ``str()`` is the sentence to show."""


class DeleteByIdUnsupportedError(CollectionDeleteRefusedError):
    """``--id`` on an engine that did not confirm :data:`FLAG`."""


class UnknownCollectionIdError(CollectionDeleteRefusedError):
    def __init__(self, collection_id: int) -> None:
        self.collection_id = collection_id
        super().__init__(f"no collection with id {collection_id} in this tenant, so nothing was deleted. "
                         f"`nx collection list` shows each collection's ID.")


class SupersededCollectionDeleteError(CollectionDeleteRefusedError):
    """The label or id names a retired row. ``successor`` is its live successor's label; ``successor_id`` its id (None when unread)."""

    def __init__(self, label: str, collection_id: int | None, successor: str, successor_id: int | None) -> None:
        self.label, self.collection_id, self.successor, self.successor_id = label, collection_id, successor, successor_id
        who = f"collection {label!r}" + (f" (id {collection_id})" if collection_id is not None else "")
        if successor and successor_id is not None:
            by = f"superseded by {successor!r} (id {successor_id})"
            hint = f" Delete the live collection with `nx collection delete --id {successor_id}`, or leave the tombstone for the history."
        elif successor:
            by = f"superseded by {successor!r}"
            hint = " Delete the live collection by its label, or leave the tombstone for the history."
        else:
            by = "retired and names no live successor"
            hint = ""
        super().__init__(f"{who} is {by}, so a delete of it is refused and nothing was deleted: the retired row keeps its place "
                         f"in the history.{hint}")


class AmbiguousCollectionLabelError(CollectionDeleteRefusedError):
    def __init__(self, label: str, ids: list[int | None]) -> None:
        self.label, self.ids = label, ids
        shown = ", ".join("?" if i is None else str(i) for i in ids)
        super().__init__(f"{len(ids)} live collections hold the label {label!r} (ids {shown}), so nothing was deleted. "
                         f"Delete one of them by its id: `nx collection delete --id <ID>`.")


def _is_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_delete_by_id(cat: Any) -> None:
    """Refuse a delete by id unless *cat*'s ``GET /version`` carries :data:`FLAG` as a JSON ``true``.

    An UNKNOWN answer refuses (a failed probe, an edge that trimmed ``/version``, a handle with no probe). Nothing is sent."""
    flags_of = getattr(cat, "_version_flags", None)
    flags: dict[str, Any] = {}
    if callable(flags_of):
        try:
            flags = flags_of() or {}
        except Exception as exc:  # noqa: BLE001 — an unreadable probe is an unknown answer, which refuses
            _log.warning("collection_delete.version_probe_failed", error=str(exc))
    if flags.get(FLAG) is True:
        return
    version = flags.get("release_version") or flags.get("app_version")
    use_label = "`nx collection delete <label>` still works"
    if not flags:
        raise DeleteByIdUnsupportedError(
            f"could not confirm that the engine deletes by collection id (GET /version gave no answer), so `--id` is refused and "
            f"nothing was sent. Engine-service {_FLOOR} or later does; {use_label}.")
    raise DeleteByIdUnsupportedError(
        f"this engine ({version or 'version unknown'}) does not delete by collection id: GET /version lacks {FLAG}, which "
        f"engine-service {_FLOOR} or later advertises, so `--id` is refused and nothing was sent. {use_label}.")


def _body_of(exc: BaseException) -> tuple[int | None, Any]:
    """``(status, decoded body)`` of the engine's answer behind *exc*. The transport turns a 409 ``collection_superseded`` into a
    ``SupersededCollectionWriteError`` that keeps only the two labels and chains the ``HTTPStatusError`` as its cause, so the cause is
    read when *exc* carries no response (the ids of the refusal are only there)."""
    seen: BaseException | None = exc
    for _ in range(4):
        if seen is None:
            break
        body: Any = getattr(seen, "engine_body", None)
        status: Any = getattr(seen, "code", None)
        response = getattr(seen, "response", None)
        if body is None and response is not None:
            status = getattr(response, "status_code", None)
            try:
                body = response.json()
            except Exception:  # noqa: BLE001 — an unreadable body is not a typed answer
                body = None
        if body is not None:
            return status if isinstance(status, int) else None, body
        seen = seen.__cause__
    return None, None


def refusal_of(exc: BaseException) -> CollectionDeleteRefusedError | None:
    """The typed refusal for the engine's 404 ``unknown_collection_id``, 409 ``collection_superseded`` and 409
    ``collection_label_ambiguous`` answers to a delete, read by status AND ``reason`` (a 409 is also ``collection_busy``), else None.
    A body missing what the error names is not turned into it: the caller keeps its generic failure."""
    from nexus.db.engine_reasons import (  # noqa: PLC0415 — deferred: keeps this module a leaf for the CLI boundary
        COLLECTION_LABEL_AMBIGUOUS_REASON, COLLECTION_SUPERSEDED_REASON, UNKNOWN_COLLECTION_ID_REASON, error_reason)

    status, body = _body_of(exc)
    if not isinstance(body, dict):
        return None
    reason = error_reason(body)
    if status == 404 and reason == UNKNOWN_COLLECTION_ID_REASON and _is_id(body.get("collection_id")):
        return UnknownCollectionIdError(body["collection_id"])
    if status != 409:
        return None
    if reason == COLLECTION_SUPERSEDED_REASON and isinstance(body.get("collection"), str):
        succ, succ_id = body.get("superseded_by"), body.get("successor_collection_id")
        cid = body.get("collection_id")
        return SupersededCollectionDeleteError(body["collection"], cid if _is_id(cid) else None,
                                               succ if isinstance(succ, str) else "", succ_id if _is_id(succ_id) else None)
    ids = body.get("collection_ids")
    if reason == COLLECTION_LABEL_AMBIGUOUS_REASON and isinstance(body.get("collection"), str) and isinstance(ids, list):
        return AmbiguousCollectionLabelError(body["collection"], [i if _is_id(i) else None for i in ids])
    return None


def delete_by_id(cat: Any, collection_id: int) -> dict[str, Any]:
    """``POST /collections/delete`` with ``{"collection_id": N}``: the route's 200 body (``deleted``, ``collection_id``, ``name``).

    The flag is checked first (:func:`require_delete_by_id`). Sent once: a blind resend after a read timeout re-runs a physical delete
    whose own counts then describe the already-emptied collection (as the label form). A refusal raises what the transport raises; the
    caller reads it with :func:`refusal_of`. A body that is not an object is refused, never read as a delete."""
    if not _is_id(collection_id) or collection_id < 1:
        raise ValueError(f"collection_id must be a positive integer, got {collection_id!r}")
    require_delete_by_id(cat)
    result = cat._post(ROUTE, {"collection_id": collection_id}, idempotent=False)
    if not isinstance(result, dict):
        raise RuntimeError(f"{ROUTE} answered {result!r}, not a JSON object; refusing to guess what was deleted")
    return result


def _successor(rows: list[dict[str, Any]], row: dict[str, Any]) -> tuple[str, int | None]:
    """The live successor of the retired *row*, as ``(label, id)``: followed by ``superseded_by_id`` when the listing carries it, else by
    label, through at most a few hops of a chain of tombstones (a cycle ends the walk). ``("", None)`` when none is found."""
    by_id = {r["collection_id"]: r for r in rows if _is_id(r.get("collection_id"))}
    seen: set[int] = set()
    current = row
    for _ in range(16):
        nxt: dict[str, Any] | None = None
        sid = current.get("superseded_by_id")
        if _is_id(sid) and sid in by_id:
            nxt = by_id[sid]
        elif current.get("superseded_by"):
            label = str(current["superseded_by"])
            held = [r for r in rows if r.get("name") == label]
            nxt = next((r for r in held if not r.get("superseded_by")), held[0] if held else None)
            if nxt is None:
                return label, None
        if nxt is None:
            return "", None
        if not nxt.get("superseded_by"):
            cid = nxt.get("collection_id")
            return str(nxt.get("name") or ""), cid if _is_id(cid) else None
        key = id(nxt)
        if key in seen:
            return "", None
        seen.add(key)
        current = nxt
    return "", None


def check_label_deletable(rows: list[dict[str, Any]], label: str) -> None:
    """Refuse, from the registry listing *rows* (every lifecycle state), a delete by *label* the engine would not refuse itself.

    * two or more LIVE rows (``superseded_by`` empty) under the label: :class:`AmbiguousCollectionLabelError` naming their ids
      (``_label_clash`` finds it);
    * rows under the label and none live: :class:`SupersededCollectionDeleteError` naming the live successor's label and id. The
      engine would remove the tombstone with a 200; the history keeps it.

    A label with no row at all passes (a T3 orphan with no registry row is still deletable), and so does one with exactly one live row."""
    held = [r for r in rows if r.get("name") == label]
    live = [r for r in held if not r.get("superseded_by")]
    if _label_clash(live)["clashing_names"]:
        raise AmbiguousCollectionLabelError(label, sorted((r.get("collection_id") for r in live), key=lambda i: (i is None, i or 0)))
    if held and not live:
        tomb = held[0]
        succ, succ_id = _successor(rows, tomb)
        cid = tomb.get("collection_id")
        raise SupersededCollectionDeleteError(label, cid if _is_id(cid) else None, succ, succ_id)
