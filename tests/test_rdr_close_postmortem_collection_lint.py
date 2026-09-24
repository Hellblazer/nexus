# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR post-mortem archival must target a SUBJECT knowledge collection,
never the owner-id-shaped ``nx catalog collection-name --content-type
knowledge`` resolution.

That resolution (``Catalog.collection_for_repo`` -> ``owner_segment_for_
tumbler``) renders ``knowledge__<repo-tumbler>__<model>__vN`` for EVERY
content type, code/docs/rdr included -- correct for those, since they are
genuinely owned by the repo. A knowledge post-mortem is not: it is
catalog-owned by the knowledge curator (a distinct owner from the repo),
and docs/collections.md Rule 1 is explicit that a knowledge collection is
a subject area, never an owner id.

``conexus/skills/rdr-close/SKILL.md`` used exactly this resolution for
post-mortem archival before nexus-vupim. Three post-mortems (RDR-203
twice, RDR-215) landed in ``knowledge__1-1__...`` on the nexus repo as a
result -- a bare owner-id name, not a subject, the class Rule 1 forbids.
The fix points post-mortem archival at the bare subject
``{repo}-rdr-research`` instead (Rule 3: type the subject, let the
catalog render the rest).

File-only: reads the skill file, touches no store, runs in ``-m lint``. A
planted violation proves the check fires (nexus-moht0: a check that
cannot fail on bad input proves nothing).
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
RDR_CLOSE_SKILL = REPO_ROOT / "conexus" / "skills" / "rdr-close" / "SKILL.md"

#: The owner-id-shaped resolution that misroutes post-mortems into the
#: repo's own tumbler collection instead of a subject collection.
_OWNER_ID_RESOLUTION = "nx catalog collection-name --content-type knowledge"

#: A `store_put`/`store put` call whose `collection=` argument is this
#: resolution (bare, `$()`-substituted, or assigned to a variable that is
#: then interpolated) rather than a literal subject. We do not need to
#: parse the whole call -- the resolution string itself is unambiguous
#: enough that its presence in a `collection=`/`KNOWLEDGE_COLL=` context is
#: always the misroute; it has no other legitimate use in this skill (RDR
#: post-mortem archival is the skill's only knowledge-collection write).
_MISROUTE_CONTEXTS = (
    "KNOWLEDGE_COLL=$(nx catalog collection-name --content-type knowledge)",
    'collection="<$KNOWLEDGE_COLL>"',
    'collection="<knowledge collection from `nx catalog collection-name --content-type knowledge`>"',
    "NEW=$(nx catalog collection-name --content-type knowledge)",
)


def postmortem_misroute_violations(text: str) -> list[str]:
    """Lines in *text* that still write a post-mortem via the owner-id
    resolution instead of a subject collection."""
    violations: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if any(ctx in line for ctx in _MISROUTE_CONTEXTS):
            violations.append(f"{lineno}: {line.strip()}")
    return violations


def test_rdr_close_postmortem_archival_targets_a_subject_collection() -> None:
    assert RDR_CLOSE_SKILL.exists(), f"expected {RDR_CLOSE_SKILL} to exist"
    text = RDR_CLOSE_SKILL.read_text(encoding="utf-8")

    violations = postmortem_misroute_violations(text)
    assert not violations, (
        "post-mortem archival still resolves the target via the owner-id-shaped "
        "`nx catalog collection-name --content-type knowledge` (nexus-vupim):\n"
        + "\n".join(violations)
    )

    # Non-vacuity: the subject the fix moved archival TO must actually be
    # present, so a future edit that deletes the fix (rather than just
    # reverting to the misroute string) is still caught.
    assert "{repo}-rdr-research" in text, (
        "expected the post-mortem archival target subject `{repo}-rdr-research` "
        "in conexus/skills/rdr-close/SKILL.md -- the fix subject is missing, not "
        "just the misroute string"
    )


def test_planted_owner_id_misroute_is_caught(tmp_path: Path) -> None:
    planted = tmp_path / "SKILL.md"
    planted.write_text(
        "### Step 6: T3 Archive\n"
        "```bash\n"
        "KNOWLEDGE_COLL=$(nx catalog collection-name --content-type knowledge)\n"
        "```\n"
        "```\n"
        'mcp__plugin_conexus_nexus__store_put(collection="<$KNOWLEDGE_COLL>", ...)\n'
        "```\n"
        "Some unrelated line that just mentions knowledge collections.\n",
        encoding="utf-8",
    )
    violations = postmortem_misroute_violations(planted.read_text(encoding="utf-8"))
    assert len(violations) == 2
    assert all("collection-name --content-type knowledge" in v or "KNOWLEDGE_COLL" in v for v in violations)


def test_planted_clean_fixture_has_no_violations(tmp_path: Path) -> None:
    planted = tmp_path / "SKILL.md"
    planted.write_text(
        "### Step 6: T3 Archive\n"
        "```\n"
        'mcp__plugin_conexus_nexus__store_put(collection="{repo}-rdr-research", ...)\n'
        "```\n",
        encoding="utf-8",
    )
    assert postmortem_misroute_violations(planted.read_text(encoding="utf-8")) == []
