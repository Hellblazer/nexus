# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Tests for ``scripts/check_engine_release_floor.py`` (nexus-i5c2u): the floor
check in both directions, source ancestry, the paired and auto-paired modes, the
wire-contract ledger, ``--ledger-only`` and ``--client-precondition`` (the
nexus-9ssih deploy-order gate, once its own script).

One test per verdict path, table-driven where inputs differ only in data.
``scripts/`` is on ``pythonpath`` via ``[tool.pytest.ini_options]``.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import check_engine_release_floor as gate
from nexus.db.managed_endpoint import (
    ManagedCapabilities,
    ManagedServiceError,
    ManagedServiceIncompatible,
    ManagedServiceUnreachable,
)
from nexus.engine_version import REQUIRED_ENGINE_VERSION
from tests._module_seam import patch_in

REPO_ROOT = Path(__file__).resolve().parents[2]
_TEST_URL = "https://example.test"


@pytest.fixture(autouse=True)
def _data_effect_relay_passes_by_default():
    """check_data_effect_relay does real git-tag + filesystem I/O against the
    checkout; only TestCheckDataEffectRelay (which shadows this fixture) and the
    battery test that sets its own return value exercise it."""
    with patch.object(gate, "check_data_effect_relay", return_value=0):
        yield


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


def _ver(v: tuple[int, int, int]) -> str:
    return ".".join(str(p) for p in v)


def _bump(v: tuple[int, int, int], n: int = 1) -> tuple[int, int, int]:
    return (v[0], v[1], v[2] + n)


_FLOOR = _ver(REQUIRED_ENGINE_VERSION)
_PAIRED_TAG = f"engine-service-v{_FLOOR}"
_FRESH_AGE_HOURS = 1.0
_STALE_AGE_HOURS = 200.0


def _git_repo(tmp_path: Path) -> tuple[Path, object]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args: str):
        return subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t.invalid", "-c", "user.name=t", *args],
            capture_output=True, text=True, check=True,
        )

    run("init", "-q")
    return repo, run


# ── The floor: cloud direction and pin direction ────────────────────────────


@pytest.mark.parametrize(
    ("probe", "rc", "stream", "needles"),
    [
        pytest.param({"return_value": _caps(_FLOOR)}, 0, "out", ["current"], id="at-floor"),
        pytest.param({"return_value": _caps(_ver(_bump(REQUIRED_ENGINE_VERSION)))}, 0, "out", ["current"], id="above-floor"),
        pytest.param({"return_value": _caps("0.1.1")}, 1, "err", ["0.1.1", _FLOOR, "engine-release"], id="stale-names-both-versions"),
        pytest.param(
            {"side_effect": ManagedServiceError("release_version 0.0.1 below floor")},
            1, "err", ["FLOOR CHECK FAILED", _FLOOR], id="incompatible-service-fails-not-passes",
        ),
        pytest.param(
            {"side_effect": ManagedServiceUnreachable("connect timed out")},
            2, "err", ["unreachable", "connect timed out"], id="unreachable-is-exit-2",
        ),
    ],
)
def test_cloud_floor_verdicts(probe, rc, stream, needles, capsys) -> None:
    with patch.object(gate, "probe_managed_service", **probe):
        got = gate.check_floor(url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION)
    assert got == rc
    text = getattr(capsys.readouterr(), stream).lower()
    for needle in needles:
        assert needle.lower() in text


@pytest.mark.parametrize(
    ("newest", "rc", "stream", "needles"),
    [
        pytest.param(_bump(REQUIRED_ENGINE_VERSION, 4), 1, "err",
                     [_FLOOR, _ver(_bump(REQUIRED_ENGINE_VERSION, 4)), "local", "REQUIRED_ENGINE_VERSION",
                      "deploy it FIRST", "1402"], id="unpinned-tag-fails-and-warns-bump-after-deploy"),
        pytest.param(REQUIRED_ENGINE_VERSION, 0, "out", ["current"], id="pin-equals-newest"),
        pytest.param(_bump(REQUIRED_ENGINE_VERSION, -1), 0, "out", ["ahead of publication"], id="pin-ahead-during-a-cut"),
        pytest.param(None, 2, "err", ["fetch-tags"], id="no-tags-visible-fails-closed"),
        pytest.param(gate._TAGS_UNAVAILABLE, 2, "err", ["failed gate"], id="git-unavailable-fails-closed"),
    ],
)
def test_pin_currency_verdicts(newest, rc, stream, needles, capsys) -> None:
    assert gate.check_pin_currency(newest) == rc
    text = getattr(capsys.readouterr(), stream)
    for needle in needles:
        assert needle.lower() in text.lower()


def test_pin_check_runs_before_the_network_probe() -> None:
    with patch.object(gate, "probe_managed_service") as probe, \
         patch.object(gate, "newest_published_engine", return_value=_bump(REQUIRED_ENGINE_VERSION)):
        assert gate.check_floor(url=_TEST_URL) == 1
    probe.assert_not_called()


def test_newest_published_engine_parses_the_tag_namespace(tmp_path) -> None:
    """Hermetic: the parser takes "0.1.56", not "engine-service-v0.1.56"; a shallow
    CI clone has no tags, so this builds its own repo (nexus-dhs30)."""
    repo, run = _git_repo(tmp_path)
    run("commit", "--allow-empty", "-q", "-m", "i")
    for tag in ("engine-service-v0.1.9", "engine-service-v0.1.56", "engine-service-v0.1.7",
                "v9.9.9", "not-an-engine-tag"):
        run("tag", tag)
    assert gate.newest_published_engine(repo_root=repo) == (0, 1, 56)  # numeric max, non-engine tags ignored


def test_newest_published_engine_reads_real_tags() -> None:
    """Reads THIS repo's tags, and tolerates a checkout that has none (nexus-dhs30)."""
    newest = gate.newest_published_engine()
    if newest is gate._TAGS_UNAVAILABLE or newest is None:
        pytest.skip(
            "checkout has no engine-service-v* tags (shallow CI clone). The parse "
            "path is covered hermetically above; release.yml uses fetch-depth: 0."
        )
    assert isinstance(newest, tuple) and len(newest) == 3
    assert newest >= (0, 1, 52)


