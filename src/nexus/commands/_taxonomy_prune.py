# SPDX-License-Identifier: AGPL-3.0-or-later
"""The plan behind ``nx taxonomy project --prune-below-threshold`` (RDR-229 P2.4, nexus-p2m1x.9).

Prune is the one taxonomy consumer that deletes, so it is split out of ``taxonomy_cmd.py``:
which collections to prune and at what threshold (:func:`prune_plan`), whether the engine
applies exact-name matching (:func:`prune_supports_exact`, :func:`exact_prune_confirmed`),
and the guards that keep a prefix match from over-deleting.
"""
from __future__ import annotations

from typing import Any, NamedTuple

import click
import structlog

_log = structlog.get_logger(__name__)


def prune_supports_exact() -> bool:
    """Whether the engine's taxonomy prune route has the exact-name mode (the request
    field ``match: "exact"``, against the default ``"prefix"``), read from ``GET /version``'s
    ``taxonomy_prune_exact_supported`` through the shared catalog client's cached probe: one
    ``/version`` read per cache window, never one per collection.

    Only a JSON ``true`` counts. An absent key, ``false`` or an unreadable ``/version`` is
    ``False``: :func:`prune_plan` then keeps the prefix-collision guard and the prune runs in
    the engine's prefix mode, as it does against an engine that predates the mode.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 - deferred to avoid circular import at module load

    try:
        probe = getattr(make_catalog_reader(), "supports_taxonomy_prune_exact", None)
        return callable(probe) and probe() is True
    except Exception:  # noqa: BLE001 - a capability probe never fails a prune; unknown keeps the guard
        _log.warning("taxonomy_prune_exact_probe_failed", exc_info=True)
        return False


def exact_prune_confirmed(taxonomy: Any) -> bool:
    """Did the engine that will run the prunes apply ``match: "exact"`` to a verification prune?

    The ``/version`` flag and the prune can reach different engines (a rollback inside the flag's
    cache window, two instances), and an engine that ignores ``match`` prunes by name prefix, so the
    first REAL exact prune could already over-delete. One prune with ``match=exact`` for a name no
    collection has (the fixed prefix ``nexus-exact-probe-`` and a random 128-bit suffix, so no
    collection name starts with it except by a collision of negligible chance) settles it first:
    even a prefix match therefore reaches no rows. The guarantee is the name, not the threshold
    (0.0 is only a conventional floor: a stored similarity can be negative, and the schema coalesces
    a missing one to -1.0, so no threshold guarantees an empty match).
    Only an echoed ``"exact"`` confirms; a missing or ``"prefix"`` echo, or a failed call, does not,
    and the caller then runs the guarded plan, which is safe on any engine.
    """
    import uuid  # noqa: PLC0415 - one call per prune sweep

    try:
        _removed, applied = taxonomy.prune_projection_below(
            f"nexus-exact-probe-{uuid.uuid4().hex}", 0.0, exact=True,
        )
    except Exception:  # noqa: BLE001 - an unanswered verification is an unconfirmed one: the guarded plan is safe
        _log.warning("taxonomy_prune_exact_verification_failed", exc_info=True)
        return False
    return applied == "exact"


class PrunePlan(NamedTuple):
    """What ``--prune-below-threshold`` will do: ``targets`` is ``(collection, threshold)``
    pairs, ``skipped`` the names pruned only to a lower threshold than their own, ``refused``
    the names left alone because their row read was unreadable."""

    targets: list[tuple[str, float]]
    skipped: list[str]
    refused: list[str]


def prune_plan(
    taxonomy: Any, threshold: float | None, *, only: str | None, exact: bool,
) -> PrunePlan:
    """The prune plan for ``--prune-below-threshold`` (RDR-229 P2.4): every collection the
    live catalog listing names, every collection holding topics and every distinct
    ``source_collection`` of the projection rows (``get_projection_counts_by_collection``), or
    just *only* (the single-source form), each at *threshold* when given, else its row's default.
    The last source matters because the engine's prefix match reaches a collection that has
    projection rows but no topics and no live row (a retired one) from a shorter name; if that
    read fails the plan cannot see such a collection, warns in the log, and the engine's
    exact-name mode is what ends the exposure.

    Prune is the one consumer that deletes, so an **unreadable** row read (the listing raised)
    is refused for that collection, with a printed line, unless an explicit *threshold* was
    given: a code__-named collection with a knowledge row would otherwise be pruned at the
    name's 0.70 instead of 0.50. The same refusal applies to a collection with an unreadable
    EXTENSION, whose guessed threshold would set the floor. A NoRow answer (no row backs the name) keeps its name
    derivation, as the old prefix sweep did. A listing that cannot be read narrows the sweep
    to the collections holding topics; the sweep is a recovery path and does not stop on it.

    The engine matches a prune by name PREFIX unless it supports the exact-name mode
    (*exact*), so pruning a collection whose name is a proper prefix of another's also
    reaches the longer one's rows. Such a collection is pruned at the lowest threshold
    among it and the collections whose names extend it: that deletes nothing an extension's
    own pass would keep, at the cost of leaving the collection's own rows between the two
    thresholds, which the plan reports as skipped. With *exact* there is nothing to guard.
    """
    from nexus.mcp_infra import get_live_collection_names  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.projection_threshold import resolve_projection_threshold  # noqa: PLC0415 - deferred to avoid circular import at module load

    if only is not None and threshold is not None:
        return PrunePlan([(only, threshold)], [], [])  # the user's own number: no listing needed
    names = set(taxonomy.get_distinct_collections())
    try:
        names.update(get_live_collection_names())
    except Exception:  # noqa: BLE001 - recovery sweep: an unreadable listing narrows it to the collections with topics, it does not stop it
        _log.warning("taxonomy_prune_listing_unreadable", exc_info=True)
    try:
        # The engine's prefix match reaches a collection that has projection rows but no topics and no
        # live row, which neither listing names: its rows come from this distinct source_collection set.
        names.update(taxonomy.get_projection_counts_by_collection())
    except Exception:  # noqa: BLE001 - recovery sweep: without it the plan cannot see collections only the projection rows name, and says so
        _log.warning("taxonomy_prune_projection_sources_unreadable", exc_info=True)
    if only is not None:
        names.add(only)
    ordered = sorted(names)
    candidates = ordered if only is None else [only]

    answers: dict[str, tuple[float, str]] = {}

    def _answer(name: str) -> tuple[float, str]:
        if name not in answers:
            if threshold is not None:
                answers[name] = (threshold, "explicit")
            else:
                resolved = resolve_projection_threshold(name)
                answers[name] = (resolved.value, resolved.source)
        return answers[name]

    targets: list[tuple[str, float]] = []
    skipped: list[str] = []
    refused: list[str] = []
    for n in candidates:
        thr, source = _answer(n)
        if source == "unreadable":
            refused.append(n)
            click.echo(
                f"  {n}: refused, its catalog row could not be read, so its threshold would be "
                "a guess from the name; pass --threshold to prune at one you choose"
            )
            continue
        if exact:
            targets.append((n, thr))
            continue
        extensions = [m for m in ordered if m != n and m.startswith(n)]
        blind = next((m for m in extensions if _answer(m)[1] == "unreadable"), None)
        if blind is not None:
            # The floor would come from a threshold guessed from the extension's name.
            refused.append(n)
            click.echo(
                f"  {n}: refused, the catalog row of {blind}, whose name it prefixes, could not be read, "
                "so the lowest threshold its prune must respect would be a guess; pass --threshold to "
                "prune at one you choose"
            )
            continue
        lowest, lowest_name = thr, None
        for m in extensions:
            if _answer(m)[0] < lowest:
                lowest, lowest_name = _answer(m)[0], m
        if lowest_name is not None:
            skipped.append(n)
            click.echo(
                f"  {n}: pruned only below {lowest:.2f} (its own threshold is {thr:.2f}): its name "
                f"prefixes {lowest_name}, and the engine's name-prefix match would also delete "
                f"{lowest_name}'s rows between {lowest:.2f} and {thr:.2f}"
            )
        targets.append((n, lowest))
    return PrunePlan(targets, skipped, refused)
