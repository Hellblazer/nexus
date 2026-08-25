# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A containerized fan-out that collected nothing must not report success.

nexus-uq3xs / nexus-moht0. `fanout.sh` shards the unit suite across N
containers and aggregates their exit codes. Per-shard pytest exit 5 (nothing
collected) is correctly tolerated -- a marker deselect can legitimately empty
ONE shard. Nothing asserted on the TOTAL, so a broken roster, a marker change
that deselected everything, or an image whose test tree never got copied would
print "total: 0 tests" and exit 0.

MEASURED on this guard's first real use (2026-08-24): a 6-shard run had two
shards SIGKILLed at their memory cap. They wrote no junit, so 9,622 of ~14,500
tests were collected and the rest silently never ran. Without the floor that
reads as "9,074 passed" plus some failures, with no signal that a third of the
suite was missing.

These drive `fanout_verdict` in tests/containers/lib/verdict.sh directly. An
earlier attempt drove fanout.sh end-to-end and was wrong: an empty roster makes
the script rewrite each shard's own .rc/.time (line 119), clobbering seeded
artifacts, and it invoked Docker for 86s besides. Extracting the verdict is
what made it testable.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_VERDICT = Path(__file__).parent / "containers" / "lib" / "verdict.sh"


def _seed(out: Path, shards: list[tuple[int, int | None]]) -> None:
    """shards: list of (rc, tests) — tests None means no junit written."""
    for i, (rc, tests) in enumerate(shards):
        (out / f"shard-{i}.rc").write_text(f"{rc}\n")
        (out / f"shard-{i}.time").write_text("0 1\n")
        (out / f"shard-{i}.log").write_text("seeded\n")
        if tests is not None:
            (out / f"shard-{i}.xml").write_text(
                f'<testsuite tests="{tests}" errors="0" failures="0" skipped="0"></testsuite>\n'
            )


def _verdict(out: Path, shards: int, floor: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{_VERDICT}"; fanout_verdict "{out}" {shards} "{floor}"'],
        capture_output=True, text=True,
    )


pytestmark = pytest.mark.skipif(not _VERDICT.exists(), reason="verdict.sh missing")


def test_zero_tests_across_all_shards_fails(tmp_path: Path) -> None:
    """The load-bearing case: every shard exits 5, nothing collected. The old
    logic called that a pass."""
    _seed(tmp_path, [(5, 0), (5, 0)])
    r = _verdict(tmp_path, 2)
    assert r.returncode != 0, "a fan-out that collected ZERO tests reported success"
    assert "0 tests collected" in r.stderr


def test_one_empty_shard_is_still_tolerated(tmp_path: Path) -> None:
    """Negative control. A marker deselect emptying ONE shard is legitimate;
    a blunt refusal here would break every roster with an uneven split."""
    _seed(tmp_path, [(5, 0), (0, 40)])
    r = _verdict(tmp_path, 2)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_missing_shard_verdict_fails(tmp_path: Path) -> None:
    """The 2026-08-24 shape: a SIGKILLed shard writes no .rc at all. Counting
    only the shards that reported would pass over the ones that died."""
    _seed(tmp_path, [(0, 100)])
    r = _verdict(tmp_path, 2)  # claim 2 shards, only 1 seeded
    assert r.returncode != 0
    assert "1 of 2 shard(s) reported" in r.stderr


def test_the_floor_catches_a_partial_collection(tmp_path: Path) -> None:
    _seed(tmp_path, [(0, 100), (0, 100)])
    r = _verdict(tmp_path, 2, floor="14000")
    assert r.returncode != 0
    assert "below the floor" in r.stderr


def test_the_floor_is_opt_in(tmp_path: Path) -> None:
    """Unset floor must not impose one, or every small roster run fails."""
    _seed(tmp_path, [(0, 100), (0, 100)])
    assert _verdict(tmp_path, 2).returncode == 0


def test_a_real_test_failure_still_fails(tmp_path: Path) -> None:
    """The floor must not become the ONLY thing that can fail the run."""
    _seed(tmp_path, [(0, 100), (1, 100)])
    assert _verdict(tmp_path, 2).returncode != 0


def test_totals_are_summed_across_shards(tmp_path: Path) -> None:
    _seed(tmp_path, [(0, 40), (0, 60)])
    r = _verdict(tmp_path, 2)
    assert "total: 100 tests" in r.stdout


def test_fanout_sources_the_real_verdict(tmp_path: Path) -> None:
    """Every test above drives verdict.sh directly, so all of them would keep
    passing if fanout.sh stopped calling it."""
    body = (_VERDICT.parent.parent / "fanout.sh").read_text()
    assert "lib/verdict.sh" in body
    assert 'fanout_verdict "$OUT" "$SHARDS"' in body
