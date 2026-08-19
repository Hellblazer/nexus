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

import json
import subprocess
from unittest.mock import MagicMock, patch

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
    """Reads THIS repo's tags — and tolerates a checkout that has none.

    nexus-dhs30, and the reason this is a skip rather than an assertion: what it
    can prove depends on the CHECKOUT, not on the code. CI clones shallow with
    no tags, so the strict version failed on every push for four commits while
    the local full-clone run was green — the mechanization's own test broken by
    the environment it runs in. `fetch-tags: true` does NOT fix that: the tags
    point at commits outside a depth-1 history, so the refs never materialise.

    The PARSE bug this was written to catch (parse_engine_version takes
    "0.1.56", not "engine-service-v0.1.56", which silently made every tag
    unparseable on this code's first run) is now caught HERMETICALLY by
    test_newest_published_engine_parses_the_tag_namespace, which builds its own
    repo. So nothing is lost by skipping here — and a skip states the
    environment fact out loud instead of asserting something the environment
    controls.

    NOT made unconditional-skip: where tags DO exist (every developer clone, and
    release.yml, which uses fetch-depth: 0 precisely so the gate can see them),
    this still checks the real repo end to end.
    """
    newest = gate.newest_published_engine()
    if newest is gate._TAGS_UNAVAILABLE or newest is None:
        pytest.skip(
            "checkout has no engine-service-v* tags (shallow CI clone). The "
            "parse path is covered hermetically by "
            "test_newest_published_engine_parses_the_tag_namespace; the release "
            "GATE gets real tags via release.yml's fetch-depth: 0."
        )
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


# ── Paired-release mode (nexus-k1c08) ───────────────────────────────────────
#
# Under the paired-release choreography (Hal directive 2026-08-02, AGENTS.md
# § Cutting a release step 0), a client release bumps REQUIRED_ENGINE_VERSION
# to an engine tag whose deploy fires AT client-tag push. Pre-tag, "cloud
# reports behind floor" is the EXPECTED state under that choreography, not the
# i5c2u/b6qlf 9-day-drift red this gate exists to catch. --paired-deploy TAG
# lets a caller assert "this specific tag is armed" -- but only when TAG
# independently verifies as published (with the SPECIFIC deploy asset),
# exactly pinned, newest, AND fresh (round-1 critique CRITICAL 1: (a)-(c)
# alone are stable facts that never expire, so a reused --paired-deploy on a
# LATER release would pass forever without a freshness bound). Any single
# miss keeps the gate red. These tests patch the git/gh wrapper helpers
# directly (_tag_exists_in_git / _paired_tag_published / _tag_age_hours)
# rather than subprocess.run itself, mirroring how the pre-existing tests
# patch probe_managed_service / newest_published_engine at the same seam;
# the wrapper helpers themselves get dedicated hermetic/subprocess-mocked
# tests further down.

_PAIRED_TAG = f"engine-service-v{_floor_str()}"

#: Round-1 fix: every test that needs to reach the cloud probe past all four
#: preconditions must now also stub the freshness check -- a real (unmocked)
#: call would run `git log` against this checkout's actual history for a tag
#: that likely doesn't exist, which fails closed (rc 2) rather than reaching
#: the probe. 1.0h is comfortably inside the default 72h window.
_FRESH_AGE_HOURS = 1.0
_STALE_AGE_HOURS = 200.0


def test_paired_mode_accepts_cloud_behind_when_all_conditions_hold(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PAIRED MODE" in out
    assert "0.0.1" in out
    assert _floor_str() in out


def test_paired_mode_tag_missing_from_git_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=False):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 1
    assert "does not exist in git" in capsys.readouterr().err


def test_paired_mode_git_unavailable_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=gate._TAGS_UNAVAILABLE):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 2
    assert "UNVERIFIABLE" in capsys.readouterr().err


