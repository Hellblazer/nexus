# SPDX-License-Identifier: AGPL-3.0-or-later
"""Put an ``os.environ`` key back after code that writes it directly (nexus-4h20a).

``monkeypatch.delenv(key, raising=False)`` records no undo when the key starts
absent, so a raw ``os.environ[key] = ...`` in the code under test outlives the
test. This context manager snapshots the key and restores it on exit, whatever
the body did.
"""
from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager


@contextmanager
def restore_env_after(key: str) -> Generator[None]:
    """Snapshot ``os.environ[key]`` (or its absence) and restore it on exit."""
    before = os.environ.get(key)
    try:
        yield
    finally:
        if os.environ.get(key) != before:
            if before is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = before


def restore_changed_keys(before: dict[str, str | None]) -> dict[str, str | None]:
    """Put back every key in ``before`` whose value changed; return the changed
    keys mapped to the value that was found (``None`` for a deleted key)."""
    leaked = {k: os.environ.get(k) for k, v in before.items() if os.environ.get(k) != v}
    for k in leaked:
        if before[k] is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = before[k]
    return leaked