def test_pinned_engine_tag_derives_from_the_floor_constant() -> None:
    assert gate._pinned_engine_tag() == "engine-service-v" + _FLOOR


# ── Source ancestry (nexus-hs4xl) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("touch", "tag", "rc", "stream", "needles"),
    [
        pytest.param(None, "engine-service-v9.9.9", 0, "out", ["current"], id="clean-at-the-tag"),
        pytest.param("service/src/main/java/A.java", "engine-service-v9.9.9", 1, "err",
                     ["SOURCE-ANCESTRY CHECK FAILED", "A.java", "engine-service-v9.9.9"], id="in-scope-drift-names-the-file"),
        pytest.param("service/src/test/java/ATest.java", "engine-service-v9.9.9", 0, "err", [], id="test-only-churn-not-flagged"),
        pytest.param(None, "engine-service-v0.0.0-nonexistent", 2, "err", ["UNVERIFIABLE", "does not exist"], id="missing-tag-fails-closed"),
    ],
)
def test_source_ancestry_verdicts(tmp_path, touch, tag, rc, stream, needles, capsys) -> None:
    repo, run = _git_repo(tmp_path)
    for rel in ("service/src/main/java/A.java", "service/src/test/java/ATest.java"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("class X {}\n")
    run("add", ".")
    run("commit", "-q", "-m", "base")
    run("tag", "engine-service-v9.9.9")
    if touch:
        (repo / touch).write_text("class X { int drift; }\n")
        run("commit", "-aq", "-m", "drift")
    assert gate.check_source_ancestry(tag, repo_root=repo) == rc
    text = getattr(capsys.readouterr(), stream)
    for needle in needles:
        assert needle in text
    if rc == 0:
        assert "SOURCE-ANCESTRY CHECK FAILED" not in text


def test_source_ancestry_git_unavailable_fails_closed(capsys) -> None:
    with patch.object(gate, "_tag_exists_in_git", return_value=gate._TAGS_UNAVAILABLE):
        assert gate.check_source_ancestry("engine-service-v9.9.9") == 2
    assert "UNVERIFIABLE" in capsys.readouterr().err


@pytest.mark.integration
@pytest.mark.mandatory_regression_pin
def test_v7_6_1_source_ancestry_regression_is_red() -> None:
    """v7.6.1 pinned engine-service-v0.1.71 (current by version number) while shipping
    service/src/main source that tag lacks (nexus-ajlz5). Needs real tags, so it lives
    in `integration` and a silent skip is a failed run (nexus-93j33)."""
    check = subprocess.run(
        ["git", "tag", "-l", "v7.6.1", "engine-service-v0.1.71"],
        capture_output=True, text=True,
    )
    if not {"v7.6.1", "engine-service-v0.1.71"} <= set(check.stdout.split()):
        pytest.skip("checkout is missing v7.6.1 and/or engine-service-v0.1.71 (shallow CI clone)")
    diff = subprocess.run(
        ["git", "diff", "--stat", "engine-service-v0.1.71", "v7.6.1", "--", gate._ANCESTRY_SCOPE],
        capture_output=True, text=True, check=True,
    )
    assert diff.stdout.strip(), (
        "expected v7.6.1 to carry service/src/main source that engine-service-v0.1.71 lacks "
        "(nexus-ajlz5); if this is empty the historical fixture no longer holds"
    )
    assert gate.check_source_ancestry("engine-service-v0.1.71") == 1


# ── main(): ancestry wiring, paired flags, mode conflicts ───────────────────


@pytest.mark.parametrize(
    ("cloud", "ancestry_rc", "rc", "ancestry_called"),
    [
        pytest.param(_FLOOR, 0, 0, True, id="clean-floor-then-ancestry"),
        pytest.param(_FLOOR, 1, 1, True, id="version-current-must-not-mask-source-stale"),
        pytest.param("0.0.1", 0, 1, False, id="failed-floor-skips-ancestry"),
    ],
)
def test_main_runs_ancestry_after_the_floor(cloud, ancestry_rc, rc, ancestry_called) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps(cloud)), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry", return_value=ancestry_rc) as ancestry:
        assert gate.main(["--url", _TEST_URL]) == rc
    assert ancestry.called is ancestry_called
    if ancestry_called:
        ancestry.assert_called_once_with(gate._pinned_engine_tag())


def test_main_threads_the_paired_flags(capsys) -> None:
    """--paired-deploy names the tag that gets ancestry-checked, and
    --paired-tag-max-age-hours is the only thing that turns a stale tag into an accept."""
    assert gate._DEFAULT_PAIRED_TAG_MAX_AGE_HOURS == 72.0
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_STALE_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION), \
         patch.object(gate, "check_source_ancestry", return_value=0) as ancestry:
        base = ["--url", _TEST_URL, "--paired-deploy", _PAIRED_TAG]
        assert gate.main(base) == 1
        assert gate.main([*base, "--paired-tag-max-age-hours", "500"]) == 0
    assert "PAIRED MODE" in capsys.readouterr().out
    ancestry.assert_called_once_with(_PAIRED_TAG)


def test_help_exits_cleanly_without_network_call() -> None:
    with patch.object(gate, "probe_managed_service") as probe, pytest.raises(SystemExit) as exc:
        gate.main(["--help"])
    assert exc.value.code == 0
    probe.assert_not_called()


