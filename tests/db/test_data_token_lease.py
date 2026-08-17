# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the cross-process data-token lease-file cache (nexus-9c7t9).

Deterministic: injected monotonic clock + injected wall clock + injected
poster, no real network, no real sleeps. The lease file lives under an
injected ``config_dir`` (``tmp_path``) rather than the real
``~/.config/nexus``.

Design of record: bd show nexus-9c7t9. Mirrors the lease-file precedent in
``nexus.db.t1`` (``publish_t1_session_lease`` / ``read_t1_session_lease``):
atomic temp-file + ``os.replace`` publish, mode 0600, fail-safe (never
fail-open) on any corruption/mismatch/staleness.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import structlog

from nexus.db.data_token import (
    DataTokenManager,
    _data_token_lease_path,
    _lease_key,
)


class _FakeClock:
    """Monotonic-style clock (matches DataTokenManager's ``clock`` param)."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeWallClock:
    """Wall-clock stand-in (matches DataTokenManager's ``wall_clock`` param).

    Deliberately a SEPARATE fake from ``_FakeClock`` -- the lease file's
    freshness math is wall-clock (cross-process comparable), while the
    in-process cache's TTL math stays on the injectable monotonic ``clock``
    unchanged from the pre-lease design. Sharing one ``_FakeWallClock``
    instance across two ``DataTokenManager``s in a test models two
    processes observing the same wall-clock instant.
    """

    def __init__(self, start: float = 1_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakePoster:
    def __init__(self, responses: list[tuple[int, dict, dict]] | None = None) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self._responses = list(responses or [])
        self._default = (200, {"data_token": "tok-default", "expires_in_seconds": 300}, {})

    def __call__(self, url: str, headers: dict, body: dict) -> tuple[int, dict, dict]:
        self.calls.append((url, dict(headers), dict(body)))
        if self._responses:
            return self._responses.pop(0)
        return self._default

    def queue(self, status: int, body: dict, headers: dict | None = None) -> None:
        self._responses.append((status, body, dict(headers or {})))


BASE_URL = "http://127.0.0.1:9999"
TENANT = "acme"


def _manager(
    poster: _FakePoster,
    clock: _FakeClock,
    *,
    config_dir: Path,
    wall_clock: Callable[[], float],
    credential: str | None = "mintcred",
    mint_tenant: str | None = "",
) -> DataTokenManager:
    cred_fn = (lambda: credential) if credential is not None else (lambda: "")
    tenant_fn = lambda: (mint_tenant or "")  # noqa: E731 — small local closure, test-only
    return DataTokenManager(
        clock=clock,
        poster=poster,
        mint_credential=cred_fn,
        mint_tenant=tenant_fn,
        config_dir=config_dir,
        wall_clock=wall_clock,
    )


# ── lease written after a mint: perms, atomicity, content shape ─────────────


def test_lease_written_after_mint_has_tight_perms_and_shape(tmp_path: Path) -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(
        poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock(),
        credential="SUPER-SECRET-MINT-CREDENTIAL",
    )

    mgr.bearer_for(BASE_URL, TENANT)

    lease_files = list(tmp_path.glob("data_token_lease.*"))
    assert len(lease_files) == 1
    path = lease_files[0]
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600

    raw = path.read_text()
    data = json.loads(raw)
    assert data["token"] == "tok-1"
    assert data["tenant"] == TENANT
    assert data["base_url_digest"] == _lease_key(BASE_URL, TENANT)
    assert "format_version" in data
    assert "minted_by_pid" in data
    assert "expires_at" in data
    assert "ttl_seconds" in data

    # SECURITY FIRST: the mint credential must never land in the lease file.
    assert "SUPER-SECRET-MINT-CREDENTIAL" not in raw

    # Atomic write: no leftover temp file.
    assert list(tmp_path.glob("data_token_lease.*.tmp")) == []


def test_lease_path_is_deterministic_from_base_url_and_tenant(tmp_path: Path) -> None:
    p1 = _data_token_lease_path(BASE_URL, TENANT, tmp_path)
    p2 = _data_token_lease_path(BASE_URL, TENANT, tmp_path)
    assert p1 == p2
    # Never embeds the raw URL in the filename.
    assert "127.0.0.1" not in p1.name
    assert "9999" not in p1.name


# ── second manager instance reads the lease instead of minting ─────────────


def test_second_manager_reads_lease_without_minting(tmp_path: Path) -> None:
    wall = _FakeWallClock()
    poster1 = _FakePoster()
    poster1.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr1 = _manager(poster1, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    first = mgr1.bearer_for(BASE_URL, TENANT)

    poster2 = _FakePoster()  # would mint "tok-default" if reached — must not be reached
    mgr2 = _manager(poster2, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    second = mgr2.bearer_for(BASE_URL, TENANT)

    assert first == second == "tok-1"
    assert poster2.calls == []  # cross-process cache hit — no mint


def test_second_manager_own_cache_reused_after_lease_borrow(tmp_path: Path) -> None:
    """Once a manager has borrowed a lease into its own in-process cache, a
    SECOND call on that same manager must not re-read the file (ordinary
    in-process cache-hit path, unaffected by the lease feature)."""
    wall = _FakeWallClock()
    poster1 = _FakePoster()
    poster1.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr1 = _manager(poster1, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    mgr1.bearer_for(BASE_URL, TENANT)

    poster2 = _FakePoster()
    clock2 = _FakeClock()
    mgr2 = _manager(poster2, clock2, config_dir=tmp_path, wall_clock=wall)
    mgr2.bearer_for(BASE_URL, TENANT)  # borrows from the lease file
    clock2.advance(1)  # well within TTL
    mgr2.bearer_for(BASE_URL, TENANT)  # in-process cache hit this time

    assert poster2.calls == []


# ── expired / near-expiry lease ignored, re-minted, rewritten ──────────────


def test_expired_lease_ignored_and_reminted(tmp_path: Path) -> None:
    wall = _FakeWallClock()
    poster1 = _FakePoster()
    poster1.queue(200, {"data_token": "tok-1", "expires_in_seconds": 100})
    mgr1 = _manager(poster1, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    mgr1.bearer_for(BASE_URL, TENANT)

    wall.advance(150)  # well past the 100s TTL — genuinely expired

    poster2 = _FakePoster()
    poster2.queue(200, {"data_token": "tok-2", "expires_in_seconds": 100})
    mgr2 = _manager(poster2, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    token = mgr2.bearer_for(BASE_URL, TENANT)

    assert token == "tok-2"
    assert len(poster2.calls) == 1
    # Rewritten: the lease file now reflects mgr2's fresh mint.
    lease_files = list(tmp_path.glob("data_token_lease.*"))
    assert len(lease_files) == 1
    assert json.loads(lease_files[0].read_text())["token"] == "tok-2"


def test_lease_within_refresh_threshold_ignored(tmp_path: Path) -> None:
    """A lease with < 20% of its granted TTL remaining reads as absent —
    the same refresh threshold the in-process cache uses (nexus-9c7t9
    design point 2c)."""
    wall = _FakeWallClock()
    poster1 = _FakePoster()
    poster1.queue(200, {"data_token": "tok-1", "expires_in_seconds": 100})
    mgr1 = _manager(poster1, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    mgr1.bearer_for(BASE_URL, TENANT)

    wall.advance(85)  # 15s remaining of 100s TTL == 15% < 20% threshold

    poster2 = _FakePoster()
    poster2.queue(200, {"data_token": "tok-2", "expires_in_seconds": 100})
    mgr2 = _manager(poster2, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    token = mgr2.bearer_for(BASE_URL, TENANT)

    assert token == "tok-2"
    assert len(poster2.calls) == 1


def test_fresh_lease_above_threshold_is_borrowed(tmp_path: Path) -> None:
    wall = _FakeWallClock()
    poster1 = _FakePoster()
    poster1.queue(200, {"data_token": "tok-1", "expires_in_seconds": 100})
    mgr1 = _manager(poster1, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    mgr1.bearer_for(BASE_URL, TENANT)

    wall.advance(50)  # 50s remaining of 100s TTL == 50% > 20% threshold

    poster2 = _FakePoster()
    mgr2 = _manager(poster2, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    token = mgr2.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert poster2.calls == []


# ── corrupt / foreign lease content ignored (fail-safe, not fail-open) ─────


def test_corrupt_lease_file_ignored(tmp_path: Path) -> None:
    path = _data_token_lease_path(BASE_URL, TENANT, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json {{{")

    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 1


def test_lease_missing_is_a_clean_miss(tmp_path: Path) -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 1


def _write_raw_lease(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_lease_with_mismatched_tenant_field_ignored(tmp_path: Path) -> None:
    path = _data_token_lease_path(BASE_URL, TENANT, tmp_path)
    _write_raw_lease(path, {
        "format_version": 1,
        "token": "stolen-tok",
        "tenant": "someone-else",  # does not match the caller-passed tenant
        "base_url_digest": _lease_key(BASE_URL, TENANT),
        "expires_at": 10_000_000.0,
        "ttl_seconds": 300,
        "minted_by_pid": 1,
    })

    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 1


def test_lease_with_mismatched_base_url_digest_ignored(tmp_path: Path) -> None:
    path = _data_token_lease_path(BASE_URL, TENANT, tmp_path)
    _write_raw_lease(path, {
        "format_version": 1,
        "token": "stolen-tok",
        "tenant": TENANT,
        "base_url_digest": "0" * 32,  # wrong digest
        "expires_at": 10_000_000.0,
        "ttl_seconds": 300,
        "minted_by_pid": 1,
    })

    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 1


def test_lease_with_wrong_format_version_ignored(tmp_path: Path) -> None:
    path = _data_token_lease_path(BASE_URL, TENANT, tmp_path)
    _write_raw_lease(path, {
        "format_version": 999,
        "token": "stolen-tok",
        "tenant": TENANT,
        "base_url_digest": _lease_key(BASE_URL, TENANT),
        "expires_at": 10_000_000.0,
        "ttl_seconds": 300,
        "minted_by_pid": 1,
    })

    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())

    token = mgr.bearer_for(BASE_URL, TENANT)

    assert token == "tok-1"
    assert len(poster.calls) == 1


# ── invalidate deletes the lease file too ───────────────────────────────────


def test_invalidate_deletes_lease_file(tmp_path: Path) -> None:
    poster = _FakePoster()
    poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())
    mgr.bearer_for(BASE_URL, TENANT)
    assert list(tmp_path.glob("data_token_lease.*"))

    mgr.invalidate(BASE_URL, TENANT)

    assert list(tmp_path.glob("data_token_lease.*")) == []


def test_invalidate_unknown_key_lease_file_absent_is_a_noop(tmp_path: Path) -> None:
    poster = _FakePoster()
    mgr = _manager(poster, _FakeClock(), config_dir=tmp_path, wall_clock=_FakeWallClock())
    mgr.invalidate(BASE_URL, TENANT)  # never minted, no lease file — must not raise
    assert poster.calls == []


def test_invalidate_does_not_clobber_siblings_fresh_lease(tmp_path: Path) -> None:
    """401-storm interleave (review round-1 Significant, compare-and-delete):
    processes A and B both hold a now-revoked token; A re-mints and
    publishes a FRESH lease; B's later invalidate of the OLD token must
    NOT delete A's fresh lease — otherwise the recovery path forces a
    needless extra mint, exactly what the cross-process cache exists to
    avoid. Falsifiable: revert _delete_lease to unconditional unlink and
    the fresh-lease assertion below fails."""
    wall = _FakeWallClock()
    poster_b = _FakePoster()
    poster_b.queue(200, {"data_token": "tok-revoked", "expires_in_seconds": 300})
    mgr_b = _manager(poster_b, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    mgr_b.bearer_for(BASE_URL, TENANT)  # B holds tok-revoked, lease published

    # Sibling A (fresh manager, own process in reality) invalidates the
    # same revoked token, re-mints, and republishes a FRESH lease.
    poster_a = _FakePoster()
    poster_a.queue(200, {"data_token": "tok-fresh", "expires_in_seconds": 300})
    mgr_a = _manager(poster_a, _FakeClock(), config_dir=tmp_path, wall_clock=wall)
    borrowed = mgr_a.bearer_for(BASE_URL, TENANT)
    assert borrowed == "tok-revoked"  # A borrowed B's lease, no mint yet
    mgr_a.invalidate(BASE_URL, TENANT)
    assert mgr_a.bearer_for(BASE_URL, TENANT) == "tok-fresh"

    # B now reports ITS 401: its invalidate compares tok-revoked against
    # the on-disk tok-fresh, must leave the file alone, and B's next
    # bearer_for borrows A's fresh lease instead of minting.
    mgr_b.invalidate(BASE_URL, TENANT)
    lease_files = list(tmp_path.glob("data_token_lease.*"))
    assert lease_files, "sibling's fresh lease was clobbered by invalidate"
    assert mgr_b.bearer_for(BASE_URL, TENANT) == "tok-fresh"
    assert len(poster_b.calls) == 1  # B never minted a second time


# ── write failure never fails the mint (best-effort) ────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_lease_write_failure_does_not_fail_mint(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("permission bits are not enforced against root")

    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir(mode=0o700)
    readonly_dir.chmod(0o500)  # read + execute, no write
    try:
        poster = _FakePoster()
        poster.queue(200, {"data_token": "tok-1", "expires_in_seconds": 300})
        mgr = _manager(poster, _FakeClock(), config_dir=readonly_dir, wall_clock=_FakeWallClock())

        with structlog.testing.capture_logs() as captured:
            token = mgr.bearer_for(BASE_URL, TENANT)

        # The mint itself succeeded — a lease-write failure is an
        # optimization loss, never a mint failure.
        assert token == "tok-1"
        events = [e.get("event") for e in captured]
        assert "data_token_lease_write_failed" in events
    finally:
        readonly_dir.chmod(0o700)  # restore so tmp_path cleanup can remove it


# ── token / credential value never logged for lease operations ─────────────


def test_lease_reuse_never_logs_token_or_credential(tmp_path: Path) -> None:
    import logging

    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))

    wall = _FakeWallClock()
    poster1 = _FakePoster()
    poster1.queue(200, {"data_token": "SUPER-SECRET-TOKEN", "expires_in_seconds": 300})
    mgr1 = _manager(
        poster1, _FakeClock(), config_dir=tmp_path, wall_clock=wall,
        credential="SUPER-SECRET-CREDENTIAL",
    )
    mgr1.bearer_for(BASE_URL, TENANT)

    poster2 = _FakePoster()
    mgr2 = _manager(
        poster2, _FakeClock(), config_dir=tmp_path, wall_clock=wall,
        credential="SUPER-SECRET-CREDENTIAL",
    )
    with structlog.testing.capture_logs() as captured:
        mgr2.bearer_for(BASE_URL, TENANT)

    rendered = str(captured)
    assert "SUPER-SECRET-TOKEN" not in rendered
    assert "SUPER-SECRET-CREDENTIAL" not in rendered
