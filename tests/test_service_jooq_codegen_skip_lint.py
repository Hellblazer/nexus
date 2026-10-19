# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pins the jOOQ codegen skip wiring in ``service/pom.xml`` (nexus-bnc56).

The two ``testcontainers-jooq-codegen-maven-plugin`` executions each boot their own
``pgvector/pgvector:pg17`` container, and release 0.0.4 (the newest ever published)
gives the build no handle on it: ``DatabaseProps`` carries an image, a user, a password
and a database name, so there is no tmpfs, no memory limit and no command. Measured on
chas 2026-10-19 the container's PGDATA sat on Docker's overlay (the host NVMe) with no
memory cap, twice per Maven invocation, outside ``PgContainerHelper`` and its gate.

The lever that is in the build's hands is not to boot the container when the generated
sources are already current. An ``initialize``-phase Ant step hashes the codegen inputs
(``pom.xml`` plus every changelog directory the execution reads), compares the hash with
the stamp written after the last successful generation, and sets the plugin's ``skip``.
This file keeps that wiring from rotting:

* each execution's ``skip`` is bound to a property the hash step sets;
* the hash covers every ``changeLogDirectory`` the execution reads, DERIVED from the
  execution's own config, so a new changelog root cannot be added without being hashed
  (a stale hit would then ship out-of-date generated sources);
* the stamp is written in a phase AFTER the generation it vouches for;
* ``-Pprebuilt-jooq`` unbinds the stamp steps along with the codegen it replaces;
* ``-Dnexus.jooq.regenerate=true`` is a real override of the skip.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_POM = Path(__file__).resolve().parents[1] / "service" / "pom.xml"
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}

CODEGEN = "testcontainers-jooq-codegen-maven-plugin"
ANTRUN = "maven-antrun-plugin"

MAIN = "generate-jooq-sources"
TEST = "generate-jooq-test-sources"
SKIP_PROP = {MAIN: "nexus.jooq.main.skip", TEST: "nexus.jooq.test.skip"}
HASH_PROP = {MAIN: "nexus.jooq.main.hash", TEST: "nexus.jooq.test.hash"}

#: Maven default-lifecycle order, for the "stamp runs after the generation" check.
LIFECYCLE = [
    "validate", "initialize", "generate-sources", "process-sources",
    "generate-resources", "process-resources", "compile", "process-classes",
    "generate-test-sources", "process-test-sources",
]


def _pom() -> ET.Element:
    return ET.parse(_POM).getroot()


def _plugin(parent: ET.Element, artifact: str) -> ET.Element:
    hits = [
        p for p in parent.findall("m:build/m:plugins/m:plugin", _NS)
        if p.findtext("m:artifactId", namespaces=_NS) == artifact
    ]
    assert len(hits) == 1, f"expected exactly one {artifact}, found {len(hits)}"
    return hits[0]


def _executions(plugin: ET.Element) -> dict[str, ET.Element]:
    return {e.findtext("m:id", namespaces=_NS): e for e in plugin.findall("m:executions/m:execution", _NS)}


def _phase(execution: ET.Element) -> str:
    return execution.findtext("m:phase", namespaces=_NS)


def _target_text(execution: ET.Element) -> str:
    target = execution.find("m:configuration/m:target", _NS)
    assert target is not None, f"execution {execution.findtext('m:id', namespaces=_NS)} has no <target>"
    text = ET.tostring(target, encoding="unicode")
    # ElementTree re-serialises the pom's default namespace as ns0: prefixes.
    return re.sub(r"(</?)ns\d+:", r"\1", re.sub(r'\sxmlns:ns\d+="[^"]*"', "", text))


def _wired(root: ET.Element) -> dict[str, ET.Element]:
    """The antrun executions that belong to this wiring (they name a nexus.jooq property)."""
    return {
        eid: e for eid, e in _executions(_plugin(root, ANTRUN)).items()
        if "nexus.jooq" in _target_text(e)
    }


def _hash_execution(root: ET.Element) -> ET.Element:
    hits = [e for e in _wired(root).values() if _phase(e) == "initialize"]
    assert len(hits) == 1, f"expected one initialize-phase hash execution, found {len(hits)}"
    return hits[0]


def _stamp_execution(root: ET.Element, exec_id: str) -> ET.Element:
    hits = [
        e for e in _wired(root).values()
        if _phase(e) != "initialize" and HASH_PROP[exec_id] in _target_text(e)
    ]
    assert len(hits) == 1, f"expected one stamp execution for {exec_id}, found {len(hits)}"
    return hits[0]


def _codegen_config(root: ET.Element) -> dict[str, ET.Element]:
    execs = _executions(_plugin(root, CODEGEN))
    assert set(execs) == {MAIN, TEST}, f"codegen executions changed: {sorted(execs)}"
    return execs


