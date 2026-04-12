# SPDX-License-Identifier: AGPL-3.0-or-later
"""JSONL tail watchers and SSE broadcasters for the nx console."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import structlog

_log = structlog.get_logger(__name__)


class JSONLTailWatcher:
    """Watch a JSONL file for new lines appended since the last poll.

    Handles truncation (e.g. from compact()) by resetting the offset when
    the file shrinks.
    """

    def __init__(self, path: Path, callback: Callable[[dict[str, Any]], None]) -> None:
        self._path = path
        self._callback = callback
        self._offset: int = 0
        self._last_mtime: float = 0.0

    async def poll(self, cadence: float = 1.0) -> None:
        """Poll the file at the given cadence. Runs until cancelled."""
        # Start from end of file if it exists
        if self._path.exists():
            self._offset = self._path.stat().st_size
            self._last_mtime = self._path.stat().st_mtime

        while True:
            try:
                await asyncio.sleep(cadence)
                self._check_for_new_lines()
            except asyncio.CancelledError:
                raise

    def _check_for_new_lines(self) -> None:
        if not self._path.exists():
            return

        stat = self._path.stat()
        if stat.st_mtime == self._last_mtime:
            return

        self._last_mtime = stat.st_mtime

        # Handle truncation (compact)
        if stat.st_size < self._offset:
            self._offset = 0

        if stat.st_size == self._offset:
            return

        with open(self._path, "r") as f:
            f.seek(self._offset)
            data = f.read()
            self._offset = f.tell()

        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                self._callback(record)
            except json.JSONDecodeError:
                _log.debug("jsonl_parse_error", path=str(self._path), line=line[:80])


class SSEBroadcaster:
    """Broadcast events to multiple SSE subscribers."""

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []

    def publish(self, event: dict[str, Any]) -> None:
        """Push an event to all connected subscribers."""
        for q in self._queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # Drop events for slow consumers

    async def subscribe(self) -> AsyncIterator[dict[str, Any]]:
        """Yield events as they arrive. Use in an SSE generator."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._queues.append(q)
        try:
            while True:
                event = await q.get()
                yield event
        except asyncio.CancelledError:
            raise
        finally:
            self._queues.remove(q)
