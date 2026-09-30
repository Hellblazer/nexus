# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cd8b7 (follow-up to nexus-280ei): the mac-arm64 native build's ABI
floor (macOS 14.0, Sonoma) is enforced by two GraalVM buildArgs
(-H:NativeLinkerOption / -H:CCompilerOption, each carrying
-mmacosx-version-min=14.0) that live in service/pom.xml's native-libs-mac
profile, scoped there deliberately (activation family=mac) rather than in
the shared `native` profile every -Pnative build activates.

That scoping is load-bearing, not cosmetic: 4076319a4 briefly put an
empty-valued form of these same options directly in the shared `native`
profile, and GraalVM's CCompilerInvoker renders an empty
-H:CCompilerOption value unconditionally into the linker argv, which broke
gcc on linux ("gcc: error: : linker input file not found", reproduced in a
gcc:13 container) -- caught and fixed same-day by 3cb2fcf5e, which moved
both buildArgs into native-libs-mac's own plugin config via
`<buildArgs combine.children="append">`.

Nothing before this file protected that fix mechanically (grepped tests/
for NativeLinkerOption/CCompilerOption/280ei prior to this bead: zero
hits) -- a future accidental revert of native-libs-mac's buildArgs, or a
re-introduction of the pin into the shared `native` profile, was only
catchable by burning a full ~40-65min hardware-gated release build (the
exact cost nexus-280ei paid: v0.1.139 draft burned, v0.1.140 needed). This
is the fast (no build, no JVM, XML-parse only), PR-gated regression test
requested in the bead.
"""
from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

POM_PATH = Path(__file__).parent.parent / "service" / "pom.xml"
MAVEN_NS = "http://maven.apache.org/POM/4.0.0"
NS = {"m": MAVEN_NS}

EXPECTED_ABI_FLOOR_ARGS = frozenset(
    {
        "-H:NativeLinkerOption=-mmacosx-version-min=14.0",
        "-H:CCompilerOption=-mmacosx-version-min=14.0",
    }
)


def _profile_build_args(root: ET.Element, profile_id: str) -> set[str]:
    """Every <buildArg> text under the named <profile>'s build config."""
    for profile in root.findall("./m:profiles/m:profile", NS):
        id_el = profile.find("m:id", NS)
        if id_el is not None and id_el.text == profile_id:
            return {
                (el.text or "").strip()
                for el in profile.findall(".//m:buildArg", NS)
                if el.text
            }
    raise AssertionError(
        f"no <profile><id>{profile_id}</id> found in {POM_PATH} — "
        f"profile renamed or removed?"
    )


def test_native_libs_mac_profile_carries_the_abi_floor_pin() -> None:
    """The 14.0 floor pin must still be present in native-libs-mac's own
    native-maven-plugin config. Fails if the pin is removed, renamed, or
    the version drifts off 14.0."""
    root = ET.parse(POM_PATH).getroot()
    args = _profile_build_args(root, "native-libs-mac")
    missing = EXPECTED_ABI_FLOOR_ARGS - args
    assert not missing, (
        f"native-libs-mac profile is missing the mac ABI-floor buildArg(s) "
        f"{missing} — service/pom.xml regressed the nexus-280ei pin "
        f"(minos 14.0 ceiling; keep in lockstep with engine-service-"
        f"release.yml's 'Assert binary ABI floor' step)"
    )


def test_shared_native_profile_does_not_carry_the_mac_only_pin() -> None:
    """The ABI-floor buildArgs must NOT leak into the shared `native`
    profile: every -Pnative build (linux included) activates that
    profile, and 4076319a4 proved an empty-valued form of these exact
    options there breaks gcc on linux. This is the regression that same-day
    fix (3cb2fcf5e) exists to prevent from recurring."""
    root = ET.parse(POM_PATH).getroot()
    shared_args = _profile_build_args(root, "native")
    leaked = shared_args & EXPECTED_ABI_FLOOR_ARGS
    assert not leaked, (
        f"mac-only ABI-floor buildArg(s) {leaked} found in the shared "
        f"`native` profile's buildArgs — this profile activates on EVERY "
        f"-Pnative build including linux, and an empty/mismatched value "
        f"there breaks gcc (nexus-280ei, 4076319a4). Confine these to "
        f"native-libs-mac (activation family=mac) instead."
    )