@pytest.mark.parametrize(
    "argv",
    [
        ["--paired-deploy", _PAIRED_TAG, "--paired-deploy-auto"],
        ["--ledger-only", "--url", _TEST_URL],
        ["--ledger-only", "--paired-deploy", _PAIRED_TAG],
        ["--ledger-only", "--paired-deploy-auto"],
        ["--client-precondition", "--ledger-only"],
        ["--client-precondition", "--url", _TEST_URL],
        ["--client-precondition", "--paired-deploy", _PAIRED_TAG],
        ["--client-precondition", "--paired-deploy-auto"],
    ],
)
def test_conflicting_modes_are_refused(argv) -> None:
    with pytest.raises(SystemExit) as exc:
        gate.main(argv)
    assert exc.value.code == 2


# ── Paired-release mode (nexus-k1c08) ───────────────────────────────────────


def _precond(*, tag=_PAIRED_TAG, newest=REQUIRED_ENGINE_VERSION, exists=True,
             published=(True, ""), age=_FRESH_AGE_HOURS, max_age=72.0) -> int:
    with patch.object(gate, "_tag_exists_in_git", return_value=exists), \
         patch.object(gate, "_paired_tag_published", return_value=published), \
         patch.object(gate, "_tag_age_hours", return_value=age):
        return gate.check_paired_preconditions(tag, newest, max_age_hours=max_age)


@pytest.mark.parametrize(
    ("kwargs", "rc", "needle"),
    [
        pytest.param({"tag": "v9.9.9"}, 1, "engine-service-v", id="non-engine-tag"),
        pytest.param({"tag": "engine-service-vSNAPSHOT"}, 1, "does not parse", id="unparseable-tag"),
        pytest.param({"exists": False}, 1, "does not exist", id="tag-missing-from-git"),
        pytest.param({"exists": gate._TAGS_UNAVAILABLE}, 2, "UNVERIFIABLE", id="git-unavailable-fails-closed"),
        pytest.param({"published": (gate._TAGS_UNAVAILABLE, "gh down")}, 2, "UNVERIFIABLE", id="gh-unavailable-fails-closed"),
        pytest.param({"published": (False, "release is still a DRAFT")}, 1, "DRAFT", id="draft-release"),
        pytest.param({"tag": f"engine-service-v{_ver(_bump(REQUIRED_ENGINE_VERSION))}"}, 1, "wrong pairing", id="tag-is-not-the-floor"),
        pytest.param({"newest": _bump(REQUIRED_ENGINE_VERSION)}, 1, "newer engine tag", id="newer-tag-exists"),
        pytest.param({"newest": gate._TAGS_UNAVAILABLE}, 2, "UNVERIFIABLE", id="newest-unreadable"),
        pytest.param({"age": _STALE_AGE_HOURS}, 1, f"{_STALE_AGE_HOURS:.1f}h", id="stale-tag-names-its-age"),
        pytest.param({"age": gate._TAGS_UNAVAILABLE}, 2, "UNVERIFIABLE", id="age-unavailable-fails-closed"),
        pytest.param({"age": -5.0}, 1, "FUTURE", id="future-dated-tag-refused"),
        pytest.param({"age": -0.1}, 0, "ARMED", id="future-within-skew-tolerance-passes"),
        pytest.param({}, 0, "ARMED", id="all-conditions-hold"),
        pytest.param({"age": _STALE_AGE_HOURS, "max_age": _STALE_AGE_HOURS + 1}, 0, "ARMED", id="stale-tag-with-explicit-override"),
    ],
)
def test_paired_preconditions(kwargs, rc, needle, capsys) -> None:
    assert _precond(**kwargs) == rc
    captured = capsys.readouterr()
    assert needle.lower() in (captured.out + captured.err).lower()


def _paired_patches(mode: str, *, probe: dict, newest=REQUIRED_ENGINE_VERSION):
    kw = {"paired_deploy": _PAIRED_TAG} if mode == "explicit" else {"paired_deploy_auto": True}
    with patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=(True, "")), \
         patch.object(gate, "_tag_age_hours", return_value=_FRESH_AGE_HOURS), \
         patch.object(gate, "probe_managed_service", **probe):
        return gate.check_floor(url=_TEST_URL, newest=newest, **kw)


_REMEDY_SENTENCE = (
    f"managed nexus service at {_TEST_URL} is release_version '0.1.17', below the minimum "
    f"required v{_FLOOR}. Upgrade the managed service, or upgrade/downgrade the nx client."
)


@pytest.mark.parametrize("mode", ["explicit", "auto"])
@pytest.mark.parametrize(
    ("probe", "rc", "out_has", "out_lacks", "err_has"),
    [
        pytest.param({"return_value": _caps("0.0.1")}, 0,
                     ["PAIRED MODE", "0.0.1", _FLOOR, "post-tag verify", "re-run this script"], [], [],
                     id="below-floor-accepted-on-tag-legitimacy-alone"),
        pytest.param(
            {"side_effect": ManagedServiceIncompatible(_REMEDY_SENTENCE, deployed_version="0.1.17", required_version=_FLOOR)},
            0, ["PAIRED MODE", "'0.1.17'"], ["Upgrade the managed service", "below the minimum required"], [],
            id="below-floor-ack-uses-the-structured-version",
        ),
        pytest.param({"return_value": _caps(_FLOOR)}, 0, ["current"], ["PAIRED MODE"], [], id="at-floor-is-a-normal-pass"),
        pytest.param({"return_value": _caps(_ver(_bump(REQUIRED_ENGINE_VERSION)))}, 0, [], ["PAIRED MODE"], [], id="above-floor-is-a-normal-pass"),
        pytest.param({"side_effect": ManagedServiceError("service returned HTTP 503")}, 2, [], ["PAIRED MODE"],
                     ["UNVERIFIABLE", "genuine below-floor"], id="generic-service-error-is-not-deploy-pending"),
        pytest.param({"return_value": _caps("not-a-version")}, 2, [], ["PAIRED MODE"],
                     ["UNVERIFIABLE", "unparseable"], id="unparseable-release-version-is-not-deploy-pending"),
        pytest.param({"side_effect": ManagedServiceUnreachable("connect timed out")}, 2, [], [],
                     ["unreachable"], id="unreachable-stays-exit-2"),
    ],
)
def test_paired_modes_cloud_outcomes(mode, probe, rc, out_has, out_lacks, err_has, capsys) -> None:
    assert _paired_patches(mode, probe=probe) == rc
    captured = capsys.readouterr()
    for needle in out_has:
        assert needle.lower() in captured.out.lower()
    for needle in out_lacks:
        assert needle not in captured.out
    for needle in err_has:
        assert needle.lower() in captured.err.lower()
    if rc == 2:
        assert "PAIRED MODE" not in captured.err


