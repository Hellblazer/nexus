# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``topic_route`` wire keys are spelled alike by the engine (Java) and the client (Python).

RDR-134 pre-release test validation G1 (nexus-7fku2.1.17). The status vocabulary and the capability flag already have
a Java-versus-Python test (``test_topic_route_status.py``). The keys around them did not: a request key renamed on one
side, or an envelope key added on one side only, would pass every unit test of that side and fail only against a live
engine. Each test scans the literal spellings out of the source of BOTH sides, fails if a scan finds nothing (a pattern
that drifted from the code would otherwise compare two empty sets and pass), and holds both equal to the closed set
named here. Adding a key means adding it in all three places.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ENGINE_HANDLER = REPO / "service/src/main/java/dev/nexus/service/http/VectorHandler.java"
ENGINE_ROUTE = REPO / "service/src/main/java/dev/nexus/service/vectors/TopicRoute.java"
CLIENT_ROUTE = REPO / "src/nexus/topic_route.py"
CLIENT_HTTP = REPO / "src/nexus/db/http_vector_client.py"

#: What the engine writes on a ``per_collection`` entry and the client reads from it.
ENVELOPE_KEYS = frozenset({
    "topic_route_status", "topic_route_error", "topic_route_coverage", "topic_route_topics",
    "topic_route_min_coverage",
})
#: What the client writes inside ``topic_route`` and the engine reads from it.
REQUEST_KEYS = frozenset({"mode", "width", "collections", "min_coverage"})


def _text(path: Path) -> str:
    assert path.is_file(), f"{path} not found: the scan has nothing to examine"
    return path.read_text(encoding="utf-8")


def _found(pattern: str, text: str, what: str) -> set[str]:
    found = set(re.findall(pattern, text))
    assert found, f"non-vacuity: {what} matched nothing; the pattern drifted from the code it scans"
    return found


def test_the_envelope_keys_the_engine_writes_are_the_ones_the_client_reads() -> None:
    engine = _found(r'\bm\.put\("(topic_route_[a-z_]+)"', _text(ENGINE_HANDLER), "VectorHandler per_collection puts")
    client = _found(r'\bentry\.get\("(topic_route_[a-z_]+)"\)', _text(CLIENT_ROUTE), "topic_route.py envelope reads")
    assert engine == client, f"engine only: {sorted(engine - client)}; client only: {sorted(client - engine)}"
    assert engine == ENVELOPE_KEYS, sorted(engine ^ ENVELOPE_KEYS)


def test_the_client_carries_every_envelope_key_it_reads() -> None:
    # reading a key is not carrying it: carry_entry_fields must also write each one onto the validated entry
    carried = _found(r'\bout\["(topic_route_[a-z_]+)"\] =', _text(CLIENT_ROUTE), "carry_entry_fields writes")
    assert carried == ENVELOPE_KEYS, sorted(carried ^ ENVELOPE_KEYS)


def test_the_request_keys_the_client_sends_are_the_ones_the_engine_reads() -> None:
    engine = _found(r'\bm\.get\("([a-z_]+)"\)', _text(ENGINE_ROUTE), "TopicRoute.parse reads")
    validated = _found(r'\btopic_route\.get\("([a-z_]+)"\)', _text(CLIENT_ROUTE), "topic_route.py _parse reads")
    sent = _found(r'\bfield\["([a-z_]+)"\] =', _text(CLIENT_ROUTE), "topic_route.py route_field writes")
    assert engine == validated == sent, (
        f"engine reads {sorted(engine)}; client validates {sorted(validated)}; client sends {sorted(sent)}"
    )
    assert engine == REQUEST_KEYS, sorted(engine ^ REQUEST_KEYS)


def test_the_request_field_itself_is_spelled_alike() -> None:
    engine = _found(r'\bbody\.get\("(topic_route)"\)', _text(ENGINE_HANDLER), "VectorHandler topic_route read")
    client = _found(r'\bbody\["(topic_route)"\] =', _text(CLIENT_HTTP), "http_vector_client topic_route write")
    assert engine == client == {"topic_route"}
