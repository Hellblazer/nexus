# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rdr-close skill's one-time migration finds the subject collection by NAME, not by a column of ``nx collection list``.

The skill used ``awk '$4=="{repo}-rdr-research"'``. That counted columns, and the list's columns have changed (the
registry id was added ahead of the content type), so the filter matched nothing and ``rename-collection`` ran with an empty
target. The skill now reads the first word of each line. These tests run the skill's own ``NEW=$(...)`` line, extracted from
the file, against listings in the old and the current column layouts.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

SKILL = Path(__file__).resolve().parent.parent / "conexus" / "skills" / "rdr-close" / "SKILL.md"

LIST_WITHOUT_ID = """\
NAME                                      CHUNKS  STORED  CONTENT_TYPE  OWNER  MODEL                DIM   STATE
knowledge__nexus-rdr-research__bge__v1        12      12  knowledge     1-1    bge-base-en-v15-768  768   live
knowledge__other-rdr-research__bge__v1         3       3  knowledge     1-2    bge-base-en-v15-768  768   live
"""

LIST_WITH_ID = """\
NAME                                      CHUNKS  STORED  ID  CONTENT_TYPE  OWNER  MODEL                DIM   STATE
knowledge__other-rdr-research__bge__v1         3       3  8   knowledge     1-2    bge-base-en-v15-768  768   live
knowledge__nexus-rdr-research__old__v1         0       4  7   knowledge     1-1    bge-base-en-v15-768  768   superseded->knowledge__nexus-rdr-research__bge__v1
knowledge__nexus-rdr-research__bge__v1        12      12  9   knowledge     1-1    bge-base-en-v15-768  768   live
"""


def _skill_filter(repo: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("NEW=$(nx collection list")]
    assert len(lines) == 1, f"the skill must carry exactly one NEW=$(nx collection list ...) line, found {lines}"
    return lines[0].replace("{repo}", repo)


def _run(listing: str, repo: str) -> str:
    script = f"nx() {{ cat <<'LIST'\n{listing}LIST\n}}\n{_skill_filter(repo)}\nprintf '%s' \"$NEW\"\n"
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    return done.stdout


@pytest.mark.parametrize("listing", [LIST_WITHOUT_ID, LIST_WITH_ID], ids=["without-id-column", "with-id-column"])
def test_the_filter_finds_the_live_subject_collection_whatever_the_columns(listing: str) -> None:
    assert _run(listing, "nexus") == "knowledge__nexus-rdr-research__bge__v1"


def test_the_filter_answers_empty_for_a_subject_with_no_collection() -> None:
    """Non-vacuity: a filter that matched everything would also pass the test above; this one fails it."""
    assert _run(LIST_WITH_ID, "absent") == ""


def test_the_skill_stops_when_the_filter_found_nothing() -> None:
    text = SKILL.read_text(encoding="utf-8")
    assert re.search(r'\[ -n "\$NEW" \] \|\|', text), "an empty $NEW must stop the migration before rename-collection runs"
