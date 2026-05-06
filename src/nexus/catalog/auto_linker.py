# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Auto-create catalog links from T1 scratch link-context entries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.catalog.catalog import Catalog
from nexus.catalog.tumbler import Tumbler

_log = structlog.get_logger()


@dataclass(frozen=True)
class LinkContext:
    """A single link target parsed from a T1 scratch link-context entry."""

    target_tumbler: str
    link_type: str


@dataclass(frozen=True)
class AutoLinkResult:
    """Structured outcome of an :func:`auto_link` call.

    Returned instead of a bare ``int`` (nexus-a414) so the caller can
    distinguish the four states an AUTO-LINK invocation can end in:

    - ``created > 0``: the recipe worked; some links materialised.
    - ``skipped_invalid_tumbler > 0``: target strings did not parse as a
      :class:`Tumbler` (e.g. T3 chash hex landed in scratch link-context
      because the upstream agent didn't extract the tumbler field from
      ``catalog_search`` results). Operationally actionable; surfaced at
      WARNING in the auto-linker so recipe-compliant agents see the gap.
    - ``skipped_missing_endpoint > 0``: tumbler parsed but no catalog
      entry resolves. Less alarming (legitimate cleanup signal during
      catalog rebuilds) but still tracked.
    - all zero with empty input: no link-context in scratch; not a
      failure, not a write.
    """

    created: int = 0
    skipped_invalid_tumbler: int = 0
    skipped_missing_endpoint: int = 0


def read_link_contexts(entries: list[dict[str, Any]]) -> list[LinkContext]:
    """Parse T1 scratch entries into LinkContext objects.

    Each entry may contain a ``targets`` array with multiple items.
    Flattens: one entry with N targets → N LinkContext objects.

    Accepts both ``"target_tumbler"`` (canonical) and ``"tumbler"``
    (shorthand used by skill SKILL.md templates). ``link_type`` defaults
    to ``"relates"`` when omitted.
    """
    contexts: list[LinkContext] = []
    for entry in entries:
        raw = entry
        # If the entry has a "content" key (raw T1 scratch format), parse it
        if isinstance(raw.get("content"), str):
            try:
                raw = json.loads(raw["content"])
            except (json.JSONDecodeError, TypeError):
                continue

        targets = raw.get("targets", [])
        for item in targets:
            # "tumbler" is the skill-authored shorthand; "target_tumbler" is canonical
            tumbler = item.get("target_tumbler") or item.get("tumbler", "")
            link_type = item.get("link_type", "relates")
            if tumbler:
                contexts.append(LinkContext(target_tumbler=tumbler, link_type=link_type))
    return contexts


def auto_link(
    cat: Catalog,
    source_tumbler: Tumbler,
    contexts: list[LinkContext],
) -> AutoLinkResult:
    """Create catalog links from source to each target in contexts.

    Uses ``link_if_absent`` for idempotency. Returns an
    :class:`AutoLinkResult` with separated counts (nexus-a414) so the
    caller can distinguish a clean miss (no contexts) from a recipe-
    compliant call that produced zero links because of bad target data.

    Skip behaviour:

    - Targets that fail :meth:`Tumbler.parse` are logged at WARNING with
      a recipe-compliance hint and counted in
      ``skipped_invalid_tumbler``. WARNING (not DEBUG, the prior level)
      because every recipe-compliant AUTO-LINK call that lands an
      invalid target was a silent miss before nexus-a414.
    - Targets whose tumbler parses but whose endpoint is not in the
      catalog are logged at DEBUG and counted in
      ``skipped_missing_endpoint``. Lower severity because the
      legitimate cause (catalog rebuild in progress, link-context
      pointing at not-yet-indexed target) is non-actionable from the
      caller's side.
    """
    if not contexts:
        return AutoLinkResult()

    created = 0
    skipped_invalid = 0
    skipped_missing = 0

    for ctx in contexts:
        try:
            target = Tumbler.parse(ctx.target_tumbler)
        except ValueError:
            skipped_invalid += 1
            _log.warning(
                "auto_link_skip_invalid_tumbler",
                tumbler=ctx.target_tumbler,
                hint=(
                    "AUTO-LINK recipe step 2 must scratch_put TUMBLER "
                    "strings (e.g. '1.2.5'), not T3 chash hex. Extract "
                    "the 'tumbler' field from catalog_search results."
                ),
            )
            continue

        try:
            was_created = cat.link_if_absent(
                source_tumbler,
                target,
                ctx.link_type,
                created_by="auto-linker",
            )
        except ValueError as exc:
            # link_if_absent raises ValueError for missing endpoints;
            # log the message to distinguish from unexpected ValueErrors
            skipped_missing += 1
            _log.debug(
                "auto_link_skip_missing_endpoint",
                source=str(source_tumbler),
                target=str(target),
                reason=str(exc),
            )
            continue

        if was_created:
            created += 1
            _log.debug(
                "auto_link_created",
                source=str(source_tumbler),
                target=str(target),
                link_type=ctx.link_type,
            )

    return AutoLinkResult(
        created=created,
        skipped_invalid_tumbler=skipped_invalid,
        skipped_missing_endpoint=skipped_missing,
    )
