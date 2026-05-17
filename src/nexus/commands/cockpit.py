# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx cockpit -- user-facing surfaces over the ORB tuplespace + bridge.

Subcommands:

- ``status``           -- per-subspace tuple counts + most-recent timestamps
                          (RDR-111 Phase 2 surface, originally introduced
                          alongside the bridge in PR #786).
- ``show <panel>``     -- render one Phase 3 panel.
- ``dashboard``        -- render all three Phase 3 panels via the auto-
                          layout primitive (RDR-111 Phase 3, nexus-ut5r).

Phase 3 panels (active-claims, recent-events, active-bindings) live under
``nexus.cockpit.panels``. The layout primitive lives under
``nexus.cockpit.layout``.

Daemon-mode awareness
---------------------

When ``NX_STORAGE_MODE=daemon`` is set, the tuplespace daemon owns
``tuples.db`` as the single writer (RDR-112 §9). Panels then route
through the daemon's ``tuplespace.list_active_claims`` and
``tuplespace.recent_events`` RPCs instead of opening a second SQLite
handle on the same file (nexus-x65c). Failure modes (no discovery
file, dead PID, RPC error) surface loud rather than silently falling
back to a direct-read; the boundary is the contract.

The ``active-bindings`` panel reads binding-profile YAML, not
SQLite, so it is unaffected by the boundary and continues to read
profiles from disk directly.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import click

from nexus.cockpit.layout import RenderedPanel, layout_vertical_stack, render_text


# ---------------------------------------------------------------------------
# Group + status (PR #786 status command, em-dashes scrubbed)
# ---------------------------------------------------------------------------


@click.group(name="cockpit")
def cockpit_group() -> None:
    """Cockpit surfaces for the ORB tuplespace (RDR-111).

    \b
    Subcommands:
      status      show recent hook events + per-subspace counts
      show        render one Phase 3 panel (active-claims, recent-events, active-bindings)
      dashboard   render all three Phase 3 panels via the layout engine
    """


@cockpit_group.command(name="status")
@click.option(
    "--window",
    "-w",
    default="1h",
    show_default=True,
    help="Time window (e.g. 10m, 1h, 24h, 7d) for recent-event counts.",
)
@click.option(
    "--db",
    "db_path_arg",
    type=click.Path(path_type=Path),
    default=None,
    help="Override path to tuples.db (defaults to ~/.config/nexus/tuples.db).",
)
def status_cmd(window: str, db_path_arg: Path | None) -> None:
    """Show tuplespace status: db size, per-subspace counts, recent timestamps."""
    db_path = db_path_arg or _default_tuples_db()
    if not db_path.exists():
        click.echo(f"tuples.db not found at {db_path}", err=True)
        click.echo(
            "The hook bridge has not run yet (or NX_BRIDGE_DISABLE is set).",
            err=True,
        )
        raise SystemExit(1)

    window_seconds = _parse_window(window)
    now = time.time()

    # storage-boundary-allow: cockpit CLI read-only inspection of tuples.db.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute("SELECT COUNT(*) AS n FROM tuples").fetchone()["n"]
        rows = conn.execute(
            """
            SELECT subspace,
                   COUNT(*) AS total_count,
                   SUM(CASE WHEN created_at > ? THEN 1 ELSE 0 END) AS recent_count,
                   MAX(created_at) AS most_recent_at
              FROM tuples
             GROUP BY subspace
             ORDER BY most_recent_at DESC NULLS LAST
            """,
            (now - window_seconds,),
        ).fetchall()
    finally:
        conn.close()

    size_bytes = db_path.stat().st_size

    click.echo(f"tuples.db        {db_path}")
    click.echo(f"size             {_human_bytes(size_bytes)}")
    click.echo(f"total tuples     {total}")
    click.echo(f"window           {window}  (recent = within window)")
    click.echo("")

    if not rows:
        click.echo("No subspaces have tuples yet.")
        return

    click.echo(f"{'subspace':<48}  {'total':>8}  {'recent':>8}  {'most-recent':>20}")
    click.echo("-" * 90)
    for r in rows:
        ts = r["most_recent_at"]
        ts_str = _fmt_relative(now - ts) if ts is not None else "-"
        click.echo(
            f"{r['subspace']:<48}  {r['total_count']:>8}  "
            f"{r['recent_count']:>8}  {ts_str:>20}"
        )


# ---------------------------------------------------------------------------
# Phase 3: show <panel> + dashboard
# ---------------------------------------------------------------------------


_PANEL_NAMES = ("active-claims", "recent-events", "active-bindings")


@cockpit_group.command(name="show")
@click.argument("panel", type=click.Choice(_PANEL_NAMES))
@click.option("--limit", default=25, show_default=True, type=int,
              help="Row cap for the recent-events panel.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit machine-readable JSON instead of the text panel.")
def show_cmd(panel: str, limit: int, as_json: bool) -> None:
    """Render a single Phase 3 panel to stdout."""
    width = _terminal_width()
    rendered = _render_one(panel, limit=limit)

    if as_json:
        click.echo(json.dumps(_panel_to_json(panel, rendered)))
        return

    descriptor = layout_vertical_stack([rendered], width=width)
    click.echo(render_text(descriptor), nl=False)


@cockpit_group.command(name="dashboard")
@click.option("--limit", default=15, show_default=True, type=int,
              help="Row cap for the recent-events panel.")
@click.option("--width", default=None, type=int,
              help="Override the layout width (default: terminal width).")
def dashboard_cmd(limit: int, width: int | None) -> None:
    """Render all three Phase 3 panels via the auto-layout engine."""
    panels = [
        _render_one("active-claims", limit=limit),
        _render_one("recent-events", limit=limit),
        _render_one("active-bindings", limit=limit),
    ]
    chosen_width = width if width is not None else _terminal_width()
    descriptor = layout_vertical_stack(panels, width=chosen_width)
    click.echo(render_text(descriptor), nl=False)


# ---------------------------------------------------------------------------
# Panel rendering helpers
# ---------------------------------------------------------------------------


def _render_one(panel: str, *, limit: int) -> RenderedPanel:
    if panel == "active-claims":
        return _render_active_claims()
    if panel == "recent-events":
        return _render_recent_events(limit=limit)
    if panel == "active-bindings":
        return _render_active_bindings()
    raise click.UsageError(f"unknown panel: {panel}")


def _is_daemon_mode() -> bool:
    """Return True if the active storage mode is daemon.

    nexus-507q (RDR-112 P6.3 cutover, 2026-05-17): the default flipped
    to daemon, so an unset env var now returns True from here.
    """
    from nexus.db import is_daemon_mode
    return is_daemon_mode()


def _open_daemon_client() -> "T2Client":  # noqa: F821 — forward ref
    """Construct a T2Client from the daemon discovery file.

    Fails loud (`click.ClickException`) under daemon mode with no
    silent fallback to direct-read — the RDR-112 §9 boundary forbids
    a second SQLite writer.
    """
    from nexus.daemon.discovery import find_t2_daemon  # noqa: PLC0415
    from nexus.daemon.t2_client import T2Client  # noqa: PLC0415

    info = find_t2_daemon()
    if info is None:
        raise click.ClickException(
            "NX_STORAGE_MODE=daemon is set but no T2 daemon discovery "
            "file was found. Start the daemon "
            "(`nx daemon t2 start --foreground` or install autostart) "
            "or unset NX_STORAGE_MODE."
        )
    uds = info.get("uds_path") or ""
    uds_path = Path(uds) if uds else None
    if uds_path is not None and uds_path.exists():
        return T2Client(uds_path=uds_path)
    return T2Client(tcp_addr=(info["tcp_host"], info["tcp_port"]))


def _render_active_claims() -> RenderedPanel:
    from nexus.cockpit.panels.active_claims import (
        ActiveClaimsResult,
        ClaimRow,
        fetch_active_claims,
    )

    if _is_daemon_mode():
        client = _open_daemon_client()
        try:
            rows_data = client.tuplespace.list_active_claims()
        finally:
            client.close()
        result = ActiveClaimsResult(
            rows=[
                ClaimRow(
                    subspace=r["subspace"],
                    tuple_id=r["tuple_id"],
                    claim_id=r.get("claim_id") or "",
                    claimant=r.get("claimant") or "",
                    ttl_remaining_seconds=r.get("ttl_remaining_seconds"),
                )
                for r in rows_data
            ]
        )
    else:
        db_path = _cockpit_tuples_db()
        if not db_path.exists():
            raise click.ClickException(
                f"tuples.db not found at {db_path}; nothing to render."
            )
        # storage-boundary-allow: cockpit CLI read-only inspection of tuples.db.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            result = fetch_active_claims(conn=conn)
        finally:
            conn.close()

    if not result.rows:
        return RenderedPanel(title="Active Claims", lines=[])

    lines: list[str] = []
    groups = result.groups_by_subspace()
    for subspace in sorted(groups):
        lines.append(f"[{subspace}]")
        for row in groups[subspace]:
            ttl = (
                f"{row.ttl_remaining_seconds:.0f}s"
                if row.ttl_remaining_seconds is not None
                else "no-ttl"
            )
            lines.append(
                f"  {row.tuple_id[:12]}  claimant={row.claimant}  ttl={ttl}"
            )
    return RenderedPanel(title="Active Claims", lines=lines)


def _render_recent_events(*, limit: int) -> RenderedPanel:
    from nexus.cockpit.panels.recent_events import (
        EventRow,
        RecentEventsResult,
        fetch_recent_events,
    )

    if _is_daemon_mode():
        client = _open_daemon_client()
        try:
            rows_data = client.tuplespace.recent_events(limit=limit)
        finally:
            client.close()
        result = RecentEventsResult(
            rows=[
                EventRow(
                    cursor=int(r["cursor"]),
                    subspace=r["subspace"],
                    op=r["op"],
                    tuple_id=r["tuple_id"],
                    ts=float(r["ts"]),
                    payload_summary=r.get("payload_summary"),
                    category=r.get("category"),
                )
                for r in rows_data
            ]
        )
    else:
        db_path = _cockpit_tuples_db()
        if not db_path.exists():
            raise click.ClickException(
                f"tuples.db not found at {db_path}; nothing to render."
            )
        # storage-boundary-allow: cockpit CLI read-only inspection of tuples.db.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            result = fetch_recent_events(conn=conn, limit=limit)
        finally:
            conn.close()

    if not result.rows:
        return RenderedPanel(title="Recent Events", lines=[])

    lines = []
    now = time.time()
    for row in result.rows:
        rel = _fmt_relative(now - row.ts) if row.ts else "?"
        lines.append(
            f"{rel:>10}  {row.op:<8}  {row.subspace:<40}  {row.tuple_id[:12]}"
        )
    return RenderedPanel(title="Recent Events", lines=lines)


def _render_active_bindings() -> RenderedPanel:
    from nexus.cockpit.panels.active_bindings import fetch_active_bindings

    profiles_dir = _cockpit_profiles_dir()
    result = fetch_active_bindings(profiles_dir=profiles_dir)

    if not result.rows and not result.errors:
        return RenderedPanel(title="Active Bindings", lines=[])

    lines: list[str] = []
    if result.errors:
        for err in result.errors:
            lines.append(f"!! profile error: {err}")
    by_profile: dict[str, list] = {}
    for r in result.rows:
        by_profile.setdefault(r.profile, []).append(r)
    for profile in sorted(by_profile):
        lines.append(f"[{profile}]")
        for r in by_profile[profile]:
            lines.append(
                f"  {r.binding_name:<28}  match: {r.match_summary}"
            )
            lines.append(
                f"  {'':<28}  action: {r.action_ref}"
            )
    return RenderedPanel(title="Active Bindings", lines=lines)


def _panel_to_json(panel: str, rendered: RenderedPanel) -> dict[str, object]:
    return {
        "panel": panel,
        "title": rendered.title,
        "lines": rendered.lines,
    }


# ---------------------------------------------------------------------------
# Path + env helpers
# ---------------------------------------------------------------------------


def _default_tuples_db() -> Path:
    return Path(os.path.expanduser("~/.config/nexus/tuples.db"))


def _cockpit_tuples_db() -> Path:
    """Path to the tuples.db the cockpit panels read.

    Overridable via ``NX_COCKPIT_TUPLES_DB`` for tests / multi-DB setups.
    Falls through to ``~/.config/nexus/tuples.db``.
    """
    override = os.environ.get("NX_COCKPIT_TUPLES_DB")
    if override:
        return Path(override)
    return _default_tuples_db()


def _cockpit_profiles_dir() -> Path:
    """Path to the bindings-profiles dir the active-bindings panel reads.

    Overridable via ``NX_COCKPIT_PROFILES_DIR``. Falls through to the
    canonical builtin location from :func:`bindings.default_profiles_dir`.
    """
    override = os.environ.get("NX_COCKPIT_PROFILES_DIR")
    if override:
        return Path(override)
    from nexus.cockpit.bindings import default_profiles_dir

    return default_profiles_dir()


def _terminal_width(default: int = 100) -> int:
    try:
        cols = shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default
    return max(40, cols)


def _parse_window(s: str) -> float:
    s = s.strip().lower()
    if not s:
        raise click.BadParameter("empty window")
    unit = s[-1]
    try:
        value = float(s[:-1])
    except ValueError:
        raise click.BadParameter(f"invalid window: {s!r}") from None
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        raise click.BadParameter(f"unknown window unit {unit!r}; use s/m/h/d")
    return value * multipliers[unit]


def _human_bytes(n: int) -> str:
    size: float = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}TiB"


def _fmt_relative(seconds_ago: float) -> str:
    if seconds_ago < 60:
        return f"{int(seconds_ago)}s ago"
    if seconds_ago < 3600:
        return f"{int(seconds_ago / 60)}m ago"
    if seconds_ago < 86400:
        return f"{seconds_ago / 3600:.1f}h ago"
    return f"{seconds_ago / 86400:.1f}d ago"
