# SPDX-License-Identifier: AGPL-3.0-or-later
"""Event-loop blocking tests for nx_answer.

Verifies nx_answer does NOT hang the process with synchronous I/O.
Uses threading + timeout to detect blocking calls that asyncio.wait_for
cannot catch (blocking before the first await).
"""
from __future__ import annotations

import asyncio
import threading

import pytest


def _run_nx_answer_in_thread(question: str, result_box: list, error_box: list):
    """Run nx_answer on a fresh event loop in a thread."""
    async def _call():
        from nexus.mcp.core import nx_answer
        return await nx_answer(question=question)

    try:
        loop = asyncio.new_event_loop()
        result_box.append(loop.run_until_complete(_call()))
    except Exception as exc:
        error_box.append(exc)
    finally:
        loop.close()


def test_nx_answer_does_not_hang():
    """nx_answer must return within 10 seconds on plan-miss.

    Runs in a thread so we can enforce a hard timeout even if the
    event loop is blocked by synchronous I/O.
    """
    result_box: list = []
    error_box: list = []
    t = threading.Thread(
        target=_run_nx_answer_in_thread,
        args=("xyzzy nonsense query no plan matches", result_box, error_box),
    )
    t.start()
    t.join(timeout=10.0)

    if t.is_alive():
        pytest.fail(
            "nx_answer hung for >10 seconds. Likely synchronous "
            "subprocess.run() or blocking I/O inside an async path."
        )

    if error_box:
        # An error is acceptable — we're testing non-blocking, not correctness.
        pass

    if result_box:
        assert isinstance(result_box[0], str)
