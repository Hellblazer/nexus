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
* the safety core is pinned piece by piece (the stamp-vs-hash comparison, the regenerate
  guard, the loadfile path equal to the stamp-write path, the names file with the tool
  versions in the checksum union, the delete-on-regenerate step, the hand-set-skip failure);
* every changelog ``include``/``includeAll``/``sqlFile`` path resolves under ``db/``, the only
  tree the hash covers.

``tests/test_service_jooq_codegen_skip_behaviour.py`` runs the Ant logic itself.
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
    """The execution's ``<target>`` as written in the pom (raw text: ElementTree would
    rename the ``ant:unless`` namespace prefix this wiring uses)."""
    exec_id = execution.findtext("m:id", namespaces=_NS)
    for m in re.finditer(
        r"<execution>\s*<id>" + re.escape(exec_id) + r"</id>.*?</execution>", _POM.read_text(encoding="utf-8"), re.DOTALL
    ):
        t = re.search(r"<target[^>]*>.*?</target>", m.group(), re.DOTALL)
        if t:
            return t.group()
    raise AssertionError(f"execution {exec_id} has no <target>")


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


# --- the safety core: the pieces whose loss turns the skip into a stale-tree generator ----------

KINDS = ["main", "test"]
EXEC_OF = {"main": MAIN, "test": TEST}


def _condition(text: str, kind: str) -> str:
    m = re.search(rf'<condition property="nexus\.jooq\.{kind}\.skip".*?</condition>', text, re.DOTALL)
    assert m, f"no skip <condition> for {kind}"
    return m.group()


@pytest.mark.parametrize("kind", KINDS)
def test_the_skip_decision_compares_the_stamp_with_the_hash_and_honours_the_override(kind: str) -> None:
    cond = _condition(_target_text(_hash_execution(_pom())), kind)
    assert 'value="true" else="false"' in cond
    assert re.search(
        rf'<equals arg1="\$\{{nexus\.jooq\.{kind}\.stamp\}}" arg2="\$\{{nexus\.jooq\.{kind}\.hash\}}"\s*/>', cond
    ), f"{kind}: the decision no longer compares the loaded stamp with the hash"
    assert '<not><istrue value="${nexus.jooq.regenerate}"/></not>' in cond, (
        f"{kind}: -Dnexus.jooq.regenerate=true is not a guard")
    assert "<and>" in cond, f"{kind}: the conditions must be conjoined"


@pytest.mark.parametrize("kind", KINDS)
def test_the_stamp_is_read_from_the_path_the_stamp_step_writes(kind: str) -> None:
    root = _pom()
    text = _target_text(_hash_execution(root))
    stamp_dir = re.search(r'<property name="nexus\.jooq\.stamp\.dir" value="([^"]+)"', text).group(1)
    load = re.search(rf'<loadfile property="nexus\.jooq\.{kind}\.stamp"\s+srcFile="([^"]+)"', text)
    assert load, f"{kind}: the stamp is no longer loaded into nexus.jooq.{kind}.stamp"
    read_path = load.group(1).replace("${nexus.jooq.stamp.dir}", stamp_dir)
    write = re.search(r'<echo file="([^"]+)" message="([^"]+)"', _target_text(_stamp_execution(root, EXEC_OF[kind])))
    assert write, f"{kind}: the stamp step no longer echoes a file"
    assert read_path == write.group(1), f"{kind}: read {read_path} but the stamp step writes {write.group(1)}"
    assert write.group(2) == "${" + HASH_PROP[EXEC_OF[kind]] + "}", "the stamp must be the hash"


@pytest.mark.parametrize("kind", KINDS)
def test_the_hash_covers_the_names_file_which_carries_the_versions(kind: str) -> None:
    """The names file puts added/removed files and the -D-overridable versions into the digest."""
    text = _target_text(_hash_execution(_pom()))
    names = f"${{nexus.jooq.stamp.dir}}/{kind}.names"
    union = re.search(rf'<checksum[^>]*totalproperty="nexus\.jooq\.{kind}\.hash".*?</checksum>', text, re.DOTALL)
    assert union and f'<file file="{names}"/>' in union.group(), f"{kind}: the names file left the checksum union"
    echo = re.search(rf'<echo file="{re.escape(names)}"\s+message="([^"]+)"', text)
    assert echo, f"{kind}: nothing writes the names file"
    for needed in ("${nexus.jooq.%s.names}" % kind, "${jooq.version}", "${postgresql.version}",
                   "${testcontainers.version}"):
        assert needed in echo.group(1), f"{kind}: {needed} is not in the hashed names file"


