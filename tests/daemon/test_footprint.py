# SPDX-License-Identifier: AGPL-3.0-or-later
"""A long-lived nexus process reports its own memory, CPU and threads; supervised
daemons carry it in their lease and ``nx doctor`` reads it back (nexus-3ceda.3)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus.daemon import footprint
from nexus.daemon.footprint import FootprintGauge, doctor_rows, lease_footprints, sample, warn_mb
from nexus.daemon.service_registry import ServiceRegistry, ServiceSupervisor

_MEASURES_RSS = sys.platform.startswith("linux") or sys.platform == "darwin"


def test_a_sample_describes_this_process() -> None:
    s = sample(now=lambda: 123.0)
    assert s["pid"] == os.getpid() and s["sampled_at"] == 123.0
    assert s["cpu_s"] >= 0 and s["threads"] >= 1
    if _MEASURES_RSS:
        assert s["rss_mb"] > 1 and s["peak_rss_mb"] >= 1


@pytest.mark.skipif(not _MEASURES_RSS, reason="resident memory is read on Linux and macOS only")
def test_resident_memory_follows_an_allocation() -> None:
    before = sample()["rss_mb"]
    block = bytearray(96 * 1024 * 1024)
    block[:: 4096] = b"\x01" * len(block[:: 4096])  # touch every page so it is resident
    after = sample()["rss_mb"]
    assert after - before > 64, (before, after)
    del block


def test_the_threshold_reads_a_positive_integer_or_falls_back() -> None:
    assert warn_mb({}) == footprint.DEFAULT_WARN_MB
    assert warn_mb({footprint.WARN_ENV: "256"}) == 256
    for bad in ("0", "-5", "lots", " "):
        assert warn_mb({footprint.WARN_ENV: bad}) == footprint.DEFAULT_WARN_MB


class _Script:
    """A sampler that returns the next rss from a list, and counts calls."""

    def __init__(self, *rss: float) -> None:
        self.rss = list(rss)
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        return {"rss_mb": self.rss.pop(0), "peak_rss_mb": 1.0}


def test_the_gauge_samples_at_most_once_per_interval() -> None:
    t = [0.0]
    script = _Script(10.0, 20.0)
    gauge = FootprintGauge("p", interval_s=30.0, warn_at_mb=100, sampler=script, clock=lambda: t[0])
    assert gauge.read()["rss_mb"] == 10.0
    t[0] = 29.9
    assert gauge.read()["rss_mb"] == 10.0 and script.calls == 1
    t[0] = 30.0
    assert gauge.read()["rss_mb"] == 20.0 and script.calls == 2


def test_the_gauge_warns_once_on_the_way_up_and_notes_the_way_down(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, str]] = []

    class _Log:
        def warning(self, event: str, **_: Any) -> None:
            events.append(("warning", event))

        def info(self, event: str, **_: Any) -> None:
            events.append(("info", event))

    monkeypatch.setattr(footprint, "_log", _Log())
    t = [0.0]
    gauge = FootprintGauge("p", interval_s=1.0, warn_at_mb=100, sampler=_Script(50, 150, 160, 90, 95),
                           clock=lambda: t[0])
    for i in range(5):
        t[0] = float(i)
        gauge.read()
    assert events == [("warning", "process_footprint_over_threshold"),
                      ("info", "process_footprint_back_under_threshold")]


def _registry(tmp_path: Path, tier: str = "aspect_worker") -> ServiceRegistry:
    return ServiceRegistry(dir=tmp_path, tier=tier, ttl=30.0, heartbeat_interval=1.0)


def _fixed_gauge(rss: float) -> FootprintGauge:
    return FootprintGauge("p", sampler=lambda: {"rss_mb": rss, "peak_rss_mb": rss, "cpu_s": 1.0,
                                                "threads": 3, "sampled_at": 0.0})


def test_a_heartbeat_carries_the_footprint_in_the_lease(tmp_path: Path) -> None:
    sup = ServiceSupervisor(_registry(tmp_path), "t-1", version="1", endpoint_provider=lambda: {"port": 1},
                            payload={"kept": True}, footprint=_fixed_gauge(42.0))
    sup.publish_once()
    sup.heartbeat_tick()
    rec = _registry(tmp_path).discover("t-1")
    assert rec is not None
    assert rec.payload["kept"] is True
    assert rec.payload["footprint"]["rss_mb"] == 42.0


def test_a_failing_gauge_never_costs_the_heartbeat(tmp_path: Path) -> None:
    def broken() -> dict[str, Any]:
        raise RuntimeError("gauge broke")

    sup = ServiceSupervisor(_registry(tmp_path), "t-1", version="1", endpoint_provider=lambda: {"port": 1},
                            footprint=FootprintGauge("p", sampler=broken))
    first = sup.publish_once()
    sup.heartbeat_tick()
    assert sup.record is not None and sup.record.heartbeat_epoch >= first.heartbeat_epoch
    assert "footprint" not in sup.record.payload
    assert not sup.fenced


def test_doctor_passes_with_nothing_running(tmp_path: Path) -> None:
    (row,) = doctor_rows(tmp_path)
    assert row.ok and row.detail == "no supervised daemon running"


def test_doctor_lists_each_daemon_and_warns_past_the_threshold(tmp_path: Path) -> None:
    for scope, rss in (("a", 120.0), ("b", 2048.0)):
        sup = ServiceSupervisor(_registry(tmp_path), scope, version="1", endpoint_provider=lambda: {"port": 1},
                                footprint=_fixed_gauge(rss))
        sup.publish_once()
        sup.heartbeat_tick()
    old = ServiceSupervisor(_registry(tmp_path, "storage_service"), "u", version="1",
                            endpoint_provider=lambda: {"port": 1})
    old.publish_once()  # published, never heartbeated: no footprint yet

    rows = lease_footprints(tmp_path)
    assert [(r["tier"], r["scope"]) for r in rows] == [("storage_service", "u"), ("aspect_worker", "a"),
                                                       ("aspect_worker", "b")]
    (row,) = doctor_rows(tmp_path, env={footprint.WARN_ENV: "1024"})
    assert not row.ok and row.warn
    assert "aspect_worker[a] 120 MB (peak 120 MB, 1 CPU s, 3 threads" in row.detail
    assert "storage_service[u] no footprint" in row.detail
    assert row.fix_suggestions and "aspect_worker[b]" in row.fix_suggestions[0]
    (calm,) = doctor_rows(tmp_path, env={footprint.WARN_ENV: "4096"})
    assert calm.ok


def test_doctor_shows_the_age_of_an_old_sample(tmp_path: Path) -> None:
    sup = ServiceSupervisor(_registry(tmp_path), "a", version="1", endpoint_provider=lambda: {"port": 1},
                            footprint=_fixed_gauge(10.0))
    sup.publish_once()
    sup.heartbeat_tick()
    rec = _registry(tmp_path).discover("a")
    assert rec is not None
    (row,) = doctor_rows(tmp_path, now=rec.heartbeat_epoch + 1.0)
    assert "sampled" in row.detail  # sampled_at 0.0 is far older than the stale bound


def test_a_stale_lease_is_not_listed(tmp_path: Path) -> None:
    sup = ServiceSupervisor(_registry(tmp_path), "gone", version="1", endpoint_provider=lambda: {"port": 1},
                            footprint=_fixed_gauge(10.0))
    rec = sup.publish_once()
    sup.heartbeat_tick()
    assert [r["scope"] for r in lease_footprints(tmp_path, now=rec.heartbeat_epoch + 1.0)] == ["gone"]
    assert lease_footprints(tmp_path, now=rec.heartbeat_epoch + 10_000.0) == []
