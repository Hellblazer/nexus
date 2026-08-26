# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``upgrade_finish.install_source`` on both layouts. nexus-0za6e.

The function answers "where did this install come from" for two user-facing
surfaces (``nx doctor``'s Process-freshness row at ``health.py``, and
``nx daemon finish-upgrade``'s header line). It read a HARDCODED
``~/.local/share/uv/tools/conexus/uv-receipt.toml`` and nothing else, which
under the generation layout is wrong twice:

1. DURING the migration window (.7 leaves the legacy tree until it has zero
   holders) the uv receipt is still readable, so a directory-tracking legacy
   install migrated onto a PyPI generation reports the LEGACY source --
   a confident wrong answer on the one string whose job is explaining why an
   upgrade did or did not move.
2. AFTER the legacy tree is reaped it says "unknown (no readable uv receipt)"
   forever.

WHY THESE TESTS OPT IN EXPLICITLY. ``tests/conftest.py`` fences ``$HOME``
session-wide, so ``install_layout`` resolves NO generation in this suite
unless a test says otherwise -- every layout-dependent branch is untested by
default and a green means nothing (T2 ``nexus/generation-layout-tests-are-
blind-by-default``). The generation cases below build a real layout via
``tests/_generation_layout.py``; the legacy cases pin ``$HOME`` at a tmpdir
holding a real uv receipt. Both branches are live in the field, so both are
asserted -- the legacy strings are pinned byte-for-byte as the non-regression
control.
"""
from __future__ import annotations

from pathlib import Path

from nexus import install_advice, install_layout, upgrade_finish

from tests._generation_layout import build as build_generation


def _write_receipt(
    generation: Path,
    *,
    source_kind: str,
    source: str,
    version: str = "",
    extras: list[str] | None = None,
) -> install_layout.Receipt:
    """Overwrite *generation*'s placeholder receipt with a real one.

    ``_generation_layout.build`` writes ``{}``, which ``read_receipt``
    rejects on schema -- deliberately, since it exists for callers that only
    need ``current`` to resolve. Anything asserting on receipt CONTENT has to
    put content there.
    """
    receipt = install_layout.Receipt(
        version=version,
        spec=install_layout.build_spec(source, extras or [], version),
        source_kind=source_kind,
        source=source,
        python="3.12.8",
        base_interpreter="/opt/python/3.12",
        created_at="2026-08-26T01:00:00Z",
        extras=extras or [],
    )
    install_layout.receipt_path(generation).write_text(receipt.to_json())
    return receipt


def _write_uv_receipt(home: Path, body: str) -> Path:
    receipt = home / ".local/share/uv/tools/conexus/uv-receipt.toml"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(body)
    return receipt


_UV_DIRECTORY = """
[[tool.requirements]]
name = "conexus"
directory = "/legacy/checkout"
"""

_UV_PINNED = """
[[tool.requirements]]
name = "conexus"
specifier = "==7.0.0"
"""

_UV_UNPINNED = """
[[tool.requirements]]
name = "conexus"
specifier = ">=7.0.0"
"""


# ---------------------------------------------------------------------------
# legacy uv tree -- the non-regression control, pinned byte-for-byte
# ---------------------------------------------------------------------------

def test_legacy_directory_install_keeps_its_uv_vocabulary(tmp_path, monkeypatch) -> None:
    """An un-migrated box still upgrades through uv, so the uv sentence is
    the RIGHT sentence there (contract 12). .7 leaves boxes in this state
    until their legacy tree has zero holders."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_uv_receipt(tmp_path, _UV_DIRECTORY)

    assert upgrade_finish.install_source() == (
        "local checkout (/legacy/checkout) — `uv tool upgrade` never "
        "consults PyPI for this install; use scripts/reinstall-tool.sh "
        "or reinstall from PyPI"
    )


def test_legacy_pinned_install_keeps_its_uv_vocabulary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_uv_receipt(tmp_path, _UV_PINNED)

    assert upgrade_finish.install_source() == (
        "PyPI, PINNED (==7.0.0) — `uv tool upgrade` will never move "
        "past the pin; reinstall unpinned "
        "(`uv tool install --reinstall conexus`)"
    )


def test_legacy_unpinned_install_keeps_its_uv_vocabulary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_uv_receipt(tmp_path, _UV_UNPINNED)

    assert upgrade_finish.install_source() == (
        "PyPI, unpinned — `uv tool upgrade conexus` upgrades normally"
    )


def test_no_receipt_anywhere_still_says_so(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert upgrade_finish.install_source() == "unknown (no readable uv receipt)"


# ---------------------------------------------------------------------------
# generation layout -- the branch that was unreachable before nexus-0za6e
# ---------------------------------------------------------------------------

def test_generation_directory_source_names_the_checkout(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    generation = build_generation(tmp_path, monkeypatch)
    _write_receipt(generation, source_kind="directory", source="/src/nexus")

    detail = upgrade_finish.install_source()

    assert detail.startswith("local checkout (/src/nexus) — ")
    assert install_advice.GENERATION_INSTALLER in detail
    assert "uv tool" not in detail, (
        "a generation box cannot be upgraded by any uv-tool command; naming "
        "one here sends the reader at .7's accepted risk (a raw uv install "
        "rebuilds the uv tree over the shims)"
    )


def test_generation_pinned_source_does_not_claim_the_pin_sticks(
    tmp_path, monkeypatch
) -> None:
    """VERIFIED BY EXECUTION 2026-08-26, not inferred: ``perform_self_install``
    passes ``--source`` and ``--extras`` and OMITS the version, so a
    generation built with ``--version 7.0.0`` upgrades normally on the next
    ``nx self install``. A control run with an explicit version DID emit
    ``--version 9.9.9``, so the omission is a real answer.

    Mirroring uv's "will never move past the pin" here would therefore be a
    confident falsehood, and ``health.py`` renders only the part BEFORE the
    em-dash -- so the summary has to be true standing alone."""
    monkeypatch.setenv("HOME", str(tmp_path))
    generation = build_generation(tmp_path, monkeypatch)
    _write_receipt(
        generation, source_kind="registry", source="conexus", version="7.0.0"
    )

    detail = upgrade_finish.install_source()
    summary = detail.split(" — ")[0]

    assert "7.0.0" in summary
    assert "PINNED" not in detail
    assert "never move" not in detail
    assert install_advice.GENERATION_INSTALLER in detail


def test_generation_unpinned_source_upgrades_normally(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    generation = build_generation(tmp_path, monkeypatch)
    _write_receipt(generation, source_kind="registry", source="conexus")

    detail = upgrade_finish.install_source()

    assert detail.startswith("PyPI — ")
    assert install_advice.GENERATION_INSTALLER in detail


def test_the_generation_receipt_wins_over_a_readable_uv_receipt(
    tmp_path, monkeypatch
) -> None:
    """FAILURE MODE 1, and the test that would have caught this bug.

    During the migration window BOTH receipts are readable. A
    directory-tracking legacy install migrated onto a PyPI generation
    reported the legacy source -- and the whole point of the string is
    explaining why an upgrade did or did not move."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_uv_receipt(tmp_path, _UV_DIRECTORY)
    generation = build_generation(tmp_path, monkeypatch)
    _write_receipt(generation, source_kind="registry", source="conexus")

    detail = upgrade_finish.install_source()

    assert "/legacy/checkout" not in detail, (
        "the legacy uv receipt is still readable during the migration "
        "window; the generation is what this box now runs"
    )
    assert detail.startswith("PyPI — ")


def test_extras_do_not_leak_into_the_source_string(tmp_path, monkeypatch) -> None:
    """``spec`` carries ``[local]``; ``source`` does not. The string answers
    WHERE the install came from, and a reader diagnosing a stuck upgrade does
    not need the extras spelled into it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    generation = build_generation(tmp_path, monkeypatch)
    _write_receipt(
        generation, source_kind="registry", source="conexus", extras=["local"]
    )

    detail = upgrade_finish.install_source()

    # The startswith is not decoration: without it this test passes on the
    # legacy branch too, where the answer is "unknown (no readable uv
    # receipt)" -- a string that also contains no "[local]". Caught by
    # mutation, 2026-08-26.
    assert detail.startswith("PyPI — ")
    assert "[local]" not in detail


# ---------------------------------------------------------------------------
# where the two classifiers legitimately disagree
# ---------------------------------------------------------------------------

def test_unreadable_generation_receipt_falls_back_but_keeps_layout_aware_advice(
    tmp_path, monkeypatch
) -> None:
    """Two questions, two classifiers, and they can disagree.

    WHERE the install came from is answered by the generation receipt, else
    the uv receipt. WHAT UPGRADES this box is answered by
    ``install_advice.upgrade_command`` off ``has_generation_layout()`` -- and
    a resolvable ``current`` with an unreadable receipt is exactly the state
    where those diverge. Describing the install from uv is the best answer
    available; ADVISING a uv-tool command on a box that has shims is not."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_uv_receipt(tmp_path, _UV_PINNED)
    build_generation(tmp_path, monkeypatch)  # receipt left as `{}`: unreadable

    detail = upgrade_finish.install_source()

    assert detail.startswith("PyPI, PINNED (==7.0.0) — ")
    assert "uv tool install --reinstall" not in detail, (
        "this box has shims; a raw uv install rebuilds the uv tree over "
        "them, which is .7's accepted risk"
    )
    assert install_advice.GENERATION_INSTALLER in detail


def test_a_dangling_current_falls_back_to_uv(tmp_path, monkeypatch) -> None:
    """``current_generation`` deliberately does not stat its target, so a
    pointer at a reaped generation resolves cleanly. There is no receipt to
    read there, and the uv answer is the better of two imperfect ones."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_uv_receipt(tmp_path, _UV_UNPINNED)
    tools = tmp_path / "reaped-tools"
    tools.mkdir()
    (tools / install_layout.CURRENT_LINK_NAME).symlink_to(tools / "gen-gone")
    monkeypatch.setenv(install_layout.TOOLS_DIR_ENV, str(tools))

    assert upgrade_finish.install_source() == (
        "PyPI, unpinned — `uv tool upgrade conexus` upgrades normally"
    )


def test_a_corrupt_uv_receipt_is_still_named_unknown(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _write_uv_receipt(tmp_path, "not toml at all ===")

    assert upgrade_finish.install_source() == "unknown (no readable uv receipt)"
