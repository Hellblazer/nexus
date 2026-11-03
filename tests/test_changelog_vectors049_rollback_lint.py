# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-qpt52: vectors-049-1 replaces thirteen function bodies; its rollback must put back the ones it replaced.

vectors-049-1 gives ``manifest_search_<dim>`` (replacing vectors-045-1's), ``manifest_qualifying_hits`` (vectors-047-1's) and
the nine route (b) functions (vectors-048-1's) six more object keys. A function body has no downgrade other than the text
it had, so the rollback block carries the previous bodies, copied once and never edited. Nothing runs a rollback of
vectors-049-1 alone (``SchemaRollbackRoundTripIntegrationTest`` rolls the whole changelog back, which drops the functions),
so a rollback body that drifted from the one it claims to restore would go unnoticed until an incident.

What it pins, over the thirteen names: the rollback body of each equals, whitespace aside, the body of the changeset that last
defined it; the forward body differs from it (a no-op replacement is not a change); and the forward block carries the six
new keys while the rollback block carries none of them.

Kill-controls: a mutated rollback body is reported.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_changelog_rls_lint import CHANGELOG_DIR, _XSD_NS

pytestmark = pytest.mark.lint

_DIMS = (384, 768, 1024)
_ROUTE_B = [
    f"{family}_{dim}"
    for family in ("search_metadata_scoped", "search_aspect_scoped", "search_graph_hop")
    for dim in _DIMS
]
_LAST_DEFINED_IN = {
    **{f"manifest_search_{dim}": ("vectors-045-manifest-search-named-keys.xml", "vectors-045-1") for dim in _DIMS},
    "manifest_qualifying_hits": ("vectors-047-manifest-qualifying-hits.xml", "vectors-047-1"),
    **{name: ("vectors-048-route-b-manifest-prefilter.xml", "vectors-048-1") for name in _ROUTE_B},
}
_NEW_KEYS = ("bib_year", "bib_authors", "bib_venue", "bib_citation_count", "indexed_at", "content_type_shown")
_THIRTEEN = sorted(_LAST_DEFINED_IN)


def _sql(path: Path, changeset_id: str, rollback: bool) -> str:
    root = ET.parse(path).getroot()
    for cs in root.iter(f"{_XSD_NS}changeSet"):
        if cs.get("id") != changeset_id:
            continue
        if rollback:
            parts = ["".join(sql.itertext()) for rb in cs.findall(f"{_XSD_NS}rollback") for sql in rb.findall(f"{_XSD_NS}sql")]
        else:
            parts = ["".join(sql.itertext()) for sql in cs.findall(f"{_XSD_NS}sql")]
        assert parts, f"{changeset_id} has no {'rollback ' if rollback else ''}<sql>"
        return "\n".join(parts)
    raise AssertionError(f"{changeset_id} not found in {path.name}")


def _function(sql: str, name: str) -> str:
    found = re.findall(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+nexus\." + re.escape(name) + r"\s*\(.*?\$\$\s*;", sql, re.DOTALL)
    assert len(found) == 1, f"expected exactly one body of nexus.{name}, found {len(found)}"
    return re.sub(r"\s+", " ", found[0]).strip()


def _forward() -> str:
    return _sql(CHANGELOG_DIR / "vectors-049-route-b-exact-keys.xml", "vectors-049-1", rollback=False)


def _rollback() -> str:
    return _sql(CHANGELOG_DIR / "vectors-049-route-b-exact-keys.xml", "vectors-049-1", rollback=True)


def disagreements(rollback_sql: str) -> list[str]:
    out: list[str] = []
    for name in _THIRTEEN:
        path, changeset = _LAST_DEFINED_IN[name]
        previous = _function(_sql(CHANGELOG_DIR / path, changeset, rollback=False), name)
        try:
            restored = _function(rollback_sql, name)
        except AssertionError as e:
            out.append(str(e))
            continue
        if restored != previous:
            out.append(f"{name}: the rollback body is not the body {changeset} defined")
    return out


def test_the_rollback_restores_the_thirteen_bodies_it_replaced():
    assert len(_THIRTEEN) == 13
    assert disagreements(_rollback()) == []


def test_the_forward_bodies_are_a_change_and_carry_the_six_keys_the_rollback_lacks():
    forward, rollback = _forward(), _rollback()
    for name in _THIRTEEN:
        path, changeset = _LAST_DEFINED_IN[name]
        previous = _function(_sql(CHANGELOG_DIR / path, changeset, rollback=False), name)
        new = _function(forward, name)
        assert new != previous, f"{name}: the forward body is the one it replaces"
        for key in _NEW_KEYS:
            assert f"'{key}'," in new, f"{name}: the forward body lacks the key {key}"
            assert f"'{key}'," not in _function(rollback, name), f"{name}: the rollback body carries the key {key}"


def test_kill_control_a_rollback_body_that_drifted_is_reported():
    rollback = _rollback()
    body = _function(rollback, "manifest_qualifying_hits")
    mutated = rollback.replace("p_cap             int", "p_cap             bigint", 1)
    assert mutated != rollback and body, "the mutation site exists"
    problems = disagreements(mutated)
    assert problems and problems[0].startswith("manifest_qualifying_hits"), problems


def test_kill_control_a_rollback_missing_a_function_is_reported():
    rollback = _rollback()
    cut = re.sub(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+nexus\.search_graph_hop_768\s*\(.*?\$\$\s*;", "", rollback, count=1, flags=re.DOTALL)
    assert cut != rollback
    problems = disagreements(cut)
    assert any("search_graph_hop_768" in p for p in problems), problems
