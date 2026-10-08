# SPDX-License-Identifier: AGPL-3.0-or-later
"""Session-start verdict on the engine substrate's prerequisites.

The substrate (``tests/_engine_substrate.py``) needs two things from the box:
a fresh service jar and the pinned engine tag's PG bundle. When either is
absent, ``ensure_engine()`` remembers the failure and re-raises it for every
substrate-backed test, so a full run reports tens of thousands of setup errors
(25,636 on 2026-10-08) that are one fact, at the cost of a full-length run.

This module asks the same two questions once, with the substrate's OWN
functions (no second copy of the freshness rule or the provisioning code), and
returns the refusal text or ``None``. ``tests/conftest.py``
``_preflight_engine_substrate`` runs it on the controller at session start and
turns a refusal into exit 75, as ``_gate_on_build_lease`` does for a build in
progress.

The bundle is provisioned HERE when it is not cached: once, on the controller,
before any xdist worker spawns, so each worker finds it in the per-tag cache
(``substrate_cache_root()``, which the HOME fence mirrors) instead of racing a
download.

Every input is injectable so the unit tests never touch the network or the
real ``service/`` tree; the defaults are the real functions, resolved at call
time so a test that patches the substrate module is honoured.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

PREFIX = "engine substrate: refusing to start — "
_ESCAPE = "Set NX_TEST_T2_SUBSTRATE=none for a run that needs no engine."


def _default_jar_reason() -> str | None:
    from tests.db import _service_fixture  # noqa: PLC0415 — deferred: test-support module, resolved at call time so a patched module is honoured

    return _service_fixture.jar_freshness_skip_reason()


def _default_resolve_pg_bin() -> Path:
    from tests import _engine_substrate  # noqa: PLC0415 — deferred, as above

    return _engine_substrate._pg_bin()


def _default_provision_failure() -> str | None:
    from tests.db import _service_fixture  # noqa: PLC0415 — deferred, as above

    return _service_fixture.last_provision_failure()


def _default_pinned_tag() -> str | None:
    from nexus.daemon import binary_install  # noqa: PLC0415 — deferred, as above

    return binary_install.PINNED_SERVICE_TAG


def refusal(
    *,
    jar_reason: Callable[[], str | None] | None = None,
    resolve_pg_bin: Callable[[], Path] | None = None,
    provision_failure: Callable[[], str | None] | None = None,
    pinned_tag: Callable[[], str | None] | None = None,
) -> str | None:
    """The one-line reason the substrate cannot boot, or ``None`` when it can.

    The jar is checked first and alone: it is a stat walk, and a bad jar makes
    a bundle download a wasted minute.
    """
    reason = (jar_reason or _default_jar_reason)()
    if reason:
        # The reason carries its own remedy (scripts/build-gate-jar.sh, or
        # "wait for the build"), so none is appended here: a second remedy is
        # how a stale jar once showed two different instructions.
        return f"{PREFIX}{reason}. {_ESCAPE}"

    tag = (pinned_tag or _default_pinned_tag)()
    try:
        pg_bin = (resolve_pg_bin or _default_resolve_pg_bin)()
    except Exception as exc:  # noqa: BLE001 — the substrate's own error (a broken NEXUS_PG_BIN, a verification failure) reported once instead of per test
        return (
            f"{PREFIX}resolving the PostgreSQL bundle for the pinned engine tag {tag} "
            f"raised {type(exc).__name__}: {exc}. Fix that, or {_ESCAPE[0].lower()}{_ESCAPE[1:]}"
        )
    if pg_bin.exists():
        return None

    failure = (provision_failure or _default_provision_failure)()
    if failure:
        return (
            f"{PREFIX}the pinned engine tag {tag} is not published (or its PG bundle is "
            f"not downloadable): {failure}. Wait for the engine release to publish and "
            "rerun, point NEXUS_PG_BIN at a PostgreSQL bin dir that has pgvector, or "
            f"{_ESCAPE[0].lower()}{_ESCAPE[1:]}"
        )
    return (
        f"{PREFIX}no PostgreSQL bundle is available for the pinned engine tag {tag} "
        "(none cached, none discoverable, and self-provisioning had no tag to fetch). "
        f"Install the PG bundle (nx init), point NEXUS_PG_BIN at one, or {_ESCAPE[0].lower()}{_ESCAPE[1:]}"
    )
