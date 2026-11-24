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

The engine mints a ``c<id>`` label for a new knowledge subject, so a subject's collection is found by its CONTENT_TYPE and
OWNER columns, never by splitting its name: both the reuse check and the migration guard are run against listings that
carry a ``c<id>`` row.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from nexus import corpus
from nexus.catalog.collection_name import CollectionAttributes

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
C_ID_SUBJECT = LIST_WITH_ID + "c12  2  2  11  knowledge  nexus-rdr-research  bge-base-en-v15-768  768  live\n"
C_ID_SUBJECT_RETIRED = LIST_WITH_ID + "c12  0  4  11  knowledge  nexus-rdr-research  bge-base-en-v15-768  768  superseded->c13\n"
C_ID_OTHER_SUBJECT = LIST_WITH_ID + "c13  2  2  12  knowledge  other-notes  bge-base-en-v15-768  768  live\n"
NO_LEGACY = "NAME  CHUNKS  STORED  ID  CONTENT_TYPE  OWNER  MODEL  DIM  STATE\nknowledge__other-notes__bge__v1  3  3  8  knowledge  1-2  m  768  live\n"
LEGACY_NO_MODEL = f"NAME  CHUNKS  STORED  ID  CONTENT_TYPE  OWNER  MODEL  DIM  STATE\n{LEGACY}  9  9  7  knowledge  1-1  -  -  live\n"


