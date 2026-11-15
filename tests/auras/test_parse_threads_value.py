# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-249 TD1, display.threads (nexus-mdbds.11.1 rule 20): the value becomes the
heading "## aura: <value>" in a threads entry, so the parser refuses anything but
one line of printable ASCII of at most 80 characters."""

from __future__ import annotations

import pytest

from nexus.auras import AuraError, parse_aura
from tests.auras._support import minimal


def _with(threads: str) -> bytes:
    return minimal().replace('threads = "a block"', f'threads = "{threads}"').encode()


@pytest.mark.parametrize("bad", ["café", "—dash", "x" * 81])
def test_a_threads_value_that_is_not_short_printable_ascii_is_refused(bad: str) -> None:
    with pytest.raises(AuraError) as err:
        parse_aura(_with(bad), name="mini")
    assert err.value.kind == "bad_threads" and err.value.key == "display.threads"


def test_an_eighty_character_ascii_threads_value_parses() -> None:
    assert parse_aura(_with("y" * 80), name="mini").threads == "y" * 80