def test_auto_paired_derives_its_tag_and_says_so(capsys) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "check_paired_preconditions", return_value=0) as precond:
        assert gate.check_floor(url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True) == 0
    assert precond.call_args[0][0] == gate._pinned_engine_tag()
    out = capsys.readouterr().out
    assert "AUTO-derived" in out and "--paired-deploy-auto" in out


def test_auto_paired_with_a_current_cloud_is_the_bare_path(capsys) -> None:
    """The paired machinery never runs, and pin currency is still enforced."""
    with patch.object(gate, "probe_managed_service", return_value=_caps(_FLOOR)), \
         patch.object(gate, "check_client_lag_ledger") as ledger, \
         patch.object(gate, "check_paired_preconditions") as precond:
        assert gate.check_floor(url=_TEST_URL, newest=REQUIRED_ENGINE_VERSION, paired_deploy_auto=True) == 0
        assert "PAIRED MODE" not in capsys.readouterr().out
        assert gate.check_floor(url=_TEST_URL, newest=_bump(REQUIRED_ENGINE_VERSION, 3), paired_deploy_auto=True) == 1
    assert "ENGINE PIN CHECK FAILED" in capsys.readouterr().err
    ledger.assert_not_called()
    precond.assert_not_called()


@pytest.mark.parametrize(
    ("published", "newest", "age", "rc"),
    [
        pytest.param((False, "release is still a DRAFT"), REQUIRED_ENGINE_VERSION, _FRESH_AGE_HOURS, 1, id="draft"),
        pytest.param((True, ""), _bump(REQUIRED_ENGINE_VERSION), _FRESH_AGE_HOURS, 1, id="newer-tag"),
        pytest.param((True, ""), REQUIRED_ENGINE_VERSION, _STALE_AGE_HOURS, 1, id="stale-tag"),
        pytest.param((gate._TAGS_UNAVAILABLE, "gh down"), REQUIRED_ENGINE_VERSION, _FRESH_AGE_HOURS, 2, id="gh-unavailable"),
    ],
)
def test_auto_paired_below_floor_refusals(published, newest, age, rc) -> None:
    with patch.object(gate, "probe_managed_service", return_value=_caps("0.0.1")), \
         patch.object(gate, "_tag_exists_in_git", return_value=True), \
         patch.object(gate, "_paired_tag_published", return_value=published), \
         patch.object(gate, "_tag_age_hours", return_value=age):
        assert gate.check_floor(url=_TEST_URL, newest=newest, paired_deploy_auto=True) == rc


def test_default_and_explicit_modes_take_their_own_paths() -> None:
    """The default path never consults the ledger or the paired battery; an explicit
    --paired-deploy wins over --paired-deploy-auto at the library level."""
    with patch.object(gate, "check_client_lag_ledger") as ledger, \
         patch.object(gate, "probe_managed_service", return_value=_caps(_FLOOR)), \
         patch.object(gate, "check_source_ancestry", return_value=0), \
         patch.object(gate, "newest_published_engine", return_value=REQUIRED_ENGINE_VERSION):
        assert gate.main(["--url", _TEST_URL]) == 0
    ledger.assert_not_called()
    with patch.object(gate, "_run_paired_precondition_battery", return_value=1) as battery, \
         patch.object(gate, "_check_floor_auto_paired") as auto:
        assert gate.check_floor(paired_deploy=_PAIRED_TAG, paired_deploy_auto=True) == 1
    battery.assert_called_once()
    auto.assert_not_called()


# ── Paired-mode git/gh helpers ──────────────────────────────────────────────


def _gh(payload=None, *, returncode=0, stdout=None, stderr=""):
    out = stdout if stdout is not None else json.dumps(payload)
    return MagicMock(returncode=returncode, stdout=out, stderr=stderr)


_BINARY = gate._REQUIRED_ASSET_NAME


@pytest.mark.parametrize(
    ("fake", "ok", "needles"),
    [
        pytest.param(_gh({"isDraft": False, "assets": [{"name": _BINARY}]}), True, [], id="published-with-the-binary"),
        pytest.param(_gh({"isDraft": False, "assets": [{"name": "nexus-pg-linux-amd64.txz"}, {"name": _BINARY}]}), True, [], id="binary-among-others"),
        pytest.param(_gh({"isDraft": True, "assets": [{"name": _BINARY}]}), False, ["DRAFT"], id="draft"),
        pytest.param(_gh({"isDraft": False, "assets": []}), False, [_BINARY, "none"], id="zero-assets"),
        pytest.param(_gh({"isDraft": False, "assets": [{"name": "nexus-pg-linux-amd64.txz"}]}), False,
                     [_BINARY, "nexus-pg-linux-amd64.txz"], id="bundle-without-the-binary-names-what-was-present"),
        pytest.param(_gh({"assets": [{"name": _BINARY}]}), gate._TAGS_UNAVAILABLE, ["isDraft"], id="missing-isdraft-key-fails-closed"),
        pytest.param(_gh(returncode=1, stdout="", stderr="release not found"), gate._TAGS_UNAVAILABLE, ["release not found"], id="gh-nonzero-exit"),
        pytest.param(_gh(stdout="not json"), gate._TAGS_UNAVAILABLE, ["unparseable"], id="unparseable-json"),
    ],
)
def test_paired_tag_published(fake, ok, needles) -> None:
    with patch_in(gate, "subprocess.run", return_value=fake):
        got, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert got is ok
    for needle in needles:
        assert needle.lower() in reason.lower()
    if ok is True:
        assert reason == ""