def _section_block(heading: str, repo: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    section = text[text.index(heading):]
    block = re.search(r"```bash\n(.*?)```", section, re.S)
    assert block, f"the section {heading!r} must carry its bash block"
    return block.group(1).replace("{repo}", repo).replace("<repo>", repo)


def _reuse_block(repo: str) -> str:
    return _section_block("Check for an existing subject", repo)


def _migration_block(repo: str) -> str:
    """The migration block, run in the shell that ran the reuse check (it calls that block's ``subject_collections``)."""
    return _reuse_block(repo) + "\n" + _section_block("#### One-time migration", repo)


def _bash(block: str, listing: str, tmp_path: Path) -> tuple[int, list[str], str, str]:
    """*block* against a fake ``nx``; the exit status, the non-list calls it made, stdout and stderr."""
    calls = tmp_path / "calls"
    calls.touch()
    fake = (
        "nx() {\n"
        '  if [ "$1" = collection ] && [ "$2" = list ]; then cat <<\'LIST\'\n' + listing + "LIST\n"
        f"  else printf '%s\\n' \"$*\" >> {calls}; fi\n"
        "}\n"
    )
    done = subprocess.run(["bash", "-c", fake + block], capture_output=True, text=True)
    return done.returncode, calls.read_text().splitlines(), done.stdout, done.stderr


CURRENT = "bge-base-en-v15-768"


def _run(listing: str, tmp_path: Path, repo: str = "nexus", cur: str | None = CURRENT) -> tuple[int, list[str], str]:
    """The migration block; the exit status, the rename calls it made and what it said on stderr. *cur* is the
    ``CUR`` the operator sets (None: unset)."""
    block = _migration_block(repo)
    status, calls, _out, err = _bash(f"CUR={cur}\n{block}" if cur is not None else block, listing, tmp_path)
    return status, calls, err


@pytest.mark.parametrize("listing", [LIST_WITHOUT_ID, LIST_WITH_ID], ids=["without-id-column", "with-id-column"])
def test_the_legacy_collection_is_renamed_onto_the_subjects_own_name_whatever_the_columns(listing: str, tmp_path: Path) -> None:
    status, calls, _err = _run(listing, tmp_path)
    assert status == 0
    assert calls == [f"catalog rename-collection {LEGACY} {TARGET} --yes"]


@pytest.mark.parametrize(
    ("listing", "said"),
    [
        (SUBJECT_TAKEN, "already has a collection"),
        (C_ID_SUBJECT, "already has a collection"),
        (NO_LEGACY, "nothing to migrate"),
        (LEGACY_NO_MODEL, "no registered model"),
    ],
    ids=["subject-taken", "subject-taken-as-c-id", "legacy-not-listed", "no-model"],
)
def test_the_migration_stops_before_renaming_when_it_cannot_work(listing: str, said: str, tmp_path: Path) -> None:
    """Non-vacuity for the test above: the block must be able to refuse, and say why on stderr. A rename onto an existing
    subject (a minted ``c<id>`` one included), or with no model, is the call the engine would refuse or mislabel."""
    status, calls, err = _run(listing, tmp_path)
    assert status == 1 and calls == []
    assert said in err


@pytest.mark.parametrize("listing", [SUBJECT_RETIRED, C_ID_SUBJECT_RETIRED, C_ID_OTHER_SUBJECT], ids=["retired", "c-id-retired", "c-id-of-another-subject"])
def test_a_retired_or_unrelated_collection_does_not_block_the_rename(listing: str, tmp_path: Path) -> None:
    status, calls, _err = _run(listing, tmp_path)
    assert status == 0 and calls == [f"catalog rename-collection {LEGACY} {TARGET} --yes"]


def test_the_migration_stops_when_the_legacy_model_is_not_the_one_this_install_writes(tmp_path: Path, cloud_mode: None) -> None:
    status, calls, err = _run(LIST_WITH_ID, tmp_path, cur="voyage-context-3")
    assert status == 1 and calls == []
    assert f"MODEL is {CURRENT}, this install writes voyage-context-3: skip" in err


def test_the_migration_will_not_guess_the_current_model(tmp_path: Path) -> None:
    status, calls, err = _run(LIST_WITH_ID, tmp_path, cur=None)
    assert status != 0 and calls == []
    assert "set CUR" in err


def test_a_retired_legacy_row_is_not_the_collection_to_migrate(tmp_path: Path) -> None:
    retired = f"NAME  CHUNKS  STORED  ID  CONTENT_TYPE  OWNER  MODEL  DIM  STATE\n{LEGACY}  0  9  7  knowledge  1-1  {CURRENT}  768  superseded->x\n"
    status, calls, err = _run(retired, tmp_path)
    assert status == 1 and calls == [] and "nothing to migrate" in err


def test_a_repo_name_with_underscores_is_hyphenated_in_the_subject_only(tmp_path: Path) -> None:
    """The engine turns ``_`` into ``-`` in a subject; the legacy collection keeps the repo name as it was."""
    legacy = "knowledge__rdr_postmortem__my_repo"
    listing = f"NAME  CHUNKS  STORED  ID  CONTENT_TYPE  OWNER  MODEL  DIM  STATE\n{legacy}  9  9  7  knowledge  1-1  {CURRENT}  768  live\n"
    status, calls, _err = _run(listing, tmp_path, repo="my_repo")
    assert status == 0
    assert calls == [f"catalog rename-collection {legacy} knowledge__my-repo-rdr-research__{CURRENT}__v1 --yes"]
    taken = listing + f"c5  2  2  9  knowledge  my-repo-rdr-research  {CURRENT}  768  live\n"
    (tmp_path / "again").mkdir()
    status, calls, err = _run(taken, tmp_path / "again", repo="my_repo")
    assert status == 1 and calls == [] and "my-repo-rdr-research already has a collection" in err


def test_the_migration_block_run_alone_refuses_before_any_rename(tmp_path: Path) -> None:
    """Every tool call is a fresh shell. Run without the reuse check that defines ``SUBJECT`` and ``subject_collections``, the
    guard would see empty output and the rename would name an empty subject; the block must stop first."""
    block = f"CUR={CURRENT}\n" + _section_block("#### One-time migration", "nexus")
    status, calls, _out, err = _bash(block, LIST_WITH_ID, tmp_path)
    assert status == 1 and calls == []
    assert "run the reuse check above first" in err


@pytest.mark.parametrize(
    ("listing", "expected"),
    [
        (LIST_WITH_ID, ""),
        (SUBJECT_TAKEN, "knowledge__nexus-rdr-research__bge-base-en-v15-768__v1"),
        (C_ID_SUBJECT, "c12"),
        (C_ID_SUBJECT_RETIRED, ""),
        (C_ID_OTHER_SUBJECT, ""),
    ],
    ids=["none", "four-segment", "c-id", "c-id-retired", "c-id-of-another-subject"],
)
def test_the_reuse_check_finds_the_subjects_collection_whatever_its_label(listing: str, expected: str, tmp_path: Path) -> None:
    status, calls, out, _err = _bash(_reuse_block("nexus"), listing, tmp_path)
    assert status == 0 and calls == []
    assert out.split() == ([expected] if expected else [])


def test_the_rename_names_the_subjects_four_segment_target_and_the_skill_says_to_run_it_before_any_write() -> None:
    """The fake ``nx`` above proves the block makes no other call; this pins what the call names, and that the text a reader
    follows puts the migration ahead of the first ``store_put`` (a first write to a new subject mints a ``c<id>`` label, and
    the rename refuses a target that exists)."""
    block = _migration_block("nexus")
    assert 'nx catalog rename-collection "$OLD" "knowledge__${SUBJECT}__${MODEL}__v1" --yes' in block
    assert "tr _ -" in block
    text = SKILL.read_text(encoding="utf-8")
    step6 = text[text.index("### Step 6: T3 Archive"):]
    ahead = step6.index("BEFORE the first `store_put`")
    assert ahead < step6.index("mcp__plugin_conexus_nexus__store_put(") < step6.index("#### One-time migration")
    assert "migrate-fallback" not in block


def test_the_skill_says_how_to_read_the_current_knowledge_model(cloud_mode: None) -> None:
    text = SKILL.read_text(encoding="utf-8")
    section = text[text.index("#### One-time migration"):]
    assert "nx config get local.embed_model" in section and "voyage-context-3" in section
    assert "newest live knowledge collection" not in section


def test_the_bare_subject_resolves_to_the_renamed_collection_and_mints_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the skill promises: after the rename, ``store_put(collection="nexus-rdr-research")`` reaches the renamed
    collection. The contrast case (no such collection) mints a ``c<id>`` label, which is the split the skill warns of;
    the mint is stubbed so the test never reaches an engine. The fake T3 answers ``find_collection`` as the catalog does,
    with a row whose owner is the subject, and records the model the lookup was keyed on (the one the skill's MODEL must
    equal)."""
    monkeypatch.setattr(corpus, "_promoted_model_token_for_read", lambda content_type: "bge-base-en-v15-768")
    monkeypatch.setattr(corpus, "effective_embedding_model_for_writes", lambda content_type: "bge-base-en-v15-768")
    monkeypatch.setattr(corpus, "mint_knowledge_subject_label", lambda *a, **k: "c7")

    class T3:
        def __init__(self, existing: set[str]) -> None:
            self.existing = existing
            self.looked_up: list[tuple[str, str, str]] = []

        def collection_exists(self, name: str) -> bool:
            return name in self.existing

        def find_collection(self, content_type: str, owner_id: str, model: str) -> CollectionAttributes | None:
            self.looked_up.append((content_type, owner_id, model))
            if TARGET not in self.existing or owner_id != "nexus-rdr-research" or model != "bge-base-en-v15-768":
                return None
            return CollectionAttributes(
                collection_id=7, label=TARGET, content_type="knowledge", owner_id="nexus-rdr-research",
                embedding_model="bge-base-en-v15-768", model_version=1, dimension=768, lifecycle_state="live",
            )

    renamed = T3({TARGET})
    assert corpus.t3_collection_name("nexus-rdr-research", t3=renamed, for_write=True) == TARGET
    assert renamed.looked_up == [("knowledge", "nexus-rdr-research", "bge-base-en-v15-768")]
    empty = T3(set())
    assert corpus.t3_collection_name("nexus-rdr-research", t3=empty, for_write=True) == "c7"
    assert empty.looked_up == [("knowledge", "nexus-rdr-research", "bge-base-en-v15-768")]
