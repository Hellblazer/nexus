# SPDX-License-Identifier: AGPL-3.0-or-later
"""``t2_prefix_scan.reachability_warning``: the fire-only SessionStart line that survives the scan's move behind ``nx brief memory``.

The scan carried the nexus-8fvp2 contract: a T2 that cannot be reached, or whose freshest entry is stale, is never silent. With
the scan no longer injected, this probe (one namespace listing, nothing rendered) keeps that signal at session start. It
returns ``""`` when T2 is healthy, which is the common case, so it costs a start nothing in bytes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from nexus.hooks import t2_prefix_scan


def _stamp(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Store:
    closed = False

    def __init__(self, rows=None, error: BaseException | None = None) -> None:
        self.rows, self.error, self.asked = rows or [], error, []

    def get_projects_with_prefix(self, prefix):
        self.asked.append(prefix)
        if self.error:
            raise self.error
        return self.rows

    def close(self) -> None:
        self.closed = True


def _patch(monkeypatch, store=None, construct_error: BaseException | None = None) -> list:
    calls: list = []
    monkeypatch.setattr(t2_prefix_scan, "configure_hook_logging", lambda: calls.append("configured"))

    def make(**kw):
        if construct_error:
            raise construct_error
        return store

    monkeypatch.setattr(t2_prefix_scan, "HttpMemoryStore", make)
    return calls


def test_healthy_and_fresh_is_silent_and_the_store_is_closed(monkeypatch) -> None:
    store = _Store([{"project": "nexus", "last_updated": _stamp(1)}])
    _patch(monkeypatch, store)
    assert t2_prefix_scan.reachability_warning("nexus") == ""
    assert store.asked == ["nexus"] and store.closed


def test_no_namespaces_is_a_genuinely_empty_t2_and_silent(monkeypatch) -> None:
    _patch(monkeypatch, _Store([]))
    assert t2_prefix_scan.reachability_warning("nexus") == ""


def test_an_unreachable_engine_at_construction_is_a_visible_line(monkeypatch) -> None:
    _patch(monkeypatch, construct_error=RuntimeError("nexus-service endpoint is not resolvable"))
    out = t2_prefix_scan.reachability_warning("nexus")
    assert out.startswith("WARNING: T2 memory unreachable: ") and "not resolvable" in out


def test_an_unreachable_engine_at_the_listing_is_a_visible_line_and_the_store_closes(monkeypatch) -> None:
    store = _Store(error=httpx.ConnectError("refused"))
    _patch(monkeypatch, store)
    assert t2_prefix_scan.reachability_warning("nexus").startswith("WARNING: T2 memory unreachable: ")
    assert store.closed


def test_a_stale_freshest_entry_warns_with_the_scans_own_wording(monkeypatch) -> None:
    _patch(monkeypatch, _Store([{"project": "nexus", "last_updated": _stamp(60)}]))
    out = t2_prefix_scan.reachability_warning("nexus")
    assert out.startswith("WARNING: T2 memory freshest entry is 60d old")


def test_the_line_is_one_capped_line_whatever_the_exception_says(monkeypatch) -> None:
    _patch(monkeypatch, construct_error=RuntimeError("boom\n" + "x" * 5000))
    out = t2_prefix_scan.reachability_warning("nexus")
    assert "\n" not in out and len(out.encode("utf-8")) <= t2_prefix_scan.REACHABILITY_LINE_MAX


def test_an_empty_project_asks_nothing(monkeypatch) -> None:
    store = _Store([])
    _patch(monkeypatch, store)
    assert t2_prefix_scan.reachability_warning("") == "" and store.asked == []


def test_logging_is_configured_before_the_store_is_touched(monkeypatch) -> None:
    """A hook process only: the probe imports the T2 client, whose ambient structlog lines would otherwise reach stdout."""
    order: list[str] = []
    monkeypatch.setattr(t2_prefix_scan, "configure_hook_logging", lambda: order.append("log"))
    monkeypatch.setattr(t2_prefix_scan, "HttpMemoryStore", lambda **k: order.append("store") or _Store([]))
    t2_prefix_scan.reachability_warning("nexus")
    assert order == ["log", "store"]