def test_paired_mode_gh_unavailable_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(
             gate, "_paired_tag_published",
             return_value=(gate._TAGS_UNAVAILABLE, "could not invoke `gh`"),
         ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 2
    assert "UNVERIFIABLE" in capsys.readouterr().err


def test_paired_mode_draft_release_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(
             gate, "_paired_tag_published",
             return_value=(False, f"release {_PAIRED_TAG} is still a DRAFT -- not published"),
         ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 1
    assert "DRAFT" in capsys.readouterr().err


def test_paired_mode_missing_deploy_asset_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(
             gate, "_paired_tag_published",
             return_value=(
                 False,
                 f"release {_PAIRED_TAG} has no `{gate._REQUIRED_ASSET_NAME}` asset -- "
                 "the binary conexus deploy actually consumes has not landed "
                 "(assets present: nexus-pg-linux-amd64.txz)",
             ),
         ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 1
    err = capsys.readouterr().err
    assert gate._REQUIRED_ASSET_NAME in err


def test_paired_mode_wrong_pairing_floor_mismatch_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    other_tag = f"engine-service-v{'.'.join(str(p) for p in _bump(REQUIRED_ENGINE_VERSION, 1))}"
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=other_tag
        )
    assert rc == 1
    err = capsys.readouterr().err
    assert "wrong pairing" in err.lower()


def test_paired_mode_newer_tag_exists_fails(capsys: pytest.CaptureFixture[str]) -> None:
    newer = _bump(REQUIRED_ENGINE_VERSION, 1)
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")):
        rc = gate.check_floor(url=_TEST_URL, newest=newer, paired_deploy=_PAIRED_TAG)
    assert rc == 1
    err = capsys.readouterr().err
    assert "newer engine tag" in err.lower()


def test_paired_mode_unreachable_stays_rc2(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(
             gate, "probe_managed_service",
             side_effect=ManagedServiceUnreachable("connect timed out"),
         ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 2
    err = capsys.readouterr().err
    assert "unreachable" in err.lower()
    assert "PAIRED MODE" not in err


def test_paired_mode_at_floor_passes_with_normal_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps(_floor_str())):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PAIRED MODE" not in out
    assert "current" in out.lower()


def test_paired_mode_above_floor_passes_with_normal_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    above = (REQUIRED_ENGINE_VERSION[0], REQUIRED_ENGINE_VERSION[1], REQUIRED_ENGINE_VERSION[2] + 1)
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(
             gate, "probe_managed_service",
             return_value=_caps(".".join(str(p) for p in above)),
         ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 0
    assert "PAIRED MODE" not in capsys.readouterr().out


def test_paired_mode_generic_managed_service_error_stays_unverifiable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review-round fix (SIGNIFICANT finding 3): a plain ManagedServiceError
    with NO structured deployed_version (non-200, malformed JSON, etc.) is
    NOT a genuine below-floor reading and must NOT be folded into paired
    acceptance, even in explicit --paired-deploy mode. This test used to
    assert the opposite (rc == 0, accepted) -- that assertion pinned the bug
    this fix closes; see _classify_probe_failure."""
    from nexus.db.managed_endpoint import ManagedServiceError

    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(
             gate, "probe_managed_service",
             side_effect=ManagedServiceError("service returned HTTP 503"),
         ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 2
    out_err = capsys.readouterr()
    assert "PAIRED MODE" not in out_err.out
    assert "UNVERIFIABLE" in out_err.err
    assert "genuine below-floor" in out_err.err.lower()


def test_paired_mode_ack_uses_structured_deployed_version_not_full_sentence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Round-1 critique SIGNIFICANT: probe_managed_service raises
    ManagedServiceIncompatible (a ManagedServiceError) BEFORE check_floor's
    own explicit comparison ever runs on the real path -- so the acceptance
    ack must be built from that exception's structured deployed_version
    field, not str(exc), or the ack embeds the whole remedy sentence via
    !r."""
    from nexus.db.managed_endpoint import ManagedServiceIncompatible

    full_sentence = (
        f"managed nexus service at {_TEST_URL} is release_version '0.1.17', "
        f"below the minimum required v{_floor_str()}. Upgrade the managed "
        "service, or upgrade/downgrade the nx client to match."
    )
    exc = ManagedServiceIncompatible(
        full_sentence, deployed_version="0.1.17", required_version=_floor_str()
    )
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", side_effect=exc):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PAIRED MODE" in out
    assert "'0.1.17'" in out  # the clean structured version, via repr
    # The full remedy sentence must NOT leak into the acknowledgment.
    assert "Upgrade the managed service" not in out
    assert "below the minimum required" not in out


def test_paired_mode_acknowledgment_names_post_tag_verify(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")):
        gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    out = capsys.readouterr().out
    assert "post-tag verify" in out.lower()
    assert "--paired-deploy" in out
    assert "re-run this script" in out.lower()


def test_paired_mode_rejects_non_engine_tag(capsys: pytest.CaptureFixture[str]) -> None:
    rc = gate.check_floor(
        url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy="v9.9.9"
    )
    assert rc == 1
    assert "engine-service-v" in capsys.readouterr().err


def test_paired_mode_rejects_unparseable_tag(capsys: pytest.CaptureFixture[str]) -> None:
    rc = gate.check_floor(
        url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION,
        paired_deploy="engine-service-vSNAPSHOT",
    )
    assert rc == 1
    assert "does not parse" in capsys.readouterr().err.lower()


def test_default_mode_unaffected_by_paired_deploy_absence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression pin: paired_deploy=None (the implicit default) must take the
    exact pre-k1c08 code path -- no PAIRED MODE text anywhere, same rc as
    test_main_returns_nonzero_on_stale_engine."""
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION):
        rc = gate.check_floor(url=_TEST_URL)
    assert rc == 1
    err = capsys.readouterr().err
    assert "PAIRED MODE" not in err
    assert "FLOOR CHECK FAILED" in err


def test_main_accepts_paired_deploy_flag(capsys: pytest.CaptureFixture[str]) -> None:
    # check_source_ancestry stubbed: this test targets the paired-deploy
    # FLAG plumbing (nexus-k1c08), not the nexus-hs4xl ancestry arm, which
    # has its own dedicated tests below and would otherwise run real `git
    # diff` against this checkout's actual (possibly source-stale, see
    # nexus-ajlz5) history.
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry", return_value=0) as mock_ancestry:
        rc = gate.main(["--url", _TEST_URL, "--paired-deploy", _PAIRED_TAG])
    assert rc == 0
    assert "PAIRED MODE" in capsys.readouterr().out
    # And the wiring itself: the paired tag, not the (unbumped) floor tag,
    # must be what gets ancestry-checked -- see the dedicated wiring tests
    # below for the reasoning.
    mock_ancestry.assert_called_once_with(_PAIRED_TAG)


def test_main_accepts_paired_tag_max_age_hours_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """The override flag must actually reach check_paired_preconditions --
    verified by making it the ONLY thing that turns a stale-tag rejection
    into an acceptance."""
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_STALE_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry", return_value=0):
        rc_default_window = gate.main(["--url", _TEST_URL, "--paired-deploy", _PAIRED_TAG])
        capsys.readouterr()
        rc_overridden = gate.main([
            "--url", _TEST_URL, "--paired-deploy", _PAIRED_TAG,
            "--paired-tag-max-age-hours", "500",
        ])
    assert rc_default_window == 1
    assert rc_overridden == 0


# ── Paired-tag freshness window (nexus-k1c08 fix round, critique CRITICAL 1) ─
#
# (a)-(c) are otherwise STABLE facts once armed: they stay true indefinitely
# if no further engine tag is cut, so a reused --paired-deploy on a LATER
# release (the promised post-tag VERIFY skipped, or the deploy silently
# failed) would get the IDENTICAL acceptance forever without this bound --
# reopening the i5c2u multi-release drift class, now mechanically approved.


def test_paired_mode_fresh_tag_passes(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 0


def test_paired_mode_stale_tag_fails_with_named_age(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_STALE_AGE_HOURS):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 1
    err = capsys.readouterr().err
    assert f"{_STALE_AGE_HOURS:.1f}h" in err
    assert f"{gate._DEFAULT_PAIRED_TAG_MAX_AGE_HOURS:.1f}h" in err
    assert "i5c2u" in err


def test_paired_mode_age_unavailable_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=gate._TAGS_UNAVAILABLE):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG
        )
    assert rc == 2
    assert "UNVERIFIABLE" in capsys.readouterr().err


def test_paired_mode_stale_tag_override_flag_honored(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_STALE_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG,
            paired_tag_max_age_hours=_STALE_AGE_HOURS + 1,
        )
    assert rc == 0


def test_paired_mode_default_window_unchanged_at_72h() -> None:
    """Pins the default so a future edit can't silently loosen/tighten it."""
    assert gate._DEFAULT_PAIRED_TAG_MAX_AGE_HOURS == 72.0


# ── Paired-mode git/gh wrapper helpers, hermetic ────────────────────────────


def test_tag_exists_in_git_hermetic(tmp_path) -> None:
    """Same hermetic-tmp-repo pattern as test_newest_published_engine_parses_
    the_tag_namespace -- decoupled from whatever tags this checkout has."""
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    (repo / "f").write_text("x")
    run("git", "add", "f")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i")
    run("git", "tag", "engine-service-v0.1.9")

    assert gate._tag_exists_in_git("engine-service-v0.1.9", repo_root=repo) is True
    assert gate._tag_exists_in_git("engine-service-v9.9.9", repo_root=repo) is False


def test_tag_exists_in_git_unavailable_when_git_missing(tmp_path) -> None:
    with patch.object(gate.subprocess, "run", side_effect=FileNotFoundError("no git")):
        result = gate._tag_exists_in_git("engine-service-v0.1.9", repo_root=tmp_path)
    assert result is gate._TAGS_UNAVAILABLE


def test_tag_age_hours_hermetic(tmp_path) -> None:
    """A commit tagged ~now must report an age near zero and well under 72h."""
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(a, cwd=repo, check=True, capture_output=True)  # noqa: E731
    run("git", "init", "-q")
    (repo / "f").write_text("x")
    run("git", "add", "f")
    run("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "i")
    run("git", "tag", "engine-service-v0.1.9")

    age = gate._tag_age_hours("engine-service-v0.1.9", repo_root=repo)
    assert isinstance(age, float)
    assert 0.0 <= age < 1.0


def test_tag_age_hours_unavailable_for_unknown_tag(tmp_path) -> None:
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)

    assert gate._tag_age_hours("engine-service-v9.9.9", repo_root=repo) is gate._TAGS_UNAVAILABLE


def test_tag_age_hours_unavailable_when_git_missing(tmp_path) -> None:
    with patch.object(gate.subprocess, "run", side_effect=FileNotFoundError("no git")):
        result = gate._tag_age_hours("engine-service-v0.1.9", repo_root=tmp_path)
    assert result is gate._TAGS_UNAVAILABLE


def test_paired_tag_published_parses_gh_json() -> None:
    fake = MagicMock(
        returncode=0,
        stdout=json.dumps({"isDraft": False, "assets": [{"name": gate._REQUIRED_ASSET_NAME}]}),
        stderr="",
    )
    with patch.object(gate.subprocess, "run", return_value=fake):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is True
    assert reason == ""


def test_paired_tag_published_detects_draft() -> None:
    fake = MagicMock(
        returncode=0,
        stdout=json.dumps({"isDraft": True, "assets": [{"name": gate._REQUIRED_ASSET_NAME}]}),
        stderr="",
    )
    with patch.object(gate.subprocess, "run", return_value=fake):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is False
    assert "DRAFT" in reason


def test_paired_tag_published_detects_zero_assets() -> None:
    fake = MagicMock(returncode=0, stdout=json.dumps({"isDraft": False, "assets": []}), stderr="")
    with patch.object(gate.subprocess, "run", return_value=fake):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is False
    assert gate._REQUIRED_ASSET_NAME in reason
    assert "none" in reason.lower()


def test_paired_tag_published_requires_specific_binary_asset() -> None:
    """Round-1 critique CRITICAL 2: engine-service-release.yml's own comments
    document that both its asset-producing matrices run fail-fast: false, so
    a non-draft release can carry real assets (a PG bundle, sha256/cosign
    sidecars) while shipping ZERO native binaries. Bare non-empty is too
    weak -- only the specific asset conexus deploy consumes proves the
    pairing is real."""
    fake = MagicMock(
        returncode=0,
        stdout=json.dumps({
            "isDraft": False,
            "assets": [
                {"name": "nexus-pg-linux-amd64.txz"},
                {"name": "nexus-pg-linux-amd64.txz.sha256"},
            ],
        }),
        stderr="",
    )
    with patch.object(gate.subprocess, "run", return_value=fake):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is False
    assert gate._REQUIRED_ASSET_NAME in reason
    assert "nexus-pg-linux-amd64.txz" in reason  # names what WAS present


def test_paired_tag_published_binary_asset_present_passes() -> None:
    fake = MagicMock(
        returncode=0,
        stdout=json.dumps({
            "isDraft": False,
            "assets": [
                {"name": "nexus-pg-linux-amd64.txz"},
                {"name": gate._REQUIRED_ASSET_NAME},
                {"name": f"{gate._REQUIRED_ASSET_NAME}.sha256"},
            ],
        }),
        stderr="",
    )
    with patch.object(gate.subprocess, "run", return_value=fake):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is True
    assert reason == ""


def test_paired_tag_published_missing_isdraft_key_fails_closed() -> None:
    """Round-1 code-review IMPORTANT: `payload.get("isDraft")` defaulting
    falsy on a missing key would silently treat 'gh's response shape
    changed' as not-draft (a pass) -- must be unverifiable instead."""
    fake = MagicMock(
        returncode=0,
        stdout=json.dumps({"assets": [{"name": gate._REQUIRED_ASSET_NAME}]}),
        stderr="",
    )
    with patch.object(gate.subprocess, "run", return_value=fake):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is gate._TAGS_UNAVAILABLE
    assert "isDraft" in reason


def test_paired_tag_published_gh_missing_fails_closed_with_remedy() -> None:
    """Round-1 code-review IMPORTANT: the FileNotFoundError message must
    state the remedy (install/auth gh), not just 'could not invoke'."""
    with patch.object(gate.subprocess, "run", side_effect=FileNotFoundError("gh not found")):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is gate._TAGS_UNAVAILABLE
    assert "could not invoke" in reason
    assert "gh auth login" in reason or "install" in reason.lower()


def test_paired_tag_published_gh_nonzero_exit_fails_closed() -> None:
    fake = MagicMock(returncode=1, stdout="", stderr="release not found")
    with patch.object(gate.subprocess, "run", return_value=fake):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is gate._TAGS_UNAVAILABLE
    assert "release not found" in reason


def test_paired_tag_published_unparseable_json_fails_closed() -> None:
    fake = MagicMock(returncode=0, stdout="not json", stderr="")
    with patch.object(gate.subprocess, "run", return_value=fake):
        ok, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert ok is gate._TAGS_UNAVAILABLE
    assert "unparseable" in reason.lower()


def test_paired_tag_published_anchors_gh_call_to_repo_root(tmp_path) -> None:
    """Round-1 code-review IMPORTANT: unlike its git siblings
    (_tag_exists_in_git, newest_published_engine), _paired_tag_published's
    gh call previously omitted cwd= anchoring -- gh would silently resolve
    whatever repo it auto-detects from the process's real cwd instead of
    failing closed the way the git helpers do."""
    fake = MagicMock(
        returncode=0,
        stdout=json.dumps({"isDraft": False, "assets": [{"name": gate._REQUIRED_ASSET_NAME}]}),
        stderr="",
    )
    with patch.object(gate.subprocess, "run", return_value=fake) as mock_run:
        gate._paired_tag_published(_PAIRED_TAG, repo_root=tmp_path)
    _, kwargs = mock_run.call_args
    assert kwargs.get("cwd") == tmp_path


def test_paired_tag_published_defaults_repo_root_to_module_parent() -> None:
    fake = MagicMock(
        returncode=0,
        stdout=json.dumps({"isDraft": False, "assets": [{"name": gate._REQUIRED_ASSET_NAME}]}),
        stderr="",
    )
    expected_root = gate.pathlib.Path(gate.__file__).resolve().parent.parent
    with patch.object(gate.subprocess, "run", return_value=fake) as mock_run:
        gate._paired_tag_published(_PAIRED_TAG)
    _, kwargs = mock_run.call_args
    assert kwargs.get("cwd") == expected_root


# ── Source-ancestry arm (nexus-hs4xl) ───────────────────────────────────────
#
# check_pin_currency and the cloud probe both compare VERSION NUMBERS. v7.6.1
# proved that insufficient: it pinned engine-service-v0.1.71 -- current by
# every number the gate compared -- while shipping 156 insertions of
# service/src/main Java (the RDR-191 F10c producer fixes) that v0.1.71's tag
# does not contain. check_source_ancestry closes that gap by diffing the
# ACTUAL source tree between the pinned tag and HEAD.


def _git_repo_with_scoped_history(tmp_path):
    """A scratch repo with a tagged commit, a fixture for building either a
    clean or a drifted history on top of it. Returns (repo_path, run)."""
    repo = tmp_path / "r"
    (repo / "service" / "src" / "main" / "java").mkdir(parents=True)
    (repo / "service" / "src" / "test" / "java").mkdir(parents=True)

    def run(*args):
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t.invalid", "-c", "user.name=t", *args],
            capture_output=True, text=True, check=True,
        )

    (repo / "service" / "src" / "main" / "java" / "A.java").write_text("class A {}\n")
    (repo / "service" / "src" / "test" / "java" / "ATest.java").write_text("class ATest {}\n")
    run("init", "-q")
    run("add", ".")
    run("commit", "-q", "-m", "base")
    run("tag", "engine-service-v9.9.9")
    return repo, run


def test_source_ancestry_clean_at_the_tag_passes(
    tmp_path, capsys: pytest.CaptureFixture[str],
) -> None:
    repo, _run = _git_repo_with_scoped_history(tmp_path)
    rc = gate.check_source_ancestry("engine-service-v9.9.9", repo_root=repo)
    assert rc == 0
    assert "current" in capsys.readouterr().out.lower()


def test_source_ancestry_in_scope_drift_fails_and_names_the_file(
    tmp_path, capsys: pytest.CaptureFixture[str],
) -> None:
    repo, run = _git_repo_with_scoped_history(tmp_path)
    (repo / "service" / "src" / "main" / "java" / "A.java").write_text("class A { int x; }\n")
    run("commit", "-aq", "-m", "drift main")

    rc = gate.check_source_ancestry("engine-service-v9.9.9", repo_root=repo)

    assert rc == 1
    err = capsys.readouterr().err
    assert "SOURCE-ANCESTRY CHECK FAILED" in err
    assert "A.java" in err
    assert "engine-service-v9.9.9" in err


def test_source_ancestry_out_of_scope_drift_is_not_flagged(
    tmp_path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Test-only churn must NOT redden this gate (bead design question 1) --
    only service/src/main/java/A.java's PRODUCTION scope counts."""
    repo, run = _git_repo_with_scoped_history(tmp_path)
    (repo / "service" / "src" / "test" / "java" / "ATest.java").write_text(
        "class ATest { void t() {} }\n"
    )
    run("commit", "-aq", "-m", "drift test-only")

    rc = gate.check_source_ancestry("engine-service-v9.9.9", repo_root=repo)

    assert rc == 0
    assert "SOURCE-ANCESTRY CHECK FAILED" not in capsys.readouterr().err


def test_source_ancestry_missing_tag_fails_closed(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    repo, _run = _git_repo_with_scoped_history(tmp_path)
    rc = gate.check_source_ancestry("engine-service-v0.0.0-nonexistent", repo_root=repo)
    assert rc == 2
    err = capsys.readouterr().err
    assert "UNVERIFIABLE" in err
    assert "does not exist" in err


def test_source_ancestry_git_unavailable_fails_closed(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=gate._TAGS_UNAVAILABLE):
        rc = gate.check_source_ancestry("engine-service-v9.9.9")
    assert rc == 2
    assert "UNVERIFIABLE" in capsys.readouterr().err


def test_pinned_engine_tag_derives_from_the_floor_constant() -> None:
    expected = "engine-service-v" + ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
    assert gate._pinned_engine_tag() == expected


# ── MANDATORY REGRESSION PIN: v7.6.1 + v0.1.71 must be RED (nexus-hs4xl) ───
#
# "Whatever ships must be proven to FAIL against the v7.6.1 tree. A gate for
# this class that passes on the tree that motivated it is vacuous." This
# targets THIS repository's real, already-pushed tags directly -- skipped
# (never xfailed) when a shallow/tagless checkout cannot see them, same
# doctrine as test_newest_published_engine_reads_real_tags above.
#
# `integration` + `mandatory_regression_pin` (nexus-93j33, review follow-up
# 2026-08-12): unlike the sibling `test_newest_published_engine_reads_real_
# tags` above -- whose own docstring justifies an unconditional skip because
# hermetic coverage exists elsewhere for the LOGIC it would otherwise check
# -- this test pins a SPECIFIC historical regression with no hermetic
# equivalent, so a silent skip here is a real coverage loss, not a
# documented redundancy. ci.yml's `test` job deliberately does NOT fetch
# tags (nexus-dhs30 -- `fetch-tags: true` was tried there and rejected: the
# engine-service-v* tags point at commits outside a depth-1 history and
# never materialise), so this test structurally cannot resolve
# `engine-service-v0.1.71` in that job and must live in `integration`
# instead, same as the sibling live-API pins in
# test_check_release_ci_evidence.py. `mandatory_regression_pin` is what
# turns "silently skipped every time `-m integration` runs without tags"
# into a failed run instead of a green one -- see tests/conftest.py's
# `_check_mandatory_pin_non_vacuity`.
@pytest.mark.integration
@pytest.mark.mandatory_regression_pin
def test_v7_6_1_source_ancestry_regression_is_red() -> None:
    check = subprocess.run(
        ["git", "tag", "-l", "v7.6.1", "engine-service-v0.1.71"],
        capture_output=True, text=True,
    )
    seen = set(check.stdout.split())
    if not {"v7.6.1", "engine-service-v0.1.71"} <= seen:
        pytest.skip(
            "checkout is missing v7.6.1 and/or engine-service-v0.1.71 "
            "(shallow CI clone) -- the scoping/logic behavior is covered "
            "hermetically above; this pins the SPECIFIC historical "
            "regression where it is observable."
        )
    diff = subprocess.run(
        ["git", "diff", "--stat", "engine-service-v0.1.71", "v7.6.1", "--",
         gate._ANCESTRY_SCOPE],
        capture_output=True, text=True, check=True,
    )
    assert diff.stdout.strip(), (
        "expected v7.6.1 to carry service/src/main source that "
        "engine-service-v0.1.71 lacks (nexus-ajlz5) -- if this is empty the "
        "historical fixture this regression pins no longer holds and the "
        "test should be re-evaluated, not silently passed"
    )
    rc = gate.check_source_ancestry("engine-service-v0.1.71")
    assert rc == 1, (
        "the source-ancestry gate must flag v7.6.1 as RED against its own "
        "pinned engine tag -- this is the exact drift nexus-ajlz5 shipped "
        "and nexus-hs4xl exists to catch"
    )


# ── main() wiring: the ancestry arm must actually run, on the right tag ────


def test_main_runs_ancestry_check_after_a_clean_floor_default_mode() -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps(_floor_str())), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry", return_value=0) as mock_ancestry:
        rc = gate.main(["--url", _TEST_URL])
    assert rc == 0
    mock_ancestry.assert_called_once_with(gate._pinned_engine_tag())


def test_main_propagates_ancestry_failure_even_when_floor_is_clean() -> None:
    """A version-current floor must NOT mask a source-stale one -- the whole
    point of nexus-hs4xl."""
    with patch.object(gate, "probe_managed_service", return_value=_caps(_floor_str())), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry", return_value=1):
        rc = gate.main(["--url", _TEST_URL])
    assert rc == 1


def test_main_skips_ancestry_check_when_floor_already_failed() -> None:
    """CI cost discipline / ordering: don't shell out to git diff when the
    cheap, already-failing check settled the verdict."""
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry") as mock_ancestry:
        rc = gate.main(["--url", _TEST_URL])
    assert rc == 1
    mock_ancestry.assert_not_called()


# ── nexus-1vogq: both-halves wire-contract client-lag ledger gate ─────────
# The paired-deploy path's complement to scripts/check_wire_contract_pairing.py's
# static tripwire: a non-empty ## Unshipped section must block the deploy by
# NAME unless every entry is explicitly acknowledged via --ack-client-lag.


def _write_ledger(tmp_path, entry: str | None = None):
    ledger = tmp_path / "wire-contract-pending.md"
    body = entry or "(none)\n"
    ledger.write_text(f"## Unshipped\n\n{body}\n## Shipped\n")
    return ledger


_FAKE_ENTRY = (
    "- `deadbeefdeadbeefdeadbeefdeadbeefdeadbeef` -- bead nexus-fake -- "
    "engine tag `engine-service-v9.9.9` -- test fixture\n"
)


def test_client_lag_ledger_empty_passes(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    ledger = _write_ledger(tmp_path)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger):
        rc = gate.check_client_lag_ledger()
    assert rc == 0
    assert "client-lag ledger clean" in capsys.readouterr().out


def test_client_lag_ledger_blocks_unacknowledged_entry(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger):
        rc = gate.check_client_lag_ledger()
    assert rc == 1
    err = capsys.readouterr().err
    assert "nexus-fake" in err
    assert "PAIRED DEPLOY BLOCKED" in err
    assert "deadbeef" in err


def test_client_lag_ledger_ack_by_bead_id_passes(tmp_path) -> None:
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger):
        rc = gate.check_client_lag_ledger(["nexus-fake"])
    assert rc == 0


def test_client_lag_ledger_wrong_ack_still_blocks(tmp_path) -> None:
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger):
        rc = gate.check_client_lag_ledger(["nexus-other"])
    assert rc == 1


def test_client_lag_ledger_partial_ack_still_blocks(tmp_path) -> None:
    """Two entries, only one acknowledged -- must still block, naming the
    unacknowledged one."""
    two_entries = _FAKE_ENTRY + (
        "- `cafef00dcafef00dcafef00dcafef00dcafef00d` -- bead nexus-other -- "
        "engine tag `engine-service-v9.9.9` -- second fixture\n"
    )
    ledger = _write_ledger(tmp_path, two_entries)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger):
        rc = gate.check_client_lag_ledger(["nexus-fake"])
    assert rc == 1


def test_check_floor_paired_mode_blocked_by_ledger_before_precondition_check(
    tmp_path,
) -> None:
    """Ledger gate runs FIRST -- check_paired_preconditions must not even be
    reached when the ledger blocks (cheap local check before anything else,
    same ordering discipline as pin-currency in default mode)."""
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger), \
         patch.object(gate, "check_paired_preconditions") as mock_precond:
        rc = gate.check_floor(paired_deploy="engine-service-v9.9.9")
    assert rc == 1
    mock_precond.assert_not_called()


def test_check_floor_paired_mode_proceeds_when_ledger_clean(tmp_path) -> None:
    ledger = _write_ledger(tmp_path)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger), \
         patch.object(gate, "check_paired_preconditions", return_value=1) as mock_precond:
        rc = gate.check_floor(paired_deploy="engine-service-v9.9.9")
    assert rc == 1
    mock_precond.assert_called_once()