@pytest.mark.parametrize("kind", KINDS)
def test_a_regenerate_decision_deletes_the_stamp_before_the_plugin_runs(kind: str) -> None:
    """THE INVARIANT: a stamp exists only while the tree matches it. Dropping this step brings
    back the first-round ship-blocker (revert the inputs after a generate-sources-only run)."""
    root = _pom()
    execution = _hash_execution(root)
    text = _target_text(execution)
    assert 'xmlns:unless="ant:unless"' in text.split(">")[0], "the target lost the ant:unless namespace"
    delete = re.search(
        rf'<delete file="\$\{{nexus\.jooq\.stamp\.dir\}}/{kind}\.sha256"[^>]*'
        rf'unless:true="\$\{{nexus\.jooq\.{kind}\.skip\}}"', text)
    assert delete, f"{kind}: no delete of the stamp when the decision is not-skip"
    assert delete.start() > text.index(f'<condition property="nexus.jooq.{kind}.skip"'), "delete before the decision"
    assert LIFECYCLE.index(_phase(execution)) < LIFECYCLE.index(_phase(_codegen_config(root)[EXEC_OF[kind]]))


def test_a_hand_set_skip_property_fails_the_build() -> None:
    text = _target_text(_hash_execution(_pom()))
    fail = re.search(r"<fail .*?</fail>", text, re.DOTALL)
    assert fail, "a -Dnexus.jooq.<x>.skip would beat the hash decision (immutable Ant properties) unchallenged"
    assert '<isset property="nexus.jooq.main.skip"/>' in fail.group()
    assert '<isset property="nexus.jooq.test.skip"/>' in fail.group()
    assert text.index("<fail ") < text.index("<condition property="), "the guard must run before the decisions"


# --- the shared cache of generated trees (nexus-fkhd3) ---------------------------------------------
# tests/test_service_jooq_codegen_skip_behaviour.py runs the Ant logic; these pin the shape that
# keeps the stamp invariant true when a restore swaps the tree.


@pytest.mark.parametrize("kind", KINDS)
def test_a_restore_deletes_the_stamp_before_it_swaps_the_tree_and_is_part_of_the_skip(kind: str) -> None:
    text = _target_text(_hash_execution(_pom()))
    stamp_del = text.index(f'<delete file="${{nexus.jooq.stamp.dir}}/{kind}.sha256" quiet="true"\n'
                           f'                                        if:true="${{nexus.jooq.{kind}.restored}}"')
    tree_del = text.index(f'<delete dir="${{{"jooq.generated.dir" if kind == "main" else "jooq.generated.test.dir"}}}"')
    copy = text.index(f'<copy todir=', tree_del)
    assert stamp_del < tree_del < copy, f"{kind}: the restore must drop the stamp, then the old tree, then copy the new one"
    assert text.index(f'property="nexus.jooq.{kind}.restored"') < text.index(f'<condition property="nexus.jooq.{kind}.skip"')
    assert f'<istrue value="${{nexus.jooq.{kind}.restored}}"/>' in _condition(text, kind), (
        f"{kind}: a restored tree must skip the codegen")
    # nothing in the inputs step writes a stamp: only the jooq-stamp-* steps do
    assert not re.search(rf'<echo[^>]*{kind}\.sha256', text), f"{kind}: the inputs step must never write a stamp"


@pytest.mark.parametrize("kind", KINDS)
def test_a_restore_never_runs_for_a_forced_regeneration_or_without_a_complete_entry(kind: str) -> None:
    text = _target_text(_hash_execution(_pom()))
    cond = re.search(rf'<condition property="nexus\.jooq\.{kind}\.restored".*?</condition>', text, re.DOTALL).group()
    assert '<not><istrue value="${nexus.jooq.regenerate}"/></not>' in cond, "-Dnexus.jooq.regenerate=true must bypass the cache"
    assert f'<available file="${{nexus.jooq.{kind}.entry}}/complete"/>' in cond, "an entry counts only with its marker"
    assert "<resourcecount" in cond, "an entry with no sources is not restorable"
    assert f'${{nexus.jooq.{kind}.hash}}' in text and f'{kind}-${{nexus.jooq.{kind}.hash}}' in text, "keyed on the input hash"


