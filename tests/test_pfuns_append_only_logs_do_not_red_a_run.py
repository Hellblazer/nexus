# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""An append-only log growing must not turn a passing run red.

nexus-pfuns follow-on. The real-config-dir guard sets `session.exitstatus = 1`
for ANY change under `~/.config/nexus/`. That conflated two different things:

  STATE MUTATION   backfill_state.json, last_seen_version -- a test wrote real
                   production state. Genuinely bad, still fails.
  APPEND           routing_log.jsonl gained lines. Untidy, not a leak.

MEASURED COST of not separating them: a 7.16.3 release-battery leg reported
exit 1 over **14,405 passing tests and zero failures** because a log file grew.
An exit code that cannot distinguish "the suite failed" from "a log file was
appended to" forces every downstream consumer to rerun a 40-minute battery to
find out which — and the rerun appends to the log again, so it is not even
self-clearing. That is the "benign failures force reruns and prolong the pain"
class, and it is worse than no guard on that path because it trains people to
ignore a real one.

The split keys on size alone, deliberately: this guard never reads content (the
directory can hold a live user's real data), and size is enough. A log that
SHRANK or was rewritten in place is a truncation — a state mutation — and
still fails. That is the case actually worth catching.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_CONFTEST = pathlib.Path(__file__).parent / "conftest.py"


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("_pfuns_conftest_probe", _CONFTEST)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover — surfaces an import break loudly
        pytest.fail(f"could not load conftest for probing: {exc!r}")
    assert hasattr(mod, "_split_appends_from_state"), (
        "the classifier is gone — this guard has silently reverted to "
        "failing on every change"
    )
    return mod


def test_a_growing_append_only_log_is_benign(guard):
    before = {"routing_log.jsonl": (1, 100)}
    after = {"routing_log.jsonl": (2, 173)}
    state, appends = guard._split_appends_from_state(
        [("MODIFIED", "routing_log.jsonl")], before, after,
    )
    assert state == [], "a grown log must not fail the run"
    assert appends == [("MODIFIED", "routing_log.jsonl")]


def test_a_truncated_log_is_still_a_state_mutation(guard):
    """THE CASE WORTH CATCHING. Shrinking means someone rewrote it."""
    before = {"routing_log.jsonl": (1, 500)}
    after = {"routing_log.jsonl": (2, 10)}
    state, appends = guard._split_appends_from_state(
        [("MODIFIED", "routing_log.jsonl")], before, after,
    )
    assert state == [("MODIFIED", "routing_log.jsonl")], "a truncation must still fail"
    assert appends == []


def test_real_state_still_fails(guard):
    """The guard's actual subject, unchanged."""
    before = {"last_seen_version": (1, 6), "backfill_state.json": (1, 40)}
    after = {"last_seen_version": (2, 6), "backfill_state.json": (2, 41)}
    changed = [("MODIFIED", "last_seen_version"), ("MODIFIED", "backfill_state.json")]
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert sorted(state) == sorted(changed), "state mutations must still fail"
    assert appends == []


def test_a_newly_created_log_is_not_treated_as_an_append(guard):
    """No 'before' entry means the run CREATED it — not an append to an
    existing log, and the guard should not wave it through on name alone."""
    state, appends = guard._split_appends_from_state(
        [("ADDED", "routing_log.jsonl")], {}, {"routing_log.jsonl": (2, 50)},
    )
    assert state == [("ADDED", "routing_log.jsonl")]
    assert appends == []


def test_an_unlisted_file_is_never_waved_through(guard):
    """Falsification control on the allowlist: growth alone must not excuse a
    file that is not a known append-only log."""
    before = {"secrets.json": (1, 10)}
    after = {"secrets.json": (2, 99)}
    state, appends = guard._split_appends_from_state(
        [("MODIFIED", "secrets.json")], before, after,
    )
    assert state == [("MODIFIED", "secrets.json")], "only NAMED append-only logs are benign"
    assert appends == []


# ── nexus-ist38 / nexus-wjkc7: the split must consume what the diff emits ───
#
# `_diff_config_dir_snapshots` and `_split_appends_from_state` used to
# disagree about shape: the diff emitted "ADDED "/"MODIFIED "/"REMOVED "
# -prefixed strings, and the split (fed only bare paths by every test above)
# looked the whole prefixed string up as a path, matched nothing, and
# classified every change as a state mutation -- so a growing routing_log
# reddened real runs while these tests stayed green. nexus-ist38 patched
# that gap with a verb-stripping helper; nexus-wjkc7 removed the string
# encoding entirely so there is exactly one shape (`(verb, rel)` tuples) and
# a caller cannot pass the wrong one.


def test_the_split_consumes_the_diffs_own_entries(guard):
    before = {
        "routing_log.jsonl": (1, 100),   # append-only log, grows
        "logs/mineru.log": (1, 10),      # ambient daemon dir
        "backfill_state.json": (1, 50),  # production STATE, rewritten smaller
    }
    after = {
        "routing_log.jsonl": (2, 200),
        "logs/mineru.log": (2, 20),
        "backfill_state.json": (2, 40),
    }
    changed = guard._diff_config_dir_snapshots(before, after)
    assert changed, "the diff itself found nothing -- this test would be vacuous"
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert state == [("MODIFIED", "backfill_state.json")]
    assert appends == [
        ("MODIFIED", "logs/mineru.log"), ("MODIFIED", "routing_log.jsonl"),
    ]


def test_a_truncated_log_still_reddens_through_the_diff(guard):
    """The SIZE rule, exercised through the diff rather than a hand-built
    tuple."""
    before = {"routing_log.jsonl": (1, 500)}
    after = {"routing_log.jsonl": (2, 10)}
    changed = guard._diff_config_dir_snapshots(before, after)
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert state == [("MODIFIED", "routing_log.jsonl")]
    assert appends == []


# ── nexus-pfuns follow-up (2026-09-25): index.log's single-generation
# rotation ────────────────────────────────────────────────────────────────
#
# src/nexus/commands/hooks.py's post-commit stanza rotates index.log itself
# when it exceeds 4 MiB: `mv -f "$NX_INDEX_LOG" "$NX_INDEX_LOG.1"`, then a
# fresh index.log is appended to. A same-filesystem `mv` is a rename -- it
# does not touch the renamed inode's content or (mtime, size) -- so the ONE
# shape this rotation produces, and nothing else does, is index.log.1's
# post-session stat landing byte-for-byte equal to index.log's pre-session
# stat. MEASURED 2026-09-25: a clean `pytest -m lint` run (0 assertion
# failures) exited 1 over exactly `MODIFIED index.log, MODIFIED
# index.log.1`, coinciding with real git-commit activity on this shared box
# during the run.
#
# index.log is governed SOLELY by this module (nexus-wjkc7): it must never
# be added to `_REAL_CONFIG_DIR_ALLOWLIST_PREFIXES`, which would make this
# stricter, shape-checked rule unreachable. This rotation rule is likewise
# scoped to the two exact names below, not a blanket `index.log*` prefix --
# an in-place rewrite of index.log that does NOT rotate must still fail.


def test_index_log_rotation_is_benign(guard):
    """The exact shape a size-triggered rotation produces: index.log.1
    (newly created here) lands exactly where index.log's baseline was."""
    before = {"index.log": (1_000, 4_200_000)}
    after = {"index.log": (5_000, 300), "index.log.1": (1_000, 4_200_000)}
    changed = guard._diff_config_dir_snapshots(before, after)
    assert changed, "the diff itself found nothing -- this test would be vacuous"
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert state == []
    assert appends == [
        ("ADDED", "index.log.1"), ("MODIFIED", "index.log"),
    ]


def test_index_log_second_rotation_is_also_benign(guard):
    """index.log.1 already existed (a prior rotation, on an earlier
    session) -- the second rotation's `mv -f` clobbers it, which is still
    exactly the rotation shape: the NEW index.log.1 stat matches THIS
    session's baseline index.log stat."""
    before = {
        "index.log": (10_000, 4_300_000),
        "index.log.1": (500, 4_100_000),
    }
    after = {
        "index.log": (20_000, 150),
        "index.log.1": (10_000, 4_300_000),
    }
    changed = guard._diff_config_dir_snapshots(before, after)
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert state == []
    assert sorted(appends) == [
        ("MODIFIED", "index.log"), ("MODIFIED", "index.log.1"),
    ]


def test_index_log_in_place_rewrite_without_rotation_still_fails(guard):
    """THE CASE WORTH CATCHING: index.log changed (shrank) but index.log.1
    did not move at all -- no rotation happened, so this must still fail."""
    before = {"index.log": (1_000, 5_000)}
    after = {"index.log": (2_000, 3_000)}
    changed = guard._diff_config_dir_snapshots(before, after)
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert state == [("MODIFIED", "index.log")]
    assert appends == []


def test_index_log_1_change_that_does_not_match_index_log_baseline_still_fails(guard):
    """index.log.1 changed, but NOT to index.log's pre-session stat -- some
    other rewrite, not this rotation. Must still fail (no index.log entry
    in this session at all, so there is nothing for it to match)."""
    before = {"index.log.1": (1, 10)}
    after = {"index.log.1": (2, 20)}
    changed = guard._diff_config_dir_snapshots(before, after)
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert state == [("MODIFIED", "index.log.1")]
    assert appends == []
