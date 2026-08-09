# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Stranded-install detection for the post-Chroma-deletion era (nexus-gynt2).

RDR-155 P4b (the N+1 release) deletes the Chroma read client AND the
migration tool itself; RDR-158 P4 deletes SQLite T2. A 4.x/5.x/early-6.x
box that pip-upgrades DIRECTLY to that release would otherwise boot a
fresh empty PG install beside its unmigrated ``chroma/chroma.sqlite3``,
``t2.db``, ``memory.db``, and ``catalog/.catalog.db`` — indistinguishable
from data loss. This module is the detection-only guard: pure file stats
(ZERO ``chromadb`` / ``sqlite3`` imports — at N+1 those substrates no
longer exist in the codebase, and the ``chroma.sqlite3`` /
``.catalog.db`` names below are filename STRINGS the RDR-155 P4b.1
inverse-grep must not flag), tripping LOUD with the literal two-hop
redirect:

  hop 1 — install the pinned last migration-capable release and run
  ``nx upgrade`` there (the RDR-185 ladder converges the pre-PG data
  migration; copy-not-move — the files stay behind as rollback sources);
  hop 2 — upgrade back to this version.

(The message says ``nx upgrade``, not ``guided-upgrade``: RDR-185 P4.1
demoted the upgrade-ceremony verbs to hidden internal primitives — a
user-facing remedy must name a verb the user can find in ``--help``,
and on every release the pin can point at, the ladder carries the
migration job. Enforced by tests/upgrade/test_verb_demotion.py.)

Data deletion is a third, separately consented act — never part of the
message, never performed here (Hal-confirmed two-hop contract,
2026-07-21).

**Armed by one constant.** :data:`LAST_MIGRATION_CAPABLE` is ``None`` on
every migration-capable release — the detector is DISARMED and every
entry point is a no-op, because on those releases ``memory.db`` /
``.catalog.db`` are still LIVE stores and the migration ladder exists
in-place (tripping would false-positive every healthy box and the
fresh-install MVV). Stamping the constant at N+1 cut time arms detection
at every wired entry point (``nx init``, CLI startup, MCP startup,
``nx doctor``) at once — the same one-constant discipline as
:data:`nexus.engine_version.REQUIRED_ENGINE_VERSION`.

**Leaf module contract**: stdlib only, no ``nexus`` imports (AST-enforced
by ``tests/test_stranded_install.py``). Callers resolve the three path
roots; :func:`nexus.config.detect_stranded_install_default` is the shared
assembler every entry point uses.

