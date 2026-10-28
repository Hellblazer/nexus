# SPDX-License-Identifier: AGPL-3.0-or-later
"""The native engine's embedded trust store leaves out roots we do not trust (nexus-j36wr).

native-image embeds the build-time default trust store, and the image has no cacerts to fall back
to, so the engine trusts whatever the build JDK trusted: v0.1.161 embedded AAA Certificate
Services (which cross-signed the eMudhra issuing CAs revoked for compromise on 2026-10-18) and the
three emSign roots. ``service/src/build/java/NativeTrustStore.java`` prunes a copy before the image
is built and checks the built executable afterwards; ``service/pom.xml``'s ``native`` profile runs
both. These tests run the real program against stand-in executables and a generated source store
named like a JDK cacerts (the build reads GraalVM's, whose aliases are "<name> [jdk]"; a Debian
OpenJDK's cacerts is the system bundle under other aliases, so the tests cannot read the runner's
own), and pin the pom wiring. A machine with no JVM FAILS this file instead of skipping it.
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


#: The generated source store: each JDK alias the program names, with the subject of the real root,
#: plus two unrelated roots so "everything but DROP is kept" has something to keep.
_ROOTS = {
    "comodoaaaca [jdk]": "CN=AAA Certificate Services, O=Comodo CA Limited, C=GB",
    "emsignrootcag1 [jdk]": "CN=emSign Root CA - G1, O=eMudhra Technologies Limited, C=IN",
    "emsignrootcag2 [jdk]": "CN=emSign Root CA - G2, O=eMudhra Technologies Limited, C=IN",
    "emsigneccrootcag3 [jdk]": "CN=emSign ECC Root CA - G3, O=eMudhra Technologies Limited, C=IN",
    "globalsignca [jdk]": "CN=GlobalSign Root CA, OU=Root CA, O=GlobalSign nv-sa, C=BE",
    "gtsrootcar1 [jdk]": "CN=GTS Root R1, O=Google Trust Services LLC, C=US",
    "controlroota [jdk]": "CN=Control Root A, O=Test, C=US",
    "controlrootb [jdk]": "CN=Control Root B, O=Test, C=US",
}


def _store(directory: Path, roots: dict[str, str]) -> Path:
    """A PKCS12 of self-signed trusted-certificate entries, one per alias, as cacerts holds them."""
    directory.mkdir(parents=True, exist_ok=True)
    store = directory / "cacerts"
    for i, (alias, subject) in enumerate(roots.items()):
        keys = directory / f"k{i}.p12"
        der = directory / f"c{i}.der"
        for args in (
            ("-genkeypair", "-keystore", str(keys), "-storetype", "PKCS12", "-storepass", "changeit",
             "-alias", "k", "-keyalg", "EC", "-groupname", "secp256r1", "-dname", subject, "-validity", "3650"),
            ("-exportcert", "-keystore", str(keys), "-storepass", "changeit", "-alias", "k", "-file", str(der)),
            ("-importcert", "-noprompt", "-keystore", str(store), "-storetype", "PKCS12", "-storepass", "changeit",
             "-alias", alias, "-file", str(der)),
        ):
            run = _keytool(*args)
            assert run.returncode == 0, run.stderr.decode()
    return store


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _store(tmp_path_factory.mktemp("source"), _ROOTS)


def _der(store: Path, alias: str) -> bytes:
    out = _keytool("-exportcert", "-keystore", str(store), "-storepass", "changeit", "-alias", alias)
    assert out.returncode == 0, out.stderr.decode()
    return out.stdout


def _aliases(store: Path) -> set[str]:
    listed = _keytool("-list", "-keystore", str(store), "-storepass", "changeit").stdout.decode()
    return {line.split(",")[0] for line in listed.splitlines() if ", trustedCertEntry" in line}


def _run(mode: str, path: Path, source: Path, *props: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_java()), f"-Dnexus.truststore.source={source}", *props, str(_PROGRAM), mode, str(path)],
        capture_output=True, text=True, check=False, timeout=180,
    )


def _executable(tmp_path: Path, source: Path, *aliases: str) -> Path:
    # Image-heap stand-in: the captured KeyStore's aliases between runs of filler, plus the DER of
    # EVERY root, as a real image carries them whatever its trust store (so a DER search would
    # fail every build; verify must key on the aliases).
    filler = bytes(range(256)) * 64
    body = filler + b"".join(a.encode("ascii") + b"\0\0" + filler for a in aliases)
    body += b"".join(_der(source, a) + filler for a in DROP + KEEP)
    exe = tmp_path / "nexus-service"
    exe.write_bytes(body)
    return exe


def test_prune_drops_the_named_roots_and_keeps_everything_else(tmp_path: Path, source: Path) -> None:
    out = tmp_path / "native-truststore" / "cacerts"
    run = _run("prune", out, source)
    assert run.returncode == 0, run.stderr
    assert _aliases(out) == set(_ROOTS) - set(DROP)
    assert "4 of 4 named roots dropped" in run.stdout


def test_prune_refuses_a_jdk_without_voyages_anchors(tmp_path: Path) -> None:
    roots = {a: s for a, s in _ROOTS.items() if a != "globalsignca [jdk]"}
    run = _run("prune", tmp_path / "out", _store(tmp_path / "src", roots))
    assert run.returncode == 1 and "has no globalsignca [jdk]" in run.stderr


def test_prune_passes_a_jdk_already_pruned_of_the_dropped_roots(tmp_path: Path) -> None:
    # The CVE-2026-91403 pool sweep pruned these roots from every pool JDK (labrun-5rb), and patched
    # GraalVM builds ship without them; a build on such a JDK reaches the end state with nothing to
    # drop. engine-service-v0.1.166's mac-arm64 leg failed here before this (run 40588808885).
    roots = {a: s for a, s in _ROOTS.items() if a in KEEP or a.startswith("control")}
    out = tmp_path / "out"
    run = _run("prune", out, _store(tmp_path / "src", roots))
    assert run.returncode == 0, run.stderr
    assert "0 of 4 named roots dropped (already absent from this JDK)" in run.stdout
    assert _aliases(out) == set(roots)


def test_verify_passes_on_a_jdk_already_pruned_of_the_dropped_roots(tmp_path: Path) -> None:
    roots = {a: s for a, s in _ROOTS.items() if a in KEEP or a.startswith("control")}
    pruned_jdk = _store(tmp_path / "src", roots)
    filler = bytes(range(256)) * 64
    (tmp_path / "nexus-service").write_bytes(filler + b"".join(a.encode("ascii") + b"\0\0" + filler for a in KEEP))
    run = _run("verify", tmp_path, pruned_jdk)
    assert run.returncode == 0, run.stderr
    assert "0 dropped roots absent, 2 required roots present" in run.stdout


def test_verify_fails_on_a_pruned_jdk_that_keeps_a_distrusted_root_under_another_name(tmp_path: Path) -> None:
    roots = {a: s for a, s in _ROOTS.items() if a in KEEP or a.startswith("control")}
    roots["renamed-emsign"] = _ROOTS["emsignrootcag1 [jdk]"]
    jdk = _store(tmp_path / "src", roots)
    filler = bytes(range(256)) * 64
    (tmp_path / "nexus-service").write_bytes(filler + b"".join(a.encode("ascii") + b"\0\0" + filler for a in KEEP))
    run = _run("verify", tmp_path, jdk)
    assert run.returncode == 1
    assert "distrusted root renamed-emsign" in run.stderr


_TRUSTED = """
import java.security.KeyStore;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;
public class Trusted {
    public static void main(String[] args) throws Exception {
        TrustManagerFactory f = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        f.init((KeyStore) null);
        for (javax.net.ssl.TrustManager m : f.getTrustManagers()) {
            if (m instanceof X509TrustManager) {
                for (java.security.cert.X509Certificate c : ((X509TrustManager) m).getAcceptedIssuers()) {
                    System.out.println(c.getSubjectX500Principal().getName());
                }
            }
        }
    }
}
"""


def test_the_pruned_store_loads_with_only_the_property_the_build_passes(tmp_path: Path, source: Path) -> None:
    # The builder captures the default trust store from javax.net.ssl.trustStore alone. A PKCS12
    # written with its defaults (protected bags) loads with ZERO roots that way, which would embed
    # an empty store; this reads it exactly as the build does, with no password.
    out = tmp_path / "cacerts"
    assert _run("prune", out, source).returncode == 0
    program = tmp_path / "Trusted.java"
    program.write_text(_TRUSTED)
    run = subprocess.run(
        [str(_java()), f"-Djavax.net.ssl.trustStore={out}", str(program)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    subjects = run.stdout.splitlines()
    assert len(subjects) == len(_ROOTS) - len(DROP), subjects
    assert any("CN=GlobalSign Root CA" in s for s in subjects)
    assert any("CN=GTS Root R1" in s for s in subjects)
    assert not any("AAA Certificate Services" in s or "emSign" in s for s in subjects)


def test_prune_refuses_a_root_of_the_distrusted_hierarchy_that_drop_does_not_name(tmp_path: Path) -> None:
    # A JDK whose cacerts names an emSign or AAA root differently would otherwise keep it silently.
    roots = {a: s for a, s in _ROOTS.items() if a != "comodoaaaca [jdk]"}
    roots["renamed-aaa"] = _ROOTS["comodoaaaca [jdk]"]
    run = _run("prune", tmp_path / "out", _store(tmp_path / "src", roots))
    assert run.returncode == 1
    assert "kept root renamed-aaa" in run.stderr and "AAA Certificate Services" in run.stderr


def test_verify_passes_an_executable_that_embeds_only_kept_roots(tmp_path: Path, source: Path) -> None:
    _executable(tmp_path, source, *KEEP, "controlroota [jdk]")
    run = _run("verify", tmp_path, source)
    assert run.returncode == 0, run.stderr
    assert "4 dropped roots absent, 2 required roots present" in run.stdout


def test_verify_fails_an_executable_that_embeds_a_dropped_root(tmp_path: Path, source: Path) -> None:
    # The released v0.1.161 binary fails exactly this way (all four embedded).
    _executable(tmp_path, source, *KEEP, "comodoaaaca [jdk]")
    run = _run("verify", tmp_path, source)
    assert run.returncode == 1
    assert "embeds dropped root comodoaaaca [jdk]" in run.stderr
    assert "emsign" not in run.stderr


def test_verify_fails_when_a_required_root_is_not_embedded(tmp_path: Path, source: Path) -> None:
    # Non-vacuity: a search that cannot see the captured store would pass every dropped root.
    _executable(tmp_path, source, "gtsrootcar1 [jdk]")
    run = _run("verify", tmp_path, source)
    assert run.returncode == 1
    assert "does not embed required root globalsignca [jdk]" in run.stderr


def test_verify_fails_where_there_is_no_executable(tmp_path: Path, source: Path) -> None:
    run = _run("verify", tmp_path, source)
    assert run.returncode == 1 and "no nexus-service executable" in run.stderr


# nexus-37n79: stand-in for `nexus-service --trust-anchors`, the engine's TrustAnchors mode: the
# default TrustManager over the store named in its source, printed in the engine's protocol.
_ANCHORS_PROBE = """
import java.security.KeyStore;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;
public class Probe {
    public static void main(String[] args) throws Exception {
        if (args.length != 1 || !args[0].equals("--trust-anchors")) throw new IllegalArgumentException(String.join(" ", args));
        System.setProperty("javax.net.ssl.trustStore", "%s");
        // keytool's stand-in stores protect their bags (the pruned store does not); the password
        // makes every store here load all its roots.
        System.setProperty("javax.net.ssl.trustStorePassword", "changeit");
        TrustManagerFactory f = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
        f.init((KeyStore) null);
        java.util.List<java.security.cert.X509Certificate> issuers = new java.util.ArrayList<>();
        for (javax.net.ssl.TrustManager m : f.getTrustManagers()) {
            if (m instanceof X509TrustManager x) issuers.addAll(java.util.List.of(x.getAcceptedIssuers()));
        }
        System.out.println("engine log line before the protocol");
        System.out.println("trust_anchors count=" + issuers.size());
        for (java.security.cert.X509Certificate c : issuers) {
            String fp = java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(c.getEncoded()));
            System.out.println("trust_anchor sha256=" + fp + " " + c.getSubjectX500Principal().getName());
        }
    }
}
"""


def _anchors(tmp_path: Path, source: Path, probe_body: str) -> subprocess.CompletedProcess[str]:
    """Prune into <tmp>/native-truststore/cacerts, as the build does, then run anchors with a probe."""
    assert _run("prune", tmp_path / "native-truststore" / "cacerts", source).returncode == 0
    probe = tmp_path / "Probe.java"
    probe.write_text(probe_body)
    return _run("anchors", tmp_path, source, f"-Dnexus.truststore.probe={probe}")


def test_anchors_passes_an_executable_that_trusts_exactly_the_pruned_store(tmp_path: Path, source: Path) -> None:
    store = (tmp_path / "native-truststore" / "cacerts").as_posix()
    run = _anchors(tmp_path, source, _ANCHORS_PROBE % store)
    assert run.returncode == 0, run.stderr
    assert f"trusts {len(_ROOTS) - len(DROP)} roots" in run.stdout and "none distrusted" in run.stdout


def test_anchors_fails_an_executable_that_trusts_the_unpruned_store(tmp_path: Path, source: Path) -> None:
    # What v0.1.161 does: the build JDK's whole cacerts, AAA and emSign included.
    run = _anchors(tmp_path, source, _ANCHORS_PROBE % source.as_posix())
    assert run.returncode == 1
    assert f"trusts {len(_ROOTS)} roots, the pruned store holds {len(_ROOTS) - len(DROP)}" in run.stderr
    assert "trusts CN=AAA Certificate Services" in run.stderr and "emSign Root CA - G1" in run.stderr


def test_anchors_fails_an_executable_that_does_not_trust_a_required_root(tmp_path: Path, source: Path) -> None:
    # Same count as the pruned store, one KEEP root swapped for an unrelated one: the subject check
    # catches what a count alone would pass.
    roots = {a: s for a, s in _ROOTS.items() if a not in DROP and a != "gtsrootcar1 [jdk]"}
    roots["swapped [jdk]"] = "CN=Swapped Root, O=Test, C=US"
    other = _store(tmp_path / "other", roots)
    run = _anchors(tmp_path, source, _ANCHORS_PROBE % other.as_posix())
    assert run.returncode == 1
    assert "does not trust required root gtsrootcar1 [jdk]" in run.stderr
    assert "extra [CN=Swapped Root,O=Test,C=US]" in run.stderr


def test_anchors_fails_an_executable_that_trusts_a_different_certificate_under_the_same_subject(
    tmp_path: Path, source: Path
) -> None:
    # A re-keyed root: every subject and the count match the pruned store, only the certificate
    # differs. Subject equality passes it; the fingerprint comparison must not.
    roots = {a: s for a, s in _ROOTS.items() if a not in DROP}
    rekeyed = _store(tmp_path / "rekeyed", roots)
    run = _anchors(tmp_path, source, _ANCHORS_PROBE % rekeyed.as_posix())
    assert run.returncode == 1
    assert "subjects differ" not in run.stderr
    assert "certificates differ from the pruned store" in run.stderr
    assert "does not trust required root globalsignca [jdk]" in run.stderr


def test_anchors_fails_an_executable_that_prints_no_count(tmp_path: Path, source: Path) -> None:
    # Non-vacuity: an executable that ignores the flag and exits 0 must not pass.
    silent = "public class Probe { public static void main(String[] args) { System.out.println(\"started\"); } }"
    run = _anchors(tmp_path, source, silent)
    assert run.returncode == 1 and "printed no trust_anchors line" in run.stderr


def test_anchors_fails_an_executable_that_exits_nonzero(tmp_path: Path, source: Path) -> None:
    failing = "public class Probe { public static void main(String[] args) { System.exit(3); } }"
    run = _anchors(tmp_path, source, failing)
    assert run.returncode == 1 and "exited 3" in run.stderr


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
    anchors = executions["verify-native-trust-anchors"]
    assert prune.findtext("m:phase", namespaces=_NS) == "prepare-package"
    assert verify.findtext("m:phase", namespaces=_NS) == "package"
    assert anchors.findtext("m:phase", namespaces=_NS) == "package"
    native_phase = plugins["native-maven-plugin"].find("m:executions/m:execution/m:phase", _NS)
    assert native_phase is not None and native_phase.text == "package"

    def args(execution: ET.Element) -> list[str]:
        return [a.get("value", "") for a in execution.iter(f"{{{_NS['m']}}}arg")]

    assert args(prune) == ["${project.basedir}/src/build/java/NativeTrustStore.java", "prune", store]
    assert args(verify) == ["${project.basedir}/src/build/java/NativeTrustStore.java", "verify", "${project.build.directory}"]
    assert args(anchors) == ["${project.basedir}/src/build/java/NativeTrustStore.java", "anchors", "${project.build.directory}"]
