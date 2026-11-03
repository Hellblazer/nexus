# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.41 (RDR-236 Phase 3): the nine route (b) functions repeat the per-occurrence object.

vectors-048-1 gives search_metadata_scoped_<dim>, search_aspect_scoped_<dim> and search_graph_hop_<dim> a manifest
predicate (``p_manifest_where`` / ``p_manifest_path``) tested on the joined row's per-occurrence object, the one
``manifest_search_<dim>`` builds (vectors-045). The design of record chose NO shared SQL helper: a sub-function taking
row composites would not inline, and a bigger inlined body costs plan time on every call. The object expression is
therefore repeated in each of the nine bodies, and nothing but this lint keeps the copies from drifting: a key added
to ``manifest_search`` and not to a route (b) body would make the two routes answer the same ``where`` differently.

What it pins, for each of the nine bodies, against ``manifest_search_384`` of vectors-045-1:
the ``jsonb_build_object(...) AS eff`` expression is the same text (whitespace aside), so the same key list with the
same fallbacks; both new parameters exist; both conjuncts test ``e.eff``; and the old signature is not left beside the
new (a surviving overload breaks the generated ``.call`` arity).

Kill-controls mutate a copy of one body and require the comparison to go red.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_changelog_rls_lint import CHANGELOG_DIR, _XSD_NS

pytestmark = pytest.mark.lint

_FAMILIES = ("search_metadata_scoped", "search_aspect_scoped", "search_graph_hop")
_DIMS = (384, 768, 1024)
_NINE = [f"{family}_{dim}" for family in _FAMILIES for dim in _DIMS]

_OBJECT_RE = re.compile(r"jsonb_build_object\(.*?\)\s+AS\s+eff", re.DOTALL)


def _changeset_sql(path: Path, changeset_id: str) -> str:
    """The text of the changeSet's own <sql> (not its rollback's)."""
    root = ET.parse(path).getroot()
    for cs in root.iter(f"{_XSD_NS}changeSet"):
        if cs.get("id") == changeset_id:
            parts = ["".join(sql.itertext()) for sql in cs.findall(f"{_XSD_NS}sql")]
            assert parts, f"{changeset_id} has no <sql> of its own"
            return "\n".join(parts)
    raise AssertionError(f"{changeset_id} not found in {path.name}")


def _function(sql: str, name: str) -> str:
    matches = re.findall(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+nexus\." + re.escape(name) + r"\s*\(.*?\$\$\s*;", sql, re.DOTALL)
    assert len(matches) == 1, f"expected exactly one body of nexus.{name}, found {len(matches)}"
    return matches[0]


def _object(body: str) -> str:
    matches = _OBJECT_RE.findall(body)
    assert len(matches) == 1, f"expected exactly one per-occurrence object, found {len(matches)}"
    return re.sub(r"\s+", " ", matches[0])


def _reference_object() -> str:
    sql = _changeset_sql(CHANGELOG_DIR / "vectors-045-manifest-search-named-keys.xml", "vectors-045-1")
    return _object(_function(sql, "manifest_search_384"))


def _route_b_sql() -> str:
    return _changeset_sql(CHANGELOG_DIR / "vectors-048-route-b-manifest-prefilter.xml", "vectors-048-1")


def drift(sql: str, reference: str) -> dict[str, list[str]]:
    """{function: [what is wrong]} for every one of the nine bodies; empty when all agree."""
    problems: dict[str, list[str]] = {}
    for name in _NINE:
        found: list[str] = []
        try:
            body = _function(sql, name)
        except AssertionError as e:
            problems[name] = [str(e)]
            continue
        try:
            if _object(body) != reference:
                found.append("the per-occurrence object differs from manifest_search_384's (vectors-045-1)")
        except AssertionError as e:
            found.append(str(e))
        for param in ("p_manifest_where jsonb", "p_manifest_path  text"):
            if param not in body:
                found.append(f"parameter `{param}` missing")
        if "(p_manifest_where IS NULL OR e.eff @> p_manifest_where)" not in body:
            found.append("the containment conjunct on e.eff is missing")
        if "(p_manifest_path IS NULL OR e.eff @@ p_manifest_path::jsonpath)" not in body:
            found.append("the jsonpath conjunct on e.eff is missing")
        if found:
            problems[name] = found
    return problems


def test_the_reference_object_is_the_full_key_list():
    # A reference that lost keys would make every copy "agree" with it; pin its width (17 keys, vectors-044 and -045).
    ref = _reference_object()
    assert ref.count("nexus.manifest_names_key(") == 17


def test_all_nine_bodies_repeat_the_manifest_search_object():
    assert drift(_route_b_sql(), _reference_object()) == {}


def test_each_function_has_exactly_one_signature_after_the_drops():
    sql = _route_b_sql()
    for name in _NINE:
        drops = re.findall(r"DROP FUNCTION IF EXISTS nexus\." + name + r"\(([^)]*)\)", sql)
        grants = re.findall(r"GRANT EXECUTE ON FUNCTION nexus\." + name + r"\(([^)]*)\)", sql)
        assert len(drops) == 1 and len(grants) == 1, (name, drops, grants)
        # the dropped signature is the vectors-031 one: it has no manifest parameters, the granted one has two more
        assert len(grants[0].split(",")) == len(drops[0].split(",")) + 2, (name, drops, grants)


def test_the_post_condition_counts_the_nine():
    assert "IF n <> 9 THEN" in _route_b_sql()


def test_kill_control_a_missing_key_in_one_body_is_caught():
    sql = _route_b_sql()
    body = _function(sql, "search_graph_hop_768")
    mutated_body = re.sub(r"'frecency_score', CASE.*?END END\s*", "", body, count=1, flags=re.DOTALL)
    # the removed key was the last one, so the trailing comma before it has to go for the SQL to stay well formed;
    # the lint reads text, so the comparison sees the missing key either way.
    assert mutated_body != body
    problems = drift(sql.replace(body, mutated_body), _reference_object())
    assert list(problems) == ["search_graph_hop_768"]


def test_kill_control_a_missing_conjunct_is_caught():
    sql = _route_b_sql()
    body = _function(sql, "search_aspect_scoped_1024")
    mutated = body.replace("       AND (p_manifest_path IS NULL OR e.eff @@ p_manifest_path::jsonpath)\n", "")
    assert mutated != body
    problems = drift(sql.replace(body, mutated), _reference_object())
    assert list(problems) == ["search_aspect_scoped_1024"]
    assert any("jsonpath conjunct" in m for m in problems["search_aspect_scoped_1024"])
