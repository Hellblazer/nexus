# SPDX-License-Identifier: AGPL-3.0-or-later
"""The upgrade-boot seed finds the collection its first ``nx store put`` minted (engine-release Step 3h Leg A).

The seed runs under the previous release's client. Since client 8.5.6 a new knowledge subject is registered
under an engine-minted ``c<id>`` label, so the ``knowledge__<subject>__`` prefix finds nothing and the label
carries no model token; 7.76.0 (Legs B and C) still names it ``knowledge__<subject>__<model>__v1``.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent.parent / "scripts" / "upgrade_boot_seed.py"
spec = importlib.util.spec_from_file_location("upgrade_boot_seed", _SCRIPT)
_mod = importlib.util.module_from_spec(spec)
sys.modules["upgrade_boot_seed"] = _mod
spec.loader.exec_module(_mod)


class _T3:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_collections(self) -> list[dict]:
        return self._rows


def test_a_minted_label_is_found_by_the_collection_the_put_added() -> None:
    t3 = _T3([{"name": "c1", "embedding_model": "bge-base-en-v1.5", "collection_id": 1}])
    assert _mod._physical(t3, "bootw-a", before=set()) == ("c1", "bge-base-en-v1.5")


def test_a_second_minted_label_is_the_one_not_listed_before() -> None:
    t3 = _T3([{"name": "c1", "embedding_model": "bge-base-en-v1.5"},
              {"name": "c2", "embedding_model": "bge-base-en-v1.5"}])
    assert _mod._physical(t3, "bootw-b", before={"c1"}) == ("c2", "bge-base-en-v1.5")


def test_a_knowledge_name_is_found_by_its_prefix_and_its_model_token() -> None:
    t3 = _T3([{"name": "knowledge__bootw-a__bge-base-en-v1.5__v1"}, {"name": "knowledge__other__x__v1"}])
    assert _mod._physical(t3, "bootw-a", before=set()) == ("knowledge__bootw-a__bge-base-en-v1.5__v1",
                                                            "bge-base-en-v1.5")


def test_a_label_whose_row_names_no_model_is_refused_not_guessed() -> None:
    with pytest.raises(SystemExit, match="embedding model"):
        _mod._physical(_T3([{"name": "c1"}]), "bootw-a", before=set())


def test_no_new_collection_is_refused() -> None:
    with pytest.raises(SystemExit, match="bootw-a"):
        _mod._physical(_T3([{"name": "c1", "embedding_model": "m"}]), "bootw-a", before={"c1"})


def test_two_new_collections_are_refused() -> None:
    rows = [{"name": "c1", "embedding_model": "m"}, {"name": "c2", "embedding_model": "m"}]
    with pytest.raises(SystemExit, match="bootw-a"):
        _mod._physical(_T3(rows), "bootw-a", before=set())
