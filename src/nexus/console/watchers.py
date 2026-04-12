# SPDX-License-Identifier: AGPL-3.0-or-later
"""JSONL tail watchers, session scanners, and SSE broadcasters for the nx console."""
from __future__ import annotations

import asyncio
import json
import os
import socket
from dataclasses import dataclass
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


# ── Session scanner ───────────────────────────────────────────────────────────


@dataclass
class SessionInfo:
    """Info about one T1 session."""

    session_id: str
    host: str
    port: int
    pid: int
    pid_alive: bool
    tcp_reachable: bool
    created_at: str


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _tcp_probe(host: str, port: int, timeout: float = 0.5) -> bool:
    if port == 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError):
        return False


def scan_sessions_sync(sessions_dir: Path) -> list[SessionInfo]:
    """Scan *.session files and probe each for liveness (synchronous)."""
    if not sessions_dir.exists():
        return []

    results: list[SessionInfo] = []
    for sf in sessions_dir.glob("*.session"):
        try:
            record = json.loads(sf.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        pid = record.get("server_pid", 0)
        host = record.get("server_host", "127.0.0.1")
        port = record.get("server_port", 0)

        pid_alive = _is_pid_alive(pid) if pid else False
        tcp_ok = _tcp_probe(host, port) if pid_alive else False

        results.append(SessionInfo(
            session_id=record.get("session_id", sf.stem),
            host=host,
            port=port,
            pid=pid,
            pid_alive=pid_alive,
            tcp_reachable=tcp_ok,
            created_at=str(record.get("created_at", "")),
        ))

    return results


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
