# SPDX-License-Identifier: AGPL-3.0-or-later
"""Capture INFO-level structlog events in a test.

The suite's structlog configuration filters below WARNING (``make_filtering_bound_logger``),
so ``structlog.testing.capture_logs`` alone sees no ``_log.info`` call. The session-end
handler's evidence is its INFO steps, so a test that asserts them lowers the filter for the
block and puts it back.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

import structlog
import structlog.testing


@contextlib.contextmanager
def info_logs() -> Iterator[list[dict[str, Any]]]:
    previous = structlog.get_config()["wrapper_class"]
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
    try:
        with structlog.testing.capture_logs() as logs:
            yield logs
    finally:
        structlog.configure(wrapper_class=previous)
