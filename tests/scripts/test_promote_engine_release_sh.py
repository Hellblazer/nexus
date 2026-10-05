"""nexus-cl14i: the forced-failure proof for the engine release's all-or-nothing gate.

The bead requires the fix to be proven by forcing the failure, not by a
green run. Burning a scratch ``engine-service-v*`` tag would cost a real
65-minute three-platform build including a macOS runner, so the assertion
half is a script and this test drives it with a stub ``gh`` on PATH: one
asset missing must exit 1 and must NOT flip the draft; the full set must
flip it exactly once.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "promote_engine_release.sh"

ARCHES = ("linux-amd64", "linux-arm64", "mac-arm64")


def _all_assets() -> list[str]:
    out: list[str] = []
    for arch in ARCHES:
        b = f"nexus-service-{arch}"
        out += [b, f"{b}.sha256", f"{b}.cosign.bundle", f"{b}.sigstore.json"]
        p = f"nexus-pg-{arch}.txz"
        out += [p, f"{p}.sha256", f"{p}.sigstore.json"]
    return out


_MIB = 1024 * 1024

#: The fixed build's binary sizes in MiB (nexus-lhr6a measured amd64 and mac; arm64 is an estimate).
FIXED_SIZES_MIB = {"linux-amd64": 150, "linux-arm64": 147, "mac-arm64": 154}


def _stub_gh(
    tmp_path: Path, assets: list[str], sizes_mib: dict[str, float] | None = None,
    *, json_view_fails: bool = False,
) -> tuple[Path, Path]:
    """A fake ``gh`` that answers ``release view`` with *assets* (names, via ``--jq``) or
    their JSON with sizes (``--json assets``), and records every call to a log file."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gh-calls.log"
    asset_lines = "\n".join(assets)
    sizes = FIXED_SIZES_MIB if sizes_mib is None else sizes_mib
    body = json.dumps({"assets": [
        {"name": a, "size": int(sizes[a.removeprefix("nexus-service-")] * _MIB)
         if a.removeprefix("nexus-service-") in sizes else 100}
        for a in assets
    ]})
    (tmp_path / "assets.json").write_text(body)
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$*\" >> '{log}'\n"
        "case \"$1 $2\" in\n"
        "  'release view')\n"
        "    case \"$*\" in\n"
        f"      *--jq*) printf '%s\\n' '{asset_lines}' ;;\n"
        + (
            "      *) echo 'gh: HTTP 502 from the release API' >&2; exit 1 ;;\n"
            if json_view_fails
            else f"      *) cat '{tmp_path / 'assets.json'}' ;;\n"
        ) +
        "    esac ;;\n"
        "  'release edit') exit 0 ;;\n"
        "  *) echo \"unexpected gh $*\" >&2; exit 99 ;;\n"
        "esac\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return bindir, log


def _run(bindir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    """Run the script; *extra* is the optional third argument (the Windows switch)."""
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), "engine-service-v0.0.0-test", "owner/repo", *extra],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )


def test_complete_asset_set_promotes_exactly_once(tmp_path: Path) -> None:
    bindir, log = _stub_gh(tmp_path, _all_assets())
    r = _run(bindir)
    assert r.returncode == 0, r.stderr + r.stdout
    calls = log.read_text().splitlines()
    edits = [c for c in calls if c.startswith("release edit")]
    assert len(edits) == 1 and "--draft=false" in edits[0], calls
    assert "all 21 expected assets present" in r.stdout


