# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/check_engine_cut_riders.py (nexus-ujbz8): the published binaries' size ceilings."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_engine_cut_riders as cr  # noqa: E402

SKILL = REPO_ROOT / ".claude" / "skills" / "engine-release" / "SKILL.md"
_MIB = 1024 * 1024


def _assets(amd: float, arm: float, mac: float) -> list[dict]:
    return [
        {"name": "nexus-service-linux-amd64", "size": int(amd * _MIB)},
        {"name": "nexus-service-linux-arm64", "size": int(arm * _MIB)},
        {"name": "nexus-service-mac-arm64", "size": int(mac * _MIB)},
        {"name": "nexus-service-linux-amd64.sha256", "size": 100},
    ]


def test_the_fixed_sizes_pass() -> None:
    rc, lines = cr.check_sizes(_assets(150, 148, 154))
    assert rc == 0, lines


def test_the_v0_1_142_sizes_fail_on_all_three() -> None:
    rc, lines = cr.check_sizes(_assets(231.8, 227.0, 193.3))
    assert rc == 1
    assert sum(1 for line in lines if line.startswith("TOO BIG")) == 3


def test_one_regressed_binary_fails_alone() -> None:
    rc, lines = cr.check_sizes(_assets(150, 148, 193.3))
    assert rc == 1
    assert [line for line in lines if line.startswith("TOO BIG")][0].count("mac-arm64") == 1


def test_linux_arm64_has_no_looser_ceiling_than_amd64() -> None:
    """nexus-lhr6a never measured arm64; its ceiling follows amd64's logic, not a guess 40 MiB high.
    A 189 MiB arm64 binary (a partial regression, ~40 MiB over the ~147 expected) passed the old 190."""
    assert cr.SIZE_CEILINGS_MIB["nexus-service-linux-arm64"] <= cr.SIZE_CEILINGS_MIB["nexus-service-linux-amd64"]
    rc, lines = cr.check_sizes(_assets(150, 189, 154))
    assert rc == 1, lines
    assert [line for line in lines if line.startswith("TOO BIG")][0].count("linux-arm64") == 1


def test_a_missing_binary_is_a_failure_not_a_skip() -> None:
    assets = [a for a in _assets(150, 148, 154) if a["name"] != "nexus-service-mac-arm64"]
    rc, lines = cr.check_sizes(assets)
    assert rc == 1
    assert any(line.startswith("MISSING") for line in lines)


def test_a_zero_byte_asset_is_a_failure() -> None:
    assets = _assets(150, 148, 154)
    assets[0]["size"] = 0
    rc, _ = cr.check_sizes(assets)
    assert rc == 1


