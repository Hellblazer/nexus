# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a long-lived nexus process costs: its own memory, CPU and threads (nexus-3ceda.3).

Orphaned daemons and slow memory growth keep coming back as problems, and
until now nothing in nexus could say how big one of its own processes was.
:func:`sample` reads the calling process's resident memory, peak resident
memory, CPU seconds and thread count from the standard library only
(``/proc`` on Linux, ``proc_pid_rusage`` through ``ctypes`` on macOS,
``resource`` for the peak), so it adds no dependency and spawns nothing.

A :class:`FootprintGauge` throttles the sample (a supervisor ticks every
second; the footprint does not need to) and logs once when resident memory
crosses its threshold, once more when it falls back under. Supervised daemons
put the latest sample in their lease payload under ``footprint``
(:class:`nexus.daemon.service_registry.ServiceSupervisor`), and
:func:`lease_footprints` reads them back for ``nx doctor``.

On Windows only the CPU seconds and thread count are reported; memory reads
``None`` there.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: Resident memory, in MiB, above which a gauge warns. ``NX_FOOTPRINT_WARN_MB`` overrides it.
DEFAULT_WARN_MB: int = 1024
#: Seconds between samples a gauge actually takes; reads in between return the last one.
DEFAULT_INTERVAL_S: float = 30.0
WARN_ENV: str = "NX_FOOTPRINT_WARN_MB"

_MIB = 1024 * 1024


