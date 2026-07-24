# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-24p05: the SQLite twin of the retention marker.

expire_relevance_log publishes a monotonic cumulative-deletes counter —
the verify-fill watermark's rollback detector — mirroring the engine's
expireRelevanceLog bump. Parity tripwire covers the signature; this covers
the behavior.
"""
from __future__ import annotations

import os

import pytest

from datetime import UTC, datetime, timedelta

from nexus.db.t2 import T2Database

_ENGINE_SUBSTRATE = os.environ.get("NX_TEST_T2_SUBSTRATE") == "engine"


def _db(tmp_path):
    return T2Database(tmp_path / "t2.db", run_migrations=True)


def test_expire_bumps_cumulative_marker(tmp_path):
    db = _db(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    # Backdated rows: the Http store's fidelity-import surface writes
    # timestamp VERBATIM (on the engine substrate this exercises the REAL
    # expireRelevanceLog marker bump); the SQLite twin has no import
    # surface, so its leg seeds raw — that branch dies with the twin at
    # the RDR-155 P4b flip.
    from nexus.db.storage_mode import has_raw_access

    if has_raw_access(db.telemetry):
        for i in range(3):
            db.telemetry.conn.execute(
                "INSERT INTO relevance_log (query, chunk_id, action, timestamp) "
                "VALUES (?, ?, 'click', ?)", (f"q{i}", f"c{i}", old),
            )
        db.telemetry.conn.commit()
    else:
        for i in range(3):
            db.telemetry.import_relevance_row(
                query=f"q{i}", chunk_id=f"c{i}", collection="",
                action="click", session_id="", timestamp=old,
            )
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


@pytest.mark.skipif(
    _ENGINE_SUBSTRATE,
    reason="SQLite-twin write-path invariant; dies with the twin at the "
    "RDR-155 P4b flip (dies-roster)",
)
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