**De-strand signal (nexus-4922x, 2026-08-09).** The PRIMARY signal is an
injected ``ladder_migration_verified`` probe (see
:func:`detect_stranded_install`'s docstring) querying the engine-side
upgrade-ladder completion record — the actual output of the CURRENT
documented remedy (``nx upgrade`` at the pin). The original
``<config>/migration-reports/*.json`` file check
(:func:`_has_verified_migration_report`) survives as a SECONDARY signal
for users who migrated via the older, unadvertised ``nx storage
migrate*`` command family — but neither of the two remedy paths the
two-hop message has ever named (``nx upgrade``, and the hidden ``nx
guided-upgrade``) writes that format, so relying on it alone left every
real user re-tripping the detector forever on hop 3. Because this module
stays a stdlib-only leaf, it cannot reach the engine itself; the probe is
always caller-injected, built in production by
:func:`nexus.config._ladder_migration_verified`.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

#: The pinned last migration-capable conexus release. Was ``None`` on every
#: release that still shipped ``nx guided-upgrade`` (all of 6.x) — detection
#: was disarmed. STAMPED at the 7.0.0 cut (2026-08-01, the N+1 release that
#: ships the RDR-158 substrate retirements): 6.18.1 is the last released
#: version whose ``nx guided-upgrade`` can read the pre-PG stores. The stamp
#: arms detection at every wired entry point (``nx init``, CLI startup, MCP
#: startup, ``nx doctor``) at once. Tripwire flipped in the same commit:
#: ``test_constant_is_stamped_on_the_post_migration_release``.
LAST_MIGRATION_CAPABLE: str | None = "6.18.1"

#: Filename of the CLI version stamp inside the nexus config dir. Duplicates
#: ``nexus.upgrade_finish.STAMP_FILENAME`` (this leaf cannot import it);
#: parity is test-enforced (``test_stamp_filename_matches_upgrade_finish``).
STAMP_FILENAME = "last_seen_version"

_REPORTS_DIRNAME = "migration-reports"

#: Sentinel distinguishing "use the module constant" from an explicit
#: ``None`` (= disarmed) passed by a caller or test.
_USE_PINNED: object = object()


@dataclass(frozen=True)
class StrandedInstall:
    """A detected stranded install: unmigrated pre-PG data on a release
    that can no longer migrate it."""

    #: Version string from the ``last_seen_version`` stamp, or ``None``
    #: when the box never wrote one (pre-stamp releases / CLI never ran).
    #: Advisory only — never gates detection.
    era: str | None
    #: Absolute paths of the pre-PG artifacts found on disk.
    artifacts: tuple[str, ...]
    #: The ``LAST_MIGRATION_CAPABLE`` value detection ran under.
    pinned_release: str

    @property
    def message(self) -> str:
        """The literal two-hop redirect (bead nexus-gynt2 spec)."""
        era_clause = (
            f"conexus {self.era}" if self.era else "an earlier, pre-PG conexus release"
        )
        pin = self.pinned_release
        return (
            f"This install carries unmigrated pre-PG data from {era_clause} "
            f"({', '.join(self.artifacts)}). This conexus version no longer ships the "
            f"migration tool, so it cannot read or migrate that data — proceeding "
            f"would look like an empty install, not data loss; nothing has been "
            f"touched. Two-hop upgrade: (1) install conexus=={pin} "
            f"(`uv tool install conexus=={pin}` or `pip install conexus=={pin}`), "
            f"(2) run `nx upgrade` there to migrate the data, "
            f"(3) upgrade back to this version."
        )


def legacy_chroma_dir() -> Path:
    """The FROZEN legacy local-Chroma store location (pre-PG era).

    RDR-155 P4b: this stopped being a configurable serving path when the
    chroma substrate retired; it survives only as the on-disk location the
    stranded-install detector (and the P3-dying legacy index leg) probes.

    Precedence (matching the retired ``config._default_local_path``):
      1. ``NX_LOCAL_CHROMA_PATH`` env var (explicit override)
      2. ``$XDG_DATA_HOME/nexus/chroma``
      3. ``~/.local/share/nexus/chroma``
    """
    import os  # noqa: PLC0415 — stdlib, branch-local

    override = os.environ.get("NX_LOCAL_CHROMA_PATH")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "nexus" / "chroma"
    return Path.home() / ".local" / "share" / "nexus" / "chroma"


def _pre_pg_artifacts(config_dir: Path, chroma_dir: Path, catalog_dir: Path) -> tuple[Path, ...]:
    """The four pre-PG store files whose presence marks unmigrated data."""
    return (
        chroma_dir / "chroma.sqlite3",
        config_dir / "t2.db",
        config_dir / "memory.db",
        catalog_dir / ".catalog.db",
    )


def _has_verified_migration_report(config_dir: Path) -> bool:
    """True when the NEWEST (mtime — migration ids are random UUIDs, same
    recency rule as doctor's ``_newest_migration_report_path``) report under
    ``<config>/migration-reports/`` records ``verification=="verified"`` with
    zero failures. Anything else — mismatch, indeterminate, a pre-6.2 report
    with no verdict, an unreadable file — is NOT proof of migration: fail
    closed (the nexus-r0esi never-silently-pass rule). Re-running the
    migration ladder on an actually-migrated box is a near-no-op re-verify;
    staying silent on an unmigrated one is indistinguishable from data loss.

    SECONDARY signal only (nexus-4922x). Neither of the two remedy paths the
    two-hop message can name at the pin release — the advertised ``nx
    upgrade`` (the ladder; records completion engine-side via
    ``HttpLadderStore``, never touches this directory) nor the hidden ``nx
    guided-upgrade`` (delegates to ``nexus.migration.driver
    .run_guided_upgrade``, which also never writes here) — produces this
    format; only the separate, unadvertised ``nx storage migrate*`` family
    does. This check survives for users who migrated via THAT older path
    (pre-dates the ladder) and would otherwise lose their de-strand.
    :func:`detect_stranded_install`'s ``ladder_migration_verified`` callback
    is the PRIMARY signal — the one the documented remedy actually
    produces.
    """
    reports_dir = config_dir / _REPORTS_DIRNAME
    try:
        candidates = sorted(
            reports_dir.glob("migration-*.json"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return False
    if not candidates:
        return False
    try:
        report = json.loads(candidates[-1].read_text())
        summary = report.get("summary") or {}
        total_failed = int(summary.get("total_failed", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return report.get("verification") == "verified" and total_failed == 0


def _read_era(config_dir: Path) -> str | None:
    """Best-effort pre-PG era from the ``last_seen_version`` stamp.

    CLOBBER GUARD (critique 21029 Critical 1): the version-transition
    trigger (``upgrade_finish.check_version_transition``) rewrites the
    stamp to the CURRENTLY RUNNING version on the first invocation after
    any upgrade — including the direct hop onto N+1 itself. A stamp equal
    to this install's own version is therefore that clobber's signature,
    not evidence the pre-PG data came from this version; reporting it
    would make the message self-contradictory ("pre-PG data from conexus
    <N+1>" on the very release that dropped the migration tool). Treat it
    as unknown — the message falls back to "an earlier, pre-PG conexus
    release", which is always true. The CLI wiring additionally runs the
    detector BEFORE the transition trigger so the first invocation still
    reports the genuine era; MCP startups (where the trigger fires
    earlier) degrade to the fallback clause. Era is advisory and never
    gates detection.
    """
    try:
        text = (config_dir / STAMP_FILENAME).read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        if text == _dist_version("conexus"):
            return None
    except PackageNotFoundError:
        pass  # no installed distribution (frozen/odd env): era passes through
    return text


def detect_stranded_install(
    config_dir: Path,
    chroma_dir: Path,
    catalog_dir: Path,
    *,
    last_migration_capable: str | None | object = _USE_PINNED,
    ladder_migration_verified: Callable[[], bool | None] | None = None,
) -> StrandedInstall | None:
    """Detect a stranded pre-PG install. Pure file stats + stdlib json (plus
    one optional injected network probe — see ``ladder_migration_verified``).

    Returns ``None`` (the overwhelmingly common case) when any of:

    - the detector is disarmed (``last_migration_capable`` is ``None`` —
      the state of every migration-capable release), which short-circuits
      before any filesystem access;
    - none of the four pre-PG store files exist (fresh box);
    - ``ladder_migration_verified`` reports the CURRENT remedy path's
      engine-side completion record as verified (see below — the PRIMARY
      migrated signal, nexus-4922x);
    - the newest legacy migration report is verified-clean (SECONDARY
      signal — see :func:`_has_verified_migration_report`; migrated box,
      files legitimately remain as copy-not-move rollback sources).

    Otherwise returns a :class:`StrandedInstall` whose ``message`` is the
    literal two-hop redirect. Callers decide loudness per entry point
    (``nx init`` refuses; CLI banners; MCP logs; doctor fails).

    ``ladder_migration_verified`` (nexus-4922x, replacing the dead
    ``migration-reports/*.json`` primary check — nothing on the CURRENT
    remedy path (``nx upgrade`` at the pin, the ladder) writes that format
    any more; see :func:`_has_verified_migration_report`'s docstring for
    the full trace). A zero-arg probe of the engine-side upgrade-ladder
    completion record — the production implementation
    (:func:`nexus.config._ladder_migration_verified`, wired in by
    :func:`nexus.config.detect_stranded_install_default`) queries
    ``HttpLadderStore`` for the historical ``substrate-etl`` rung, which is
    what the pin's ``nx upgrade`` records on a real migration. Contract:

    - ``True`` — the engine confirms the migration verified. De-strands
      immediately, WITHOUT consulting the legacy report file.
    - ``False`` — the engine is reachable and has no such record. Falls
      through to the legacy report-file check (still may de-strand a user
      who migrated via the older ``nx storage migrate*`` path).
    - ``None`` — the probe could not reach or query the engine at all
      (unresolvable endpoint, connection error, any exception the probe
      itself caught). Treated IDENTICALLY to ``False`` — stay stranded,
      never silently de-strand on "can't tell". This is the expected state
      at ``nx init`` time on a genuinely stranded box, since the engine has
      not been provisioned yet; the probe implementation logs a structured
      warning so the degradation is loud even though it never raises here.
    - ``None`` for this PARAMETER (the default) — no ladder check at all,
      reproducing the exact pre-nexus-4922x behavior (legacy report file
      only). Callers that never opt in (most existing tests) are
      unaffected.
    """
    pin = (
        LAST_MIGRATION_CAPABLE
        if last_migration_capable is _USE_PINNED
        else last_migration_capable
    )
    if pin is None:
        return None
    found = tuple(
        str(p)
        for p in _pre_pg_artifacts(config_dir, chroma_dir, catalog_dir)
        if p.is_file()
    )
    if not found:
        return None
    if ladder_migration_verified is not None and ladder_migration_verified():
        return None
    if _has_verified_migration_report(config_dir):
        return None
    return StrandedInstall(
        era=_read_era(config_dir),
        artifacts=found,
        pinned_release=str(pin),
    )
