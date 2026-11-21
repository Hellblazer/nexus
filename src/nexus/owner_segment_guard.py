# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The refusal of a corpus name that would build a repo's collection name (nexus-p2m1x.28).

Its own module, not ``corpus.py``: that file is at its size ceiling, and a module-level compiled regex there is read
by the collection-name parse census as a name parser, which this is not (it tests a corpus tag, never a collection).
"""
from __future__ import annotations

import re

import click

_OWNER_SEGMENT_SHAPE = re.compile(r"[0-9]+-[0-9]+")
"""The collection-name owner segment of a repo owner tumbler: ``catalog.collection_name.owner_segment_for_tumbler``
joins the tumbler's first two dot-segments with a hyphen (``1.20`` -> ``1-20``), so a segment is two digit runs
and one hyphen."""


class TumblerShapedCorpusError(click.UsageError):
    """A corpus name that is the owner segment of a repo owner."""


def require_corpus_name(corpus: str) -> str:
    """Return *corpus* unchanged, or raise :class:`TumblerShapedCorpusError` when it would name a repo's collection.

    ``docs__{corpus}`` becomes ``docs__<corpus with _ as ->__<model>__v1``, and a repo whose owner tumbler is
    ``1.20`` has the same name for its docs collection (owner segment ``1-20``). A corpus named ``1-20`` (or
    ``1_20``, which folds to it) builds that name, so the corpus and the repo write into one collection by name.
    This is the one check behind every corpus-to-collection path. ``nexus.corpus.docs_leaf_fallback_collection_name``
    is the one builder of a docs name from a corpus and calls it (the three ``doc_indexer`` fallbacks, the dry-run
    preview, and ``nx dt``'s default markdown name through ``_resolve_dt_collection``); the CLI front doors call it
    first, before any work, when no ``--collection`` is given: ``nx index md|pdf``, ``nx dt index`` (before any
    DEVONthink record is read, so a mixed batch refuses up front) and ``nx dt capture`` for a non-PDF capture.
    """
    segment = corpus.replace("_", "-")
    if _OWNER_SEGMENT_SHAPE.fullmatch(segment):
        raise TumblerShapedCorpusError(
            f"corpus name {corpus!r} has the shape of a repo owner segment (<digits>-<digits>, such as 1-20). "
            f"The docs collection built from it, docs__{segment}__<model>__v1, is also the docs collection of the "
            f"repo whose owner tumbler is {segment.replace('-', '.', 1)}, so the corpus and that repo would merge "
            "by name. Choose a corpus name with a letter in it."
        )
    return corpus