def _changelog_roots(execution: ET.Element) -> list[str]:
    raw = execution.findtext("m:configuration/m:liquibase/m:changeLogDirectory", namespaces=_NS)
    assert raw, "a codegen execution lost its changeLogDirectory"
    roots = [r.strip().removeprefix("${project.basedir}/") for r in raw.split(",")]
    assert all(r and "${" not in r for r in roots), f"unexpected changeLogDirectory shape: {raw}"
    return roots


def test_the_plugin_has_no_container_knob_so_the_skip_is_the_lever() -> None:
    """The reason this wiring exists: no newer plugin and no tmpfs/memory setting.

    If a release ever adds one, this is the place to notice and prefer it.
    """
    root = _pom()
    assert _plugin(root, CODEGEN).findtext("m:version", namespaces=_NS) == "0.0.4"
    for execution in _codegen_config(root).values():
        database = execution.find("m:configuration/m:database", _NS)
        assert {c.tag.split("}")[1] for c in database} == {"type", "containerImage"}


@pytest.mark.parametrize("exec_id", [MAIN, TEST])
def test_each_codegen_execution_skips_on_its_hash_property(exec_id: str) -> None:
    root = _pom()
    execution = _codegen_config(root)[exec_id]
    assert execution.findtext("m:configuration/m:skip", namespaces=_NS) == "${" + SKIP_PROP[exec_id] + "}"
    # Ant properties are immutable and Maven passes its own in: a default declared in
    # <properties> would win over the hash step's decision and pin the skip forever.
    assert root.find(f"m:properties/m:{SKIP_PROP[exec_id]}", _NS) is None
    assert f'property="{SKIP_PROP[exec_id]}"' in _target_text(_hash_execution(root))


@pytest.mark.parametrize("exec_id", [MAIN, TEST])
def test_the_hash_covers_every_changelog_root_the_execution_reads(exec_id: str) -> None:
    root = _pom()
    text = _target_text(_hash_execution(root))
    kind = "main" if exec_id == MAIN else "test"
    assert f'totalproperty="{HASH_PROP[exec_id]}"' in text and 'algorithm="SHA-256"' in text
    fileset = re.search(
        r'<fileset id="nexus\.jooq\.' + kind + r'\.inputs".*?</fileset>', text, re.DOTALL)
    assert fileset, f"no input fileset for {kind}"
    includes = set(re.findall(r'<include name="([^"]+)"', fileset.group()))
    # The checksum must hash that fileset, not some other one.
    assert text.count(f'refid="nexus.jooq.{kind}.inputs"') >= 2, "hash and name list both read the fileset"
    assert "pom.xml" in includes, "the jOOQ config lives in the pom: a pom change must regenerate"
    for changelog_root in _changelog_roots(_codegen_config(root)[exec_id]):
        assert f"{changelog_root}/db/**" in includes, (
            f"{exec_id} reads {changelog_root} but the hash does not cover {changelog_root}/db/**; "
            "a changelog edit there would skip regeneration and ship stale generated sources")


@pytest.mark.parametrize("exec_id", [MAIN, TEST])
def test_the_stamp_is_written_after_the_generation_it_vouches_for(exec_id: str) -> None:
    root = _pom()
    codegen_phase = LIFECYCLE.index(_phase(_codegen_config(root)[exec_id]))
    assert LIFECYCLE.index(_phase(_stamp_execution(root, exec_id))) > codegen_phase
    assert LIFECYCLE.index(_phase(_hash_execution(root))) < codegen_phase


def test_a_stamp_only_counts_when_the_generated_sources_are_still_there() -> None:
    text = _target_text(_hash_execution(_pom()))
    # Stamp present but sources deleted (a partial clean) must regenerate.
    assert text.count("<resourcecount") >= 2, "both skip decisions check the sources exist"
    assert "nexus.jooq.regenerate" in text, "no -Dnexus.jooq.regenerate=true override"


def test_the_hash_step_exports_its_properties_to_the_plugin_config() -> None:
    plugin = _plugin(_pom(), ANTRUN)
    assert plugin.findtext("m:configuration/m:exportAntProperties", namespaces=_NS) == "true"


def test_prebuilt_profile_unbinds_the_antrun_steps_with_the_codegen_it_replaces() -> None:
    """-Pprebuilt-jooq compiles downloaded sources: a stamp written there would vouch for
    sources this checkout never generated."""
    root = _pom()
    profile = next(
        p for p in root.findall("m:profiles/m:profile", _NS) if p.findtext("m:id", namespaces=_NS) == "prebuilt-jooq"
    )
    unbound = {eid for eid, e in _executions(_plugin(profile, ANTRUN)).items() if _phase(e) == "none"}
    wired = set(_wired(root))
    assert len(wired) == 3, f"expected hash + two stamp executions, found {sorted(wired)}"
    assert wired <= unbound