@pytest.mark.parametrize("missing", ["nexus-service-linux-amd64", "nexus-service-mac-arm64.cosign.bundle", "nexus-pg-linux-arm64.txz.sigstore.json"])
def test_one_missing_asset_fails_and_leaves_the_draft(tmp_path: Path, missing: str) -> None:
    assets = [a for a in _all_assets() if a != missing]
    bindir, log = _stub_gh(tmp_path, assets)
    r = _run(bindir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert missing in r.stdout
    assert "DRAFT" in r.stdout
    assert not any(c.startswith("release edit") for c in log.read_text().splitlines()), (
        "a missing asset must never flip the draft flag"
    )


@pytest.mark.parametrize("arch", ARCHES)
def test_an_oversized_binary_leaves_the_draft(tmp_path: Path, arch: str) -> None:
    """nexus-ujbz8: the size ceiling is BLOCKING here, so a regression never publishes."""
    sizes = dict(FIXED_SIZES_MIB)
    sizes[arch] = 231.8
    bindir, log = _stub_gh(tmp_path, _all_assets(), sizes)
    r = _run(bindir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "TOO BIG" in r.stdout and f"nexus-service-{arch}" in r.stdout
    assert "DRAFT" in r.stdout
    assert not any(c.startswith("release edit") for c in log.read_text().splitlines()), (
        "an oversized binary must never flip the draft flag"
    )


def test_unreadable_sizes_leave_the_draft(tmp_path: Path) -> None:
    """Fail closed: a release whose asset JSON has no sizes is never promoted on the missing evidence."""
    bindir, log = _stub_gh(tmp_path, _all_assets(), {"linux-amd64": 0, "linux-arm64": 0, "mac-arm64": 0})
    r = _run(bindir)
    assert r.returncode == 1, r.stdout + r.stderr
    assert not any(c.startswith("release edit") for c in log.read_text().splitlines())


def test_a_failing_json_release_view_fails_closed_and_leaves_the_draft(tmp_path: Path) -> None:
    """All 21 names are present, then the second `gh release view --json assets` (the one that
    carries the sizes) fails: the draft must stay, because the sizes were never read."""
    bindir, log = _stub_gh(tmp_path, _all_assets(), json_view_fails=True)
    r = _run(bindir)
    assert r.returncode != 0, r.stdout + r.stderr
    calls = log.read_text().splitlines()
    assert sum(c.startswith("release view") for c in calls) == 2, calls
    assert not any(c.startswith("release edit") for c in calls), (
        "a release whose sizes could not be fetched must never be promoted"
    )


def test_zero_assets_fails_cleanly(tmp_path: Path) -> None:
    bindir, log = _stub_gh(tmp_path, [])
    r = _run(bindir)
    assert r.returncode == 1
    assert "release edit" not in log.read_text()


# nexus-f9bgu.14 (RDR-224 P0.4): the Windows assets BLOCK promotion, but only once the repo
# variable NX_WINDOWS_RELEASE_LEGS is on. The workflow passes the switch in as the third
# argument ("on" or "off"); the script reads no GitHub state and no environment.

WINDOWS_PG_ASSETS = [
    "nexus-pg-windows-x64.txz",
    "nexus-pg-windows-x64.txz.sha256",
    "nexus-pg-windows-x64.txz.sigstore.json",
]


def _edits(log: Path) -> list[str]:
    return [c for c in log.read_text().splitlines() if c.startswith("release edit")]


def test_switch_off_is_the_default_and_expects_the_21_assets(tmp_path: Path) -> None:
    """No third argument and an explicit "off" behave the same: 21 assets, no Windows."""
    bindir, log = _stub_gh(tmp_path, _all_assets())
    r = _run(bindir, "off")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "all 21 expected assets present" in r.stdout
    assert len(_edits(log)) == 1


def test_switch_off_does_not_wait_for_windows_assets(tmp_path: Path) -> None:
    """Non-vacuity of the off state: the Windows names are absent from the release, and the
    release still promotes. A script that expected them regardless would exit 1 here."""
    assets = _all_assets()
    assert not set(WINDOWS_PG_ASSETS) & set(assets)
    bindir, log = _stub_gh(tmp_path, assets)
    r = _run(bindir, "off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_edits(log)) == 1


def test_switch_off_ignores_windows_assets_that_happen_to_be_attached(tmp_path: Path) -> None:
    bindir, log = _stub_gh(tmp_path, _all_assets() + WINDOWS_PG_ASSETS)
    r = _run(bindir, "off")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all 21 expected assets present" in r.stdout


def test_switch_on_with_the_full_windows_set_promotes_exactly_once(tmp_path: Path) -> None:
    bindir, log = _stub_gh(tmp_path, _all_assets() + WINDOWS_PG_ASSETS)
    r = _run(bindir, "on")
    assert r.returncode == 0, r.stderr + r.stdout
    assert "all 24 expected assets present" in r.stdout
    edits = _edits(log)
    assert len(edits) == 1 and "--draft=false" in edits[0], edits


def test_switch_on_without_any_windows_asset_leaves_the_draft(tmp_path: Path) -> None:
    """P0.4: a missing Windows leg keeps every platform's release a draft."""
    bindir, log = _stub_gh(tmp_path, _all_assets())
    r = _run(bindir, "on")
    assert r.returncode == 1, r.stdout + r.stderr
    for name in WINDOWS_PG_ASSETS:
        assert name in r.stdout
    assert "DRAFT" in r.stdout
    assert not _edits(log), "a missing Windows asset must never flip the draft flag"


@pytest.mark.parametrize("missing", WINDOWS_PG_ASSETS)
def test_switch_on_names_each_single_missing_windows_asset(tmp_path: Path, missing: str) -> None:
    assets = _all_assets() + [a for a in WINDOWS_PG_ASSETS if a != missing]
    bindir, log = _stub_gh(tmp_path, assets)
    r = _run(bindir, "on")
    assert r.returncode == 1, r.stdout + r.stderr
    assert missing in r.stdout
    assert not _edits(log)


@pytest.mark.parametrize("bad", ["ON", "true", "1", "", "yes"])
def test_a_switch_value_other_than_on_or_off_is_refused_before_gh(tmp_path: Path, bad: str) -> None:
    """The workflow normalises to on/off; anything else here is a wiring bug, not an "off"."""
    bindir, log = _stub_gh(tmp_path, _all_assets())
    r = _run(bindir, bad)
    assert r.returncode == 2, r.stdout + r.stderr
    assert not log.exists() or not log.read_text().strip(), "gh must not be called on a bad switch"