def test_check_floor_paired_mode_proceeds_when_ledger_acknowledged(tmp_path) -> None:
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger), \
         patch.object(gate, "check_paired_preconditions", return_value=1) as mock_precond:
        rc = gate.check_floor(
            paired_deploy="engine-service-v9.9.9", ack_client_lag=["nexus-fake"]
        )
    assert rc == 1
    mock_precond.assert_called_once()


def test_default_mode_never_consults_client_lag_ledger(tmp_path) -> None:
    """Non-paired mode is unaffected -- the ledger gate is a paired-deploy-only
    precondition, not a general floor-check requirement."""
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger), \
         patch.object(gate, "check_client_lag_ledger") as mock_ledger, \
         patch.object(gate, "probe_managed_service", return_value=_caps(_floor_str())), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry", return_value=0):
        gate.main(["--url", _TEST_URL])
    mock_ledger.assert_not_called()


def test_main_accepts_ack_client_lag_flag() -> None:
    with patch.object(gate, "check_client_lag_ledger", return_value=0) as mock_ledger, \
         patch.object(gate, "check_paired_preconditions", return_value=1):
        gate.main(
            [
                "--paired-deploy", "engine-service-v0.1.1",
                "--ack-client-lag", "nexus-fake",
                "--ack-client-lag", "nexus-other",
            ]
        )
    mock_ledger.assert_called_once_with(["nexus-fake", "nexus-other"])


