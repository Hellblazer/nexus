# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A gateway 503 holds every client process that shares a config dir (nexus-v1929).

The gateway retry loops (``http_vector_client._request`` for T3 and
``RefreshableHttpStoreMixin._once_with_gateway_retry`` for T2/catalog) each
back off on their own process's clock. On 2026-10-31 nineteen concurrent
``nx upgrade --auto`` children each retried the managed edge's 503s, about
2,000 gateway retries in all, and kept its admission pool at zero permits:
the retry load grew with the process count.

This module puts the backoff on disk. A 503 writes a not-before time to
``<config dir>/gateway-hold.json``; every client honours it before its first
attempt and stretches its retry sleeps to it. The strike count is shared too,
so the hold grows with the number of 503s the box has seen in the last
:data:`STRIKE_WINDOW_S`, whichever process saw them. A lone process sees
exactly its old 2/5/10 s schedule. Waiters past another process's hold get up
to :data:`MAX_JITTER_S` of jitter so they do not all resend at once.

Everything here is best effort. An unreadable, garbled or unwritable file
means no shared hold, never a failed request. The callers do the sleeping
through their own ``time.sleep``, so this module never blocks.
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import structlog

_log = structlog.get_logger(__name__)

#: The hold file's name under the nexus config dir.
HOLD_FILENAME: str = "gateway-hold.json"

#: No hold, however it was written, keeps a client waiting longer than this.
HOLD_CAP_S: float = 30.0

#: The most jitter added to a wait that another process's hold imposed.
MAX_JITTER_S: float = 2.0

#: A 503 more than this long after the previous one starts the strike count over.
STRIKE_WINDOW_S: float = 60.0

#: The shared hold after the Nth 503 in the strike window. The first three
#: match ``gateway_backoff._GATEWAY_RETRY_SLEEPS``, so a single process keeps
#: its schedule; later strikes come from other processes and lengthen it.
ESCALATION_S: tuple[float, ...] = (2.0, 5.0, 10.0, 20.0, 30.0)


def _hold_path() -> Path:
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred: keeps this leaf module free of config's import graph

    return nexus_config_dir() / HOLD_FILENAME


def _read(path: Path) -> dict[str, float]:
    """The hold file's numeric fields, or ``{}`` when it is absent or unusable."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key in ("not_before", "strikes", "last_503"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value:
            out[key] = float(value)
    return out


def _safe_url(url: str) -> str:
    """Scheme, host and port only: the file must never carry credentials."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}"
    except ValueError:
        return ""


def _write(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gateway-hold.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _jitter(remaining: float, rng: random.Random | None) -> float:
    return (rng or random).uniform(0.0, min(MAX_JITTER_S, 0.5 * remaining))


def wait_before_attempt(*, now: float | None = None, rng: random.Random | None = None) -> float:
    """Seconds this process should wait before sending, per the shared hold.

    ``0.0`` when there is no hold or it has passed. A hold in force returns
    its remaining time, capped at :data:`HOLD_CAP_S`, plus jitter.
    """
    now = time.time() if now is None else now
    try:
        state = _read(_hold_path())
    except Exception:  # noqa: BLE001 — best effort: a config-dir resolution failure means no shared hold
        return 0.0
    remaining = min(state.get("not_before", 0.0) - now, HOLD_CAP_S)
    if remaining <= 0.0:
        return 0.0
    return remaining + _jitter(remaining, rng)


def record_503(
    delay: float,
    *,
    retry_after: float | None,
    url: str = "",
    now: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Record a 503 in the shared hold and return how long to sleep before resending.

    *delay* is the caller's own scheduled sleep for this retry; *retry_after*
    is the response's parsed ``Retry-After`` (seconds), if any. The result is
    the longest of those, the hold for the shared strike count, and any hold
    another process already set, capped at :data:`HOLD_CAP_S`. Jitter is added
    only when another process's hold is what made it longer.

    A *delay* of zero with no *retry_after* returns ``0.0`` and writes
    nothing: that is how the test suites switch the gateway schedule off.
    """
    if delay <= 0.0 and not retry_after:
        return 0.0
    now = time.time() if now is None else now
    own = min(max(delay, retry_after or 0.0), HOLD_CAP_S)
    try:
        path = _hold_path()
        lock_path = path.with_name(HOLD_FILENAME + ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        from nexus._locking import lock_fd, unlock_fd  # noqa: PLC0415 — deferred: only a 503 pays for it

        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            lock_fd(fd, blocking=True)
            try:
                state = _read(path)
                recent = now - state.get("last_503", float("-inf")) <= STRIKE_WINDOW_S
                strikes = int(state.get("strikes", 0.0)) + 1 if recent else 1
                escalated = ESCALATION_S[min(strikes, len(ESCALATION_S)) - 1]
                prior = state.get("not_before", 0.0) - now
                hold = min(max(own, escalated, prior), HOLD_CAP_S)
                _write(path, {
                    "not_before": max(state.get("not_before", 0.0), now + hold),
                    "strikes": strikes,
                    "last_503": now,
                    "url": _safe_url(url),
                    "pid": os.getpid(),
                })
            finally:
                unlock_fd(fd)
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 — best effort: an unwritable config dir falls back to this process's own schedule
        _log.debug("gateway_hold_record_failed", error=type(exc).__name__)
        return own
    if hold <= own:
        return own
    _log.info("gateway_hold_shared", strikes=strikes, hold_s=hold, own_s=own)
    return hold + _jitter(hold - own, rng)