def test_main_reads_a_saved_release_and_overrides_a_ceiling(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    saved = tmp_path / "rel.json"
    saved.write_text(json.dumps({"assets": _assets(150, 148, 154)}))
    assert cr.main(["sizes", "engine-service-vX", "--assets-json", str(saved)]) == 0
    assert cr.main(["sizes", "engine-service-vX", "--assets-json", str(saved), "--max", "nexus-service-linux-amd64=100"]) == 1
    assert cr.main(["sizes", "engine-service-vX", "--assets-json", str(tmp_path / "absent.json")]) == 2
    capsys.readouterr()


def _stub_release_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> Path:
    """A fake release CLI first on PATH: records its argv, answers with release JSON or fails."""
    import os
    import stat

    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    log = tmp_path / "calls.log"
    body = json.dumps({"assets": _assets(150, 148, 154)})
    stub = bindir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$*\" >> '{log}'\n"
        + (f"printf '%s' '{body}'\n" if ok else "echo 'HTTP 502' >&2; exit 1\n")
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_repo_is_passed_through_to_the_release_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`sizes --repo OWNER/REPO` reaches the release CLI as `--repo OWNER/REPO`: the promote script
    runs from a checkout that is not necessarily the release's repo."""
    log = _stub_release_cli(tmp_path, monkeypatch, ok=True)
    assert cr.main(["sizes", "engine-service-vX", "--repo", "owner/repo"]) == 0
    call = log.read_text().strip()
    assert call == "release view engine-service-vX --repo owner/repo --json assets", call


def test_without_repo_no_repo_flag_is_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _stub_release_cli(tmp_path, monkeypatch, ok=True)
    assert cr.main(["sizes", "engine-service-vX"]) == 0
    assert "--repo" not in log.read_text()


def test_a_failing_release_view_is_unverifiable_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    _stub_release_cli(tmp_path, monkeypatch, ok=False)
    assert cr.main(["sizes", "engine-service-vX", "--repo", "owner/repo"]) == 2
    err = capsys.readouterr().err
    assert "UNVERIFIABLE" in err
    assert "HTTP 502" in err, "the failure names the release CLI's own error, not a downstream JSON parse error"


def test_the_engine_release_skill_runs_the_sizes_subcommand() -> None:
    text = SKILL.read_text()
    assert "scripts/check_engine_cut_riders.py sizes" in text


# nexus-f9bgu.9 (RDR-224 P1.2): the Windows engine archive has its own ceiling, checked only
# while the Windows legs are on (`--windows on`), where it is also REQUIRED. Off (the default)
# the archive is neither required nor measured, so a release with the switch off is exactly as before.

WIN = "nexus-service-windows-x64.txz"


def _with_windows(mib: float, assets: list[dict] | None = None) -> list[dict]:
    return [*(assets if assets is not None else _assets(150, 148, 154)), {"name": WIN, "size": int(mib * _MIB)}]


def test_windows_archive_has_its_own_ceiling_and_it_is_not_in_the_default_set() -> None:
    assert WIN in cr.WINDOWS_SIZE_CEILINGS_MIB
    assert WIN not in cr.SIZE_CEILINGS_MIB, "the default set must stay the three binaries the switch-off state publishes"
    assert 40 < cr.WINDOWS_SIZE_CEILINGS_MIB[WIN] <= 70, (
        "measured 32.1 MiB for an -Ob build (2026-10-05); a ceiling far above the -O2 build "
        "stops catching a doubled library or a .pdb"
    )


def test_default_off_ignores_the_windows_archive_whatever_its_size() -> None:
    assert cr.check_sizes(_with_windows(500))[0] == 0
    assert cr.check_sizes(_assets(150, 148, 154))[0] == 0, "absent and off: still a pass"


def test_on_requires_and_measures_the_windows_archive() -> None:
    ceilings = cr.ceilings_for(windows=True)
    assert WIN in ceilings
    assert cr.check_sizes(_with_windows(34), ceilings)[0] == 0
    rc, lines = cr.check_sizes(_with_windows(120), ceilings)
    assert rc == 1 and any(line.startswith("TOO BIG") and WIN in line for line in lines)
    rc, lines = cr.check_sizes(_assets(150, 148, 154), ceilings)
    assert rc == 1 and any(line.startswith("MISSING") and WIN in line for line in lines)


def test_ceilings_for_off_is_exactly_the_default_set() -> None:
    assert cr.ceilings_for(windows=False) == cr.SIZE_CEILINGS_MIB


def test_main_windows_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    saved = tmp_path / "rel.json"
    saved.write_text(json.dumps({"assets": _with_windows(34)}))
    base = ["sizes", "engine-service-vX", "--assets-json", str(saved)]
    assert cr.main(base) == 0
    assert cr.main([*base, "--windows", "off"]) == 0
    assert cr.main([*base, "--windows", "on"]) == 0
    saved.write_text(json.dumps({"assets": _assets(150, 148, 154)}))
    assert cr.main([*base, "--windows", "off"]) == 0
    assert cr.main([*base, "--windows", "on"]) == 1
    assert "nexus-service-windows-x64.txz" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cr.main([*base, "--windows", "maybe"])
