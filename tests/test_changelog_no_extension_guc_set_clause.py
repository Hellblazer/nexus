# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-f3yxx: no Liquibase function may attach an extension GUC as a
function-level SET clause.

``CREATE FUNCTION ... SET hnsw.ef_search = 400`` looks harmless and passes
every test that migrates as a superuser. It fails on a real boot:
Postgres refuses it with "permission denied to set parameter" when the
migration role is not a superuser and the extension's shared library is
not yet loaded in the migrating session, because the name is then an
unknown placeholder that only a superuser may attach to a function. Every
production boot migrates as a non-superuser. The first cut of taxonomy-018
did exactly this; the candidate-migration leg caught it on 2026-09-25 and
the engine suite (superuser) never saw it.

Set such values inside the body with ``set_config(name, value, true)``,
which any role may call.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_CHANGELOG = Path(__file__).resolve().parents[1] / "service/src/main/resources/db/changelog"
#: GUC prefixes defined by extensions the engine loads (pgvector's index
#: types). A core setting such as search_path is not affected.
_EXTENSION_PREFIXES = ("hnsw", "ivfflat")
_SET_CLAUSE = re.compile(
    r"^\s*SET\s+(" + "|".join(_EXTENSION_PREFIXES) + r")\.\w+\s*(=|TO\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _forward_sql(text: str) -> str:
    """The changelog text minus <rollback> blocks, which restore older
    bodies verbatim and are not this lint's subject."""
    return re.sub(r"<rollback>.*?</rollback>", "", text, flags=re.DOTALL)


def test_changelog_directory_exists_and_has_files() -> None:
    files = list(_CHANGELOG.glob("*.xml"))
    assert len(files) > 100, f"expected the real changelog tree at {_CHANGELOG}, found {len(files)} files"


def test_no_function_level_set_of_an_extension_guc() -> None:
    offenders = []
    for path in sorted(_CHANGELOG.glob("*.xml")):
        for m in _SET_CLAUSE.finditer(_forward_sql(path.read_text())):
            offenders.append(f"{path.name}: {m.group(0).strip()}")
    assert not offenders, (
        "function-level SET of an extension GUC fails non-superuser migrations; "
        "use set_config(name, value, true) in the body instead:\n  " + "\n  ".join(offenders)
    )


def test_the_pattern_matches_the_shape_it_forbids() -> None:
    """Non-vacuity: the regex catches the exact clause taxonomy-018 first shipped."""
    assert _SET_CLAUSE.search("LANGUAGE plpgsql\nSECURITY INVOKER\nSET hnsw.ef_search = 400\nAS $fn$")
    assert _SET_CLAUSE.search("SET hnsw.iterative_scan = 'strict_order'")
    assert not _SET_CLAUSE.search("SET search_path = nexus, public")
    assert not _SET_CLAUSE.search("PERFORM set_config('hnsw.ef_search', '400', true);")
