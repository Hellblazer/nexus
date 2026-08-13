# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-191 Phase 4 repoint batch, Step A (bead nexus-o8dil.18) -- the
mechanical mitigation DECISION 2 (T2 nexus/rdr-191-repoint-batch-execution-
plan-2026-08-13 [22445] section 1) requires in the SAME diff as the
changeset it protects.

DECISION 2 keeps the nine combined-query functions
(search_metadata_scoped_<dim>, search_graph_hop_<dim>,
search_topic_scoped_<dim>) as NINE independent, typed, LANGUAGE sql bodies
rather than a single dim-parameterised collapse -- collapsing would force an
untyped vector parameter and a runtime column choice, un-binding the
planner from the dim-specific HNSW index and reintroducing F13's measured
~250x silent seq-scan regression. The accepted cost of keeping nine bodies
is DRIFT: nothing stops a future edit from touching the 384 arm of a family
without touching its 768/1024 siblings identically. [22445] section 1 names
the mitigation explicitly: "Mitigate MECHANICALLY, not by review: a lint
asserting the nine bodies are token-identical modulo 384|768|1024."

THIS LINT is that mitigation. For each of the three families it extracts
all three CREATE OR REPLACE FUNCTION bodies from
db/changelog-staged/vectors-005-repoint-functions-views.xml (the file's
STAGED location -- see that file's own header and the sibling coupling
lints in this module family for why: dormant-but-tested changesets live in
a sibling directory invisible to the changelog-parity drift lints, per T2
nexus/rdr-191-p4-unify-changeset-2026-08-13 [22433] round 3), normalizes
away every occurrence of the family's own dim tokens (384/768/1024,
replaced with a placeholder), and asserts the three normalized bodies are
BYTE-IDENTICAL. A body that differs after normalization has drifted from
its siblings in something OTHER than the dim token -- exactly the class
this lint exists to catch mechanically rather than leave to review.

WHY WHOLE-BODY normalization, not a line-by-line diff: the nine bodies
differ from each other in exactly one respect by construction (DECISION
2's whole point) -- the embedding column name (embedding_<dim>) and the
`p_query vector(<dim>)` parameter type. Stripping every digit-string
384/768/1024 token and comparing the remainder for exact equality is a
strictly stronger check than "the queries look similar" -- it proves there
is NOTHING ELSE different, byte for byte.

KILL-CONTROL: test_kill_control_a_divergent_where_clause_is_caught mutates
a COPY of one arm's body (adds an extra AND clause not present in its
siblings) and asserts the comparison goes RED -- demonstrating the lint
would actually catch the class of bug it exists to prevent, not just prove
today's three arms happen to already agree.

PER-ARM SELF-CONSISTENCY (added 2026-08-13, substantive-critic finding on
T2 nexus/rdr-191-repoint-batch-step-a-critique-2026-08-13 [22454]):
blanket-normalizing EVERY 384/768/1024 token to one placeholder before the
cross-arm comparison discards WHICH digit appeared WHERE. A body whose
distance/ORDER BY/JOIN wrongly references the WRONG embedding_<dim> column
-- e.g. the 768 arm ranking by embedding_384 instead of embedding_768, the
exact F13 shape (an untyped/mismatched embedding column defeating HNSW
index binding) -- normalizes to text IDENTICAL to a correctly-transposed
sibling, so the cross-arm check alone reports PASS on precisely the bug
class DECISION 2's mitigation exists to prevent.

