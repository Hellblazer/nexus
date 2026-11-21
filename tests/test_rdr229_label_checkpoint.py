# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.6 review I3 (nexus-p2m1x.26): ``delete_checkpoint(content_hash, collection)`` names a file after the
collection label, ``<hash>-<label>.json``. The indexer calls it for every document it writes. A label may now be 255 bytes
(a file name over the filesystem's 255 would raise ``ENAMETOOLONG``, which is not ``FileNotFoundError``) or hold ``:``
(not a legal Windows file name). Only a label of the old collection-name shape can have a legacy checkpoint file, since
no indexing path has written one since RDR-223, so any other label returns early and touches no path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus import checkpoint

_HASH = "a" * 64


@pytest.fixture()
def ckpt_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(checkpoint, "CHECKPOINT_DIR", tmp_path)
    return tmp_path


def test_a_legacy_checkpoint_of_an_old_shape_collection_is_still_deleted(ckpt_dir: Path) -> None:
    legacy = ckpt_dir / f"{_HASH}-code__repo-1a2b3c4d__voyage-code-3__v1.json"
    legacy.write_text("{}", encoding="utf-8")

    checkpoint.delete_checkpoint(_HASH, "code__repo-1a2b3c4d__voyage-code-3__v1")

    assert not legacy.exists()


def test_a_200_byte_label_is_a_no_op_not_an_enametoolong(ckpt_dir: Path) -> None:
    checkpoint.delete_checkpoint(_HASH, "é" * 100)  # 200 bytes: <hash>-<label>.json is over 255 bytes


def test_a_label_with_a_colon_touches_no_path(ckpt_dir: Path) -> None:
    # A file that happens to carry the name a ':' label would map to (legal on POSIX) is not ours to delete:
    # no legacy checkpoint can exist for such a label.
    bystander = ckpt_dir / f"{_HASH}-notes 50%: café.json"
    bystander.write_text("{}", encoding="utf-8")

    checkpoint.delete_checkpoint(_HASH, "notes 50%: café")

    assert bystander.exists()


@pytest.mark.parametrize("label", ["a/b", "..", "a\\b", "x" * 64, "", "a b"])
def test_any_label_outside_the_old_shape_returns_early(ckpt_dir: Path, label: str) -> None:
    checkpoint.delete_checkpoint(_HASH, label)  # no raise, no path built
