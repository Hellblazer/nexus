# SPDX-License-Identifier: AGPL-3.0-or-later
"""Git subprocess wrappers for the catalog (nexus-mbm extraction 1/5).

The catalog is a Dolt-style git repo: ``owners.jsonl``,
``documents.jsonl``, ``links.jsonl``, ``events.jsonl`` are tracked;
the SQLite projection (``.catalog.db``) is regenerated on demand and
.gitignored. Git operations live here so the main ``Catalog`` class
stays a thin orchestrator.

Public surface:

- :func:`run_git` — minimal ``subprocess.run`` wrapper with a 30s
  timeout, captures stdout/stderr, raises ``RuntimeError`` on
  non-zero return when ``check=True``.
- :func:`ensure_git_identity` — sets a benign local ``user.name`` /
  ``user.email`` if neither global nor local is configured. Lets
  ``Catalog.init`` succeed on CI runners and fresh dev machines.
- :func:`clone_catalog` — ``git clone <remote> <catalog_path>`` with
  the catalog's standard timeout/error envelope.
- :func:`init_repo` — ``git init`` + create empty JSONL files +
  ``.gitignore`` + initial commit if no commits exist yet.
- :func:`add_remote_origin_if_missing` — idempotent
  ``git remote add origin <remote>``; no-op if already set.
- :func:`commit_and_push` — ``git add -A && git commit -m <message>``;
  pushes to origin if a remote is configured. Returns ``True`` if a
  commit was created (i.e. there were staged changes).
- :func:`pull_origin_if_remote` — ``git pull`` if a remote is
  configured; no-op otherwise.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import structlog

_log = structlog.get_logger(__name__)


_GIT_TIMEOUT_SECONDS: int = 30


def run_git(
    args: list[str], cwd: Path, check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in *cwd* with a fixed timeout.

    Captures stdout/stderr as text. When ``check=True`` a non-zero
    return code raises ``RuntimeError`` with the trimmed stderr.
    """
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git command failed: {result.stderr.strip()}")
    return result


def ensure_git_identity(cwd: Path) -> None:
    """Set local git identity if none is configured.

    ``Catalog.init`` runs ``git commit``, which fails with "Author
    identity unknown" when neither global nor local user.name /
    user.email is set. Real users with global git config see their
    own identity; environments without one (CI runners, fresh
    machines) get a benign ``Nexus Catalog <nexus@local>`` fallback
    so the initial commit succeeds.
    """
    name = run_git(["git", "config", "user.name"], cwd=cwd, check=False)
    if name.returncode != 0 or not name.stdout.strip():
        run_git(["git", "config", "user.name", "Nexus Catalog"], cwd=cwd)
    email = run_git(["git", "config", "user.email"], cwd=cwd, check=False)
    if email.returncode != 0 or not email.stdout.strip():
        run_git(["git", "config", "user.email", "nexus@local"], cwd=cwd)


def clone_catalog(remote: str, catalog_path: Path) -> None:
    """Clone *remote* into *catalog_path*.

    Raises ``RuntimeError`` on failure; the caller decides whether
    to fall back to ``init_repo``. Used by ``Catalog.init`` for the
    "remote provided + no local repo yet" path (new machine).
    """
    result = subprocess.run(
        ["git", "clone", remote, str(catalog_path)],
        capture_output=True, text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to clone catalog from {remote}: "
            f"{result.stderr.strip()}"
        )
    _log.info("catalog_cloned_from_remote", remote=remote)


def init_repo(catalog_path: Path) -> None:
    """``git init`` + create empty JSONL files + ``.gitignore`` +
    initial commit if no commits yet.

    Idempotent: running on an already-initialised catalog is a no-op
    for steps that have already happened (``git init`` is
    short-circuited by an existing ``.git`` dir, JSONL files are
    only created if missing, the initial commit only fires when
    ``git rev-parse HEAD`` fails).
    """
    catalog_path.mkdir(parents=True, exist_ok=True)
    if not (catalog_path / ".git").exists():
        run_git(["git", "init"], cwd=catalog_path)
    ensure_git_identity(catalog_path)

    for name in ("documents.jsonl", "owners.jsonl", "links.jsonl"):
        p = catalog_path / name
        if not p.exists():
            p.touch()

    gitignore = catalog_path / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".catalog.db\n")

    head = run_git(["git", "rev-parse", "HEAD"], cwd=catalog_path, check=False)
    if head.returncode != 0:
        run_git(["git", "add", "-A"], cwd=catalog_path)
        run_git(["git", "commit", "-m", "Init catalog"], cwd=catalog_path)


def add_remote_origin_if_missing(catalog_path: Path, remote: str) -> None:
    """Add ``origin`` remote pointing at *remote* if not already set."""
    r = run_git(["git", "remote"], cwd=catalog_path, check=False)
    if "origin" not in r.stdout:
        run_git(
            ["git", "remote", "add", "origin", remote],
            cwd=catalog_path,
        )


def commit_and_push(catalog_dir: Path, message: str) -> bool:
    """``git add -A && git commit -m <message>`` plus a best-effort
    ``git push`` to origin if a remote is configured.

    Returns ``True`` if a commit was created (working tree had
    staged changes), ``False`` if the working tree was clean.

    Caller is responsible for holding the catalog directory flock
    around this call. Push failures are non-fatal — they are logged
    by git's own stderr but the function does not raise.
    """
    run_git(["git", "add", "-A"], cwd=catalog_dir)
    status = run_git(["git", "status", "--porcelain"], cwd=catalog_dir)
    if not status.stdout.strip():
        return False
    run_git(["git", "commit", "-m", message], cwd=catalog_dir)
    remote = run_git(["git", "remote"], cwd=catalog_dir, check=False)
    if remote.stdout.strip():
        run_git(
            ["git", "push", "-u", "origin", "HEAD"],
            cwd=catalog_dir, check=False,
        )
    return True


def pull_origin_if_remote(catalog_dir: Path) -> bool:
    """``git pull`` if a remote is configured. Returns ``True``
    when a pull was attempted (regardless of success), ``False``
    when no remote is set."""
    remote = run_git(["git", "remote"], cwd=catalog_dir, check=False)
    if not remote.stdout.strip():
        return False
    run_git(["git", "pull"], cwd=catalog_dir, check=False)
    return True