# ── Auto-paired mode (--paired-deploy-auto, nexus-gc9ir) ──────────────────
#
# v7.10.0 (2026-08-18): release.yml runs check_engine_release_floor.py BARE,
# with no human to type --paired-deploy <tag>, so a routine paired release's
# EXPECTED pre-deploy cloud-behind state red'd the publish. --paired-deploy-
# auto derives the candidate tag from REQUIRED_ENGINE_VERSION and, ONLY when
# the cloud is confirmed below that floor, runs the IDENTICAL verification
# battery --paired-deploy already applies. These tests patch the same seams
# as the --paired-deploy tests above (_tag_exists_in_git /
# _paired_tag_published / _tag_age_hours / probe_managed_service) plus
# check_paired_preconditions / check_client_lag_ledger directly where the
# wiring itself (not the underlying logic, already covered above) is what's
# under test.


def test_auto_paired_derives_tag_from_required_engine_version() -> None:
    """The candidate tag must be _pinned_engine_tag() -- derived, never a
    separately hand-typed literal (same discipline as PINNED_SERVICE_TAG)."""
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "check_client_lag_ledger", return_value=0), \
         patch.object(gate, "check_paired_preconditions", return_value=0) as mock_precond:
        rc = gate.check_floor(
            url=_TEST_URL, newest=_PIN_CURRENT, paired_deploy_auto=True,
        )
    assert rc == 0
    mock_precond.assert_called_once()
    called_tag = mock_precond.call_args[0][0]
    assert called_tag == gate._pinned_engine_tag()


