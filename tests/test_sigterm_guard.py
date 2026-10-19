# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/_sigterm_guard.py names the test that leaks SIGTERM state and puts it back (nexus-yqzpw)."""

from __future__ import annotations

import os
import signal
import warnings

import pytest

from tests._sigterm_guard import term_left_as_found

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX signal dispositions and mask")


def _blocked() -> bool:
    return signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, [])


def test_a_body_that_ignores_and_blocks_term_is_named_and_undone() -> None:
    before = signal.getsignal(signal.SIGTERM)
    try:
        with pytest.warns(UserWarning, match=r"leaker::t left SIGTERM ignored and blocked"):
            with term_left_as_found("leaker::t"):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
        assert signal.getsignal(signal.SIGTERM) is before
        assert not _blocked()
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])
        signal.signal(signal.SIGTERM, before)


def test_a_body_that_restores_what_it_changed_is_silent() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with term_left_as_found("tidy::t"):
            prior = signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
            signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])
            signal.signal(signal.SIGTERM, prior)


def test_a_state_that_was_already_there_is_not_blamed_on_the_body() -> None:
    before = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with term_left_as_found("innocent::t"):
                pass
    finally:
        signal.signal(signal.SIGTERM, before)
