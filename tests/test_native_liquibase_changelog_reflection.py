# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yqd.2.22: every Liquibase setter the engine's changelogs reach by
reflection must be registered in the native-image metadata.

Found by the engine 0.1.195 release smoke (smoke-native-data-move, linux-amd64
and linux-arm64): the native engine logged
`data_move_libraries_failed ... MissingReflectionRegistrationError: Cannot
reflectively invoke method 'public void liquibase.change.core.SQLFileChange
.setPath(java.lang.String)'`, so neither vendored library (ddl_utils, dml_utils)
applied natively. On the JVM they apply. The traced metadata held
SQLFileChange, CreateProcedureChange and RawSQLChange with `<init>` only,
because the trace ran changelogs that used none of the vendored elements.

Liquibase sets each XML attribute, and the element body, through the change
class's setter (`ChangeParameterMetaData.setValue` for changes,
`ObjectUtil.setProperty` for preconditions). A JVM test cannot see a missing
registration, so this test reads the changelogs and the metadata files and joins
them by name.

Scope of "reflective": `databaseChangeLog`, `changeSet`, `include`, `property`,
`comment`, `validCheckSum` and `rollback` are read by Liquibase's own parser
code (`ChangeSet.load`, `DatabaseChangeLog.handleChildNode`) with no reflective
setter call, so they carry no registration. Every other element must map to a
Liquibase class: through `EXPLICIT_CLASS_FOR_ELEMENT` where the name is not
mechanical, otherwise `liquibase.change.core.<Element>Change`. An element that
maps to neither fails the test, so a new element cannot pass unexamined.

This test covers setters only, from the XML text, and is a cheap cross-check. The
authority is the JVM test `LiquibaseReflectionRegistrationTest`
(service/src/test/java/dev/nexus/service/nativeimage), which asks Liquibase's own
metadata for the read AND write method of every change parameter: getters ARE
reached natively (`MarkChangeSetRanGenerator` reads `*name` and `path` through
`getDescription()`, and `CreateProcedureChange.generateCheckSum` falls back to a
serialiser that reads every parameter).

Fast: XML and JSON parsing only, no engine and no build.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

ROOT = Path(__file__).parent.parent
RESOURCES = ROOT / "service" / "src" / "main" / "resources"
DB_DIR = RESOURCES / "db"
NATIVE_DIR = RESOURCES / "META-INF" / "native-image"
TRACED_METADATA = NATIVE_DIR / "traced" / "reachability-metadata.json"
REFLECT_CONFIG = NATIVE_DIR / "reflect-config.json"

# The two trees the engine loads: its own changelogs and the vendored libraries.
CHANGELOG_ROOTS = (DB_DIR / "changelog", DB_DIR / "vendor")

# Elements Liquibase reads itself, with no reflective setter call.
NON_REFLECTIVE_ELEMENTS = frozenset({
    "databaseChangeLog", "changeSet", "include", "property",
    "comment", "validCheckSum", "rollback",
})

CHANGE_PACKAGE = "liquibase.change.core."
PRECONDITION_PACKAGE = "liquibase.precondition.core."

# Element names whose class is not `<Element>Change`.
EXPLICIT_CLASS_FOR_ELEMENT = {
    "sql": CHANGE_PACKAGE + "RawSQLChange",
    "sqlFile": CHANGE_PACKAGE + "SQLFileChange",
    "createProcedure": CHANGE_PACKAGE + "CreateProcedureChange",
    "preConditions": PRECONDITION_PACKAGE + "PreconditionContainer",
    "sqlCheck": PRECONDITION_PACKAGE + "SqlPrecondition",
    "dbms": PRECONDITION_PACKAGE + "DBMSPrecondition",
}

ABSTRACT_SQL_CHANGE = "liquibase.change.AbstractSQLChange"

# The class whose method Liquibase's setter lookup returns, where it is not the
# element's own class. A registration on an ancestor covers only the methods
# that ancestor declares, so a subclass override (SQLFileChange.setSql) needs
# its own entry. (class, setter) not listed here is declared on the class itself.
DECLARING_CLASS_OVERRIDES = {
    (CHANGE_PACKAGE + "RawSQLChange", "setSql"): ABSTRACT_SQL_CHANGE,
    (CHANGE_PACKAGE + "RawSQLChange", "setSplitStatements"): ABSTRACT_SQL_CHANGE,
    (CHANGE_PACKAGE + "RawSQLChange", "setStripComments"): ABSTRACT_SQL_CHANGE,
    (CHANGE_PACKAGE + "SQLFileChange", "setSplitStatements"): ABSTRACT_SQL_CHANGE,
    (CHANGE_PACKAGE + "SQLFileChange", "setStripComments"): ABSTRACT_SQL_CHANGE,
}

# The setter for an element's text body (Liquibase's DIRECT_VALUE parameter).
BODY_SETTER = {
    CHANGE_PACKAGE + "RawSQLChange": "setSql",
    CHANGE_PACKAGE + "SQLFileChange": "setSql",
    CHANGE_PACKAGE + "CreateProcedureChange": "setProcedureText",
    PRECONDITION_PACKAGE + "SqlPrecondition": "setSql",
}

_MISSING = object()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _changelog_files() -> list[Path]:
    return sorted(p for r in CHANGELOG_ROOTS for p in r.rglob("*.xml"))