def test_auto_paired_cloud_meets_floor_is_normal_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The headline 'must not weaken anything' contract: when the cloud
    already meets the floor, auto mode is a byte-for-byte bare-invocation
    pass -- the paired machinery (ledger, git/gh tag verification) is never
    even invoked."""
    with patch.object(gate, "probe_managed_service", return_value=_caps(_floor_str())), \
         patch.object(gate, "check_client_lag_ledger") as mock_ledger, \
         patch.object(gate, "check_paired_preconditions") as mock_precond:
        rc = gate.check_floor(
            url=_TEST_URL, newest=_PIN_CURRENT, paired_deploy_auto=True,
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PAIRED MODE" not in out
    assert "current" in out.lower()
    mock_ledger.assert_not_called()
    mock_precond.assert_not_called()


def test_auto_paired_cloud_meets_floor_still_enforces_pin_currency(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auto mode must not skip the pin-currency direction either -- an
    unpinned newer tag still fails the gate even when the cloud is current."""
    newer = _bump(REQUIRED_ENGINE_VERSION, 3)
    with patch.object(gate, "probe_managed_service", return_value=_caps(_floor_str())):
        rc = gate.check_floor(
            url=_TEST_URL, newest=newer, paired_deploy_auto=True,
        )
    assert rc == 1
    err = capsys.readouterr().err
    assert "ENGINE PIN CHECK FAILED" in err


