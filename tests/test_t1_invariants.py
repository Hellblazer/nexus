# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression scaffolding for the T1 lifecycle data-loss class.

Background — the fix-loop pattern: ``#567 → #569 → #572 → #573 →
#574 → #575/#576``. Each fix patched a witnessed instance of a
class-of-bugs; the class re-instantiated at the next code path that
wasn't audited. The unified fix in PR for ``nexus-gxq5`` closes the
class — these tests encode the invariants the class violated, so any
future patch that re-introduces the same shape fails CI before merge.

The deep-analyst's brief (T1 scratch ``f9125457``, ``fef6153a``) is
the source of truth for the invariant set.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"


# ── Invariant: no silent EphemeralClient fallback ──────────────────────────


# The two production paths where EphemeralClient is allowed:
#
#   1. ``T1Database.__init__`` skip_t1 branch in ``src/nexus/db/t1.py``
#      — explicit opt-in via ``NEXUS_SKIP_T1=1``; operator subprocesses
#      from ``claude_dispatch`` use this.
#   2. ``cloud-mode local_t3`` in ``src/nexus/commands/index.py`` —
#      explicit opt-in for the wheel's local-only fallback when no
#      Voyage credentials are configured. Different concern from T1.
#
# Anything else is a regression.
_EPHEMERAL_ALLOWLIST = {
    ("src/nexus/db/t1.py", "skip_t1 branch"),
    ("src/nexus/commands/index.py", "cloud-mode local_t3"),
}


def test_no_silent_ephemeral_client_outside_allowlist() -> None:
    """Invariant: ``chromadb.EphemeralClient(`` constructions are
    confined to the explicit opt-in paths.

    PR #569 fixed site 1 (constructor's no-record-no-skip-t1 branch).
    Issue #576 surfaced site 2 (``_reconnect`` fallback) and Phase A
    fixed it. This test prevents site 3 from appearing silently.

    If you NEED to add a new EphemeralClient construction, update the
    ``_EPHEMERAL_ALLOWLIST`` and document the explicit opt-in
    contract in the same commit.
    """
    pat = re.compile(r"chromadb\.EphemeralClient\s*\(")
    findings: list[tuple[str, int, str]] = []
    for f in sorted(SRC_ROOT.rglob("*.py")):
        rel = f.relative_to(REPO_ROOT)
        try:
            text = f.read_text()
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line) and "#" not in line.split("EphemeralClient")[0]:
                findings.append((str(rel), i, line.strip()))

    # Filter out doc/comment-only lines (the regex already requires
    # "(" so docstring examples that say "EphemeralClient" without
    # the open-paren are excluded; but allow the comment-marker check
    # for paranoia).
    real = [
        (rel, lineno, src) for (rel, lineno, src) in findings
        if not src.lstrip().startswith("#")
    ]

    expected_files = {p for (p, _) in _EPHEMERAL_ALLOWLIST}
    found_files = {rel for (rel, _, _) in real}
    unexpected = found_files - expected_files
    assert not unexpected, (
        f"chromadb.EphemeralClient() construction in non-allow-listed "
        f"file(s): {sorted(unexpected)}.\n"
        f"All findings: {real}\n"
        f"Expected only: {sorted(expected_files)}.\n"
        f"If this is intentional, update _EPHEMERAL_ALLOWLIST in "
        f"tests/test_t1_invariants.py and document the explicit "
        f"opt-in contract in the same commit."
    )


# ── Invariant: _reconnect uses same resolution chain as constructor ────────


def test_reconnect_uses_same_resolution_chain_as_constructor() -> None:
    """Invariant I7: when T1Database loses connectivity and reconnects,
    the reconnected instance's behaviour must be identical to a fresh
    ``T1Database()`` — same fail-loud contract, same lookup semantics.

    Pre-fix the constructor used UUID-keyed
    ``_resolve_session_record_with_retry`` while ``_reconnect`` used
    legacy ``find_ancestor_session`` (PID-keyed PPID walker). The
    asymmetry meant reconnect ALWAYS missed UUID-keyed records — the
    direct mechanism behind the silent EphemeralClient fallback in
    GH #576.

    AST-style audit: both paths must mention
    ``_resolve_session_record_with_retry`` in their bodies.
    """
    text = (SRC_ROOT / "db" / "t1.py").read_text()
    # Find the bodies of __init__ (constructor) and _reconnect.
    # Simple grep approach: the function names must appear at module
    # level near the resolver.
    ctor_body = _extract_function_body(text, "def __init__")
    recon_body = _extract_function_body(text, "def _reconnect")
    assert "_resolve_session_record_with_retry" in ctor_body, (
        "constructor expected to use _resolve_session_record_with_retry"
    )
    assert "_resolve_session_record_with_retry" in recon_body, (
        "_reconnect MUST use the same UUID-keyed resolver as the "
        "constructor (GH #576 invariant I7). Pre-fix it called "
        "find_ancestor_session (PID-keyed legacy walker) which "
        "could not find UUID-keyed records."
    )