def warn_mb(env: Mapping[str, str] | None = None) -> int:
    """The warn threshold: ``NX_FOOTPRINT_WARN_MB`` when it is a positive integer, else the default."""
    raw = ((os.environ if env is None else env).get(WARN_ENV) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_WARN_MB


def _linux() -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    with open("/proc/self/status", encoding="ascii", errors="replace") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if key in ("VmRSS", "VmHWM"):
                out["rss_mb" if key == "VmRSS" else "peak_rss_mb"] = int(rest.split()[0]) / 1024
            elif key == "Threads":
                out["threads"] = int(rest.split()[0])
    return out


def _macos() -> dict[str, float | None]:
    import ctypes  # noqa: PLC0415 -- only a sample on macOS pays it

    lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    # rusage_info_v0: a 16-byte uuid, then u64 user_time, system_time,
    # pkg_idle_wkups, interrupt_wkups, pageins, wired_size, resident_size,
    # phys_footprint, ... The buffer is larger than v0 on purpose.
    buf = (ctypes.c_uint64 * 32)()
    if lib.proc_pid_rusage(os.getpid(), 0, ctypes.byref(buf)) != 0:
        return {}
    # buf[0:2] hold the uuid; resident_size is the eighth u64 after it.
    return {"rss_mb": buf[2 + 6] / _MIB, "idle_wakeups": float(buf[2 + 2])}


def _peak_from_resource() -> float | None:
    try:
        import resource  # noqa: PLC0415 -- absent on Windows
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and KiB on Linux.
    return peak / _MIB if sys.platform == "darwin" else peak / 1024


def sample(*, now: Callable[[], float] = time.time) -> dict[str, Any]:
    """The calling process's footprint now. Never raises: a field it could not
    read is ``None``. ``rss_mb`` and ``peak_rss_mb`` are MiB; ``cpu_s`` is user
    plus system seconds since start."""
    found: dict[str, float | None] = {}
    try:
        if sys.platform.startswith("linux"):
            found = _linux()
        elif sys.platform == "darwin":
            found = _macos()
    except Exception:  # noqa: BLE001 -- a gauge must not break the process it measures
        found = {}
    if found.get("peak_rss_mb") is None:
        try:
            found["peak_rss_mb"] = _peak_from_resource()
        except Exception:  # noqa: BLE001
            found["peak_rss_mb"] = None
    t = os.times()
    threads = found.get("threads")
    out: dict[str, Any] = {
        "pid": os.getpid(),
        "rss_mb": _round(found.get("rss_mb")),
        "peak_rss_mb": _round(found.get("peak_rss_mb")),
        "cpu_s": round(t.user + t.system, 2),
        "threads": int(threads) if threads is not None else threading.active_count(),
        "sampled_at": round(now(), 3),
    }
    wakeups = found.get("idle_wakeups")
    if wakeups is not None:
        out["idle_wakeups"] = int(wakeups)
    return out


def _round(v: float | None) -> float | None:
    return None if v is None else round(v, 1)


class FootprintGauge:
    """A throttled :func:`sample` that warns when resident memory crosses its threshold.

    *sampler*, *clock* (monotonic seconds) and *warn_at_mb* are injectable for tests.
    """

    def __init__(
        self,
        name: str,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        warn_at_mb: int | None = None,
        sampler: Callable[[], dict[str, Any]] = sample,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._interval_s = interval_s
        self._warn_at_mb = warn_mb() if warn_at_mb is None else warn_at_mb
        self._sampler = sampler
        self._clock = clock
        self._last: dict[str, Any] | None = None
        self._taken_at: float | None = None
        self._over = False

    def read(self) -> dict[str, Any]:
        """The latest sample, taking a new one when the last is older than the interval."""
        now = self._clock()
        if self._last is None or self._taken_at is None or now - self._taken_at >= self._interval_s:
            self._last = dict(self._sampler())
            self._taken_at = now
            self._check(self._last)
        return dict(self._last)

    def _check(self, s: Mapping[str, Any]) -> None:
        rss = s.get("rss_mb")
        if rss is None:
            return
        over = rss > self._warn_at_mb
        if over and not self._over:
            _log.warning("process_footprint_over_threshold", process=self._name, rss_mb=rss,
                         warn_at_mb=self._warn_at_mb, peak_rss_mb=s.get("peak_rss_mb"))
        elif self._over and not over:
            _log.info("process_footprint_back_under_threshold", process=self._name, rss_mb=rss,
                      warn_at_mb=self._warn_at_mb)
        self._over = over


#: The tiers whose daemons heartbeat through ServiceSupervisor, so their leases carry
#: a footprint. MinerU publishes a lease but not through the supervisor, so it is not here.
SUPERVISED_TIERS: tuple[str, ...] = ("storage_service", "aspect_worker")


def lease_footprints(
    config_dir: Path, *, tiers: Iterable[str] = SUPERVISED_TIERS, now: float | None = None,
) -> list[dict[str, Any]]:
    """The footprint each fresh supervised lease under *config_dir* reports, as
    ``{"tier", "scope", "footprint"}`` rows ordered by tier and scope. A lease
    with no footprint (an older daemon) is listed with ``footprint`` None."""
    from nexus.daemon.service_registry import ServiceRegistry, ttl_for_tier  # noqa: PLC0415 -- avoids an import cycle

    stamp = time.time() if now is None else now
    rows: list[dict[str, Any]] = []
    for tier in tiers:
        try:
            records = ServiceRegistry(dir=config_dir, tier=tier, ttl=ttl_for_tier(tier)).records()
        except Exception:  # noqa: BLE001 -- a doctor row must not crash on one unreadable tier
            continue
        for record in records:
            if not record.is_fresh(stamp):
                continue
            fp = record.payload.get("footprint") if isinstance(record.payload, dict) else None
            rows.append({"tier": tier, "scope": record.scope_key,
                         "footprint": fp if isinstance(fp, dict) else None})
    return rows


#: A sample older than this is shown with its age: the daemon may be wedged.
STALE_SAMPLE_S: float = 300.0


def _describe(fp: Mapping[str, Any] | None, now: float) -> str:
    if fp is None:
        return "no footprint reported (a daemon started before this release)"
    rss, peak = fp.get("rss_mb"), fp.get("peak_rss_mb")
    head = f"{rss:.0f} MB" if isinstance(rss, (int, float)) else "memory unknown"
    parts = []
    if isinstance(peak, (int, float)):
        parts.append(f"peak {peak:.0f} MB")
    if isinstance(fp.get("cpu_s"), (int, float)):
        parts.append(f"{fp['cpu_s']:.0f} CPU s")
    if isinstance(fp.get("threads"), int):
        parts.append(f"{fp['threads']} threads")
    at = fp.get("sampled_at")
    if isinstance(at, (int, float)) and now - at > STALE_SAMPLE_S:
        parts.append(f"sampled {now - at:.0f} s ago")
    return head + (f" ({', '.join(parts)})" if parts else "")


def doctor_rows(
    config_dir: Path | None = None, *, env: Mapping[str, str] | None = None, now: float | None = None,
) -> list[Any]:
    """The ``nx doctor`` "Process footprint" row: each running supervised daemon's
    own memory, warning when one is past the threshold. With no supervised daemon
    running there is nothing to measure, and the row says so and passes."""
    from nexus.health import HealthResult  # noqa: PLC0415 -- avoids an import cycle

    if config_dir is None:
        from nexus.config import nexus_config_dir  # noqa: PLC0415

        config_dir = nexus_config_dir()
    stamp = time.time() if now is None else now
    rows = lease_footprints(config_dir, now=stamp)
    if not rows:
        return [HealthResult(label="Process footprint", ok=True, detail="no supervised daemon running")]
    limit = warn_mb(env)
    over = [r for r in rows if isinstance((r["footprint"] or {}).get("rss_mb"), (int, float))
            and r["footprint"]["rss_mb"] > limit]
    detail = "; ".join(f"{r['tier']}[{r['scope']}] {_describe(r['footprint'], stamp)}" for r in rows)
    if over:
        names = ", ".join(f"{r['tier']}[{r['scope']}]" for r in over)
        return [HealthResult(
            label="Process footprint", ok=False, warn=True, detail=detail,
            fix_suggestions=[f"{names} is past {limit} MB ({WARN_ENV}); its log may show what grew, "
                             "and restarting the daemon frees the memory"],
        )]
    return [HealthResult(label="Process footprint", ok=True, detail=detail)]


__all__ = ["DEFAULT_INTERVAL_S", "DEFAULT_WARN_MB", "SUPERVISED_TIERS", "WARN_ENV", "FootprintGauge",
           "doctor_rows", "lease_footprints", "sample", "warn_mb"]
