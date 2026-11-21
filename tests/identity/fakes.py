# SPDX-License-Identifier: AGPL-3.0-or-later
"""Hand-built stand-ins for the identity tests: a process table and a fixed-response identity client.

``FakeProcesses`` answers the three questions the holder check asks of the machine (is a pid running, when did it
start, what does its registry file say) from a dict, so a test can stage a crashed holder, a reused pid or a
resumed process without starting one. ``FakeClient`` answers ``resolve_identity``'s lookups from fixed responses;
the policy tests that need the engine's own answers use a real engine instead (tests/identity/conftest.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nexus.identity.bind_client import IdentityApiError

HOST = "testhost"


def ps_style(instant: str) -> str:
    """``2026-11-17T08:00:00Z`` as the registry spells procStart: ``Tue Nov 17 08:00:00 2026`` (UTC)."""
    return datetime.strptime(instant, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).strftime("%a %b %d %H:%M:%S %Y")


class FakeProcesses:
    """A process table: pid -> OS start instant. The registry files are written to *sessions_dir*."""

    def __init__(self, sessions_dir: Path, *, host: str = HOST, platform: str = "darwin") -> None:
        self.sessions_dir = sessions_dir
        self.host = host
        #: every name this machine answers to when it is not just *host* (a binding written under an older hostname)
        self.names: tuple[str, ...] | None = None
        self.platform = platform
        self.procs: dict[int, str] = {}
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def start(self, pid: int, os_start: str, *, started_at: int | None = None, session_id: str = "s-0",
              name: str = "", entrypoint: str = "cli", registry: bool = True, kind: str = "interactive",
              tmux: str | None = None) -> None:
        """A running process. *os_start* is the UTC instant ``2026-11-17T08:00:00Z``."""
        self.procs[pid] = os_start
        if registry:
            self.write_registry(pid, started_at if started_at is not None else pid * 1000, os_start,
                                session_id=session_id, name=name, entrypoint=entrypoint, kind=kind, tmux=tmux)

    def write_registry(self, pid: int, started_at: int, os_start: str, *, session_id: str = "s-0", name: str = "",
                       entrypoint: str = "cli", kind: str = "interactive", tmux: str | None = None) -> None:
        data: dict[str, Any] = {
            "pid": pid, "sessionId": session_id, "startedAt": started_at, "procStart": ps_style(os_start),
            "kind": kind, "entrypoint": entrypoint, "name": name, "updatedAt": started_at,
        }
        if tmux:
            data["tmux"] = tmux
        (self.sessions_dir / f"{pid}.json").write_text(json.dumps(data), encoding="utf-8")

    def set_session(self, pid: int, session_id: str) -> None:
        """Claude Code writes the registry's sessionId before SessionStart fires (``/clear`` spike, 253-research-5)."""
        path = self.sessions_dir / f"{pid}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            data["sessionId"] = session_id
            path.write_text(json.dumps(data), encoding="utf-8")

    def stop(self, pid: int, *, keep_registry: bool = True) -> None:
        """The process exits. A crash leaves its registry file behind, a clean exit removes it."""
        self.procs.pop(pid, None)
        if not keep_registry:
            (self.sessions_dir / f"{pid}.json").unlink(missing_ok=True)

    # the probes the holder check calls
    def alive(self, pid: int) -> bool:
        return pid in self.procs

    def os_start(self, pid: int) -> str | None:
        return self.procs.get(pid)


class FakeClient:
    """An identity client with fixed answers: sessions maps a session id to its index row."""

    def __init__(self, sessions: dict[str, dict[str, Any]] | None = None,
                 bindings: list[dict[str, Any]] | None = None) -> None:
        self.sessions = sessions or {}
        self.bindings = bindings or []
        self.calls: list[tuple[str, Any]] = []

    def session(self, session_id: str) -> dict[str, Any]:
        self.calls.append(("session", session_id))
        if session_id not in self.sessions:
            raise IdentityApiError(404, "unknown_session", f"no identity has held session {session_id}", {})
        return self.sessions[session_id]

    def bindings_by_host(self, host: str, pid: int | None = None) -> list[dict[str, Any]]:
        self.calls.append(("bindings_by_host", (host, pid)))
        return [b for b in self.bindings if b.get("holder", {}).get("host") == host
                and (pid is None or b["holder"].get("pid") == pid)]
