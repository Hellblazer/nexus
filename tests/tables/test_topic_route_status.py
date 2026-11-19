# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``topic_route_status`` vocabulary table (RDR-134 Step 5a-i, nexus-7fku2.1.5)."""
from __future__ import annotations

import re
from pathlib import Path

from nexus.db.http_vector_client import TOPIC_ROUTE_CAPABILITY_FLAG
from nexus.tables.check import check_table
from nexus.tables.load import load_packaged_table
from nexus.tables.topic_route_status import STATUSES

REPO = Path(__file__).resolve().parents[2]
JAVA_ENUM = REPO / "service/src/main/java/dev/nexus/service/vectors/TopicRoute.java"
VERSION_HANDLER = REPO / "service/src/main/java/dev/nexus/service/http/VersionHandler.java"


def test_the_table_lints_clean_and_holds_the_four_statuses() -> None:
    assert check_table(load_packaged_table("topic-route-status.toml")) == []
    assert STATUSES == ("routed", "no_topics", "fallback", "skipped")


def test_the_table_equals_the_engines_status_enum() -> None:
    # Two spellings of one closed set cannot drift: the engine's wire names are the table's rows.
    text = JAVA_ENUM.read_text(encoding="utf-8")
    body = re.search(r"enum Status \{\s*(.*?);", text, re.DOTALL)
    assert body, "TopicRoute.Status enum not found (non-vacuity: the scan must examine something)"
    wire = tuple(re.findall(r'[A-Z_]+\("([a-z_]+)"\)', body.group(1)))
    assert len(wire) == 4, wire
    assert wire == STATUSES


def test_the_client_flag_literal_is_the_engines() -> None:
    # The capability flag is spelled once in the engine's VersionHandler and once in the client; they are equal.
    text = VERSION_HANDLER.read_text(encoding="utf-8")
    flags = set(re.findall(r"vector_[a-z_]*topic_route[a-z_]*_supported", text))  # javadoc and the escaped string both
    assert flags == {TOPIC_ROUTE_CAPABILITY_FLAG}, flags
    assert TOPIC_ROUTE_CAPABILITY_FLAG == "vector_topic_route_supported"
