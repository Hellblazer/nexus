# SPDX-License-Identifier: AGPL-3.0-or-later
"""tests/_sigterm_guard.py names the test that leaks SIGTERM state and puts it back, and
run_suite starts a suite with TERM at its default whatever the worker carries (nexus-yqzpw)."""

from __future__ import annotations

import os
import signal
import subprocess
import warnings
from pathlib import Path

import pytest

from tests._sigterm_guard import run_suite, term_left_as_found, term_state_of_this_worker

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


def test_a_suite_starts_with_term_at_its_default_even_when_this_worker_leaked_it(tmp_path: Path) -> None:
    """Positive control for run_suite: a worker with TERM ignored AND blocked still starts a
    suite that a TERM kills, and the leak is named in a warning. The negative leg spawns the
    same script plainly under the same state and it survives its own TERM (the 5588beead
    shard 3 shape), so the control cannot pass on a host where the state does not carry."""
    assert term_state_of_this_worker() is None, "the worker arrived leaked; the control needs a clean start"
    script = tmp_path / "self_term.sh"
    script.write_text('kill -TERM $$\necho survived\n', encoding="utf-8")
    before = signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGTERM])
    try:
        plain = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)
        with pytest.warns(UserWarning, match="TERM ignored, TERM blocked"):
            result = run_suite(script, cwd=tmp_path, env=dict(os.environ))
        assert term_state_of_this_worker() == "TERM ignored, TERM blocked", "the spawn must restore the caller's state"
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])
        signal.signal(signal.SIGTERM, before)
    assert (plain.returncode, plain.stdout) == (0, "survived\n"), "negative leg: the leaked state must reach a plain spawn"
    assert result.returncode == -signal.SIGTERM, (result.returncode, result.stdout)
    assert "survived" not in result.stdout