def test_paired_tag_published_gh_missing_and_cwd_anchoring(tmp_path) -> None:
    with patch_in(gate, "subprocess.run", side_effect=FileNotFoundError("gh not found")):
        got, reason = gate._paired_tag_published(_PAIRED_TAG)
    assert got is gate._TAGS_UNAVAILABLE and "could not invoke" in reason and "gh auth login" in reason
    ok = _gh({"isDraft": False, "assets": [{"name": _BINARY}]})
    with patch_in(gate, "subprocess.run", return_value=ok) as run:
        gate._paired_tag_published(_PAIRED_TAG, repo_root=tmp_path)
        assert run.call_args[1]["cwd"] == tmp_path
        gate._paired_tag_published(_PAIRED_TAG)
        assert run.call_args[1]["cwd"] == gate.pathlib.Path(gate.__file__).resolve().parent.parent


def test_tag_existence_and_age_helpers(tmp_path) -> None:
    repo, run = _git_repo(tmp_path)
    run("commit", "--allow-empty", "-q", "-m", "i")
    run("tag", "engine-service-v0.1.9")
    assert gate._tag_exists_in_git("engine-service-v0.1.9", repo_root=repo) is True
    assert gate._tag_exists_in_git("engine-service-v9.9.9", repo_root=repo) is False
    age = gate._tag_age_hours("engine-service-v0.1.9", repo_root=repo)
    assert isinstance(age, float) and 0.0 <= age < 1.0
    assert gate._tag_age_hours("engine-service-v9.9.9", repo_root=repo) is gate._TAGS_UNAVAILABLE
    with patch_in(gate, "subprocess.run", side_effect=FileNotFoundError("no git")):
        assert gate._tag_exists_in_git("engine-service-v0.1.9", repo_root=repo) is gate._TAGS_UNAVAILABLE
        assert gate._tag_age_hours("engine-service-v0.1.9", repo_root=repo) is gate._TAGS_UNAVAILABLE


# ── The wire-contract ledger (nexus-1vogq, nexus-1emxn) ─────────────────────


def _write_ledger(tmp_path, entry: str | None = None):
    ledger = tmp_path / "wire-contract-pending.md"
    ledger.write_text(f"## Unshipped\n\n{entry or '(none)' + chr(10)}\n## Shipped\n")
    return ledger


_FAKE_ENTRY = (
    "- `deadbeefdeadbeefdeadbeefdeadbeefdeadbeef` -- bead nexus-fake -- "
    "engine tag `engine-service-v9.9.9` -- test fixture\n"
)
_ADDITIVE_ENTRY = (
    "- `cafebabecafebabecafebabecafebabecafebabe` -- bead nexus-addv -- "
    "engine tag `engine-service-v9.9.9` -- [additive] old client + new engine safe\n"
)
_NOT_ADDITIVE_ENTRY = (
    "- `feedfacefeedfacefeedfacefeedfacefeedface` -- bead nexus-notad -- "
    "engine tag `engine-service-v9.9.9` -- [not-additive] the engine must wait\n"
)
_BOTH_TOKENS_ENTRY = (
    "- `beadbeadbeadbeadbeadbeadbeadbeadbeadbead` -- bead nexus-both -- "
    "engine tag `engine-service-v9.9.9` -- [additive] but also [not-additive]\n"
)


@pytest.mark.parametrize(
    ("entry", "rc", "stream", "has", "lacks"),
    [
        pytest.param(None, 0, "out", ["client-lag ledger clean"], [], id="empty"),
        pytest.param(_FAKE_ENTRY, 1, "err", ["nexus-fake", "PAIRED DEPLOY BLOCKED", "deadbeef"], [], id="tokenless-entry-blocks"),
        pytest.param(_NOT_ADDITIVE_ENTRY, 1, "err", ["nexus-notad"], [], id="not-additive-blocks"),
        pytest.param(_BOTH_TOKENS_ENTRY, 1, "err", ["nexus-both"], [], id="both-tokens-is-not-additive"),
        pytest.param(_ADDITIVE_ENTRY, 0, "out", ["[additive]", "nexus-addv", "nexus-1emxn"], [], id="all-additive-authorizes"),
        pytest.param(_ADDITIVE_ENTRY + _NOT_ADDITIVE_ENTRY, 1, "err", ["nexus-notad"], ["nexus-addv"], id="mixed-names-only-the-blocking-entry"),
    ],
)
def test_client_lag_ledger_verdicts(tmp_path, entry, rc, stream, has, lacks, capsys) -> None:
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", _write_ledger(tmp_path, entry)):
        assert gate.check_client_lag_ledger() == rc
    text = getattr(capsys.readouterr(), stream)
    for needle in has:
        assert needle in text
    for needle in lacks:
        assert needle not in text


def test_the_paired_battery_runs_the_ledger_first_then_tag_then_relay(tmp_path) -> None:
    """Cheap local check first: a blocking ledger means the tag preconditions are never
    reached; a clean ledger reaches them; a refusing DATA EFFECT relay fails the whole
    battery (nexus-iu43o)."""
    battery = (_PAIRED_TAG, REQUIRED_ENGINE_VERSION, gate._DEFAULT_PAIRED_TAG_MAX_AGE_HOURS)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", _write_ledger(tmp_path, _FAKE_ENTRY)), \
         patch.object(gate, "check_paired_preconditions") as precond:
        assert gate._run_paired_precondition_battery(*battery) == 1
    precond.assert_not_called()
    clean = _write_ledger(tmp_path)
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", clean), \
         patch.object(gate, "check_paired_preconditions", return_value=1) as precond:
        assert gate._run_paired_precondition_battery(*battery) == 1
    precond.assert_called_once()
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", clean), \
         patch.object(gate, "check_paired_preconditions", return_value=0), \
         patch.object(gate, "check_data_effect_relay", return_value=1):
        assert gate._run_paired_precondition_battery(*battery) == 1


