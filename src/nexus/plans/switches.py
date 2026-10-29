# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The two on/off vocabularies of the RDR-232 environment switches (nexus-jypmj.2.8), and the one warning.

``NX_ANSWER_KIND_ELIGIBILITY`` and ``NX_ANSWER_STRUCTURED_EXTRACT`` are on unless set to an off value. A value in
neither vocabulary (``disable``, ``disabled``, ``off!``) is read as on, as it always was, so a user who meant to
turn a switch off and misspelled it would get the opposite and see nothing. It logs one warning per variable per
process; the behaviour does not change.

``NX_ANSWER_COVERAGE`` (nexus-jypmj.3.3) is ON by default since Sam's decision of 2026-10-28 (nexus-jypmj.17): unset
or blank reads as on, and ``0``/``false``/``off``/``no`` is the kill switch. Its unrecognised-value rule is the
other way round from the two switches above: a value in neither vocabulary (``disable``, ``disabled``, ``off!``)
is read as OFF and logs the same one warning. A typo of either intent therefore lands on the unassessed
behaviour, which changes no answer; only an explicit on value (``1``, ``true``, ``yes``, ``on``) or leaving the
variable unset runs the assessment.
"""
from __future__ import annotations

from collections.abc import Mapping

import structlog

_log = structlog.get_logger(__name__)

__all__ = [
    "ANSWER_COVERAGE_ENABLED",
    "ANSWER_COVERAGE_ENV",
    "OFF_VALUES",
    "ON_VALUES",
    "coverage_enabled",
    "reset_warned",
    "warn_unrecognised",
]

#: The values that turn a switch off (any case, trimmed).
OFF_VALUES: frozenset[str] = frozenset({"0", "false", "off", "no"})
#: The values that say on out loud. Unset and blank are on too (all three switches), and are not unrecognised.
ON_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})

#: The kill switch of the Phase 3 coverage boundary (RDR-232 section 4, nexus-jypmj.3.3).
ANSWER_COVERAGE_ENV = "NX_ANSWER_COVERAGE"
#: What an unset or blank :data:`ANSWER_COVERAGE_ENV` means (an unrecognised value is off, whatever this is). One
#: source for the default; a test flips it. On since Sam's decision of 2026-10-28 (nexus-jypmj.17), taken together
#: with the concurrent per-stream assessment so the default does not carry the serial latency the Phase 3 canary
#: measured (nexus-jypmj.3.10).
ANSWER_COVERAGE_ENABLED: bool = True

_warned: set[str] = set()


def warn_unrecognised(variable: str, raw: str, *, read_as: str = "on") -> None:
    """Log once per process per *variable* that *raw* (already trimmed and lower-cased) is in neither vocabulary,
    and what it was read as."""
    if not raw or raw in OFF_VALUES or raw in ON_VALUES or variable in _warned:
        return
    _warned.add(variable)
    _log.warning(
        "answer_switch_value_unrecognised",
        variable=variable,
        value=raw[:40],
        read_as=read_as,
        off_values=sorted(OFF_VALUES),
    )


def reset_warned() -> None:
    """Forget which variables have warned (a test hook)."""
    _warned.clear()


def coverage_enabled(environ: Mapping[str, str]) -> bool:
    """Whether ``nx_answer`` runs the coverage boundary for a request, decided now from *environ*.

    Unset or blank: :data:`ANSWER_COVERAGE_ENABLED` (``True``). A value in neither vocabulary: off whatever the
    default, so a mistyped value never runs the assessor, and one warning per process. An on value
    (``1``, ``true``, ``yes``, ``on``): on.
    An off value (``0``, ``false``, ``off``, ``no``): off. Off means every run is unassessed and ``nx_answer``
    behaves exactly as before Phase 3 (no boundary is handed to the runner, so no assessment call, no repair, no
    skipped generation, no partial notice).
    """
    raw = str(environ.get(ANSWER_COVERAGE_ENV, "")).strip().lower()
    if raw in ON_VALUES:
        return True
    if raw in OFF_VALUES:
        return False
    if not raw:
        return ANSWER_COVERAGE_ENABLED
    warn_unrecognised(ANSWER_COVERAGE_ENV, raw, read_as="off")
    return False
