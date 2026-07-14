# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-24p05: the SQLite twin of the retention marker.

expire_relevance_log publishes a monotonic cumulative-deletes counter —
the verify-fill watermark's rollback detector — mirroring the engine's
expireRelevanceLog bump. Parity tripwire covers the signature; this covers
the behavior.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nexus.db.t2 import T2Database


def _db(tmp_path):
    return T2Database(tmp_path / "t2.db", run_migrations=True)


def test_expire_bumps_cumulative_marker(tmp_path):
    db = _db(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    for i in range(3):
        db.telemetry.conn.execute(
            "INSERT INTO relevance_log (query, chunk_id, action, timestamp) "
            "VALUES (?, ?, 'click', ?)", (f"q{i}", f"c{i}", old),
        )
    db.telemetry.conn.commit()
    db.telemetry.log_relevance("fresh", "cf", "click")

    assert db.telemetry.expire_relevance_log(days=90) == 3
    assert db.telemetry.get_retention_markers(["nexus.relevance_log"]) == {
        "nexus.relevance_log": 3
    }
    # Nothing left to sweep: marker unchanged (no bump-on-zero).
    assert db.telemetry.expire_relevance_log(days=90) == 0
    assert db.telemetry.get_retention_markers(["nexus.relevance_log"]) == {
        "nexus.relevance_log": 3
    }


def test_never_swept_relation_is_absent(tmp_path):
    db = _db(tmp_path)
    assert db.telemetry.get_retention_markers(
        ["nexus.relevance_log", "nexus.search_telemetry"]
    ) == {}
    assert db.telemetry.get_retention_markers([]) == {}


def test_relevance_timestamp_format_invariant(tmp_path):
    """Review 68509ac8 Low: the fresh-window fill compares timestamps
    LEXICOGRAPHICALLY against a datetime.now(UTC).isoformat() cutoff — sound
    only while every relevance_log write uses the same +00:00-suffixed
    isoformat shape. Pin the write path's format so a future Z-suffixed or
    naive writer fails here instead of silently mis-bucketing rows."""
    import re

    db = _db(tmp_path)
    db.telemetry.log_relevance("fmt probe", "cfmt", "click")
    ts = db.telemetry.conn.execute(
        "SELECT timestamp FROM relevance_log WHERE chunk_id='cfmt'"
    ).fetchone()[0]
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+\+00:00", ts
    ), f"relevance_log timestamp format drifted: {ts!r}"