def test_ledger_only_runs_the_ledger_and_nothing_else(tmp_path) -> None:
    for entry, rc in ((_FAKE_ENTRY, 1), (None, 0)):
        with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", _write_ledger(tmp_path, entry)), \
             patch.object(gate, "probe_managed_service") as probe, \
             patch.object(gate, "check_source_ancestry") as ancestry:
            assert gate.main(["--ledger-only"]) == rc
        probe.assert_not_called()
        ancestry.assert_not_called()


# ── --client-precondition (nexus-9ssih deploy-order gate) ───────────────────

_TEST_ENGINE = "engine-service-vTEST"


@pytest.fixture
def table(monkeypatch):
    """A one-row ENGINE_CLIENT_PRECONDITIONS for _TEST_ENGINE with an injectable verdict."""
    monkeypatch.setitem(gate.ENGINE_CLIENT_PRECONDITIONS, _TEST_ENGINE, {"deadbeef": "test precondition"})

    def set_git(*, release="v0.0.1", ancestor=True):
        monkeypatch.setattr(gate, "latest_release_tag", lambda: release)

        def is_ancestor(commit, tag):
            if isinstance(ancestor, Exception):
                raise ancestor
            return ancestor

        monkeypatch.setattr(gate, "is_ancestor", is_ancestor)

    return set_git


@pytest.mark.parametrize(
    ("ancestor", "rc", "stream", "needle"),
    [
        pytest.param(False, 1, "err", "BLOCKED", id="required-commit-unreleased-blocks"),
        pytest.param(True, 0, "out", "OK: all client preconditions", id="required-commit-released-passes"),
        pytest.param(RuntimeError("git exploded"), 2, "err", "CANNOT VERIFY", id="unverifiable-git-state-is-exit-2-not-a-pass"),
    ],
)
def test_client_precondition_hand_table(table, ancestor, rc, stream, needle, capsys) -> None:
    table(ancestor=ancestor)
    assert gate.check_client_precondition(_TEST_ENGINE) == rc
    assert needle in getattr(capsys.readouterr(), stream)


def test_client_precondition_runs_the_relay_check_first(monkeypatch) -> None:
    """A refused DATA EFFECT relay returns before the hand table is consulted, so an
    empty or populated table can never mask it."""
    monkeypatch.setattr(gate, "latest_release_tag", lambda: pytest.fail("hand table reached"))
    monkeypatch.setitem(gate.ENGINE_CLIENT_PRECONDITIONS, _TEST_ENGINE, {"deadbeef": "x"})
    with patch.object(gate, "check_data_effect_relay", return_value=1):
        assert gate.check_client_precondition(_TEST_ENGINE) == 1


def test_client_precondition_vacuity_message_names_both_sources(table, tmp_path, monkeypatch, capsys) -> None:
    """Both sources empty -> an unmistakable "verified NOTHING"; a populated hand table
    that verified something must not print it (nexus-f9z84)."""
    ledger = _write_ledger(tmp_path)
    monkeypatch.setattr(gate._wire_ledger, "DEFAULT_LEDGER_PATH", ledger)
    assert gate.check_client_precondition("engine-service-v0.0.0-nonexistent") == 0
    out = capsys.readouterr().out
    for needle in ("VACUOUS", "0 preconditions registered", "0 entries", str(ledger), "verified NOTHING from EITHER source"):
        assert needle in out
    table(ancestor=True)
    assert gate.check_client_precondition(_TEST_ENGINE) == 0
    assert "VACUOUS" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("entry", "rc"),
    [(_FAKE_ENTRY, 1), (_NOT_ADDITIVE_ENTRY, 1), (_ADDITIVE_ENTRY, 0), (None, 0)],
    ids=["tokenless-blocks", "not-additive-blocks", "additive-authorizes", "empty"],
)
def test_client_precondition_consults_the_ledger_for_any_tag(tmp_path, entry, rc) -> None:
    """Not tag-scoped: an unpaired deploy of ANY engine tag risks carrying an unshipped
    client half live (protocol-audit [22511] Gap 1)."""
    with patch.object(gate._wire_ledger, "DEFAULT_LEDGER_PATH", _write_ledger(tmp_path, entry)):
        assert gate.main(["--client-precondition", "engine-service-v0.0.0-nonexistent"]) == rc


def test_client_precondition_defaults_to_the_pinned_tag() -> None:
    with patch.object(gate, "check_client_precondition", return_value=0) as check:
        assert gate.main(["--client-precondition"]) == 0
        assert gate.main(["--client-precondition", "engine-service-vX"]) == 0
    assert [c.args[0] for c in check.call_args_list] == [gate._pinned_engine_tag(), "engine-service-vX"]


def test_client_precondition_git_helpers(tmp_path, monkeypatch) -> None:
    """CI checkouts are shallow and tagless, so carry git state (found on 7.0.0)."""
    repo, run = _git_repo(tmp_path)
    run("commit", "--allow-empty", "-q", "-m", "one")
    run("commit", "--allow-empty", "-q", "-m", "two")
    run("tag", "v1.2.3")
    run("tag", "engine-service-v9.9.9")
    monkeypatch.chdir(repo)  # the helpers run in cwd by design
    tag = gate.latest_release_tag()
    assert tag == "v1.2.3" and re.fullmatch(r"v\d+\.\d+\.\d+", tag)  # never an engine tag
    assert gate.is_ancestor(run("rev-parse", "HEAD~1").stdout.strip(), "HEAD")


def test_stale_precondition_rows_do_not_outlive_the_floor() -> None:
    """Non-vacuity: the live table is almost always empty, so also plant rows (nexus-f9z84)."""
    assert gate.stale_precondition_rows() == []
    planted = {
        "engine-service-v0.1.70": {"deadbeef": "floor minus one"},
        "engine-service-v0.1.71": {"beadfeed": "exactly the floor"},
        "engine-service-v0.1.99": {"c0ffee00": "ahead"},
        "next": {"f00dface": "always-ahead sentinel"},
    }
    assert set(gate.stale_precondition_rows(table=planted, floor=(0, 1, 71))) == {
        "engine-service-v0.1.70", "engine-service-v0.1.71",
    }
    assert gate.stale_precondition_rows(table={"engine-service-v0.1.72": {"d": "ahead"}}, floor=(0, 1, 71)) == []
    assert gate.stale_precondition_rows() == gate.stale_precondition_rows(
        table=gate.ENGINE_CLIENT_PRECONDITIONS, floor=REQUIRED_ENGINE_VERSION,
    )


