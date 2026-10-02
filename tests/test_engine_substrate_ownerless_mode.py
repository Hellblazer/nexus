# SPDX-License-Identifier: AGPL-3.0-or-later
"""The substrate's ownerless-write mode pin (RDR-223 Phase 3, nexus-z0o2p.24; blank handling
nexus-z0o2p.36 round 2).

The engine parses an unset or BLANK ``NX_OWNERLESS_WRITE_MODE`` as log-only; the substrate pins
``enforce`` so every substrate-backed test sees the refusal. A caller environment that carries the
variable EMPTY (``NX_OWNERLESS_WRITE_MODE=`` in a shell or a CI matrix cell) must count as unset,
exactly as the local launcher treats it, or the suite boots a log-only engine without saying so.

    NX_TEST_T2_SUBSTRATE=none uv run pytest tests/test_engine_substrate_ownerless_mode.py -q
"""
from __future__ import annotations

import pytest

from tests._engine_substrate import _pin_ownerless_write_mode

_ENV = "NX_OWNERLESS_WRITE_MODE"


def test_an_absent_value_is_pinned_to_enforce() -> None:
    env: dict[str, str] = {}
    _pin_ownerless_write_mode(env)
    assert env[_ENV] == "enforce"


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t", " \n "])
def test_a_blank_value_counts_as_unset_and_is_pinned_to_enforce(blank: str) -> None:
    env = {_ENV: blank}
    _pin_ownerless_write_mode(env)
    assert env[_ENV] == "enforce", repr(blank)


@pytest.mark.parametrize("explicit", ["log-only", "enforce", "LOG-ONLY"])
def test_an_explicit_value_wins(explicit: str) -> None:
    env = {_ENV: explicit}
    _pin_ownerless_write_mode(env)
    assert env[_ENV] == explicit


def test_the_boot_path_goes_through_the_pin() -> None:
    """The pin only protects the suite if ``_boot`` calls it, not a hand-copied ``setdefault``."""
    import inspect

    from tests import _engine_substrate

    src = inspect.getsource(_engine_substrate._boot)
    assert "_pin_ownerless_write_mode(env)" in src
    assert 'setdefault("NX_OWNERLESS_WRITE_MODE"' not in src
