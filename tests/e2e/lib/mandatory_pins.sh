#!/usr/bin/env bash
# shellcheck disable=SC2034  # a sourced constants file: its callers use every name
# The one place that names the `mandatory_regression_pin` tests' two homes (nexus-z0o2p.41).
#
# Sourced by tests/e2e/local-service-gate.sh (which must NOT run them) and
# tests/e2e/release-preflight.sh (which must). Two files naming one set by hand is how
# a pin ends up run by neither.
#
# WHY THEY LEAVE THE LOCAL-SERVICE GATE. The gate runs under a fenced HOME that never mirrors
# ~/.config/gh (tests/e2e/lib/fence_home.sh, FENCE_ALWAYS_SHADOWS: a credential store is never
# mirrored into a gate that provisions and discards things), so `gh auth status` fails there and
# every GitHub-backed pin skips, and tests/conftest.py holds a skipped pin to
# NX_MANDATORY_PIN_SKIP_BUDGET (default 0). The gate therefore read FAILED on every run, and it
# carries the cut battery's positive control. Sam's decision (2026-10-01): move the pins out of the
# gate's selection and run them where a real HOME with gh auth exists; keep the fence and keep the
# zero budget. Do not pass a token into the fence and do not raise the budget for the gate.

# The gate's pytest marker expression. `not mandatory_regression_pin` is the point of this file.
LSG_PYTEST_MARK_EXPR="integration and not lived_in and not cloud_mode and not mandatory_regression_pin"

# What the pins' own run selects.
MANDATORY_PIN_MARK_EXPR="integration and mandatory_regression_pin"

# Exact count of `mandatory_regression_pin` tests, the same discipline as the lived_in and
# cloud_mode carve-outs in local-service-gate.sh: a new pin must bump this consciously, because
# the gate's selection no longer runs it and the pins gate asserts it ran this many.
# 2026-10-01: 4 = three CI-evidence pins (tests/scripts/test_check_release_ci_evidence.py) and the
# release-floor source-ancestry pin (tests/scripts/test_check_engine_release_floor.py).
MANDATORY_PIN_EXPECTED=4
