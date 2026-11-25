# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1060, superseded by RDR-229 P3.6 (nexus-p2m1x.26): the 63-character cap is gone.

GH #1060 asked for an actionable error when a collection name overflowed ChromaDB's 63-character cap (relabeling a
voyage-named collection to a local bge token pushed ``code__conductor-sys-monitoring-b25083f0__bge-base-en-v15-768__v1``
to 64 characters), with a hint to run ``nx catalog collection-name``. ``validate_collection_name`` is now the label
rule (item 7 of the RDR): a label may run to 255 UTF-8 bytes, so that name is a legal label and there is no
overflow to explain. These tests keep the incident's name and pin the new behaviour: accepted, and no stale hint.
The 255-byte limit's own refusal is in ``tests/test_rdr229_label_rule.py``.
"""
from __future__ import annotations

import pytest

from nexus.corpus import validate_collection_name

# code__conductor-sys-monitoring-b25083f0__bge-base-en-v15-768__v1 = 64 chars
_OVERFLOW_NAME = "code__conductor-sys-monitoring-b25083f0__bge-base-en-v15-768__v1"
_KNOWLEDGE_OVERFLOW = "knowledge__conductor-sys-monitoring-b25083f0__bge-base-en-v15-768__v1"


def test_overflow_name_is_actually_64_chars() -> None:
    assert len(_OVERFLOW_NAME) == 64


@pytest.mark.parametrize("name", [_OVERFLOW_NAME, _KNOWLEDGE_OVERFLOW])
def test_the_names_that_overflowed_63_characters_are_legal_labels(name: str) -> None:
    assert len(name) > 63
    validate_collection_name(name)


def test_the_remedy_hint_went_with_the_cap() -> None:
    """The only refusal left for a long label is the 255-byte one, and it carries no ``nx catalog collection-name`` hint."""
    with pytest.raises(ValueError) as exc_info:
        validate_collection_name("code__" + "y" * 300)
    assert "nx catalog collection-name" not in str(exc_info.value)
    assert "--content-type" not in str(exc_info.value)


@pytest.mark.parametrize(
    "name",
    ["a" * 63, "code__1-36__bge-base-en-v15-768__v1", "code__nexus-abc123__model-code__v1"],
)
def test_names_within_the_old_cap_still_validate(name: str) -> None:
    validate_collection_name(name)
