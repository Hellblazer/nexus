# SPDX-License-Identifier: AGPL-3.0-or-later
"""Battery legs launch a private copy of the jar they built, taken under the build lease, not the shared target/ path.

Release battery on release/v8.5.1 f24d07391, 2026-11-19 16:16Z: the gate built and stamped
service/target/nexus-service-1.0-SNAPSHOT.jar, verified it, released the service lease, and ten seconds
later launched that path. In between, the battery's dual-gate leg (same worktree, in parallel) took the
lease and restored the gate-jar cache's jar onto the same path (dual.log: "gate jar cache HIT key=de032621...").
The service then served the cached jar's build_ref (ce86dad8c+1795102251-17121, an earlier battery's) and the
artifact-identity check failed; a rerun passed on timing alone. The fix copies the verified jar into the
gate's scratch dir while the lease is still held, re-checks the copy's exact build_ref, and launches the copy.
"""

from __future__ import annotations

import re
import subprocess
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HELPER = REPO / "scripts" / "lib" / "gate-jar-cache.sh"
GATE = REPO / "tests" / "e2e" / "local-service-gate.sh"
DUAL = REPO / "tests" / "e2e" / "dual-embedder-gate.sh"


def _jar(path: Path, build_ref: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("META-INF/nexus/release.properties", f"release_version=0.1.197\nbuild_ref={build_ref}\nname=nexus\n")
        z.writestr("dev/nexus/Main.class", b"\xca\xfe\xba\xbe")
    return path


def _run(jar: Path, dest: Path, build_ref: str) -> subprocess.CompletedProcess[str]:
    script = f'source "{HELPER}"; gate_jar_private_copy "$1" "$2" "$3"'
    return subprocess.run(["bash", "-c", script, "_", str(jar), str(dest), build_ref],
                          capture_output=True, text=True, check=False, timeout=60)


def _ref_in(jar: Path) -> str:
    with zipfile.ZipFile(jar) as z:
        props = z.read("META-INF/nexus/release.properties").decode()
    m = re.search(r"^build_ref=(.*)$", props, re.M)
    assert m, props
    return m.group(1)


def test_the_verified_jar_is_copied_to_the_private_path(tmp_path: Path) -> None:
    jar = _jar(tmp_path / "target.jar", "f24d07391+1795104992-49774")
    dest = tmp_path / "scratch" / "gate-service.jar"
    dest.parent.mkdir()
    r = _run(jar, dest, "f24d07391+1795104992-49774")
    assert r.returncode == 0, r.stderr
    assert dest.read_bytes() == jar.read_bytes()


def test_a_sibling_rewriting_the_shared_jar_afterwards_does_not_reach_the_copy(tmp_path: Path) -> None:
    jar = _jar(tmp_path / "target.jar", "f24d07391+1795104992-49774")
    dest = tmp_path / "gate-service.jar"
    assert _run(jar, dest, "f24d07391+1795104992-49774").returncode == 0
    # The dual-gate cache restore, in place (a hard link would carry this into the launched jar).
    _jar(jar, "ce86dad8c+1795102251-17121")
    assert _ref_in(dest) == "f24d07391+1795104992-49774"
    # ... and by unlink-and-relink.
    jar.unlink()
    _jar(jar, "ce86dad8c+1795102251-17121")
    assert _ref_in(dest) == "f24d07391+1795104992-49774"


def test_a_jar_that_does_not_carry_this_runs_build_ref_is_refused(tmp_path: Path) -> None:
    jar = _jar(tmp_path / "target.jar", "ce86dad8c+1795102251-17121")
    dest = tmp_path / "gate-service.jar"
    r = _run(jar, dest, "f24d07391+1795104992-49774")
    assert r.returncode != 0
    assert "f24d07391+1795104992-49774" in r.stderr and "ce86dad8c+1795102251-17121" in r.stderr
    assert not dest.exists(), "a refused copy is removed, so nothing can launch it"


def test_the_build_ref_must_match_exactly_not_as_a_prefix(tmp_path: Path) -> None:
    jar = _jar(tmp_path / "target.jar", "abc1")
    dest = tmp_path / "gate-service.jar"
    assert _run(jar, dest, "abc").returncode != 0
    assert not dest.exists()


def test_a_jar_with_no_release_properties_is_refused(tmp_path: Path) -> None:
    jar = tmp_path / "target.jar"
    with zipfile.ZipFile(jar, "w") as z:
        z.writestr("dev/nexus/Main.class", b"\xca\xfe\xba\xbe")
    dest = tmp_path / "gate-service.jar"
    assert _run(jar, dest, "abc").returncode != 0
    assert not dest.exists()


def test_the_gate_copies_under_the_lease_and_launches_only_the_copy() -> None:
    text = GATE.read_text()
    calls = [m.start() for m in re.finditer(r"gate_jar_private_copy \"\$JAR\"", text)]
    assert len(calls) == 1, f"expected one gate_jar_private_copy call in the gate, found {len(calls)}"
    build = text.index("./mvnw -q package -DskipTests")
    restores = [m.start() for m in re.finditer(r"release_props_restore_and_release ", text)]
    success_restore = max(r for r in restores if r < text.index("# 3. Resolve a launch artifact"))
    assert build < calls[0] < success_restore, "the copy is taken after the build and before the lease is released"
    launches = re.findall(r"NEXUS_SERVICE_JAR=([^\")\s]+)", text)
    assert launches == ["$GATE_JAR"], f"the gate launches only its private copy, found {launches}"


def test_the_dual_gate_boots_only_the_copy_build_gate_jar_made_under_its_lease() -> None:
    # The mirror case: dual-gate's own copy used to be a plain cp after build-gate-jar.sh had released the lease,
    # so local-service-gate.sh's rebuild could land between the two (scripts/build-gate-jar_test.sh Test 13 pins
    # that --copy-to copies under the lease).
    text = DUAL.read_text()
    builds = re.findall(r"build-gate-jar\.sh\"[^\n]*", text)
    assert len(builds) == 1 and '--copy-to "$SCRATCH/engine.jar"' in builds[0], builds
    assert re.search(r'^\s*DG_JAR="\$SCRATCH/engine\.jar"', text, re.M)
    assert '"copied $DG_JAR"' in text, "the leg refuses unless build-gate-jar.sh reports the copy"
    assert not re.search(r'^\s*cp "\$DG_JAR"', text, re.M), "no copy after the lease is released"
    assert re.findall(r"NEXUS_SERVICE_JAR=([^\s;]+)", text) == ['"$DG_JAR"']
