# SPDX-License-Identifier: AGPL-3.0-or-later
"""The rdr-close skill's one-time migration runs against the CLI it names.

Two defects, both in the skill's shell block:

* it found the subject collection with ``awk '$4==...'``, counting columns of ``nx collection list`` (the registry id was
  later added ahead of the content type, so the filter matched nothing);
* it minted the subject with a ``store_put`` first and then renamed the legacy collection onto it, but
  ``nx catalog rename-collection`` refuses a target that exists, and a first write to a new subject mints a ``c<id>``
  label, so the step could never work.

The step now renames the legacy collection onto the subject's own four-segment name BEFORE any ``store_put``. These tests
run the skill's own block, extracted from the file, with a fake ``nx`` that serves a listing and records the rename, and
check that the bare subject then resolves to the renamed collection.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from nexus import corpus

SKILL = Path(__file__).resolve().parent.parent / "conexus" / "skills" / "rdr-close" / "SKILL.md"

LEGACY = "knowledge__rdr_postmortem__nexus"
TARGET = "knowledge__nexus-rdr-research__bge-base-en-v15-768__v1"

LIST_WITHOUT_ID = f"""\
NAME                                CHUNKS  STORED  CONTENT_TYPE  OWNER  MODEL                DIM  STATE
{LEGACY}   9       9       knowledge     1-1    bge-base-en-v15-768  768  live
knowledge__other-notes__bge__v1          3       3  knowledge     1-2    bge-base-en-v15-768  768  live
"""

LIST_WITH_ID = f"""\
NAME                                CHUNKS  STORED  ID  CONTENT_TYPE  OWNER  MODEL                DIM  STATE
knowledge__other-notes__bge__v1          3       3  8   knowledge     1-2    bge-base-en-v15-768  768  live
{LEGACY}   9       9  7   knowledge     1-1    bge-base-en-v15-768  768  live
"""

SUBJECT_TAKEN = LIST_WITH_ID + "knowledge__nexus-rdr-research__bge-base-en-v15-768__v1  2  2  9  knowledge  1-1  bge-base-en-v15-768  768  live\n"
SUBJECT_RETIRED = LIST_WITH_ID + "knowledge__nexus-rdr-research__old__v1  0  4  6  knowledge  1-1  bge-base-en-v15-768  768  superseded->x\n"
NO_LEGACY = "NAME  CHUNKS  STORED  ID  CONTENT_TYPE  OWNER  MODEL  DIM  STATE\nknowledge__other-notes__bge__v1  3  3  8  knowledge  1-2  m  768  live\n"
LEGACY_NO_MODEL = f"NAME  CHUNKS  STORED  ID  CONTENT_TYPE  OWNER  MODEL  DIM  STATE\n{LEGACY}  9  9  7  knowledge  1-1  -  -  live\n"


def _migration_block(repo: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    section = text[text.index("#### One-time migration"):]
    block = re.search(r"```bash\n(.*?)```", section, re.S)
    assert block, "the migration section must carry its bash block"
    return block.group(1).replace("{repo}", repo).replace("<repo>", repo)


def _run(listing: str, tmp_path: Path, repo: str = "nexus") -> tuple[int, list[str]]:
    """The skill's block against a fake ``nx``; the exit status and the rename calls it made."""
    calls = tmp_path / "calls"
    calls.touch()
    fake = (
        "nx() {\n"
        '  if [ "$1" = collection ] && [ "$2" = list ]; then cat <<\'LIST\'\n' + listing + "LIST\n"
        f"  else printf '%s\\n' \"$*\" >> {calls}; fi\n"
        "}\n"
    )
    done = subprocess.run(["bash", "-c", fake + _migration_block(repo)], capture_output=True, text=True)
    return done.returncode, calls.read_text().splitlines()


@pytest.mark.parametrize("listing", [LIST_WITHOUT_ID, LIST_WITH_ID], ids=["without-id-column", "with-id-column"])
def test_the_legacy_collection_is_renamed_onto_the_subjects_own_name_whatever_the_columns(listing: str, tmp_path: Path) -> None:
    status, calls = _run(listing, tmp_path)
    assert status == 0
    assert calls == [f"catalog rename-collection {LEGACY} {TARGET} --yes"]


@pytest.mark.parametrize("listing", [SUBJECT_TAKEN, NO_LEGACY, LEGACY_NO_MODEL], ids=["subject-taken", "no-legacy", "no-model"])
def test_the_migration_stops_before_renaming_when_it_cannot_work(listing: str, tmp_path: Path) -> None:
    """Non-vacuity for the test above: the block must be able to refuse. A rename onto an existing subject, or with no
    model, is the call the engine would refuse or mislabel."""
    status, calls = _run(listing, tmp_path)
    assert status == 1 and calls == []


def test_a_retired_collection_of_the_subject_does_not_block_the_rename(tmp_path: Path) -> None:
    status, calls = _run(SUBJECT_RETIRED, tmp_path)
    assert status == 0 and calls == [f"catalog rename-collection {LEGACY} {TARGET} --yes"]


def test_the_skill_does_not_mint_the_subject_before_the_rename() -> None:
    block = _migration_block("nexus")
    assert "store_put" not in block and "migrate-fallback" not in block


def test_the_bare_subject_resolves_to_the_renamed_collection_and_mints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the skill promises: after the rename, ``store_put(collection="nexus-rdr-research")`` reaches the renamed
    collection. The contrast case (no such collection) mints a ``c<id>`` label, which is the split the skill warns of;
    the mint is stubbed so the test never reaches an engine."""
    monkeypatch.setattr(corpus, "_promoted_model_token_for_read", lambda content_type: "bge-base-en-v15-768")
    monkeypatch.setattr(corpus, "mint_knowledge_subject_label", lambda *a, **k: "c7")

    class T3:
        def __init__(self, existing: set[str]) -> None:
            self.existing = existing

        def collection_exists(self, name: str) -> bool:
            return name in self.existing

    renamed = T3({TARGET})
    assert corpus.t3_collection_name("nexus-rdr-research", t3=renamed, for_write=True) == TARGET
    assert corpus.t3_collection_name("nexus-rdr-research", t3=T3(set()), for_write=True) == "c7"
