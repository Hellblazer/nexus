# SPDX-License-Identifier: AGPL-3.0-or-later
"""Topic labels after a document is flagged sensitive late (RDR-243 Phase 3 fix r1, nexus-rfvyy.8).

A topic's label and terms are computed from its members' text when ``nx taxonomy discover`` runs. Since Phase 3
discover leaves a sensitive member out, but a topic built BEFORE the document was flagged keeps a label (and c-TF-IDF
terms) that carry words of it, and the Knowledge Map (``nexus.context``, pasted into every subagent) and the MCP
cluster headers print that label.

The smallest change that makes such a label unreachable from those surfaces is to replace it at the moment the flag
is set: :func:`withhold_topic_labels` gives every topic that holds a chunk of the newly flagged documents (and its
ancestors, whose label was computed over the same members) the label :data:`TOPIC_LABEL_WITHHELD`, and
:func:`purge_knowledge_map_caches` drops the cached Knowledge Map files that were written with the old labels. The
rebuild that gives those topics real labels back, without the document, is
``nx taxonomy discover --force <collection>``.

No per-search cost: the work is done once, by the verb that sets the flag.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "REDISCOVER_HINT", "TOPIC_LABEL_WITHHELD", "purge_knowledge_map_caches", "withhold_after_flag", "withhold_after_flag_best_effort",
    "withhold_topic_labels",
]

#: What a topic that holds a late-flagged document's chunks is labelled until it is rediscovered. It is the header
#: text the MCP search render already uses for a label it will not print.
TOPIC_LABEL_WITHHELD: Final = "label withheld"

#: The sentence the flagging verb prints after relabelling.
REDISCOVER_HINT: Final = (
    "Topics built before the flag keep terms computed from the document. Their labels were replaced with "
    f"'{TOPIC_LABEL_WITHHELD}'; rebuild them without the document with `nx taxonomy discover --force <collection>`."
)

_BATCH: Final = 300


def _chashes_of(cat: Any, tumblers: Iterable[str]) -> list[str]:
    """The chash of every chunk the manifests of *tumblers* name."""
    wanted = [str(t) for t in tumblers]
    out: dict[str, None] = {}
    for start in range(0, len(wanted), _BATCH):
        manifests = cat.get_manifests(wanted[start:start + _BATCH])
        for rows in manifests.values():
            for m in rows:
                out[m.chash] = None
    return list(out)


def withhold_topic_labels(taxonomy: Any, cat: Any, tumblers: Iterable[str]) -> list[int]:
    """Relabel every topic holding a chunk of *tumblers* (and each ancestor) :data:`TOPIC_LABEL_WITHHELD`.

    Returns the topic ids relabelled, sorted. Raises whatever the catalog or taxonomy raises: the caller says
    whether a failure is loud (the verb) or logged."""
    chashes = _chashes_of(cat, tumblers)
    topic_ids: set[int] = set()
    for start in range(0, len(chashes), _BATCH):
        topic_ids.update(int(t) for t in taxonomy.get_assignments_for_docs(chashes[start:start + _BATCH]).values())
    # A split topic's root was labelled over the same members; walk to the roots.
    pending, seen = list(topic_ids), set(topic_ids)
    while pending:
        topic = taxonomy.get_topic_by_id(pending.pop())
        parent = (topic or {}).get("parent_id")
        if parent is not None and int(parent) not in seen:
            seen.add(int(parent))
            pending.append(int(parent))
    for tid in sorted(seen):
        taxonomy.update_topic_label(tid, TOPIC_LABEL_WITHHELD)
    return sorted(seen)


def withhold_after_flag(cat: Any, tumblers: Iterable[str]) -> list[int]:
    """What a verb that has just flagged *tumblers* runs: relabel the topics that hold them, and when any was
    relabelled drop the Knowledge Map caches. Returns the relabelled topic ids. Raises on a catalog or taxonomy
    failure; the caller decides how loud that is."""
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 - deferred: the T2 handle is only needed here
    from nexus.db.t2 import T2Database  # noqa: PLC0415 - as above

    with T2Database(default_db_path()) as db:
        ids = withhold_topic_labels(db.taxonomy, cat, tumblers)
    if ids:
        purge_knowledge_map_caches()
    return ids


def withhold_after_flag_best_effort(cat: Any, tumblers: Iterable[str], *, source: str) -> list[int]:
    """:func:`withhold_after_flag` for a path that flags a document as a side effect (a late ``--sensitive``
    write, ``nx index repo --sensitive-glob``, an ``.nxexp`` import): the flag is set and the caller carries on,
    so a failure of the topic bookkeeping is logged (``sensitive_topics_not_relabelled``, naming *source* and
    the rebuild command) and never raised. Returns the relabelled topic ids, ``[]`` on failure."""
    wanted = [str(t) for t in tumblers]
    if not wanted:
        return []
    try:
        return withhold_after_flag(cat, wanted)
    except Exception as exc:  # noqa: BLE001 - never block the caller on the topic bookkeeping
        _log.warning(
            "sensitive_topics_not_relabelled", source=source, tumblers=wanted[:5], count=len(wanted),
            error=f"{type(exc).__name__}: {exc}"[:200], hint="run `nx taxonomy discover --force <collection>`",
        )
        return []


def purge_knowledge_map_caches() -> int:
    """Delete the cached Knowledge Map files (they were written with the old labels). Returns how many went.

    The next ``nx index repo`` / ``nx taxonomy discover`` writes them again from the current labels."""
    from nexus.context import _ctx_nexus_config_dir  # noqa: PLC0415 - deferred: nexus.context pulls the catalog and config helpers

    base = _ctx_nexus_config_dir()
    files = [*(base / "context").glob("*.txt"), base / "context_l1.txt"]
    removed = 0
    for path in files:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            _log.warning("knowledge_map_purge_failed", path=str(path), error=f"{type(exc).__name__}: {exc}"[:200])
    return removed
