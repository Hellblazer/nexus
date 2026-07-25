# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``scripts/check_engine_release_floor.py`` (nexus-i5c2u, Phase 4).

Root cause this closes: AGENTS.md's release-checklist "Engine-freshness gate"
step was pure prose -- a human had to manually run
``git log <pinned-engine-tag>..HEAD -- service/`` and eyeball whether the drift
was "non-trivial AND cloud-relevant". That eyeball check was skipped in
practice: the cloud engine sat at v0.1.17 for 9+ days across multiple client
releases while develop's ``REQUIRED_ENGINE_VERSION`` floor moved to v0.1.34.
This script makes the check mechanical and blocking: probe the live managed
service, compare against the floor, exit non-zero (with a remedy) if stale.

``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]`` in
``pyproject.toml``, so ``check_engine_release_floor`` imports directly with no
``sys.path`` hack.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import check_engine_release_floor as gate
from nexus.db.managed_endpoint import ManagedCapabilities, ManagedServiceUnreachable
from nexus.engine_version import REQUIRED_ENGINE_VERSION

_TEST_URL = "https://example.test"


def _caps(release_version: str) -> ManagedCapabilities:
    return ManagedCapabilities(
        base_url=_TEST_URL,
        app_version="1.0-SNAPSHOT",
        release_version=release_version,
        embedding_mode="voyage",
        embedding_models=[],
        schema_latest_id=None,
        schema_changeset_count=None,
    )


def _floor_str() -> str:
    return ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)


#: The pin-currency half is exercised by its own tests below. The pre-existing
#: tests below target the CLOUD half, so they pass an explicitly-current pin —
#: otherwise every one of them would fail on the real repo (which legitimately
#: has engine tags ahead of the pin) and stop testing what they were written for.
_PIN_CURRENT = REQUIRED_ENGINE_VERSION


def test_engine_at_or_above_floor_passes(capsys: pytest.CaptureFixture[str]) -> None:
    above = (REQUIRED_ENGINE_VERSION[0], REQUIRED_ENGINE_VERSION[1], REQUIRED_ENGINE_VERSION[2] + 1)
    with patch.object(gate, "probe_managed_service", return_value=_caps(".".join(str(p) for p in above))):
        rc = gate.check_floor(url=_TEST_URL, newest=_PIN_CURRENT)
    assert rc == 0
    out = capsys.readouterr().out
    assert "current" in out.lower()


def test_engine_exactly_at_floor_passes(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps(_floor_str())):
        rc = gate.check_floor(url=_TEST_URL, newest=_PIN_CURRENT)
    assert rc == 0


def test_stale_engine_fails_and_names_both_versions(capsys: pytest.CaptureFixture[str]) -> None:
    stale = "0.1.1"
    assert (0, 1, 1) < REQUIRED_ENGINE_VERSION
    with patch.object(gate, "probe_managed_service", return_value=_caps(stale)):
        rc = gate.check_floor(url=_TEST_URL, newest=_PIN_CURRENT)
    # Exact code, not just non-zero: a regression that swapped the
    # documented stale(1)/unreachable(2) exit codes must be caught here.
    assert rc == 1
    err = capsys.readouterr().err
    assert stale in err
    assert _floor_str() in err
    assert "engine-release" in err  # points at the remedy skill


def test_unreachable_service_fails_loud_without_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(
        gate,
        "probe_managed_service",
        side_effect=ManagedServiceUnreachable("connect timed out"),
    ):
        rc = gate.check_floor(url=_TEST_URL, newest=_PIN_CURRENT)
    # Exact code: unreachable must be distinguishable from stale/incompatible.
    assert rc == 2
    err = capsys.readouterr().err
    assert "unreachable" in err.lower()
    assert "connect timed out" in err


def test_main_returns_nonzero_on_stale_engine(capsys: pytest.CaptureFixture[str]) -> None:
    # newest_published_engine is patched to a current pin so this asserts the
    # CLOUD direction. Without it, main() would exit 1 on the real repo's
    # pin-currency failure and pass for the wrong reason — a vacuous green.
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION):
        rc = gate.main(["--url", _TEST_URL])
    assert rc == 1
    assert "FLOOR CHECK FAILED" in capsys.readouterr().err


def test_help_exits_cleanly_without_network_call() -> None:
    with patch.object(gate, "probe_managed_service") as mock_probe:
        with pytest.raises(SystemExit) as exc_info:
            gate.main(["--help"])
    assert exc_info.value.code == 0
    mock_probe.assert_not_called()


# ── Pin currency: the OTHER direction (nexus-6igii / Hal directive 2026-07-15) ──
#
# The cloud half above answers "is the deployed engine behind what we pin?".
# Nothing answered "is what we pin behind what we cut?" until 2026-07-25 — and
# that is the LOCAL-install delivery path. Cloud users get whatever conexus
# deployed regardless of this constant; local-mode installs get ONLY the pinned
# identity. So an engine tag cut, gated, published, and never pinned reaches
# nobody, while the cloud check reports "current" and exits 0. That is exactly
# how the pin sat at v0.1.52 across engine tags .53 .54 .55 .56.


def _bump(v: tuple[int, int, int], n: int = 1) -> tuple[int, int, int]:
    return (v[0], v[1], v[2] + n)