def _registered_methods() -> tuple[dict[str, set[str]], set[str], set[str]]:
    """(class -> explicitly registered method names, classes registered with
    all declared/public methods, every registered class)."""
    named: dict[str, set[str]] = {}
    all_methods: set[str] = set()
    registered: set[str] = set()
    metadata = json.loads(TRACED_METADATA.read_text())
    for entry in metadata["reflection"]:
        cls = entry["type"]
        if not isinstance(cls, str):  # {"proxy": [...]} entries
            continue
        registered.add(cls)
        named.setdefault(cls, set()).update(m["name"] for m in entry.get("methods", []))
        if any(entry.get(flag) for flag in ("allDeclaredMethods", "allPublicMethods")):
            all_methods.add(cls)
    for entry in json.loads(REFLECT_CONFIG.read_text()):
        cls = entry["name"]
        registered.add(cls)
        named.setdefault(cls, set()).update(m["name"] for m in entry.get("methods", []))
        if any(entry.get(flag) for flag in ("allDeclaredMethods", "allPublicMethods")):
            all_methods.add(cls)
    return named, all_methods, registered


def _class_for(element: str, registered: set[str]) -> str | None:
    if element in EXPLICIT_CLASS_FOR_ELEMENT:
        return EXPLICIT_CLASS_FOR_ELEMENT[element]
    derived = f"{CHANGE_PACKAGE}{element[0].upper()}{element[1:]}Change"
    return derived if derived in registered else None


def _setter(attribute: str) -> str:
    return "set" + attribute[0].upper() + attribute[1:]


def _examined_pairs() -> tuple[list[tuple[str, str, str, str]], list[str], set[str]]:
    """Every (file, element, class, setter) a changelog reaches by reflection,
    the unmapped element names, and the element names seen."""
    _, _, registered = _registered_methods()
    pairs: list[tuple[str, str, str, str]] = []
    unmapped: list[str] = []
    seen: set[str] = set()
    for path in _changelog_files():
        rel = str(path.relative_to(DB_DIR))
        for el in ET.parse(path).getroot().iter():
            name = _local(el.tag)
            seen.add(name)
            if name in NON_REFLECTIVE_ELEMENTS:
                continue
            cls = _class_for(name, registered)
            if cls is None:
                unmapped.append(f"{rel}: <{name}>")
                continue
            for attribute in el.attrib:
                if attribute.startswith("{"):  # xmlns / xsi:schemaLocation
                    continue
                pairs.append((rel, name, cls, _setter(attribute)))
            # Liquibase's XML handler drops whitespace-only text, so a body
            # reaches the setter only when it has content.
            if (el.text or "").strip():
                body = BODY_SETTER.get(cls)
                if body is None:
                    unmapped.append(f"{rel}: <{name}> has a body but no body setter is known")
                else:
                    pairs.append((rel, name, cls, body))
    return pairs, unmapped, seen


def _is_registered(cls: str, setter: str) -> bool:
    named, all_methods, _ = _registered_methods()
    owner = DECLARING_CLASS_OVERRIDES.get((cls, setter), cls)
    return setter in named.get(owner, ()) or owner in all_methods


def test_every_changelog_element_maps_to_a_liquibase_class() -> None:
    _, unmapped, _ = _examined_pairs()
    assert not unmapped, (
        "changelog elements with no known Liquibase class (add them to "
        "EXPLICIT_CLASS_FOR_ELEMENT or NON_REFLECTIVE_ELEMENTS after reading "
        "how Liquibase loads them):\n" + "\n".join(sorted(set(unmapped)))
    )


def test_the_scan_is_not_vacuous() -> None:
    pairs, _, seen = _examined_pairs()
    files = _changelog_files()
    sys.stderr.write(f"examined {len(pairs)} (element, setter) pairs in {len(files)} changelogs\n")
    assert len(files) > 200
    assert pairs, "no (element, attribute) pair examined"
    # The elements whose setters the 2026-11-18 failure was about.
    for element in ("sqlFile", "createProcedure", "sql", "sqlCheck", "dbms", "preConditions"):
        assert element in seen, f"no <{element}> found: the scan is not reading the vendored trees"
    assert {cls for _, _, cls, _ in pairs} >= {
        CHANGE_PACKAGE + "SQLFileChange",
        CHANGE_PACKAGE + "CreateProcedureChange",
    }


def test_every_reflective_setter_a_changelog_uses_is_registered_for_native() -> None:
    pairs, _, _ = _examined_pairs()
    assert pairs
    distinct = sorted({(cls, setter) for _, _, cls, setter in pairs})
    missing = [(cls, setter) for cls, setter in distinct if not _is_registered(cls, setter)]
    sys.stderr.write(f"examined {len(pairs)} pairs, {len(distinct)} distinct (class, setter); "
                     f"{len(missing)} not registered\n")
    assert not missing, (
        "native image would fail with MissingReflectionRegistrationError on "
        "these setters (register them in reflect-config.json; the traced file loses hand edits on a re-trace):\n"
        + "\n".join(f"  {cls}.{setter}" for cls, setter in missing)
    )


@pytest.mark.parametrize(
    ("cls", "setter", "registered"),
    [
        # A class registered with allDeclaredMethods covers its setters.
        (CHANGE_PACKAGE + "RawSQLChange", "setSql", True),
        (CHANGE_PACKAGE + "SQLFileChange", "setPath", True),
        # A class registered with <init> only covers no setter: the check must see that.
        (CHANGE_PACKAGE + "AddColumnChange", "setTableName", False),
        ("no.such.Class", "setPath", False),
    ],
)
def test_the_registration_check_distinguishes_present_from_absent(
    cls: str, setter: str, registered: bool,
) -> None:
    assert _is_registered(cls, setter) is registered
