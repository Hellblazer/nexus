# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Ambient daemon output must not fail a run; real state mutation still must.

nexus-pfuns, widened 2026-08-24 on Sam's call. The real-config-dir guard
snapshots ``(mtime_ns, size)`` and never reads content, so ANY write by a live
daemon registers as a mutation -- including re-stamping an identical value.
``~/.config/nexus/logs/`` is written continuously by the aspect-worker, the T2
daemon, the storage service, the MCP servers, MinerU and the watchdog, none of
which the suite owns or can stop.

WHY IT HAD NOT BEEN HIT: the aspect-worker was WEDGED for hours by the
nexus-yg70j cwd bug. The quiet period was the outage, not evidence the guard
was fine -- so fixing the daemon raises the flake rate rather than lowering it.
(Observation credited to the peer session driving the aspect work.)

THE DISCRIMINATOR IS STRUCTURAL, NOT "IT GREW". The standing rule is that
growth alone never excuses an unlisted file, and this change does not weaken
it: ``_APPEND_ONLY_REAL_CONFIG_LOGS`` keeps its strict grow-only semantics for
named files, and the new exemption is "a live daemon owns this DIRECTORY".
Rotation is why growth-only would not have sufficed -- rolling a log to ``.1``
and opening a fresh one is a create plus a shrink.

Every test below pairs with a negative control. A widening that also stopped
catching `last_seen_version` would be a disabled guard wearing a rationale.
"""
from __future__ import annotations

import pytest

from tests.conftest import (  # type: ignore[attr-defined]
    _AMBIENT_DAEMON_DIRS,
    _APPEND_ONLY_REAL_CONFIG_LOGS,
    _split_appends_from_state,
)


def _split(changed, before, after):
    """Wrap bare rel-paths as ``(verb, rel)`` pairs -- the verb is
    immaterial to ``_split_appends_from_state``'s classification (only
    ``rel`` is looked up in *before*/*after*), so every test below can keep
    naming just the path it cares about."""
    return _split_appends_from_state([("MODIFIED", rel) for rel in changed], before, after)


# ── ambient daemon output is tolerated, in every direction ──────────────────


@pytest.mark.parametrize(
    ("rel", "before", "after", "shape"),
    [
        ("logs/aspect_worker_daemon.log", (1, 100), (2, 400), "grew"),
        ("logs/aspect_worker_daemon.log.1", None, (2, 900), "created by rotation"),
        ("logs/aspect_worker_daemon.log", (1, 900), (2, 0), "truncated by rotation"),
        ("logs/mcp.log", (1, 10), (2, 11), "grew"),
        ("logs/t2_daemon.log", (1, 10), (2, 11), "grew"),
        ("logs/storage_service.log", (1, 10), (2, 11), "grew"),
        ("logs/watchdog.log", (1, 10), (2, 11), "grew"),
        ("logs/index-nexus-571b8edd.log", (1, 10), (2, 11), "per-repo index log"),
    ],
)
def test_daemon_output_is_not_a_state_mutation(rel, before, after, shape) -> None:
    b = {rel: before} if before else {}
    a = {rel: after}
    state, appends = _split([rel], b, a)
    assert state == [], f"{shape} under logs/ was treated as state: {state}"
    assert appends == [("MODIFIED", rel)]


# ── the negative controls: what must STILL fail ────────────────────────────


@pytest.mark.parametrize(
    ("rel", "before", "after", "why"),
    [
        ("last_seen_version", (1, 6), (2, 6), "the exact 2026-08-24 incident file"),
        ("backfill_state.json", (1, 40), (2, 40), "production state named in the guard's docstring"),
        ("config.yml", (1, 200), (2, 260), "operator config"),
        ("catalog/catalog.db", (1, 10), (2, 99), "a database outside logs/"),
        ("logs_not_really/x.log", (1, 10), (2, 11), "prefix must not match a sibling dir"),
    ],
)
def test_real_state_mutation_still_fails(rel, before, after, why) -> None:
    state, appends = _split([rel], {rel: before}, {rel: after})
    assert state == [("MODIFIED", rel)], f"widening swallowed a real mutation ({why})"
    assert appends == []


def test_a_named_append_log_that_shrank_still_fails() -> None:
    """The pre-existing strict rule is untouched: a top-level append-only log
    that SHRANK is a truncation, not an append."""
    rel = "routing_log.jsonl"
    assert rel in _APPEND_ONLY_REAL_CONFIG_LOGS
    state, appends = _split([rel], {rel: (1, 900)}, {rel: (2, 10)})
    assert state == [("MODIFIED", rel)] and appends == []


def test_a_named_append_log_that_grew_is_still_benign() -> None:
    rel = "index.log"
    state, appends = _split([rel], {rel: (1, 10)}, {rel: (2, 99)})
    assert state == [] and appends == [("MODIFIED", rel)]


# ── the widening must stay scoped ──────────────────────────────────────────


def test_the_exemption_is_one_directory_not_a_blanket() -> None:
    """A future edit adding "" or "/" here would exempt the whole config dir
    and silently retire the guard."""
    assert _AMBIENT_DAEMON_DIRS == ("logs/",)
    assert all(d and d.endswith("/") for d in _AMBIENT_DAEMON_DIRS)


def test_a_mixed_batch_is_split_not_collapsed() -> None:
    """The realistic shape: a daemon logging while a real leak happens. The
    leak must survive the presence of benign neighbours -- an all-or-nothing
    classifier would report green here."""
    changed = ["logs/aspect_worker_daemon.log", "last_seen_version"]
    before = {"logs/aspect_worker_daemon.log": (1, 10), "last_seen_version": (1, 6)}
    after = {"logs/aspect_worker_daemon.log": (2, 90), "last_seen_version": (2, 6)}
    state, appends = _split(changed, before, after)
    assert state == [("MODIFIED", "last_seen_version")]
    assert appends == [("MODIFIED", "logs/aspect_worker_daemon.log")]