def _extract_function_body(text: str, def_marker: str) -> str:
    """Crude function-body extractor: returns the source from the
    ``def_marker`` line through the next dedent at module indent (4
    spaces). Sufficient for the symbol-presence assertions above.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_func = False
    base_indent = -1
    for line in lines:
        if not in_func:
            if def_marker in line:
                in_func = True
                base_indent = len(line) - len(line.lstrip())
                out.append(line)
            continue
        if line.strip() == "":
            out.append(line)
            continue
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent <= base_indent and line.strip():
            break
        out.append(line)
    return "\n".join(out)


# ── Invariant: reconcile rewrites JSON content, not just filename ──────────


def test_reconcile_rewrites_json_content_not_just_filename() -> None:
    """Invariant: ``reconcile_owned_chroma`` must keep the JSON
    content's ``session_id`` field aligned with the filename. Phase B.

    Pre-fix ``Path.replace`` only renamed the file; the JSON content
    kept the pre-reconcile UUID. ``sweep_stale_sessions`` (pre-Phase
    C) compared JSON content against ``current_session`` and reaped
    every healthy parent record on subprocess SessionStart. Even
    after Phase C makes sweep filename-stem-based, JSON content
    consistency is still load-bearing for any future code that reads
    ``record["session_id"]`` (e.g. console watchers, telemetry).
    """
    text = (SRC_ROOT / "mcp" / "core.py").read_text()
    func = _extract_function_body(text, "def reconcile_owned_chroma")
    # The function body must contain BOTH the filename rename AND a
    # JSON content rewrite. The rewrite uses ``json.dumps`` + atomic
    # write via ``os.O_TRUNC``.
    assert "old_path.replace" in func or ".replace(new_path)" in func, (
        "reconcile_owned_chroma must perform the filename rename"
    )
    assert "json.dumps" in func, (
        "reconcile_owned_chroma must rewrite JSON content (Phase B). "
        "Pre-fix the filename rename left JSON's session_id field "
        "stale; sweep then reaped the canonical record on next fire."
    )


# ── Invariant: subprocess SessionStart never sweeps parent's records ───────


def test_full_576_chain_no_data_loss_under_subprocess_sessionstart(
    tmp_path, monkeypatch,
) -> None:
    """End-to-end #576 reproduction (no Claude Code, no real chroma).

    Walks the full data-loss chain from issue #576 and asserts that
    after Phases A through F land, the chain is broken at multiple
    points:

      1. Lifespan writes ``sessions/<lifespan>.session`` (JSON + name
         both at lifespan UUID).
      2. SessionStart hook writes ``current_session=<canonical>``.
      3. ``reconcile_owned_chroma`` renames the file AND rewrites
         JSON content (Phase B).
      4. Plan-runner spawns ``claude -p`` with NX_SESSION_ID=<canonical>.
         That subprocess fires its OWN nexus SessionStart hook
         (``nexus.hooks.session_start``).
      5. Phase F: subprocess SessionStart skips ``sweep_stale_sessions``
         entirely. Even if it did sweep, Phase C (filename-stem
         comparison) means the canonical record is not uuid_stale.
      6. After subprocess returns, the canonical record MUST still
         exist on disk — pre-fix it would have been unlinked, leading
         to T1 reconnect → silent EphemeralClient fallback (Phase A
         closes that as a final safety net).
    """
    import json
    import os

    from nexus.mcp import core as core_mod
    from nexus.session import write_session_record_by_id

    # ─── Phase 1+2: lifespan + SessionStart drift ───────────────────────
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    pointer = tmp_path / "current_session"
    monkeypatch.setattr("nexus.db.t1.SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("nexus.hooks.SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr("nexus.session.CLAUDE_SESSION_FILE", pointer)

    lifespan_uuid = "lifespan-uuid-aaaa-bbbb-cccc-aaaaaaaaaaaa"
    canonical_uuid = "canonical-uuid-bbbb-cccc-dddd-bbbbbbbbbbbb"

    record = write_session_record_by_id(
        sessions_dir, lifespan_uuid,
        host="127.0.0.1", port=12345, server_pid=os.getpid(),
        claude_root_pid=os.getpid(),
    )
    assert record.exists()

    pointer.write_text(canonical_uuid)

    # ─── Phase 3: reconcile renames file + rewrites JSON ────────────────
    core_mod._OWNED_CHROMA.clear()
    core_mod._OWNED_CHROMA.update({
        "session_id": lifespan_uuid,
        "server_pid": os.getpid(),
        "session_file": str(record),
        "tmpdir": str(tmp_path / "tmp"),
    })
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    assert core_mod.reconcile_owned_chroma() is True

    canonical_record = sessions_dir / f"{canonical_uuid}.session"
    assert canonical_record.exists(), "Phase 3: file must be at canonical name"
    assert not record.exists(), "Phase 3: lifespan-name file must be gone"
    body = json.loads(canonical_record.read_text())
    assert body["session_id"] == canonical_uuid, (
        "Phase B: JSON content must be rewritten to canonical UUID"
    )
    assert body["server_pid"] == os.getpid(), (
        "Phase B: other JSON fields must survive rewrite"
    )
    core_mod._OWNED_CHROMA.clear()

    # ─── Phases 4+5: plan-runner subprocess SessionStart ────────────────
    # Simulate the subprocess by setting NX_SESSION_ID + calling
    # nexus.hooks.session_start directly. ``stop_t1_server`` is patched
    # because if Phase C/F regressed and sweep fired, the test would
    # otherwise try to send SIGTERM to the test's own pid.
    monkeypatch.setenv("NX_SESSION_ID", canonical_uuid)
    monkeypatch.setattr("nexus.session.stop_t1_server", lambda _pid: None)
    monkeypatch.setattr("nexus.session._is_pid_alive", lambda _pid: True)

    from nexus.hooks import session_start
    session_start()

    # ─── Phase 6: canonical record SURVIVES the subprocess SessionStart ─
    assert canonical_record.exists(), (
        "GH #576: canonical session record must survive subprocess "
        "SessionStart fire. Phase F (skip sweep when NX_SESSION_ID set) "
        "is the primary defense; Phase C (filename-stem comparison) is "
        "the secondary. Pre-fix this assertion fails because the "
        "subprocess sweep ran uuid_stale=True (JSON content stale, "
        "filename was canonical) and unlinked the parent's record."
    )
    body_after = json.loads(canonical_record.read_text())
    assert body_after["session_id"] == canonical_uuid


def test_session_start_subprocess_skip_sweep_present() -> None:
    """Invariant: when ``NX_SESSION_ID`` is set in the environment,
    ``session_start`` must NOT call ``sweep_stale_sessions`` against
    the parent's SESSIONS_DIR. Phase F.

    AST audit: the function body must have a guard branching on
    ``NX_SESSION_ID`` BEFORE the sweep_stale_sessions call.
    """
    text = (SRC_ROOT / "hooks.py").read_text()
    func = _extract_function_body(text, "def session_start")
    # Find the position of the inherited-session check and the sweep
    # call. The check must come first.
    sweep_idx = func.find("sweep_stale_sessions(SESSIONS_DIR)")
    inherited_idx = func.find("NX_SESSION_ID")
    assert sweep_idx > 0, "session_start must call sweep_stale_sessions"
    assert inherited_idx > 0, (
        "session_start must check NX_SESSION_ID before sweep (Phase F)"
    )
    assert inherited_idx < sweep_idx, (
        "Phase F: NX_SESSION_ID guard must be evaluated BEFORE "
        "sweep_stale_sessions. Subprocess SessionStart firing in a "
        "plan-runner ``claude -p`` must not touch the parent's "
        "SESSIONS_DIR (GH #576)."
    )
