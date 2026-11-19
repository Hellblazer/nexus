# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-134 Step 7 (nexus-7fku2.2.1): the plan runner carries ``topic_route`` from ``search.topic_route_nx_answer``.

The transport mirrors the document confinement's ``chash_allow`` (``document_confinement.chash_allow_scope``):

1. :func:`configured_route` reads the setting once per plan run (``off``, ``top1`` or ``band:<width>``; default
   ``off``). A value it cannot read logs ``topic_route_setting_invalid`` and is off, never routed.
2. The runner attaches the route to a step's args with :func:`route_for_step`: a ``search`` or ``query`` step whose
   ``scope.taxonomy_domain`` is ``prose``, and not a ``query`` step on the catalog route (``author``,
   ``content_type``, ``follow_links``, ``subtree``), which never reaches ``search_cross_corpus``, and not a step
   that names a ``topic`` (a post-filter to that topic).
3. The default dispatcher pops it before the kwargs-drop pass (neither tool has the parameter) and runs the tool
   inside :func:`topic_route_scope`; ``search_cross_corpus`` reads :func:`active_topic_route` when its caller passed
   no ``topic_route`` of its own.

The runner decides only whether to route. Which collections are routed is ``nexus.topic_route.route_field``'s:
prose prefixes only, so a ``corpus: all`` step or a local bge model group that mixes code and docs names only the
prose collections. Leaf module: stdlib, structlog and ``document_confinement`` (itself stdlib-only), because
``search_engine`` and ``mcp.core`` import it lazily on their hot paths.
"""
from __future__ import annotations

import contextlib
import math
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from typing import Any, Final

import structlog

from nexus.plans.document_confinement import _QUERY_CATALOG_PARAMS

_log = structlog.get_logger(__name__)

#: The key under the config's ``search`` block.
SETTING_KEY: Final = "topic_route_nx_answer"
SETTING_DEFAULT: Final = "off"
#: The step tools whose route reaches ``search_cross_corpus``.
ROUTED_TOOLS: Final[tuple[str, ...]] = ("search", "query")
#: The structlog event for a setting value that is not ``off``, ``top1`` or ``band:<width>``.
INVALID_EVENT: Final = "topic_route_setting_invalid"
#: The structlog event for a config that could not be read for the setting (a malformed ``entity`` section).
CONFIG_UNREADABLE_EVENT: Final = "topic_route_config_unreadable"

_ROUTE: ContextVar[dict[str, Any] | None] = ContextVar("nexus_topic_route", default=None)


def parse_setting(value: Any) -> dict[str, Any] | None:
    """The ``topic_route`` request value for a setting value, or ``None`` for off.

    ``None``, ``False`` (YAML 1.1 reads an unquoted ``off`` as false) and ``"off"`` are off. ``"top1"`` gives
    ``{"mode": "top1"}``; ``"band:<w>"`` gives ``{"mode": "band", "width": w}`` for a finite ``w > 0``. Anything
    else logs :data:`INVALID_EVENT` and is off.
    """
    if value is None or value is False:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("off", ""):
            return None
        if text == "top1":
            return {"mode": "top1"}
        if text.startswith("band:"):
            try:
                width = float(text[len("band:"):])
            except ValueError:
                width = math.nan
            if math.isfinite(width) and width > 0:
                return {"mode": "band", "width": width}
    _log.warning(INVALID_EVENT, setting=f"search.{SETTING_KEY}", value=repr(value), fallback="off")
    return None


def setting_from_config(cfg: Mapping[str, Any]) -> dict[str, Any] | None:
    search = cfg.get("search")
    raw = search.get(SETTING_KEY, SETTING_DEFAULT) if isinstance(search, Mapping) else SETTING_DEFAULT
    return parse_setting(raw)


def configured_route() -> dict[str, Any] | None:
    """The route the configured ``search.topic_route_nx_answer`` asks for, or ``None``. Read once per plan run.

    A config whose ``entity`` section is malformed raises :class:`~nexus.entity_keys.EntityConfigError` from
    ``load_config``. The route is optional, and RDR-147's resolution boundary is where that error is judged, so here it
    means no route and one warning, never the end of the plan run (develop red, nexus-7fku2.2.1)."""
    from nexus.config import load_config  # noqa: PLC0415 — keeps this module leaf; config is heavy
    from nexus.entity_keys import EntityConfigError  # noqa: PLC0415 — as above

    try:
        cfg = load_config()
    except EntityConfigError as exc:
        _log.warning(CONFIG_UNREADABLE_EVENT, setting=f"search.{SETTING_KEY}", error=str(exc), fallback="off")
        return None
    return setting_from_config(cfg)


def route_for_step(
    tool: str, scope: Mapping[str, Any] | None, args: Mapping[str, Any], route: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """The ``topic_route`` a step carries, or ``None`` when it is not routed."""
    if route is None or tool not in ROUTED_TOOLS or not scope:
        return None
    if scope.get("taxonomy_domain") != "prose":
        return None
    if tool == "query" and any(args.get(k) for k in _QUERY_CATALOG_PARAMS):
        return None
    if args.get("topic"):
        # ``topic`` post-filters to a named topic; routing to the query's nearest one first would leave the
        # filter little or nothing to keep.
        return None
    return dict(route)


def active_topic_route() -> dict[str, Any] | None:
    """The route the tool call in progress asks ``search_cross_corpus`` for, or ``None``."""
    route = _ROUTE.get()
    return dict(route) if route is not None else None


@contextlib.contextmanager
def topic_route_scope(route: Mapping[str, Any] | None) -> Iterator[None]:
    """Make *route* the ``topic_route`` ``search_cross_corpus`` sends for the tool call run inside the block."""
    token = _ROUTE.set(dict(route) if route is not None else None)
    try:
        yield
    finally:
        _ROUTE.reset(token)
