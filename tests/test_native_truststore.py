# SPDX-License-Identifier: AGPL-3.0-or-later
"""The native engine's embedded trust store leaves out roots we do not trust (nexus-j36wr).

native-image embeds the build-time default trust store, and the image has no cacerts to fall back
to, so the engine trusts whatever the build JDK trusted: v0.1.161 embedded AAA Certificate
Services (which cross-signed the eMudhra issuing CAs revoked for compromise on 2026-10-18) and the
three emSign roots. ``service/src/build/java/NativeTrustStore.java`` prunes a copy before the image
is built and checks the built executable afterwards; ``service/pom.xml``'s ``native`` profile runs
both. These tests run the real program on the real JDK cacerts against stand-in executables, and
pin the pom wiring. A machine with no JVM FAILS this file instead of skipping it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SERVICE = Path(__file__).resolve().parents[1] / "service"
_PROGRAM = _SERVICE / "src" / "build" / "java" / "NativeTrustStore.java"
_POM = _SERVICE / "pom.xml"
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}

DROP = ["comodoaaaca [jdk]", "emsignrootcag1 [jdk]", "emsignrootcag2 [jdk]", "emsigneccrootcag3 [jdk]"]
KEEP = ["globalsignca [jdk]", "gtsrootcar1 [jdk]"]


def _java() -> Path:
    java = shutil.which("java")
    assert java, "no java on PATH: this file needs a JDK (it fails rather than skip, nexus-moht0)"
    return Path(java)


def _keytool(*args: str) -> subprocess.CompletedProcess[bytes]:
    keytool = _java().resolve().parent / ("keytool.exe" if os.name == "nt" else "keytool")
    return subprocess.run([str(keytool), *args], capture_output=True, check=False, timeout=60)


def _der(alias: str) -> bytes:
    out = _keytool("-exportcert", "-cacerts", "-storepass", "changeit", "-alias", alias)
    assert out.returncode == 0, out.stderr.decode()
    return out.stdout


def _run(mode: str, path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_java()), str(_PROGRAM), mode, str(path)], capture_output=True, text=True, check=False, timeout=180,
    )


def _executable(tmp_path: Path, *aliases: str) -> Path:
    # Image-heap stand-in: the captured KeyStore's aliases between runs of filler, plus the DER of
    # EVERY root, as a real image carries them whatever its trust store (so a DER search would
    # fail every build; verify must key on the aliases).
    filler = bytes(range(256)) * 64
    body = filler + b"".join(a.encode("ascii") + b"\0\0" + filler for a in aliases)
    body += b"".join(_der(a) + filler for a in DROP + KEEP)
    exe = tmp_path / "nexus-service"
    exe.write_bytes(body)
    return exe


def test_prune_drops_the_named_roots_and_keeps_voyages_anchors(tmp_path: Path) -> None:
    out = tmp_path / "native-truststore" / "cacerts"
    run = _run("prune", out)
    assert run.returncode == 0, run.stderr
    listed = _keytool("-list", "-keystore", str(out), "-storepass", "changeit").stdout.decode()
    aliases = {line.split(",")[0] for line in listed.splitlines() if ", trustedCertEntry" in line}
    assert not aliases & set(DROP), aliases & set(DROP)
    assert set(KEEP) <= aliases
    jdk = _keytool("-list", "-cacerts", "-storepass", "changeit").stdout.decode()
    jdk_aliases = {line.split(",")[0] for line in jdk.splitlines() if ", trustedCertEntry" in line}
    assert aliases == jdk_aliases - set(DROP)
    assert len(aliases) > 50, f"a pruned store of {len(aliases)} roots is not the JDK's cacerts"


_TRUSTED = """
import java.security.KeyStore;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;
public class Trusted {
    public static void main(String[] args) throws Exception {
        TrustManagerFactory f = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        f.init((KeyStore) null);
        for (var m : f.getTrustManagers()) {
            if (m instanceof X509TrustManager x) {
                for (var c : x.getAcceptedIssuers()) System.out.println(c.getSubjectX500Principal().getName());
            }
        }
    }
}
"""


def test_the_pruned_store_loads_with_only_the_property_the_build_passes(tmp_path: Path) -> None:
    # The builder captures the default trust store from javax.net.ssl.trustStore alone. A PKCS12
    # written with its defaults (protected bags) loads with ZERO roots that way, which would embed
    # an empty store; this reads it exactly as the build does, with no password.
    out = tmp_path / "cacerts"
    assert _run("prune", out).returncode == 0
    program = tmp_path / "Trusted.java"
    program.write_text(_TRUSTED)
    run = subprocess.run(
        [str(_java()), f"-Djavax.net.ssl.trustStore={out}", str(program)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    subjects = run.stdout.splitlines()
    jdk = _keytool("-list", "-cacerts", "-storepass", "changeit").stdout.decode()
    assert len(subjects) == sum(", trustedCertEntry" in line for line in jdk.splitlines()) - len(DROP)
    assert any("CN=GlobalSign Root CA" in s for s in subjects)
    assert any("CN=GTS Root R1" in s for s in subjects)
    assert not any("AAA Certificate Services" in s or "emSign" in s for s in subjects)


def test_prune_refuses_a_root_of_the_distrusted_hierarchy_that_drop_does_not_name(tmp_path: Path) -> None:
    # A JDK whose cacerts names an emSign or AAA root differently would otherwise keep it silently.
    store = tmp_path / "cacerts"
    for alias in [*KEEP, "comodoaaaca [jdk]"]:
        der = tmp_path / "x.der"
        der.write_bytes(_der(alias))
        name = "renamed-aaa" if alias == "comodoaaaca [jdk]" else alias
        imp = _keytool("-importcert", "-noprompt", "-keystore", str(store), "-storetype", "PKCS12",
                       "-storepass", "changeit", "-alias", name, "-file", str(der))
        assert imp.returncode == 0, imp.stderr.decode()
    run = subprocess.run(
        [str(_java()), f"-Dnexus.truststore.source={store}", str(_PROGRAM), "prune", str(tmp_path / "out")],
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert run.returncode == 1
    assert "kept root renamed-aaa" in run.stderr and "AAA Certificate Services" in run.stderr


def test_verify_passes_an_executable_that_embeds_only_kept_roots(tmp_path: Path) -> None:
    _executable(tmp_path, *KEEP, "digicertglobalrootg2 [jdk]")
    run = _run("verify", tmp_path)
    assert run.returncode == 0, run.stderr
    assert "4 dropped roots absent, 2 required roots present" in run.stdout


def test_verify_fails_an_executable_that_embeds_a_dropped_root(tmp_path: Path) -> None:
    # The released v0.1.161 binary fails exactly this way (all four embedded).
    _executable(tmp_path, *KEEP, "comodoaaaca [jdk]")
    run = _run("verify", tmp_path)
    assert run.returncode == 1
    assert "embeds dropped root comodoaaaca [jdk]" in run.stderr
    assert "emsign" not in run.stderr


def test_verify_fails_when_a_required_root_is_not_embedded(tmp_path: Path) -> None:
    # Non-vacuity: a search that cannot see embedded certificates would pass every dropped root.
    _executable(tmp_path, "gtsrootcar1 [jdk]")
    run = _run("verify", tmp_path)
    assert run.returncode == 1
    assert "does not embed required root globalsignca [jdk]" in run.stderr


def test_verify_fails_where_there_is_no_executable(tmp_path: Path) -> None:
    run = _run("verify", tmp_path)
    assert run.returncode == 1 and "no nexus-service executable" in run.stderr


def test_the_program_and_this_file_name_the_same_roots() -> None:
    src = _PROGRAM.read_text()

    def listed(name: str) -> list[str]:
        block = re.search(rf"List<String> {name} = List\.of\((.*?)\);", src, re.S)
        assert block, name
        return re.findall(r'"([^"]+)"', block.group(1))

    assert (listed("DROP"), listed("KEEP")) == (DROP, KEEP)


def _plugins(parent: ET.Element) -> list[ET.Element]:
    return parent.findall("m:build/m:plugins/m:plugin", _NS)


def _artifact(plugin: ET.Element) -> str:
    return plugin.findtext("m:artifactId", default="", namespaces=_NS)


def test_the_native_profile_builds_from_the_pruned_store_and_verifies_after_the_image() -> None:
    root = ET.parse(_POM).getroot()
    main = [_artifact(p) for p in _plugins(root)]
    # verify shares the package phase with build-native; Maven runs one phase's executions in the
    # order the plugins appear in the main build, so antrun must come after native-maven-plugin.
    assert main.index("maven-antrun-plugin") > main.index("native-maven-plugin"), main

    profile = next(p for p in root.findall("m:profiles/m:profile", _NS) if p.findtext("m:id", namespaces=_NS) == "native")
    plugins = {_artifact(p): p for p in _plugins(profile)}
    build_args = [a.text for a in plugins["native-maven-plugin"].iter(f"{{{_NS['m']}}}buildArg")]
    store = "${project.build.directory}/native-truststore/cacerts"
    assert f"-J-Djavax.net.ssl.trustStore={store}" in build_args
    assert not [a for a in build_args if a and "trustStorePassword" in a], "the store is unprotected; no password"

    executions = {
        e.findtext("m:id", namespaces=_NS): e for e in plugins["maven-antrun-plugin"].findall("m:executions/m:execution", _NS)
    }
    prune, verify = executions["prune-native-truststore"], executions["verify-native-truststore"]
    assert prune.findtext("m:phase", namespaces=_NS) == "prepare-package"
    assert verify.findtext("m:phase", namespaces=_NS) == "package"
    native_phase = plugins["native-maven-plugin"].find("m:executions/m:execution/m:phase", _NS)
    assert native_phase is not None and native_phase.text == "package"

    def args(execution: ET.Element) -> list[str]:
        return [a.get("value", "") for a in execution.iter(f"{{{_NS['m']}}}arg")]

    assert args(prune) == ["${project.basedir}/src/build/java/NativeTrustStore.java", "prune", store]
    assert args(verify) == ["${project.basedir}/src/build/java/NativeTrustStore.java", "verify", "${project.build.directory}"]