analyze_self_consistency (below) closes that gap: for each of the nine
arms, it knows the arm's OWN dim from its function name suffix, and
asserts every dim-token occurrence remaining in that arm's OWN body
(after removing the function name's own token) equals that OWN dim --
any OTHER dim token is an immediate, named violation. This check runs
INDEPENDENTLY of, and BEFORE, the cross-arm structural comparison; a body
can pass self-consistency and still fail cross-arm drift (a genuinely
different predicate), and -- as the kill-control below demonstrates -- a
body can pass the OLD cross-arm check while failing self-consistency (a
wrong-column transposition that "looks" structurally identical after
blanket normalization).

KILL-CONTROL (self-consistency): test_kill_control_b_wrong_embedding_
column_in_one_arm_is_caught mutates ONLY the 768 arm's ORDER BY clause to
rank by embedding_384 instead of embedding_768 (single-token F13-shape
mutation) and asserts (a) analyze_self_consistency flags exactly that
function, AND (b) analyze_nine_body_drift -- the ORIGINAL, pre-existing
cross-arm check -- does NOT flag it, proving the blanket-normalization
blind spot is real, not hypothetical.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.test_changelog_rls_lint import CHANGELOG_DIR, _XSD_NS

pytestmark = pytest.mark.lint

STAGED_DIR_NAME = "changelog-staged"
VECTORS_005_BASENAME = "vectors-005-repoint-functions-views.xml"

# (family name, [changeset ids to search], [function-name templates])
_FAMILIES: dict[str, list[str]] = {
    "search_metadata_scoped": [
        "search_metadata_scoped_384",
        "search_metadata_scoped_768",
        "search_metadata_scoped_1024",
    ],
    "search_graph_hop": [
        "search_graph_hop_384",
        "search_graph_hop_768",
        "search_graph_hop_1024",
    ],
    "search_topic_scoped": [
        "search_topic_scoped_384",
        "search_topic_scoped_768",
        "search_topic_scoped_1024",
    ],
}


# NOT \b384\b: `\b` treats underscore as a word character, so it fails to
# find a boundary in `embedding_384`/`chunks_384` -- exactly the column-name
# and table-name shapes this lint most needs to normalize. Anchor on
# "not adjacent to another digit" instead, which matches 384/768/1024
# wherever they appear (embedding_384, vector(384), chunks_384, ..., a bare
# "384") without also matching inside an unrelated larger number.
_DIM_TOKEN_RE = re.compile(r"(?<!\d)(384|768|1024)(?!\d)")


def _vectors_005_path() -> Path:
    # This lint pins BODY drift, not file location (location is
    # test_changelog_staged_batch_coupling_lint.py's invariant). Resolve from
    # whichever location the batch state has the file in, preferring the
    # registered home so the lint keeps working across the Step B flip.
    registered = CHANGELOG_DIR / VECTORS_005_BASENAME
    if registered.exists():
        return registered
    return CHANGELOG_DIR.parent / STAGED_DIR_NAME / VECTORS_005_BASENAME


def _extract_sql_bodies(xml_path: Path) -> str:
    """Concatenate every <sql> element's text content across every
    changeSet in the file, in document order. Cheap and sufficient: we only
    need to locate `CREATE OR REPLACE FUNCTION nexus.<name>(...) ... AS $$
    ... $$;` spans by name, not to model changeset structure."""
    tree = ET.parse(xml_path)
    root = tree.getroot()
    parts: list[str] = []
    for sql_el in root.iter(f"{_XSD_NS}sql"):
        parts.append("".join(sql_el.itertext()))
    return "\n".join(parts)


def _extract_function_body(all_sql: str, function_name: str) -> str:
    """Extract one `CREATE OR REPLACE FUNCTION nexus.<function_name>(...)`
    statement's full text, up through its closing `$$;` (or `$fn$;`) --
    i.e. everything from the CREATE keyword through the dollar-quote close
    and trailing semicolon. Raises AssertionError with a clear message if
    the function is not found exactly once, since a miss here would
    silently skip the drift check rather than fail loud."""
    pattern = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+nexus\." + re.escape(function_name)
        + r"\s*\(.*?\$(?:fn)?\$\s*;",
        re.DOTALL,
    )
    matches = pattern.findall(all_sql)
    assert len(matches) == 1, (
        f"expected exactly one CREATE OR REPLACE FUNCTION nexus.{function_name}(...) "
        f"body in {VECTORS_005_BASENAME}, found {len(matches)} -- the nine-body drift "
        "lint cannot compare a function it cannot uniquely locate"
    )
    return matches[0]


def _normalize_dim_tokens(body: str, function_name_with_dim: str) -> str:
    """Strip the function's OWN name (which necessarily contains its dim
    token and would otherwise never match its siblings) plus every
    remaining standalone 384/768/1024 token, replacing each with a fixed
    placeholder so three dim-identical-but-for-the-token bodies compare
    byte-equal."""
    body = body.replace(function_name_with_dim, "DIMFN")
    return _DIM_TOKEN_RE.sub("DIM", body)


def _own_dim(function_name: str) -> str:
    """Extract the arm's own declared dim from its typed function name
    suffix (e.g. "search_metadata_scoped_768" -> "768")."""
    m = re.search(r"_(\d+)$", function_name)
    assert m, f"cannot extract a trailing dim token from function name {function_name!r}"
    return m.group(1)


