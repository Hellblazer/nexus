"""nexus-cl14i: the forced-failure proof for the engine release's all-or-nothing gate.

The bead requires the fix to be proven by forcing the failure, not by a
green run. Burning a scratch ``engine-service-v*`` tag would cost a real
65-minute three-platform build including a macOS runner, so the assertion
half is a script and this test drives it with a stub ``gh`` on PATH: one
asset missing must exit 1 and must NOT flip the draft; the full set must
flip it exactly once.
"""

from __future__ import annotations

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


def _stub_gh(tmp_path: Path, assets: list[str]) -> tuple[Path, Path]:
    """A fake ``gh`` that answers ``release view`` with *assets* and records
    every ``release edit`` call to a log file."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "gh-calls.log"
    asset_lines = "\n".join(assets)
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f"echo \"$*\" >> '{log}'\n"
        "case \"$1 $2\" in\n"
        f"  'release view') printf '%s\\n' '{asset_lines}' ;;\n"
        "  'release edit') exit 0 ;;\n"
        "  *) echo \"unexpected gh $*\" >&2; exit 99 ;;\n"
        "esac\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return bindir, log


def _run(bindir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    return subprocess.run(
        ["/bin/bash", str(SCRIPT), "engine-service-v0.0.0-test", "owner/repo"],
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


def test_zero_assets_fails_cleanly(tmp_path: Path) -> None:
    bindir, log = _stub_gh(tmp_path, [])
    r = _run(bindir)
    assert r.returncode == 1
    assert "release edit" not in log.read_text()
