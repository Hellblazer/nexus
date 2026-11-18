# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-134 Step 5a-i (nexus-7fku2.1.5): the client half of ``topic_route`` on search-per-collection.

The engine's ``POST /v1/vectors/search-per-collection`` takes ``topic_route: {mode, width, collections}`` and
answers each ``per_collection`` entry with a ``topic_route_status`` (RDR-134 Steps 3 and 4). This module holds what
the client does around it, apart from the two big modules that call it:

* :func:`route_field` builds the request field. It is prose-only: ``collections`` is always an EXPLICIT list of the
  request's prose-prefix names, because the engine reads an absent or JSON-null ``collections`` as "route every
  collection of the request", code ones included, and on a local bge install code and docs share one model group
  and so one request. A request with nothing eligible, or with ``chash_allow`` or a metadata ``where`` (which the
  engine leaves unrouted anyway), gets no field at all.
* :func:`carry_entry_fields` keeps the engine's four ``topic_route_*`` keys on a validated ``per_collection``
  entry. They are optional diagnostics: a key of the wrong type is dropped, never a reason to refuse the envelope
  (a refusal switches the route off for a minute).
* :class:`TopicRouteTally` collects one search's per-collection statuses and logs the ``search_topic_route`` event.

``subtree`` and catalog-route ``query`` steps never reach ``search_cross_corpus`` (RDR-134 Step 5a), so the event
has no ``subtree`` reason: its count at this layer is structurally zero, and a test fails the day that changes.
"""
from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

import structlog

_log = structlog.get_logger(__name__)

#: Collection-name prefixes that may be routed. Code collections never are.
PROSE_PREFIXES: Final[tuple[str, ...]] = ("knowledge__", "docs__", "rdr__", "paper__")
MODES: Final[tuple[str, ...]] = ("top1", "band")
EVENT: Final = "search_topic_route"

#: Why a whole request went unrouted, as the event names it.
REASON_CHASH_ALLOW: Final = "chash_allow"
REASON_WHERE: Final = "where"
REASON_NO_PROSE: Final = "no_prose_collections"
REASON_ROUTE_OFF: Final = "per_collection_route_off"
REASON_CAPABILITY: Final = "capability_absent"


def is_prose(collection: str) -> bool:
    return collection.startswith(PROSE_PREFIXES)


def _parse(topic_route: Mapping[str, Any]) -> tuple[str, float | None, list[str] | None]:
    """``(mode, width, listed collections or None)``; ``ValueError`` for what the engine would 400."""
    mode = topic_route.get("mode")
    if mode not in MODES:
        raise ValueError(f"topic_route mode must be one of {MODES}, got {mode!r}")
    width: float | None = None
    if mode == "band":
        w = topic_route.get("width")
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0:
            raise ValueError(f"topic_route width must be a finite number >= 0 for band, got {w!r}")
        width = float(w)
    listed = topic_route.get("collections")
    if listed is not None:
        if isinstance(listed, str) or not isinstance(listed, Iterable) or not all(isinstance(c, str) for c in listed):
            raise ValueError("topic_route collections must be a list of collection names")
        listed = list(listed)
    return mode, width, listed


def validate(topic_route: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` when *topic_route* is a value the engine would refuse."""
    _parse(topic_route)


def unrouted_reason(*, where: Mapping | None, chash_allow: Sequence[str] | None) -> str | None:
    """The request-level field that leaves a request unrouted (the engine marks every collection ``skipped``)."""
    if chash_allow is not None:
        return REASON_CHASH_ALLOW
    if where:
        return REASON_WHERE
    return None