def analyze_self_consistency(xml_path: Path = None) -> dict[str, list[str]]:
    """Return {function_name: [reasons]} for every one of the nine typed
    arms whose body contains a dim token OTHER than its own declared dim
    (see module docstring's PER-ARM SELF-CONSISTENCY section for why this
    check exists alongside, not instead of, analyze_nine_body_drift).
    Empty dict means every arm is internally consistent."""
    path = xml_path if xml_path is not None else _vectors_005_path()
    all_sql = _extract_sql_bodies(path)

    violations: dict[str, list[str]] = {}
    for fn_names in _FAMILIES.values():
        for name in fn_names:
            own_dim = _own_dim(name)
            body = _extract_function_body(all_sql, name)
            # Drop the function's own name occurrence first -- its name
            # necessarily embeds its own (correct) dim token and is not
            # itself a body reference to check.
            body_without_name = body.replace(name, "")
            alien = sorted({
                tok for tok in _DIM_TOKEN_RE.findall(body_without_name)
                if tok != own_dim
            })
            if alien:
                violations[name] = [
                    f"{name} (own dim {own_dim}) contains alien dim token(s) {alien} "
                    "in its body -- a wrong-embedding-column reference (F13 shape) "
                    "that blanket DIM-token normalization would hide from the "
                    "cross-arm drift check"
                ]
    return violations


def analyze_nine_body_drift(xml_path: Path = None) -> dict[str, list[str]]:
    """Return {family_name: [reasons]} for every family whose three
    normalized bodies do NOT all match. Empty dict means every family's
    three arms are token-identical modulo their dim token."""
    path = xml_path if xml_path is not None else _vectors_005_path()
    all_sql = _extract_sql_bodies(path)

    violations: dict[str, list[str]] = {}
    for family, fn_names in _FAMILIES.items():
        bodies = [_extract_function_body(all_sql, name) for name in fn_names]
        normalized = [_normalize_dim_tokens(b, n) for b, n in zip(bodies, fn_names)]
        if len(set(normalized)) != 1:
            reasons = []
            base = normalized[0]
            for name, norm in zip(fn_names[1:], normalized[1:]):
                if norm != base:
                    reasons.append(
                        f"{name} differs from {fn_names[0]} after dim-token normalization "
                        "-- the two bodies are NOT identical modulo 384|768|1024"
                    )
            violations[family] = reasons
    return violations


# ===========================================================================
# Tests
# ===========================================================================


def test_real_repo_nine_bodies_are_token_identical_within_each_family():
    """The live vectors-005-repoint-functions-views.xml: all three families'
    three arms must be identical modulo their own dim token. This is the
    permanent guard -- it must pass today and go RED the moment a future
    edit touches one dim's arm without mirroring the change to its
    siblings."""
    violations = analyze_nine_body_drift()
    assert violations == {}, violations


def test_kill_control_a_divergent_where_clause_is_caught(tmp_path):
    """Synthetic kill-control: write a copy of the real file with one arm's
    body mutated (an extra AND clause the other two arms do not carry), and
    assert the drift lint goes RED naming that family. Demonstrates the
    lint actually detects the class of bug DECISION 2's mitigation exists
    to prevent, not merely that today's three arms happen to already
    agree."""
    real_path = _vectors_005_path()
    original = real_path.read_text()

    # Inject a divergent predicate into ONLY the 768 arm of
    # search_metadata_scoped -- a realistic "someone tightened one dim's
    # filter and forgot its siblings" mutation. Raw file text carries XML
    # entity-escaped operators (@&gt; not @>), since this compares against
    # the UNPARSED file text, not the itertext()-decoded body.
    marker = "AND (p_where        IS NULL OR c.metadata @&gt; p_where)\n     ORDER BY c.embedding_768"
    assert marker in original, (
        "kill-control fixture assumption broken -- the expected 768-arm marker "
        "text was not found verbatim in the real file; update this test's marker "
        "to match the current body shape"
    )
    mutated = original.replace(
        marker,
        "AND (p_where        IS NULL OR c.metadata @&gt; p_where)\n"
        "       AND c.chunk_text &lt;&gt; ''\n"
        "     ORDER BY c.embedding_768",
        1,
    )
    assert mutated != original

    mutated_path = tmp_path / VECTORS_005_BASENAME
    mutated_path.write_text(mutated)

    violations = analyze_nine_body_drift(mutated_path)
    assert "search_metadata_scoped" in violations, violations
    assert violations["search_metadata_scoped"], violations
    # The other two families must NOT be flagged -- the mutation only
    # touched one family, so a correct lint's blast radius is exactly one.
    assert "search_graph_hop" not in violations, violations
    assert "search_topic_scoped" not in violations, violations


