# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn a streaming-pipeline stage that polls forever into a failure (RDR-223).

A run whose uploader waits for a count that can never be reached has no exit, and the
orchestrator's pool threads are non-daemon, so a hung stage would hold the whole pytest process
open. Patch the stages' poll sleep with :func:`bound_the_polling` in any test that could end in
that state (a hard-kill rerun, a stage that was never told to stop).
"""
from __future__ import annotations

import pytest


def bound_the_polling(monkeypatch: pytest.MonkeyPatch, limit: int = 400) -> None:
    """The poll sleep itself raises once it has been called *limit* times."""
    import nexus.pipeline_stages as ps

    calls = {"n": 0}
    real = ps.time

    class _BoundedTime:
        def sleep(self, seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > limit:
                raise AssertionError(f"a stage polled {limit} times without finishing: the run never ends")
            real.sleep(seconds)

        def __getattr__(self, name: str):
            return getattr(real, name)

    monkeypatch.setattr(ps, "time", _BoundedTime())
