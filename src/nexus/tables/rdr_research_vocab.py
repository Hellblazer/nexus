# SPDX-License-Identifier: AGPL-3.0-or-later
"""The vocabulary of an RDR research record, read from ``rdr-research-vocab.toml`` (RDR-240 TD7, nexus-qo7ie.33).

``nx rdr``'s gate preamble names these in its assumption audit, and the ``research_add`` procedure reads the same table through
``tables.get``, so the two cannot disagree.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table


@lru_cache(maxsize=1)
def load_research_vocab() -> Table:
    return load_packaged_table("rdr-research-vocab.toml")


#: Research classifications, in table order.
CLASSIFICATIONS: Final[tuple[str, ...]] = load_research_vocab().dimensions["classification"].domain
#: Verification methods, in table order.
METHODS: Final[tuple[str, ...]] = load_research_vocab().dimensions["method"].domain