def route_field(
    topic_route: Mapping[str, Any],
    collection_names: Sequence[str],
    *,
    where: Mapping | None = None,
    chash_allow: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    """The request's ``topic_route`` field, or ``None`` when nothing is to be routed.

    ``collections`` is the prose-prefix subset of *collection_names* (further narrowed by the caller's own
    ``collections``, which must name collections of the request), always a list. ``width`` is sent for ``band``
    only.
    """
    mode, width, listed = _parse(topic_route)
    names = list(collection_names)
    if listed is not None:
        unknown = [c for c in listed if c not in names]
        if unknown:
            raise ValueError(f"topic_route collections names {unknown[0]!r}, which is not in the request's collections")
    if unrouted_reason(where=where, chash_allow=chash_allow) is not None:
        return None
    eligible = [c for c in names if is_prose(c) and (listed is None or c in listed)]
    if not eligible:
        return None
    field: dict[str, Any] = {"mode": mode}
    if width is not None:
        field["width"] = width
    field["collections"] = eligible
    return field


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def carry_entry_fields(entry: Mapping[str, Any], out: dict[str, Any]) -> None:
    """Copy the engine's ``topic_route_*`` keys from a raw ``per_collection`` entry onto *out*, each only when
    present and of the right type. An entry whose arm errored carries no status, and an engine that does not
    route carries none anywhere: *out* then gains nothing."""
    status = entry.get("topic_route_status")
    if isinstance(status, str) and status:
        out["topic_route_status"] = status
    error = entry.get("topic_route_error")
    if isinstance(error, str):
        out["topic_route_error"] = error
    coverage = entry.get("topic_route_coverage")
    if _is_number(coverage):
        out["topic_route_coverage"] = coverage
    topics = entry.get("topic_route_topics")
    if _is_number(topics) and float(topics).is_integer() and topics >= 0:
        out["topic_route_topics"] = int(topics)


class TopicRouteTally:
    """One ``search_cross_corpus`` call's routing outcome, logged once as ``search_topic_route``.

    Built only for a call that asked for routing. Statuses come from the engine, per prose collection; a prose
    collection with no status (its arm errored, its group fell to the batched path, an engine that does not route)
    is ``unreported``, which is not a status and never counts as ``skipped``. A request left unrouted by
    ``chash_allow`` or a metadata ``where`` counts every prose collection as ``skipped``, as the engine would have
    reported them. Collections that are not prose are ``ineligible`` and never listed.
    """

    def __init__(
        self,
        topic_route: Mapping[str, Any],
        collections: Sequence[str],
        *,
        where: Mapping | None,
        chash_allow: Sequence[str] | None,
        per_collection_route: bool,
    ) -> None:
        self._mode, self._width, _ = _parse(topic_route)
        names = list(dict.fromkeys(collections))
        self._eligible = [c for c in names if is_prose(c)]
        self._ineligible = len(names) - len(self._eligible)
        self._entries: dict[str, dict[str, Any]] = {}
        reason = unrouted_reason(where=where, chash_allow=chash_allow)
        if not self._eligible:
            reason = REASON_NO_PROSE
        elif reason is None and not per_collection_route:
            reason = REASON_ROUTE_OFF
        self._reason = reason
        if reason in (REASON_CHASH_ALLOW, REASON_WHERE):
            self._entries = {c: {"topic_route_status": "skipped"} for c in self._eligible}

    @property
    def sent(self) -> bool:
        """False when the request was left unrouted before any engine call, so no group asks for the field."""
        return self._reason is None

    def record(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """Take the validated ``per_collection`` entries of one engine response. Safe from worker threads: a
        collection belongs to one group, so each key is written by one call."""
        if not self.sent:
            return
        eligible = set(self._eligible)
        for entry in entries:
            name = entry.get("collection")
            if name in eligible and isinstance(entry.get("topic_route_status"), str):
                self._entries[name] = dict(entry)

    def emit(self, t3: Any) -> None:
        """Log the event. Exactly one per tally; the caller calls it once, after the fan-out."""
        from nexus.tables.topic_route_status import STATUSES  # noqa: PLC0415 — keeps this module's import graph leaf-light

        statuses = {c: e["topic_route_status"] for c, e in self._entries.items()}
        reason = self._reason
        unreported = [c for c in self._eligible if c not in statuses]
        if reason is None and unreported and not statuses:
            # Nothing came back with a status. Say why when the engine cannot route (the probe is cached).
            probe = getattr(t3, "supports_topic_route", None)
            try:
                if callable(probe) and probe() is False:
                    reason = REASON_CAPABILITY
            except Exception:  # noqa: BLE001 — a diagnostic probe never fails the search
                pass
        counts = {s: sum(1 for v in statuses.values() if v == s) for s in STATUSES}
        fields: dict[str, Any] = {
            "mode": self._mode,
            "width": self._width,
            "eligible": len(self._eligible),
            "ineligible": self._ineligible,
            "statuses": statuses,
            **counts,
            "unrecognized": sum(1 for v in statuses.values() if v not in STATUSES),
            "unreported": len(unreported),
            "unrouted_reason": reason,
        }
        for key, field in (("errors", "topic_route_error"), ("coverage", "topic_route_coverage"),
                           ("topics", "topic_route_topics")):
            held = {c: e[field] for c, e in self._entries.items() if field in e}
            if held:
                fields[key] = held
        _log.info(EVENT, **fields)