@pytest.mark.parametrize("kind", KINDS)
def test_publishing_stages_beside_the_entry_and_renames_it_into_place_marker_last(kind: str) -> None:
    root = _pom()
    text = _target_text(_stamp_execution(root, EXEC_OF[kind]))
    stage = f"${{nexus.jooq.{kind}.stage}}"
    entry = f"${{nexus.jooq.{kind}.entry}}"
    assert f'value="${{nexus.jooq.cache.dir}}/.staging.{kind}.' in text, "the staging dir must sit in the cache root (same filesystem)"
    copy = text.index(f'<copy todir="{stage}/generated"')
    marker = text.index(f'<echo file="{stage}/complete"')
    rename = text.index(f'<move file="{stage}" tofile="{entry}"')
    assert copy < marker < rename, f"{kind}: copy, then the completeness marker, then the rename"
    assert text.index("<echo file=") < copy, "the stamp is still the stamp step's first act"
    assert "producer" in text, "an entry names what produced it, so a template-based generator can say so (nexus-v0ts5)"


# --- every changelog reference resolves under the hashed db/ tree --------------------------------

_SRC = _POM.parent / "src"
_RESOURCE_ROOTS = (_SRC / "main" / "resources", _SRC / "test" / "resources")
_CHANGELOG_REFERENCES = {
    "include": "file", "includeAll": "path", "sqlFile": "path", "loadData": "file", "loadUpdateData": "file",
}


def _changelog_references() -> list[tuple[Path, str, str, bool]]:
    """(changelog file, element, referenced path, relativeToChangelogFile) for every reference."""
    found = []
    for res in _RESOURCE_ROOTS:
        for changelog in sorted((res / "db").rglob("*.xml")):
            for el in ET.parse(changelog).getroot().iter():
                tag = el.tag.rsplit("}", 1)[-1]
                attr = _CHANGELOG_REFERENCES.get(tag)
                if attr and el.get(attr):
                    found.append((changelog, tag, el.get(attr), el.get("relativeToChangelogFile") == "true"))
    return found


def _resolves_under_db(changelog: Path, ref: str, relative: bool) -> bool:
    if ref.startswith(("/", "classpath:", "file:", "http")) or ".." in Path(ref).parts:
        return False
    if relative:
        hits = [(changelog.parent / ref).resolve()]
    else:
        if not ref.startswith("db/"):
            return False
        hits = [(r / ref).resolve() for r in _RESOURCE_ROOTS if (r / ref).exists()]
    db_roots = [(r / "db").resolve() for r in _RESOURCE_ROOTS]
    return bool(hits) and all(h.exists() and any(h.is_relative_to(d) for d in db_roots) for h in hits)


def test_every_changelog_reference_resolves_under_db() -> None:
    """The skip hashes src/{main,test}/resources/db/** only. A changelog reference that leaves db/
    (another resources/ directory, classpath:, an absolute path, ..) would be edited without moving
    the hash, and the generated sources would go stale silently."""
    refs = _changelog_references()
    assert len(refs) > 100, f"only {len(refs)} changelog references found: the scan is not looking at the changelogs"
    bad = [f"{c.relative_to(_SRC)}: <{tag} {ref}>" for c, tag, ref, rel in refs if not _resolves_under_db(c, ref, rel)]
    assert not bad, "changelog references outside the hashed db/ tree (or unresolvable):\n  " + "\n  ".join(bad)


def test_the_reference_scan_rejects_a_reference_that_leaves_db(tmp_path: Path) -> None:
    """Positive control: the rule is not a tautology."""
    probe = _RESOURCE_ROOTS[0] / "db" / "changelog" / "db.changelog-master.xml"
    assert _resolves_under_db(probe, "db/changelog/memory-001-baseline.xml", False)
    assert not _resolves_under_db(probe, "db/changelog/no-such-file.xml", False)
    assert not _resolves_under_db(probe, "sql/other.sql", False)
    assert not _resolves_under_db(probe, "classpath:db/changelog/memory-001-baseline.xml", False)
    assert not _resolves_under_db(probe, "../../../pom.xml", True)


def test_the_codegen_master_changelogs_live_under_db() -> None:
    for execution in _codegen_config(_pom()).values():
        path = execution.findtext("m:configuration/m:liquibase/m:changeLogPath", namespaces=_NS)
        assert path and path.startswith("db/"), f"codegen master changelog {path!r} is outside the hashed db/ tree"