@pytest.mark.real_ledger
def test_the_ledger_import_reaches_the_real_checked_in_file() -> None:
    """tests/scripts/conftest.py isolates every other test onto an empty ledger; this one
    proves the production import is live, not stubbed."""
    path = gate._wire_ledger.DEFAULT_LEDGER_PATH
    assert path.is_file() and path.name == "wire-contract-pending.md"
    blocking = gate._wire_ledger.classify_unshipped(gate._wire_ledger.parse_ledger(path)).blocking
    assert gate.check_client_lag_ledger() == (1 if blocking else 0)


def test_the_engine_release_skill_invokes_the_precondition_before_the_tag_push() -> None:
    """An unwired gate is a prose gate with extra steps (nexus-qc4p1). It surfaces early,
    but gates the DEPLOY, never the tag cut (7.1.0/v0.1.62 inversion)."""
    skill = (REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md").read_text()
    call = "check_engine_release_floor.py --client-precondition"
    assert call in skill
    assert skill.index(call) < skill.index("git push origin engine-service-v")
    assert "never the tag cut" in skill


# ── DATA EFFECT relay (nexus-iu43o) ─────────────────────────────────────────


class TestCheckDataEffectRelay:
    """Direct tests for check_data_effect_relay / _previous_engine_tag
    against a REAL, small, synthetic git repo (never this checkout) --
    the same fixture shape list_data_effects' own tests use."""

    @pytest.fixture(autouse=True)
    def _data_effect_relay_passes_by_default(self):
        """Shadows the module-level fixture of the same name (class scope
        wins) -- these tests ARE check_data_effect_relay, so it must not
        be mocked away here."""
        yield

    @staticmethod
    def _git(repo, *args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    @pytest.fixture
    def two_tag_repo(self, tmp_path):
        """v1 = engine-service-v0.1.1 (one additive changeset), v2 =
        engine-service-v0.1.2 (gains one data-effecting DELETE, no DATA
        EFFECT: line yet -- the fixture's job is to give the relay check
        something real to refuse, pass, or skip on)."""
        repo = tmp_path / "repo"
        changelog_dir = repo / "service" / "src" / "main" / "resources" / "db" / "changelog"
        changelog_dir.mkdir(parents=True)
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "t@t")
        self._git(repo, "config", "user.name", "t")

        master_tmpl = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog\n'
            '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
            '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
            'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
            '{includes}\n</databaseChangeLog>\n'
        )

        def _cs(body: str) -> str:
            return (
                '<?xml version="1.0" encoding="UTF-8"?>\n<databaseChangeLog\n'
                '    xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
                '    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
                '    xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog '
                'http://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-4.4.xsd">\n'
                f"{body}\n</databaseChangeLog>\n"
            )

        (changelog_dir / "a.xml").write_text(_cs(
            '    <changeSet id="a-1" author="t"><comment>Additive.</comment>\n'
            '        <sql splitStatements="true">CREATE TABLE nexus.widgets (id int);</sql>\n'
            "    </changeSet>"
        ))
        (changelog_dir / "db.changelog-master.xml").write_text(
            master_tmpl.format(includes='    <include file="a.xml"/>')
        )
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", "v1")
        self._git(repo, "tag", "engine-service-v0.1.1")

        (changelog_dir / "a.xml").write_text(_cs(
            '    <changeSet id="a-1" author="t"><comment>Additive.</comment>\n'
            '        <sql splitStatements="true">CREATE TABLE nexus.widgets (id int);</sql>\n'
            "    </changeSet>\n"
            '    <changeSet id="a-2" author="t"><comment>Deletes stale widgets.</comment>\n'
            '        <sql splitStatements="true">DELETE FROM nexus.widgets WHERE stale = true;</sql>\n'
            "    </changeSet>"
        ))
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "-q", "-m", "v2")
        self._git(repo, "tag", "engine-service-v0.1.2")
        return repo

    def test_previous_engine_tag_finds_the_immediately_older_tag(self, two_tag_repo) -> None:
        assert gate._previous_engine_tag("engine-service-v0.1.2", two_tag_repo) == "engine-service-v0.1.1"

    def test_previous_engine_tag_is_none_for_the_oldest_tag(self, two_tag_repo) -> None:
        assert gate._previous_engine_tag("engine-service-v0.1.1", two_tag_repo) is None

    def test_refuses_when_never_recorded(self, two_tag_repo, capsys) -> None:
        rc = gate.check_data_effect_relay("engine-service-v0.1.2", two_tag_repo)
        assert rc == 1
        assert "REFUSED" in capsys.readouterr().err

    def test_passes_once_recorded(self, two_tag_repo, capsys) -> None:
        _data_effects_module = gate._data_effects
        _data_effects_module.record_relay_attestation(
            "engine-service-v0.1.1", "engine-service-v0.1.2", repo_root=two_tag_repo
        )
        rc = gate.check_data_effect_relay("engine-service-v0.1.2", two_tag_repo)
        assert rc == 0
        assert "RELAY ATTESTATION OK" in capsys.readouterr().out

    def test_not_applicable_for_the_oldest_tag(self, two_tag_repo, capsys) -> None:
        rc = gate.check_data_effect_relay("engine-service-v0.1.1", two_tag_repo)
        assert rc == 0
        assert "NOT-APPLICABLE" in capsys.readouterr().out


# ── --require-windows (nexus-f9bgu.28, RDR-224 critique S4) ─────────────────


