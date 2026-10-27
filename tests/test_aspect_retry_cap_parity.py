# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The engine's reclaim cap equals the worker's retry cap (nexus-j67ix.2).

``AspectRepository.reclaimStale`` counts a reclaim as an attempt and terminal-fails
a row seen at ``retry_count >= RETRY_MAX_ATTEMPTS`` (RDR-235 Gap 2). The worker's
own ladder is ``_RETRY_MAX_ATTEMPTS`` in ``nexus.aspect_worker``. The Java side
cannot import the Python constant, so this pins the two numbers equal: a drift
would make a reclaim give up earlier or later than a worker-observed failure.

Non-vacuity: the Java constant must be found by the regex, or the test fails.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from nexus.aspect_worker import _RETRY_MAX_ATTEMPTS

pytestmark = pytest.mark.lint

_JAVA = (
    Path(__file__).resolve().parent.parent
    / "service/src/main/java/dev/nexus/service/db/AspectRepository.java"
)


def test_reclaim_cap_equals_worker_retry_cap() -> None:
    src = _JAVA.read_text(encoding="utf-8")
    m = re.search(r"static\s+final\s+int\s+RETRY_MAX_ATTEMPTS\s*=\s*(\d+)\s*;", src)
    assert m is not None, "AspectRepository.RETRY_MAX_ATTEMPTS not found: the pin is vacuous"
    assert int(m.group(1)) == _RETRY_MAX_ATTEMPTS, (
        f"engine reclaim cap {m.group(1)} != worker _RETRY_MAX_ATTEMPTS {_RETRY_MAX_ATTEMPTS}"
    )
