# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixture for the ``scripts/check_review.py`` tests (RDR-258 Phase 1 steps 5 and 7, nexus-2jomg.2).

A real git repository with dated commits and a directory of downloaded ``class-a-items`` artifacts, shaped like what the
review reads: a check module born 70 days before ``UNTIL``, demoted (``pytest.mark.class_a`` added) some days later, and one
develop push per day after that, each with an artifact. The default history is the realistic one: a run every day, two
failing items until a fix lands 30 days before ``UNTIL`` and one after it.
"""
from __future__ import annotations

import calendar
import importlib.util
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNTIL = date(2026, 11, 28)
STEM = "test_foo_check"
MODULE = f"tests/{STEM}.py"
GUARDS = "nexus-1; src/foo/"
ITEMS_BEFORE = ["pkg/a.py:10", "pkg/b.py:20"]
ITEMS_AFTER = ["pkg/a.py:10"]


def load(name: str = "check_review"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "check_review.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str, when: date | None = None) -> str:
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    if when is not None:
        stamp = f"{when.isoformat()}T12:00:00+0000"
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=False)
    assert done.returncode == 0, f"git {args}: {done.stderr}"
    return done.stdout.strip()


def commit(repo: Path, files: dict[str, str | None], days_ago: int, subject: str = "c") -> str:
    """Commit ``files`` (None deletes) dated ``days_ago`` before ``UNTIL``."""
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            git(repo, "rm", "-q", rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        git(repo, "add", rel)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", subject, when=UNTIL - timedelta(days=days_ago))
    return git(repo, "rev-parse", "HEAD")


def commit_many(repo: Path, specs: list[tuple[str, dict[str, str | None], int, str]]) -> dict[str, str]:
    """Many commits in one ``git fast-import`` (a push a day for 70 days is 70 commits, and one process each is minutes).
    ``specs`` is ``(key, files, days_ago, subject)`` in history order; returns ``{key: sha}`` and resets the tree to the result."""
    parts: list[str] = []
    for n, (_key, files, days_ago, subject) in enumerate(specs, 1):
        stamp = calendar.timegm((UNTIL - timedelta(days=days_ago)).timetuple()) + 12 * 3600
        parts.append(f"commit refs/heads/develop\nmark :{n}\ncommitter t <t@t> {stamp} +0000\ndata {len(subject.encode())}\n{subject}\n")
        for rel, text in files.items():
            parts.append(f"D {rel}\n" if text is None else f"M 100644 inline {rel}\ndata {len(text.encode())}\n{text}\n")
    marks = repo / ".git" / "marks.tmp"
    done = subprocess.run(["git", "-C", str(repo), "fast-import", "--quiet", f"--export-marks={marks}"], input="".join(parts).encode(),
                          capture_output=True, check=False, env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})
    assert done.returncode == 0, done.stderr.decode()
    by_mark = dict(line.split()[:2] for line in marks.read_text().splitlines())
    git(repo, "reset", "-q", "--hard", "develop")
    return {key: by_mark[f":{n}"] for n, (key, *_rest) in enumerate(specs, 1)}


def module_text(guards: str | None = GUARDS, marked: bool = True, owner: str = "ci", klass: str = "A",
                tests: tuple[tuple[str, bool], ...] | None = None) -> str:
    """A check module. ``tests`` is ``((name, class_a?), ...)``; default is one test and a module-level class_a mark."""
    head = [f"# owner: {owner}"] + ([f"# guards: {guards}"] if guards is not None else []) + [f"# class: {klass}"]
    if tests is None:
        mark = "pytestmark = [pytest.mark.lint, pytest.mark.class_a]" if marked else "pytestmark = pytest.mark.lint"
        return "\n".join([*head, "import pytest", mark, "", "def test_x():", "    assert True", ""])
    lines = [*head, "import pytest", "pytestmark = pytest.mark.lint", ""]
    for name, is_a in tests:
        lines += (["@pytest.mark.class_a"] if is_a else []) + [f"def {name}():", "    assert True", ""]
    return "\n".join(lines)


def artifact(art: Path, sha: str, items: list[str], *, examined: int | None = 5, name: str = "class-a-items", module: str = MODULE) -> None:
    """One develop push's items file. ``examined`` None leaves the check out of ``required`` (the check did not run)."""
    rows = [{"check": Path(module).stem, "test": f"{module}::test_x", "kind": "failure", "id": i, "detail": ""} for i in items]
    required = [] if examined is None else [{"prefix": module.removesuffix(".py"), "min": 1, "examined": examined}]
    payload = {"name": name, "pytest_exit": 1 if items else 0, "passed": 5, "failed": 1 if items else 0, "skipped": 0, "errored": 0,
               "required": required, "items": rows}
    (art / sha).mkdir(parents=True, exist_ok=True)
    (art / sha / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def history(tmp_path: Path, *, demoted_days_ago: int = 61, guards: str | None = GUARDS, fix_files: dict[str, str] | None = None,
            fix_day: int = 30, skip: frozenset[int] = frozenset(), step: int = 1, items_before: list[str] | None = None,
            items_after: list[str] | None = None, klass: str = "A", extra: dict[str, str] | None = None,
            tests: tuple[tuple[str, bool], ...] | None = None, tail: str = "",
            between: dict[str, str | None] | None = None, registry: str | None = "") -> tuple[Path, Path, dict[str, str]]:
    """Check module born 70 days ago, demoted ``demoted_days_ago`` days ago, then a develop push every ``step`` days (minus
    ``skip``) with an artifact. At ``fix_day`` the push also changes ``fix_files`` (default: only docs/other.md) and the failing
    items drop from ``items_before`` to ``items_after``."""
    repo, art = tmp_path / "repo", tmp_path / "artifacts"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "develop")
    before, after = items_before or ITEMS_BEFORE, items_after or ITEMS_AFTER
    specs: list[tuple[str, dict[str, str | None], int, str]] = [
        ("born", {MODULE: module_text(guards, marked=False, klass=klass, tests=tuple((n, False) for n, _ in tests) if tests else None) + tail,
                  "src/foo/x.py": "x = 1\n", "conf/allowlist.txt": "a\n",
                  **({} if registry is None else {"tests/quarantine.toml": registry}), **(extra or {})}, 70, "born"),
        *([("between", between, 65, "between the birth and the demotion")] if between else []),
        ("demote", {MODULE: module_text(guards, klass=klass, tests=tests) + tail}, demoted_days_ago, "demote")]
    listed = {"demote": before}
    for d in range(demoted_days_ago - step, -1, -step):
        if d in skip:
            continue
        files: dict[str, str | None] = {"docs/other.md": f"run {d}\n"}
        if d <= fix_day < d + step:
            files.update(fix_files or {})
        specs.append((f"d{d}", files, d, f"run {d}"))
        listed[f"d{d}"] = after if d <= fix_day else before
    sha = commit_many(repo, specs)
    for key, items in listed.items():
        artifact(art, sha[key], items)
    return repo, art, sha


def record(cr, repo: Path, art: Path, since: date | None = None, priors=(), rev: str = "HEAD", demoted_days_ago: int = 61) -> dict:
    return cr.build_record(repo, rev, since or UNTIL - timedelta(days=demoted_days_ago + 9), UNTIL, art, priors=priors)


def check(rec: dict, stem: str = STEM) -> dict:
    (one,) = [c for c in rec["checks"] if c["check"] == stem]
    return one