class TestRequireWindows:
    """The opt-in Windows-assets check: 36 assets on a published tag (27 on one cut before the
    POSIX engine archives), and nothing else changes."""

    TAG = "engine-service-v0.1.140"
    ALL = gate.expected_engine_assets(windows=True)

    @staticmethod
    def _release(names, *, draft=False):
        return _gh({"isDraft": draft, "assets": [{"name": n} for n in names]})

    def test_the_asset_sets_are_30_and_36(self) -> None:
        assert len(gate.expected_engine_assets(windows=False)) == 30
        assert len(self.ALL) == 36 and len(set(self.ALL)) == 36
        old = gate.expected_engine_assets(windows=True, posix_archives=False)
        assert len(old) == 27 and set(old) < set(self.ALL)
        assert set(self.ALL) - set(old) == {
            f"nexus-service-{arch}.txz{suffix}"
            for arch in ("linux-amd64", "linux-arm64", "mac-arm64")
            for suffix in ("", ".sha256", ".sigstore.json")
        }
        assert set(gate.expected_engine_assets(windows=False)) < set(self.ALL)
        windows = set(self.ALL) - set(gate.expected_engine_assets(windows=False))
        assert windows == {
            f"{a}{suffix}"
            for a in ("nexus-pg-windows-x64.txz", "nexus-service-windows-x64.txz")
            for suffix in ("", ".sha256", ".sigstore.json")
        }

    def test_a_published_release_with_all_27_passes(self, capsys) -> None:
        with patch_in(gate, "subprocess.run", return_value=self._release(self.ALL)):
            assert gate.check_windows_assets(self.TAG) == 0
        assert "all 36 assets" in capsys.readouterr().out

    def test_a_release_from_before_the_posix_archives_is_held_to_its_27(self, capsys) -> None:
        """The pinned engine at the time of this change (v0.1.149) has no .txz for Linux or macOS."""
        old = gate.expected_engine_assets(windows=True, posix_archives=False)
        with patch.object(gate.subprocess, "run", return_value=self._release(old)):
            assert gate.check_windows_assets(self.TAG) == 0
        assert "all 27 assets" in capsys.readouterr().out

    def test_a_partial_set_of_posix_archives_is_held_to_all_36(self, capsys) -> None:
        old = gate.expected_engine_assets(windows=True, posix_archives=False)
        with patch.object(gate.subprocess, "run",
                          return_value=self._release([*old, "nexus-service-mac-arm64.txz"])):
            assert gate.check_windows_assets(self.TAG) == 1
        err = capsys.readouterr().err
        assert "missing 8 of 36" in err and "nexus-service-linux-amd64.txz" in err

    def test_a_cut_with_the_switch_off_is_named_as_such(self, capsys) -> None:
        with patch_in(gate, "subprocess.run", return_value=self._release(gate.expected_engine_assets(windows=False))):
            assert gate.check_windows_assets(self.TAG) == 1
        err = capsys.readouterr().err
        assert "missing 6 of 36" in err and "nexus-service-windows-x64.txz" in err
        assert "NX_WINDOWS_RELEASE_LEGS off" in err and "another cut" in err

    def test_any_single_missing_asset_fails_and_is_named(self, capsys) -> None:
        for victim in self.ALL:
            with patch_in(gate, "subprocess.run", return_value=self._release([n for n in self.ALL if n != victim])):
                assert gate.check_windows_assets(self.TAG) == 1, victim
            assert victim in capsys.readouterr().err

    def test_a_draft_fails_even_with_every_asset(self, capsys) -> None:
        with patch_in(gate, "subprocess.run", return_value=self._release(self.ALL, draft=True)):
            assert gate.check_windows_assets(self.TAG) == 1
        assert "DRAFT" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "fake",
        [
            _gh(returncode=1, stdout="", stderr="release not found"),
            _gh(stdout="not json"),
            _gh({"assets": []}),
            _gh({"isDraft": False}),
            _gh(["a"]),
        ],
        ids=["gh-fails", "not-json", "no-isdraft", "no-assets", "wrong-shape"],
    )
    def test_an_unreadable_answer_is_unverifiable_never_a_pass(self, fake, capsys) -> None:
        with patch_in(gate, "subprocess.run", return_value=fake):
            assert gate.check_windows_assets(self.TAG) == 2
        assert "CANNOT VERIFY" in capsys.readouterr().err

    def test_a_missing_gh_is_unverifiable(self, capsys) -> None:
        with patch_in(gate, "subprocess.run", side_effect=FileNotFoundError("gh")):
            assert gate.check_windows_assets(self.TAG) == 2
        assert "CANNOT VERIFY" in capsys.readouterr().err

    def test_the_flag_defaults_to_the_pinned_tag_and_names_it_in_the_gh_call(self) -> None:
        with patch_in(gate, "subprocess.run", return_value=self._release(self.ALL)) as run:
            assert gate.main(["--require-windows"]) == 0
        assert run.call_args[0][0][:3] == ["gh", "release", "view"]
        assert run.call_args[0][0][3] == gate._pinned_engine_tag()
        with patch_in(gate, "subprocess.run", return_value=self._release(self.ALL)) as run:
            assert gate.main(["--require-windows", self.TAG]) == 0
        assert run.call_args[0][0][3] == self.TAG

    @pytest.mark.parametrize("other", [["--ledger-only"], ["--url", _TEST_URL], ["--paired-deploy-auto"], ["--client-precondition"]])
    def test_the_flag_is_mutually_exclusive_with_every_other_mode(self, other) -> None:
        with pytest.raises(SystemExit):
            gate.main(["--require-windows", *other])

    def test_the_default_behaviour_never_consults_the_windows_check(self) -> None:
        """Without the flag nothing about the gate changes: check_windows_assets is not reached."""
        with patch.object(gate, "check_windows_assets") as win, \
             patch.object(gate, "check_floor", return_value=0), \
             patch.object(gate, "check_source_ancestry", return_value=0):
            assert gate.main([]) == 0
        win.assert_not_called()
