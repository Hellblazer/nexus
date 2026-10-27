# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The two on/off vocabularies of the RDR-232 environment switches (nexus-jypmj.2.8), and the one warning.

``NX_ANSWER_KIND_ELIGIBILITY`` and ``NX_ANSWER_STRUCTURED_EXTRACT`` are on unless set to an off value. A value
in neither vocabulary (``disable``, ``disabled``, ``off!``) is read as on, as it always was, so a user who
meant to turn a switch off and misspelled it would get the opposite and see nothing. It logs one warning per
variable per process; the behaviour does not change.
"""
from __future__ import annotations

import structlog

_log = structlog.get_logger(__name__)

__all__ = ["OFF_VALUES", "ON_VALUES", "reset_warned", "warn_unrecognised"]

#: The values that turn a switch off (any case, trimmed).
OFF_VALUES: frozenset[str] = frozenset({"0", "false", "off", "no"})
#: The values that say on out loud. Unset and blank are on too, and are not unrecognised.
ON_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})

_warned: set[str] = set()


def warn_unrecognised(variable: str, raw: str) -> None:
    """Log once per process per *variable* that *raw* (already trimmed and lower-cased) is in neither vocabulary."""
    if not raw or raw in OFF_VALUES or raw in ON_VALUES or variable in _warned:
        return
    _warned.add(variable)
    _log.warning(
        "answer_switch_value_unrecognised",
        variable=variable,
        value=raw[:40],
        read_as="on",
        off_values=sorted(OFF_VALUES),
    )


def reset_warned() -> None:
    """Forget which variables have warned (a test hook)."""
    _warned.clear()
