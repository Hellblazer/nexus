# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.6 review I5/S1 (nexus-p2m1x.26): the client's label rule is reachable in production at the rename target.

``nx collection rename`` and ``nx catalog rename-collection`` both go through ``rename_collection_data_plane``. A rename
target is a label the caller is creating (an existing target is refused as a collision first), so
``validate_collection_name`` judges it there and refuses with a ``ClickException`` before the engine is asked. The
registration call (``HttpCatalogClient.register_collection``) is NOT gated on the client: it cannot tell a new label from
an existing one at that point, and an existing row keeps its label whatever it is (RDR item 8), so the engine is the
authority there, and a refusal comes back as its 400.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import click
import pytest

from nexus.collection_rename import rename_collection_data_plane

_OLD = "notes-old"


def _t3() -> MagicMock:
    t3 = MagicMock()
    t3.collection_exists.side_effect = lambda name: name == _OLD
    return t3


def _catalog() -> MagicMock:
    cat = MagicMock()
    cat.rename_collection_cascade.return_value = {"catalog_documents": 0}
    return cat


@pytest.mark.parametrize("target", ["c42", "a/b", "x" * 256, "a\x00b", "a\nb", "é" * 128], ids=lambda s: repr(s)[:12])
def test_a_rename_target_the_label_rule_refuses_never_reaches_the_engine(target: str) -> None:
    catalog = _catalog()

    with pytest.raises(click.ClickException) as ei:
        rename_collection_data_plane(_OLD, target, t3_db=_t3(), catalog=catalog)

    assert "label" in str(ei.value.message)
    catalog.rename_collection_cascade.assert_not_called()


@pytest.mark.parametrize("target", ["My Notes", "notes 50%: café", "a" * 100, "C1", "c1a"])
def test_a_legal_label_is_sent_to_the_engine(target: str) -> None:
    catalog = _catalog()

    rename_collection_data_plane(_OLD, target, t3_db=_t3(), catalog=catalog)

    catalog.rename_collection_cascade.assert_called_once()
    assert catalog.rename_collection_cascade.call_args.args[:2] == (_OLD, target)


def test_a_target_that_already_exists_is_the_collision_not_a_label_refusal() -> None:
    t3 = MagicMock()
    t3.collection_exists.return_value = True

    with pytest.raises(click.ClickException, match="already exists"):
        rename_collection_data_plane(_OLD, "c42", t3_db=t3, catalog=_catalog())
