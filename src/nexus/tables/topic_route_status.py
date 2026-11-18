# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``topic_route_status`` vocabulary, read from ``topic-route-status.toml`` (RDR-134 Step 5a-i, nexus-7fku2.1.5).

The engine reports one of these on each ``per_collection`` entry of a routed search-per-collection response, and
:mod:`nexus.topic_route` counts them in its ``search_topic_route`` event.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Final

from nexus.tables.load import Table, load_packaged_table

TABLE_RESOURCE: Final = "topic-route-status.toml"


@lru_cache(maxsize=1)
def load_topic_route_status() -> Table:
    return load_packaged_table(TABLE_RESOURCE)


#: The statuses, in table order.
STATUSES: Final[tuple[str, ...]] = load_topic_route_status().dimensions["status"].domain