def test_auto_paired_below_floor_all_conditions_met_passes_with_auto_ack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PAIRED MODE" in out
    assert "0.0.1" in out
    assert _floor_str() in out
    assert "AUTO-derived" in out
    assert "--paired-deploy-auto" in out


def test_auto_paired_below_floor_managed_service_error_uses_structured_version(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """probe_managed_service fails closed (raises ManagedServiceIncompatible)
    on a below-floor release_version on the real path -- auto mode must
    accept via that exception branch too, same as the explicit flag."""
    from nexus.db.managed_endpoint import ManagedServiceIncompatible

    exc = ManagedServiceIncompatible(
        "full remedy sentence not for the ack", deployed_version="0.1.5",
        required_version=_floor_str(),
    )
    with patch.object(gate, "probe_managed_service", side_effect=exc), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "'0.1.5'" in out
    assert "full remedy sentence" not in out


def test_auto_paired_generic_managed_service_error_stays_unverifiable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review-round fix (SIGNIFICANT finding 3), auto-mode side: a plain
    ManagedServiceError with no structured deployed_version (endpoint
    error, malformed response) must NOT be folded into paired acceptance --
    same rule as the explicit --paired-deploy path
    (test_paired_mode_generic_managed_service_error_stays_unverifiable)."""
    from nexus.db.managed_endpoint import ManagedServiceError

    with patch.object(
        gate, "probe_managed_service",
        side_effect=ManagedServiceError("managed service returned HTTP 503"),
    ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 2
    out_err = capsys.readouterr()
    assert "PAIRED MODE" not in out_err.out
    assert "UNVERIFIABLE" in out_err.err
    assert "genuine below-floor" in out_err.err.lower()


def test_auto_paired_unparseable_release_version_stays_unverifiable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Defense-in-depth: the REAL probe never returns a caps with an
    unparseable release_version (it raises first), but the post-probe
    comparison branch must not silently accept one either if reached."""
    with patch.object(gate, "probe_managed_service", return_value=_caps("not-a-version")):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 2
    out_err = capsys.readouterr()
    assert "PAIRED MODE" not in out_err.out
    assert "UNVERIFIABLE" in out_err.err
    assert "unparseable" in out_err.err.lower()


def test_paired_mode_unparseable_release_version_stays_unverifiable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Explicit-mode mirror of the auto-mode test above."""
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps("not-a-version")):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy=_PAIRED_TAG,
        )
    assert rc == 2
    out_err = capsys.readouterr()
    assert "PAIRED MODE" not in out_err.out
    assert "UNVERIFIABLE" in out_err.err
    assert "unparseable" in out_err.err.lower()


def test_auto_paired_below_floor_accepts_without_deploy_liveness_signal_by_design(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """DELIBERATE-BEHAVIOR PIN (review round finding 2) -- do not "fix" this
    without a conscious decision: auto mode accepts a below-floor cloud on
    TAG legitimacy alone (published, exactly pinned, newest, fresh). It has
    NO way to observe whether the deploy relay actually fired or converged
    -- that is a real, accepted gap, not an oversight. The backstop is the
    pre-existing DAILY engine-floor-verify job
    (.github/workflows/scheduled-failure-watch.yml, bare gate against the
    real public endpoint, protocol-audit [22511] Gap 2), which would catch
    a still-stale cloud within at most 24h via the "Scheduled workflows are
    failing silently" tracked GH issue. If a future change adds a
    deploy-liveness signal to THIS gate, this test must be updated
    deliberately -- its failure is the tripwire for that decision, not a
    bug to silence."""
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 0
    assert "PAIRED MODE" in capsys.readouterr().out


def test_auto_paired_below_floor_draft_release_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(
             gate, "_paired_tag_published",
             return_value=(False, f"release {gate._pinned_engine_tag()} is still a DRAFT -- not published"),
         ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 1
    assert "DRAFT" in capsys.readouterr().err


def test_auto_paired_below_floor_newer_tag_exists_fails(capsys: pytest.CaptureFixture[str]) -> None:
    newer = _bump(REQUIRED_ENGINE_VERSION, 1)
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")):
        rc = gate.check_floor(url=_TEST_URL, newest=newer, paired_deploy_auto=True)
    assert rc == 1
    assert "newer engine tag" in capsys.readouterr().err.lower()


def test_auto_paired_below_floor_stale_tag_fails(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_STALE_AGE_HOURS):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 1
    err = capsys.readouterr().err
    assert f"{_STALE_AGE_HOURS:.1f}h" in err


def test_auto_paired_below_floor_gh_unavailable_fails_closed(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(
             gate, "_paired_tag_published",
             return_value=(gate._TAGS_UNAVAILABLE, "could not invoke `gh`"),
         ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 2
    assert "UNVERIFIABLE" in capsys.readouterr().err


def test_auto_paired_below_floor_ledger_blocks_before_preconditions(tmp_path) -> None:
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger), \
         patch.object(gate, "check_paired_preconditions") as mock_precond:
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 1
    mock_precond.assert_not_called()


def test_auto_paired_unreachable_fails_rc2_no_paired_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch.object(
        gate, "probe_managed_service",
        side_effect=ManagedServiceUnreachable("connect timed out"),
    ):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True,
        )
    assert rc == 2
    err = capsys.readouterr().err
    assert "unreachable" in err.lower()
    assert "PAIRED MODE" not in err


def test_default_mode_unaffected_by_paired_deploy_auto_absence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """paired_deploy_auto=False (the implicit default) must take the exact
    pre-gc9ir code path."""
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION):
        rc = gate.check_floor(url=_TEST_URL)
    assert rc == 1
    err = capsys.readouterr().err
    assert "PAIRED MODE" not in err
    assert "FLOOR CHECK FAILED" in err


def test_explicit_paired_deploy_takes_priority_over_auto_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When (in Python-API use, not CLI, which enforces mutual exclusion)
    both are set, the explicit tag wins -- library-level tiebreak documented
    on check_floor."""
    other_tag = f"engine-service-v{'.'.join(str(p) for p in _bump(REQUIRED_ENGINE_VERSION, 2))}"
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")):
        rc = gate.check_floor(
            url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION,
            paired_deploy=other_tag, paired_deploy_auto=True,
        )
    # other_tag != REQUIRED_ENGINE_VERSION -> explicit-mode "wrong pairing"
    # rejection, proving the explicit tag (not the auto-derived one) drove
    # the check.
    assert rc == 1
    assert "wrong pairing" in capsys.readouterr().err.lower()


def test_main_accepts_paired_deploy_auto_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry", return_value=0) as mock_ancestry:
        rc = gate.main(["--url", _TEST_URL, "--paired-deploy-auto"])
    assert rc == 0
    assert "PAIRED MODE" in capsys.readouterr().out
    # Ancestry must run against the pinned tag -- args.paired_deploy is None
    # for auto mode, so main()'s `args.paired_deploy or _pinned_engine_tag()`
    # already resolves correctly with no auto-specific wiring needed.
    mock_ancestry.assert_called_once_with(gate._pinned_engine_tag())


def test_main_rejects_paired_deploy_and_paired_deploy_auto_together() -> None:
    with pytest.raises(SystemExit) as exc_info:
        gate.main([
            "--paired-deploy", "engine-service-v0.1.1",
            "--paired-deploy-auto",
        ])
    assert exc_info.value.code == 2


# ── nexus-55r6o: --ledger-only pre-tag CLI entry point ────────────────────
#
# The publish-time gate (release.yml's --paired-deploy-auto invocation) has
# no --ack-client-lag path (no human present) -- an unacknowledged
# docs/wire-contract-pending.md ## Unshipped entry fails CLOSED on a FROZEN
# tag tree with no CI-side remedy (a workflow_dispatch retry re-checks out
# the same immutable tag; a ledger fix landed after the tag exists is
# invisible to it). --ledger-only moves the identical check_client_lag_ledger
# semantics into PR-gated release-branch CI, where the tree is still
# mutable, WITHOUT the network probe (check_floor) or the git-only ancestry
# check (check_source_ancestry) -- purely the tree-static ledger read.


def test_ledger_only_runs_check_client_lag_ledger_and_nothing_else(tmp_path) -> None:
    ledger = _write_ledger(tmp_path)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger), \
         patch.object(gate, "check_floor") as mock_floor, \
         patch.object(gate, "check_source_ancestry") as mock_ancestry, \
         patch.object(gate, "probe_managed_service") as mock_probe:
        rc = gate.main(["--ledger-only"])
    assert rc == 0
    mock_floor.assert_not_called()
    mock_ancestry.assert_not_called()
    mock_probe.assert_not_called()


def test_ledger_only_blocks_on_unacknowledged_entry(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger):
        rc = gate.main(["--ledger-only"])
    assert rc == 1
    assert "nexus-fake" in capsys.readouterr().err


def test_ledger_only_accepts_ack_client_lag(tmp_path) -> None:
    ledger = _write_ledger(tmp_path, _FAKE_ENTRY)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger):
        rc = gate.main(["--ledger-only", "--ack-client-lag", "nexus-fake"])
    assert rc == 0


def test_ledger_only_rejects_url_together() -> None:
    with pytest.raises(SystemExit) as exc_info:
        gate.main(["--ledger-only", "--url", _TEST_URL])
    assert exc_info.value.code == 2


def test_ledger_only_rejects_paired_deploy_together() -> None:
    with pytest.raises(SystemExit) as exc_info:
        gate.main(["--ledger-only", "--paired-deploy", "engine-service-v0.1.1"])
    assert exc_info.value.code == 2


def test_ledger_only_rejects_paired_deploy_auto_together() -> None:
    with pytest.raises(SystemExit) as exc_info:
        gate.main(["--ledger-only", "--paired-deploy-auto"])
    assert exc_info.value.code == 2
