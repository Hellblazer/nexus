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
    state, appends = guard._split_appends_from_state(["routing_log.jsonl"], before, after)
    assert state == [], "a grown log must not fail the run"
    assert appends == ["routing_log.jsonl"]


def test_a_truncated_log_is_still_a_state_mutation(guard):
    """THE CASE WORTH CATCHING. Shrinking means someone rewrote it."""
    before = {"routing_log.jsonl": (1, 500)}
    after = {"routing_log.jsonl": (2, 10)}
    state, appends = guard._split_appends_from_state(["routing_log.jsonl"], before, after)
    assert state == ["routing_log.jsonl"], "a truncation must still fail"
    assert appends == []


def test_real_state_still_fails(guard):
    """The guard's actual subject, unchanged."""
    before = {"last_seen_version": (1, 6), "backfill_state.json": (1, 40)}
    after = {"last_seen_version": (2, 6), "backfill_state.json": (2, 41)}
    changed = ["last_seen_version", "backfill_state.json"]
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert sorted(state) == sorted(changed), "state mutations must still fail"
    assert appends == []


def test_a_newly_created_log_is_not_treated_as_an_append(guard):
    """No 'before' entry means the run CREATED it — not an append to an
    existing log, and the guard should not wave it through on name alone."""
    state, appends = guard._split_appends_from_state(
        ["routing_log.jsonl"], {}, {"routing_log.jsonl": (2, 50)},
    )
    assert state == ["routing_log.jsonl"]
    assert appends == []


def test_an_unlisted_file_is_never_waved_through(guard):
    """Falsification control on the allowlist: growth alone must not excuse a
    file that is not a known append-only log."""
    before = {"secrets.json": (1, 10)}
    after = {"secrets.json": (2, 99)}
    state, appends = guard._split_appends_from_state(["secrets.json"], before, after)
    assert state == ["secrets.json"], "only NAMED append-only logs are benign"
    assert appends == []


# ── nexus-ist38: the split must consume what the diff actually emits ────────
#
# Every test above hands `_split_appends_from_state` a BARE path. The guard
# hands it `_diff_config_dir_snapshots`'s output, which prefixes each entry
# with "ADDED "/"MODIFIED "/"REMOVED ". For the guard's whole life the split
# looked the whole prefixed string up as a path, matched nothing, and
# classified every change as a state mutation -- so a growing routing_log
# reddened real runs while these tests stayed green. The composition, not
# either half, is the subject here.


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
    assert state == ["MODIFIED backfill_state.json"]
    assert appends == ["MODIFIED logs/mineru.log", "MODIFIED routing_log.jsonl"]


def test_rel_path_of_diff_entry_strips_every_verb_and_passes_bare_paths(guard):
    assert guard._rel_path_of_diff_entry("MODIFIED routing_log.jsonl") == "routing_log.jsonl"
    assert guard._rel_path_of_diff_entry("ADDED logs/x.log") == "logs/x.log"
    assert guard._rel_path_of_diff_entry("REMOVED a/b.lock") == "a/b.lock"
    assert guard._rel_path_of_diff_entry("routing_log.jsonl") == "routing_log.jsonl"


def test_a_truncated_log_still_reddens_through_the_diff(guard):
    """The SIZE rule, exercised through the diff rather than on a bare path.

    Honest about what it does not do (code review [24116]): this passes with
    the verb strip absent too -- pre-fix code reached the same verdict by a
    different route (unparsed entry -> state) than post-fix code does
    (parsed, but shrank). It guards the size rule against a future edit that
    widens the append branch; the verb strip's own falsifiable pin is
    ``test_the_split_consumes_the_diffs_own_entries`` above."""
    before = {"routing_log.jsonl": (1, 500)}
    after = {"routing_log.jsonl": (2, 10)}
    changed = guard._diff_config_dir_snapshots(before, after)
    state, appends = guard._split_appends_from_state(changed, before, after)
    assert state == ["MODIFIED routing_log.jsonl"]
    assert appends == []