def test_real_repo_nine_bodies_are_self_consistent_per_arm():
    """The live vectors-005-repoint-functions-views.xml: every one of the
    nine arms must reference ONLY its own declared dim in its body. This is
    the permanent guard for the wrong-embedding-column (F13) regression
    class -- see module docstring's PER-ARM SELF-CONSISTENCY section."""
    violations = analyze_self_consistency()
    assert violations == {}, violations


def test_kill_control_b_wrong_embedding_column_in_one_arm_is_caught(tmp_path):
    """F13-shape kill-control: mutate ONLY the 768 arm of
    search_metadata_scoped so its ORDER BY ranks by embedding_384 instead
    of embedding_768 (a single wrong-column token transposition -- the
    exact defect class DECISION 2's nine-typed-entry-points design exists
    to keep bound to the right HNSW index). Asserts:

      (a) analyze_self_consistency flags exactly
          search_metadata_scoped_768, naming the alien '384' token.
      (b) analyze_nine_body_drift -- the ORIGINAL, pre-existing cross-arm
          check -- does NOT flag it: blanket DIM-token normalization
          converts both the correct '768' tokens and the alien '384' token
          to the same placeholder, so the mutated body remains
          byte-identical to its normalized siblings. This is the concrete
          demonstration that the drift check alone has a blind spot for
          this bug class, and that the self-consistency check closes it.
    """
    real_path = _vectors_005_path()
    original = real_path.read_text()

    # Scoped to ONLY the 768 arm's ORDER BY, immediately before the 1024
    # arm's CREATE begins -- unique in the file (verified via the assert
    # below), unlike the bare "ORDER BY c.embedding_768" fragment which
    # also appears in search_graph_hop and search_topic_scoped.
    marker = (
        "     ORDER BY c.embedding_768 &lt;=&gt; p_query\n"
        "     LIMIT p_n\n"
        "$$;\n"
        "\n"
        "CREATE OR REPLACE FUNCTION nexus.search_metadata_scoped_1024("
    )
    assert original.count(marker) == 1, (
        "kill-control fixture assumption broken -- the expected 768-arm "
        "ORDER BY marker was not found exactly once verbatim in the real "
        "file; update this test's marker to match the current body shape"
    )
    mutated = original.replace(
        marker,
        (
            "     ORDER BY c.embedding_384 &lt;=&gt; p_query\n"
            "     LIMIT p_n\n"
            "$$;\n"
            "\n"
            "CREATE OR REPLACE FUNCTION nexus.search_metadata_scoped_1024("
        ),
        1,
    )
    assert mutated != original

    mutated_path = tmp_path / VECTORS_005_BASENAME
    mutated_path.write_text(mutated)

    self_consistency_violations = analyze_self_consistency(mutated_path)
    assert "search_metadata_scoped_768" in self_consistency_violations, self_consistency_violations
    assert "384" in self_consistency_violations["search_metadata_scoped_768"][0]

    # The other eight arms must NOT be flagged -- the mutation touched only one.
    assert len(self_consistency_violations) == 1, self_consistency_violations

    drift_violations = analyze_nine_body_drift(mutated_path)
    assert drift_violations == {}, (
        "expected the ORIGINAL cross-arm drift check to be BLIND to this "
        "mutation (proving the gap self-consistency closes) -- got "
        f"{drift_violations!r} instead"
    )


def test_all_nine_functions_are_individually_locatable():
    """Sanity guard for the extraction machinery itself: every one of the
    nine expected function names must be found exactly once in the real
    file (covered implicitly by test_real_repo_nine_bodies_are_token_identical_
    within_each_family via _extract_function_body's own assert, but stated
    here explicitly so a missing/duplicated function name fails with a
    dedicated, unambiguous test name rather than being folded into the
    drift assertion's output)."""
    all_sql = _extract_sql_bodies(_vectors_005_path())
    for family, fn_names in _FAMILIES.items():
        for name in fn_names:
            # _extract_function_body raises its own AssertionError on a
            # miss or a duplicate; calling it here for its side effect is
            # the check.
            _extract_function_body(all_sql, name)
