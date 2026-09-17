# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-v1zdu: the shared known-vs-unknown collection helper nexus-3ygp3
asked for. Unit-level coverage of nexus.catalog.membership; per-site
integration coverage (the planted-unknown-name, exit-non-zero contract)
lives beside each guarded command's own test module."""
from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest

from nexus.catalog.membership import (
    collection_is_known,
    refuse_if_collection_unknown,
    unknown_collection_message,
)


def test_collection_is_known_true_when_get_collection_returns_row():
    cat = MagicMock()
    cat.get_collection.return_value = {"name": "docs__nexus-1-1__voyage-context-3__v1"}
    assert collection_is_known(cat, "docs__nexus-1-1__voyage-context-3__v1") is True


def test_collection_is_known_false_when_get_collection_returns_none():
    cat = MagicMock()
    cat.get_collection.return_value = None
    assert collection_is_known(cat, "bogus") is False


def test_unknown_collection_message_names_the_collection():
    msg = unknown_collection_message("bogus__collection")
    assert "bogus__collection" in msg
    assert "nx collection list" in msg


def test_unknown_collection_message_includes_orphan_rows_when_given():
    msg = unknown_collection_message("bogus", orphan_rows=7)
    assert "7 row(s)" in msg


def test_unknown_collection_message_omits_orphan_clause_when_zero():
    msg = unknown_collection_message("bogus", orphan_rows=0)
    assert "row(s)" not in msg


def test_refuse_if_collection_unknown_noop_when_entries_nonempty():
    cat = MagicMock()
    refuse_if_collection_unknown(cat, "any", entries=[object()])
    cat.get_collection.assert_not_called()


def test_refuse_if_collection_unknown_noop_when_known_and_empty():
    cat = MagicMock()
    cat.get_collection.return_value = {"name": "known__collection"}
    refuse_if_collection_unknown(cat, "known__collection", entries=[])
    cat.get_collection.assert_called_once_with("known__collection")


def test_refuse_if_collection_unknown_raises_when_unknown_and_empty():
    cat = MagicMock()
    cat.get_collection.return_value = None
    with pytest.raises(click.ClickException, match="bogus"):
        refuse_if_collection_unknown(cat, "bogus", entries=[])


def test_refuse_if_collection_unknown_precomputed_known_skips_lookup():
    cat = MagicMock()
    refuse_if_collection_unknown(cat, "any", entries=[], known=True)
    cat.get_collection.assert_not_called()


def test_refuse_if_collection_unknown_precomputed_unknown_skips_lookup_and_raises():
    cat = MagicMock()
    with pytest.raises(click.ClickException, match="bogus"):
        refuse_if_collection_unknown(cat, "bogus", entries=[], known=False)
    cat.get_collection.assert_not_called()