def test_unpinned_gated_tag_fails_and_names_both_versions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The headline case: a published tag ahead of the pin blocks the release."""
    newer = _bump(REQUIRED_ENGINE_VERSION, 4)
    rc = gate.check_pin_currency(newer)
    assert rc == 1
    err = capsys.readouterr().err
    assert _floor_str() in err
    assert ".".join(str(p) for p in newer) in err
    # The message must state WHY it matters, not just that numbers differ.
    assert "local" in err.lower()
    assert "REQUIRED_ENGINE_VERSION" in err


def test_unpinned_failure_warns_about_bumping_before_deploy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Naive remediation (bump immediately) breaks every cloud client, because
    probe_managed_service fails closed below the pinned identity. The remedy
    text must carry that ordering constraint or the fix causes GH #1402
    inverted."""
    gate.check_pin_currency(_bump(REQUIRED_ENGINE_VERSION, 1))
    err = capsys.readouterr().err
    assert "deploy it FIRST" in err
    assert "1402" in err


def test_pin_equal_to_newest_tag_passes(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.check_pin_currency(REQUIRED_ENGINE_VERSION) == 0
    assert "current" in capsys.readouterr().out.lower()


def test_pin_ahead_of_newest_tag_passes() -> None:
    """The pin may legitimately lead during a cut (constant bumped, tag not yet
    pushed). Only the pin FALLING BEHIND is the defect."""
    older = (REQUIRED_ENGINE_VERSION[0], REQUIRED_ENGINE_VERSION[1], REQUIRED_ENGINE_VERSION[2] - 1)
    assert gate.check_pin_currency(older) == 0


def test_no_tags_visible_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    """CI's actions/checkout fetches no tags by default. A gate that sees an
    empty list must FAIL, never report success — the vacuous-green mode."""
    rc = gate.check_pin_currency(None)
    assert rc == 2
    assert "fetch-tags" in capsys.readouterr().err


def test_git_unavailable_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    rc = gate.check_pin_currency(gate._TAGS_UNAVAILABLE)
    assert rc == 2
    err = capsys.readouterr().err
    assert "failed gate" in err.lower()


def test_pin_check_runs_before_the_network_probe() -> None:
    """Ordering is deliberate: the pin half is local and cheap, so a release
    blocked on an unpinned tag says so without contacting anything."""
    with patch.object(gate, "probe_managed_service") as mock_probe, \
         patch.object(gate, "newest_published_engine",
                      return_value=_bump(REQUIRED_ENGINE_VERSION, 1)):
        rc = gate.check_floor(url=_TEST_URL)
    assert rc == 1
    mock_probe.assert_not_called()


def test_newest_published_engine_parses_the_tag_namespace(tmp_path) -> None:
    """HERMETIC parser check — the important half, decoupled from the checkout.

    The sibling test below reads THIS repo's tags, which conflated two things:
    "the parser works" and "this checkout has tags". A shallow CI checkout
    fetches no tags, so the sibling failed on every push from a797dbd4 onward
    and four commits landed on red CI (nexus-dhs30). This one builds its own
    repo, so the parse bug it exists to catch — parse_engine_version takes
    "0.1.56", NOT "engine-service-v0.1.56", which silently made every tag
    unparseable on this code's first run — is caught in ANY environment.
    """
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    (repo / "f").write_text("x")
    run("git", "add", "f")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i")
    for tag in ("engine-service-v0.1.9", "engine-service-v0.1.56", "engine-service-v0.1.7",
                "v9.9.9", "not-an-engine-tag"):
        run("git", "tag", tag)

    newest = gate.newest_published_engine(repo_root=repo)

    assert newest == (0, 1, 56), newest  # numeric max, not lexicographic
    # The non-engine tags must be ignored, not crash the parse.


def test_newest_published_engine_reads_real_tags() -> None:
    """Non-vacuity: the discovery function must actually parse this repo's tags.

    Guards the bug this code shipped with on its first run — parse_engine_version
    takes a VERSION string, not the `engine-service-vX.Y.Z` tag form, so every
    tag silently failed to parse and the gate reported "zero tags visible".
    """
    newest = gate.newest_published_engine()
    assert newest is not gate._TAGS_UNAVAILABLE, "git tag lookup failed in-repo"
    assert newest is not None, "engine-service-v* tags exist in this repo"
    assert isinstance(newest, tuple) and len(newest) == 3
    assert newest >= (0, 1, 52)


def test_incompatible_service_error_fails_loud(capsys: pytest.CaptureFixture[str]) -> None:
    """The generic ManagedServiceError branch had ZERO coverage.

    Demonstrated by the test-validator: replacing that branch's body with
    `return 0` left all 14 tests green. It is reachable in production —
    probe_managed_service raises ManagedServiceIncompatible (a
    ManagedServiceError, NOT a ManagedServiceUnreachable) for a below-floor,
    missing, or unparseable release_version. An uncovered branch that returns
    the SUCCESS code would report a stale cloud engine as current, which is the
    exact failure this gate exists to prevent.
    """
    from nexus.db.managed_endpoint import ManagedServiceError

    with patch.object(gate, "probe_managed_service",
                      side_effect=ManagedServiceError("release_version 0.0.1 below floor")), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION):
        rc = gate.check_floor(url=_TEST_URL)

    assert rc == 1, "an incompatible managed service must FAIL the gate, not pass it"
    err = capsys.readouterr().err
    assert "FLOOR CHECK FAILED" in err
    assert _floor_str() in err, "the message must name the required floor"
