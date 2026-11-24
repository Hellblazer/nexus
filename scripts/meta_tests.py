#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run the census and lint meta-tests a diff can turn red, before the push.

A meta-test checks the repo's shape rather than a behaviour: it scans files
(every module under src/nexus, every test, every doc) and fails on a new one
that breaks a rule. 42% of develop's failed CI runs from 10-19 to 11-17 failed
only on these (T2 nexus/observability-lookback-2026-11-17, item 1), mostly
because the whole lint bucket is a lab run that a push skipped. This script
picks the few a diff reaches and runs them in seconds.

The meta-tests (``known``):

* every test file under tests/ that carries ``pytest.mark.lint`` or
  ``pytest.mark.census`` (a census that stays in the default loop);
* the session-items censuses conftest names in
  ``_SESSION_ITEMS_CENSUS_TEST_NODEIDS``: they judge the tests collected in
  their own invocation, so they run together with the changed test files;
* the engine's Java tests that walk source trees (``Files.walk``, ``list``,
  ``find`` or ``newDirectoryStream``), except ``*IntegrationTest``.

Each Python meta-test's surface is derived, never declared: ``build`` runs
each test file in its own pytest process under ``tests/_surface_trace.py``
and writes what it read to ``tests/meta_surfaces.json.gz``, with a hash of the
file, the tests/ modules it imports and the conftests above it. A changed path
selects a test when the test opened or statted it, listed its directory and the
path was added or deleted (for a new directory, its first ancestor that exists
at the base), ran a subprocess
that enumerates a subtree holding it, or imports it directly (a ``src/nexus``
or ``tests`` module). A test runs whatever the diff when the manifest does not
hold it, that hash changed since the build, its traced run failed or skipped,
the trace recorded no read, or it ran a subprocess whose reads the trace
cannot see and declared no ``META_SCANS`` (the roots those subprocesses read).
Every such test is listed by reason, so a new meta-test cannot fall out unseen. A Java meta-test's surface is ``service/src/``.

Usage::

    scripts/meta_tests.py select --base origin/develop      # what would run, and why
    scripts/meta_tests.py run --base origin/develop         # run the Python ones
    scripts/meta_tests.py build                             # rewrite the manifest (a lab run)
"""
from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

#: Gzipped: a binary file, so the repo-wide text censuses it records (route names, retired
#: library names, paths) do not read their own findings back out of it, and a tenth the size.
MANIFEST = "tests/meta_surfaces.json.gz"
MARKS = ("lint", "census")
CONFTEST = "tests/conftest.py"
SESSION_ITEMS_NAME = "_SESSION_ITEMS_CENSUS_TEST_NODEIDS"
JAVA_TEST_ROOT = "service/src/test/java"
JAVA_WALK = re.compile(r"Files\.(walk|list|find|newDirectoryStream)\(")
JAVA_SURFACE = "service/src/"
#: The two invocations, as the shell lines they are (files follow). The meta-tests in a selected
#: file only: a file may hold behaviour tests beside its census.
META_RUN = "uv run pytest -q -rfEs -p no:randomly --color=no -o addopts= -m '(lint or census) and not integration and not slow'"
#: -rp lists each pass, so a guard counts only when its PASSED line appears (a skip inside
#: tests/conftest.py is reported at the conftest, not at the guard file).
#: The session-items guard judges what its own invocation collected, so the changed test files are
#: collected with it unfiltered (a -m that deselects some reads as a partial view, and the guard skips).
GUARD_RUN = "uv run pytest -q -rfEsp -p no:randomly --color=no -o addopts= -m 'not integration and not slow'"
#: Meta-tests read files; none needs the engine, so no gate jar is built or checked for them, and
#: no suite lease is taken (tests/conftest.py takes one only for a substrate that boots the engine).
ENV = {"NX_TEST_T2_SUBSTRATE": "none"}
#: The guard run, as CI's census-only job runs it (.github/workflows/ci.yml): tests/conftest.py
#: skip-marks every item outside the census module, so the changed test files are collected (the
#: census judges them) but none executes, and any non-zero exit is a real red.
GUARD_ENV = {"NX_CENSUS_ONLY_JOB": "1", "NX_SCENARIO_SKIP_BUDGET": "999"}
#: What a green run of this script does not cover, printed at the end of every run.
NOT_COVERED = (
    "not covered here: the rest of the -m lint bucket and the full-corpus census job (develop CI runs them; push-train"
    " rule 1 no longer asks for them by hand, Sam 2026-11-23), census-style tests that carry neither mark, and the"
    " Java source walkers (printed above when selected)"
)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def git_names(repo: Path, *args: str) -> set[str]:
    """Paths from a git listing run with -z: a name may hold spaces, and -z leaves it unquoted."""
    return {n for n in git(repo, *args, "-z").split("\0") if n}


#: Per-test lists of paths, inverted on disk: path -> id of the set of tests that read it.
_INVERTED = ("opened", "statted", "listed")


def load_manifest(path: Path) -> dict:
    """The manifest as {"tests": {test: {"sha256", "opened", "statted", "listed", "prefixes", ...}}}."""
    disk = json.loads(gzip.decompress(path.read_bytes()))
    names = disk["names"]
    tests = {n: {**disk["tests"][n], **{k: [] for k in _INVERTED}} for n in names}
    for kind in _INVERTED:
        for p, sid in disk[kind].items():
            for i in disk["sets"][sid]:
                tests[names[i]][kind].append(p)
    for entry in tests.values():
        for kind in _INVERTED:
            entry[kind].sort()
    return {**{k: v for k, v in disk.items() if k not in ("names", "sets", "tests", *_INVERTED)}, "tests": tests}


def write_manifest(path: Path, manifest: dict) -> None:
    """Deterministic bytes (no name or mtime in the gzip header). Most paths are read by one of a
    few hundred groups of tests, so each path names its group's id instead of the tests."""
    tests = manifest["tests"]
    names = sorted(tests)
    index = {n: i for i, n in enumerate(names)}
    sets: dict[tuple[int, ...], int] = {}
    disk: dict = {k: v for k, v in manifest.items() if k != "tests"}
    disk["names"] = names
    disk["tests"] = {n: {k: v for k, v in e.items() if k not in _INVERTED} for n, e in tests.items()}
    for kind in _INVERTED:
        readers: dict[str, list[int]] = {}
        for n in names:
            for p in tests[n].get(kind, ()):
                readers.setdefault(p, []).append(index[n])
        disk[kind] = {p: sets.setdefault(tuple(v), len(sets)) for p, v in sorted(readers.items())}
    disk["sets"] = [list(s) for s, _ in sorted(sets.items(), key=lambda kv: kv[1])]
    raw = json.dumps(disk, separators=(",", ":"), sort_keys=True).encode()
    path.write_bytes(gzip.compress(raw, compresslevel=9, mtime=0))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── discovery ────────────────────────────────────────────────────────────────


def _marks_in(tree: ast.AST) -> set[str]:
    """The pytest marks among MARKS this module applies anywhere (pytest.mark.X)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in MARKS
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "mark"
        ):
            found.add(node.attr)
    return found


def session_items_files(repo: Path) -> set[str]:
    return {i.split("::", 1)[0] for i in session_items_nodeids(repo)}


def session_items_nodeids(repo: Path) -> set[str]:
    """The census node ids the guard registry names: the tests that judge session.items, not
    their siblings in the same file."""
    tree = ast.parse((repo / CONFTEST).read_text())
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == SESSION_ITEMS_NAME for t in targets):
            value = getattr(node, "value", None)
            if value is None:
                break
            ids = [c.value for c in ast.walk(value) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            return set(ids)
    raise SystemExit(f"meta_tests: {CONFTEST} no longer defines {SESSION_ITEMS_NAME}; update this script")


def python_meta_tests(repo: Path) -> dict[str, set[str]]:
    """Test file -> its kinds ('lint', 'census', 'session-items')."""
    out: dict[str, set[str]] = {}
    for path in sorted((repo / "tests").rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        if "__pycache__" in rel or not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if not any(f"mark.{m}" in text for m in MARKS):
            continue
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                marks = _marks_in(ast.parse(text, filename=rel))
        except (SyntaxError, UnicodeDecodeError):
            continue
        if marks:
            out[rel] = marks
    for rel in session_items_files(repo):
        out.setdefault(rel, set()).add("session-items")
    return out


def java_meta_tests(repo: Path) -> list[str]:
    out = []
    root = repo / JAVA_TEST_ROOT
    if not root.is_dir():
        return out
    for path in sorted(root.rglob("*Test.java")):
        if path.name.endswith("IntegrationTest.java"):
            continue
        if JAVA_WALK.search(path.read_text(errors="replace")):
            out.append(path.relative_to(repo).as_posix())
    return out


def declared_scans(repo: Path, test: str) -> list[str]:
    """``META_SCANS``, a module-level tuple of repo paths a test file may declare: the roots its
    subprocesses read, which the trace cannot see. Only a test that runs such a subprocess needs
    one; a declared root counts as a subtree the test enumerates."""
    try:
        tree = ast.parse((repo / test).read_text())
    except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
        return []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(x, ast.Name) and x.id == "META_SCANS" for x in targets) and node.value is not None:
                return sorted(c.value.rstrip("/") for c in ast.walk(node.value)
                              if isinstance(c, ast.Constant) and isinstance(c.value, str))
    return []


# ── imports (the half of a surface the trace leaves out) ────────────────────


def module_files(repo: Path, test: str) -> set[str]:
    """Repo files the test file imports directly: src/nexus, tests and bare scripts/ or tests/
    modules, with package __init__s."""
    try:
        tree = ast.parse((repo / test).read_text())
    except (SyntaxError, UnicodeDecodeError, FileNotFoundError):
        return set()
    names: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            # Relative: from the package directory, `level` dots up.
            pkg = (repo / test).parent
            for _ in range(node.level - 1):
                pkg = pkg.parent
            for a in node.names:
                parts = [*(node.module.split(".") if node.module else []), a.name]
                for i in range(1, len(parts) + 1):
                    stem = pkg.joinpath(*parts[:i])
                    for cand in (stem.with_suffix(".py"), stem / "__init__.py"):
                        if cand.is_file() and cand.resolve().is_relative_to(repo.resolve()):
                            relative.add(cand.relative_to(repo).as_posix())
    # A bare name may resolve through scripts/ (pyproject pythonpath), the test's own directory
    # or tests/.
    bare_roots = [repo / "scripts", (repo / test).parent, repo / "tests"]
    out: set[str] = set(relative)
    for name in names:
        top = name.split(".", 1)[0]
        if top == "nexus":
            bases = [repo / "src"]
        elif top == "tests":
            bases = [repo]
        else:
            bases = bare_roots
        parts = name.split(".")
        # Every root that holds the name: which one pytest picks depends on the import mode and
        # the directory, and taking all of them can only over-select.
        for base in bases:
            for i in range(1, len(parts) + 1):
                stem = base.joinpath(*parts[:i])
                for cand in (stem.with_suffix(".py"), stem / "__init__.py"):
                    if cand.is_file():
                        out.add(cand.relative_to(repo).as_posix())
    return out


def tests_closure(repo: Path, test: str) -> list[str]:
    """The test file, every tests/ module it imports transitively, and the conftest.py files from
    tests/ down to its directory: what can change its scan while the file itself stays put."""
    seen: set[str] = set()
    todo = [test]
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        todo.extend(m for m in module_files(repo, cur) if m.startswith("tests/") and m not in seen)
    parts = test.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        conf = "/".join([*parts[:i], "conftest.py"])
        if (repo / conf).is_file():
            seen.add(conf)
    return sorted(seen)


def surface_hash(repo: Path, test: str) -> str:
    h = hashlib.sha256()
    for rel in tests_closure(repo, test):
        h.update(rel.encode() + b"\0")
        h.update(sha256(repo / rel).encode() + b"\0")
    return h.hexdigest()


# ── selection ────────────────────────────────────────────────────────────────


@dataclass
class Selection:
    known: dict[str, set[str]]
    java_known: list[str]
    selected: dict[str, str] = field(default_factory=dict)  # test file -> reason
    always: dict[str, str] = field(default_factory=dict)  # test file -> why it runs whatever the diff
    java: dict[str, str] = field(default_factory=dict)
    changed: list[str] = field(default_factory=list)


def changed_paths(repo: Path, base: str) -> list[str]:
    names = git_names(repo, "diff", "--no-renames", "--name-only", f"{base}...HEAD")
    names |= git_names(repo, "diff", "--no-renames", "--name-only", "HEAD")
    names |= git_names(repo, "ls-files", "--others", "--exclude-standard")
    return sorted(names)


@dataclass
class BaseTree:
    """What existed at the base: a changed path absent here was added; one absent from the
    worktree now was deleted. Only those two change a directory's listing."""
    dirs: set[str]
    files: set[str]
    #: Paths that exist now; None reads the worktree. A replay of a past push sets it.
    now: set[str] | None = None

    def added_or_deleted(self, repo: Path, path: str) -> str | None:
        exists = (repo / path).exists() if self.now is None else path in self.now
        if path not in self.files:
            return "gaining" if exists else None
        return None if exists else "losing"

    def dir_exists_now(self, repo: Path, d: str) -> bool:
        if self.now is None:
            return (repo / d).is_dir() if d else True
        return d == "" or any(p.startswith(d + "/") for p in self.now)


def base_tree(repo: Path, base: str) -> BaseTree:
    try:
        at = git(repo, "merge-base", base, "HEAD").strip()
    except subprocess.CalledProcessError:
        at = base
    files = git_names(repo, "ls-tree", "-r", "--name-only", at)
    dirs = {""} | {d for f in files for d in _ancestors(f)}
    return BaseTree(dirs=dirs, files=files)


def _ancestors(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(len(parts), -1, -1)]


def _listed_hit(path: str, listed: set[str], existing: set[str]) -> bool:
    for anc in _ancestors(path):
        if anc in existing:
            return anc in listed
    return "" in listed


def _prefix_hit(path: str, prefixes: list[str]) -> bool:
    return any(p == "" or path == p or path.startswith(p + "/") for p in prefixes)


def select(repo: Path, changed: list[str], base: str, manifest: dict | None = None,
           tree: BaseTree | None = None) -> Selection:
    if manifest is None:
        mpath = repo / MANIFEST
        manifest = load_manifest(mpath) if mpath.is_file() else {}
    surfaces: dict = (manifest or {}).get("tests", {})
    known = python_meta_tests(repo)
    sel = Selection(known=known, java_known=java_meta_tests(repo), changed=changed)
    existing = tree or (base_tree(repo, base) if changed else BaseTree(dirs={""}, files=set()))
    changed_tests = [p for p in changed if p.startswith("tests/") and p.endswith(".py")]

    for test, kinds in known.items():
        if "session-items" in kinds:
            if changed_tests:
                sel.selected[test] = f"judges the tests collected with it; runs with the {len(changed_tests)} changed test file(s)"
            continue
        entry = surfaces.get(test)
        path = repo / test
        if entry is None:
            sel.always[test] = "not in the manifest (new since the build): surface unknown"
            continue
        if not path.is_file() or entry.get("sha256") != surface_hash(repo, test):
            sel.always[test] = ("it, a tests/ module it imports or a conftest above it changed since the manifest"
                                " was built: surface may have moved")
            continue
        if entry.get("incomplete"):
            sel.always[test] = "its traced run did not complete cleanly: " + entry["incomplete"]
            continue
        declared = declared_scans(repo, test)
        if entry.get("opaque") and not declared:
            sel.always[test] = "runs a subprocess whose reads the trace cannot see: " + entry["opaque"][0]
            continue
        if declared:
            entry = {**entry, "prefixes": [*entry.get("prefixes", ()), *declared]}
        if not any(entry.get(k) for k in ("opened", "statted", "listed", "prefixes")):
            sel.always[test] = "the trace recorded no read (a cached or shared scan?): surface unknown"
            continue
        # Imports of the test file and of the tests/ helpers and conftests it pulls in.
        imports = set().union(*(module_files(repo, m) for m in tests_closure(repo, test)))
        hit = _hit(test, entry, changed, existing, imports, repo)
        if hit:
            sel.selected[test] = hit
    if any(p.startswith(JAVA_SURFACE) for p in changed):
        first = next(p for p in changed if p.startswith(JAVA_SURFACE))
        for test in sel.java_known:
            sel.java[test] = f"walks the engine source tree; {first} changed"
    return sel


def _hit(test: str, entry: dict, changed: list[str], existing: BaseTree, imports: set[str], repo: Path) -> str | None:
    opened = set(entry.get("opened", ())) | set(entry.get("statted", ()))
    listed = set(entry.get("listed", ()))
    prefixes = list(entry.get("prefixes", ()))
    for p in changed:
        if p == test:
            return "the test file itself changed"
        if p in opened:
            return f"reads {p}"
        if p in imports:
            return f"imports {p}"
        if _prefix_hit(p, prefixes):
            return f"enumerates a subtree holding {p}"
        # A listing changes only when an entry comes or goes; an edit to a file the test only
        # listed does not reach it (an edit to a file it read is caught above).
        change = existing.added_or_deleted(repo, p)
        if change == "gaining" and _listed_hit(p, listed, existing.dirs):
            return f"lists the directory gaining {p}"
        if change == "losing":
            # The parent loses the entry; a directory the deletion empties vanishes from its own parent.
            for anc in _ancestors(p):
                if anc in listed:
                    return f"lists the directory losing {p}"
                if existing.dir_exists_now(repo, anc):
                    break
    return None


def java_class(test: str) -> str:
    return Path(test).stem


def report(sel: Selection, out=None) -> None:
    out = out or sys.stdout
    kinds = [k for v in sel.known.values() for k in v]
    print(
        f"meta-tests known: {len(sel.known)} Python ({kinds.count('lint')} lint, {kinds.count('census')} census,"
        f" {kinds.count('session-items')} session-items), {len(sel.java_known)} Java; changed paths: {len(sel.changed)}",
        file=out,
    )
    print(f"selected by the diff: {len(sel.selected)}", file=out)
    for test, why in sorted(sel.selected.items()):
        print(f"  {test}: {why}", file=out)
    print(f"run whatever the diff: {len(sel.always)}", file=out)
    for test, why in sorted(sel.always.items()):
        print(f"  {test}: {why}", file=out)
    if sel.java:
        print(f"Java meta-tests selected: {len(sel.java)} (a heavy engine run; see the command below)", file=out)


def java_command(sel: Selection) -> str | None:
    if not sel.java:
        return None
    classes = ",".join(sorted(java_class(t) for t in sel.java))
    return (
        "scripts/mvnw-leased.sh --lab-run lab-m -q test -Dnexus.ddl.suite=main"
        f" -Dsurefire.failIfNoSpecifiedTests=false -Dtest={shlex.quote(classes)}"
    )


# ── commands ─────────────────────────────────────────────────────────────────


def cmd_select(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    if args.names_from_stdin:
        data = sys.stdin.read()
        changed = sorted({n.strip("\n") for n in (data.split("\0") if "\0" in data else data.splitlines()) if n.strip()})
    else:
        changed = changed_paths(repo, args.base)
    t0 = time.monotonic()
    sel = select(repo, changed, args.base)
    if args.json:
        json.dump(
            {"selected": sel.selected, "always": sel.always, "java": sel.java, "changed": sel.changed,
             "known": len(sel.known), "java_known": len(sel.java_known)},
            sys.stdout, indent=1, sort_keys=True,
        )
        print()
    else:
        report(sel)
        print(f"selection took {time.monotonic() - t0:.1f}s")
        cmd = java_command(sel)
        if cmd:
            print(cmd)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    t0 = time.monotonic()
    changed = changed_paths(repo, args.base)
    sel = select(repo, changed, args.base)
    t_select = time.monotonic() - t0
    report(sel)
    picked = sorted(set(sel.selected) | set(sel.always))
    guards = [f for f in picked if "session-items" in sel.known.get(f, ())]
    files = [f for f in picked if f not in guards and (repo / f).is_file()]
    changed_tests = sorted(p for p in changed if p.startswith("tests/") and Path(p).name.startswith("test_")
                           and p.endswith(".py") and (repo / p).is_file())
    print(f"selection took {t_select:.1f}s")
    rc = 0
    runs = []
    if files:
        runs.append(("meta-tests", [*shlex.split(META_RUN), *args.pytest_args, *files]))
    if guards:
        runs.append(("session-items guard with the changed test files",
                     [*shlex.split(GUARD_RUN), *guards, *changed_tests]))
    if not runs:
        print("no Python meta-test selected")
    for label, argv in runs:
        print("$ " + shlex.join(argv), flush=True)
        t1 = time.monotonic()
        is_guard = label.startswith("session-items")
        code, passed = _run_streaming(argv, repo, GUARD_ENV if is_guard else {})
        print(f"{label} ran in {time.monotonic() - t1:.1f}s (exit {code})")
        if not is_guard and code == 5:
            # Every meta-test in the picked files is integration- or slow-marked: nothing to judge here.
            print("no meta-test collected from the picked files (all deselected by -m)")
            code = 0
        census = {n for n in session_items_nodeids(repo) if n.split("::", 1)[0] in guards}
        if is_guard:
            # Only the census node's own PASSED line counts: a skip also exits 0, and its siblings
            # in the guard file pass whether or not it judged anything.
            if not census <= passed:
                print("the session-items guard did not pass (it failed, or skipped because its invocation looked"
                      " partial): run the full-corpus census job (push-train rule 1) for this push")
                code = 1
        rc = rc or code
    cmd = java_command(sel)
    if cmd:
        print("Java meta-tests to run (heavy, not run here; the engine class's full suite also covers them):")
        print(cmd)
    if any(p.startswith("tests/") and p.endswith(".py") and not Path(p).name.startswith("test_") for p in changed):
        print("a tests/ helper or conftest changed: the session-items guard judges only a full collection, so run"
              " the full-corpus census job (push-train rule 1)")
    print(NOT_COVERED)
    return rc


def _run_streaming(argv: list[str], repo: Path, extra_env: dict[str, str]) -> tuple[int, set[str]]:
    """Run pytest, echoing its output; also return the node ids its summary reports PASSED."""
    proc = subprocess.Popen(argv, cwd=repo, env={**os.environ, **ENV, **extra_env},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    passed: set[str] = set()
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        word, _, rest = line.partition(" ")
        if word == "PASSED" and rest.split():
            passed.add(rest.split()[0])
    return proc.wait(), passed


def _trace_one(repo: Path, test: str, out: Path, extra: list[str]) -> tuple[dict, str | None]:
    """Trace one test file in its own pytest process, so a cache or session fixture another file
    warmed cannot hide this file's reads. Returns its reads and, when the run was not clean, why."""
    out.mkdir(parents=True)
    # -p loads the plugin before pytest puts the rootdir on sys.path.
    path = os.pathsep.join(p for p in (str(repo), os.environ.get("PYTHONPATH", "")) if p)
    env = dict(os.environ, **ENV, NX_SURFACE_TRACE_OUT=str(out), PYTHONPATH=path, TMPDIR=str(out.parent))
    argv = [*shlex.split(META_RUN), "-p", "tests._surface_trace", *extra, test]
    proc = subprocess.run(argv, cwd=repo, env=env, capture_output=True, text=True, errors="replace")
    merged: dict[str, set[str]] = {}
    for frag in out.glob("trace-*.json"):
        for kind, items in json.loads(frag.read_text()).get(test, {}).items():
            merged.setdefault(kind, set()).update(items)
    tail = [ln for ln in proc.stdout.splitlines() if ln.strip()][-1:] or ["no output"]
    why = None
    if proc.returncode != 0:
        failed = [ln for ln in proc.stdout.splitlines() if ln.startswith(("FAILED", "ERROR"))][:1]
        why = f"pytest exit {proc.returncode}: {tail[0][:120]}" + (f"; {failed[0][:200]}" if failed else "")
    elif re.search(r"\b\d+ skipped\b", tail[0]):
        why = f"a skip, so part of it never ran: {tail[0][:160]}"
    return merged, why


def _outside(repo: Path) -> str:
    tmp = os.path.realpath(tempfile.gettempdir())
    root = os.path.realpath(repo)
    return "/tmp" if tmp == root or tmp.startswith(root + os.sep) else tmp


def cmd_build(args: argparse.Namespace) -> int:
    from concurrent.futures import ThreadPoolExecutor

    repo = Path(args.repo).resolve()
    known = python_meta_tests(repo)
    files = sorted(f for f, k in known.items() if "session-items" not in k)
    jobs = args.jobs or int(os.environ.get("LAB_CPUS") or os.cpu_count() or 2)
    print(f"tracing {len(files)} meta-test files, {jobs} at a time", flush=True)
    t0 = time.monotonic()
    # Outside the repo: a lab copy's TMPDIR is under it, where a whole-repo scan would read the
    # temporary trees of the tests traced beside it.
    scratch = _outside(repo)
    with tempfile.TemporaryDirectory(prefix="meta-surface-", dir=scratch) as out:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = dict(zip(files, pool.map(
                lambda ij: _trace_one(repo, ij[1], Path(out) / str(ij[0]), args.pytest_args), enumerate(files))))
    # A whole-repo scan can read another test's temporary files while the two run side by side
    # (inside a lab copy TMPDIR is under the repo); a file whose trace failed is traced again alone.
    retry = [f for f in files if results[f][1] and not results[f][1].startswith("a skip")]
    for i, test in enumerate(retry):
        with tempfile.TemporaryDirectory(prefix="meta-surface-retry-", dir=scratch) as out:
            again = _trace_one(repo, test, Path(out) / str(i), args.pytest_args)
        if not again[1]:
            print(f"  {test}: clean when traced alone", flush=True)
            results[test] = again
    tests = {}
    for test in files:
        reads, why = results[test]
        entry = {"sha256": surface_hash(repo, test), **compact(reads)}
        if why:
            entry["incomplete"] = why
        tests[test] = entry
    manifest = {
        "about": "Generated by scripts/meta_tests.py build; do not edit. What each meta-test read, per file.",
        "built_from": git(repo, "rev-parse", "HEAD").strip(),
        "tests": tests,
    }
    write_manifest(repo / MANIFEST, manifest)
    incomplete = sorted(t for t, e in tests.items() if e.get("incomplete"))
    print(f"wrote {MANIFEST}: {len(tests)} meta-tests in {time.monotonic() - t0:.0f}s;"
          f" {len(incomplete)} with an incomplete trace (they run whatever the diff):")
    for test in incomplete:
        print(f"  {test}: {tests[test]['incomplete']}")
    return 0


def compact(kinds: dict) -> dict[str, list[str]]:
    """One test's traced reads, less what selects nothing more: a path under a recorded
    prefix, a stat of a path it opened."""
    prefixes = sorted(_minimal_prefixes(kinds.get("prefixes", ())))

    def free(p: str) -> bool:
        return not _prefix_hit(p, prefixes)

    listed = sorted(p for p in kinds.get("listed", ()) if free(p))
    opened = sorted(p for p in kinds.get("opened", ()) if free(p))
    statted = sorted(p for p in kinds.get("statted", ()) if free(p) and p not in opened)
    return {"opened": opened, "statted": statted, "listed": listed, "prefixes": prefixes,
            "opaque": sorted(kinds.get("opaque", ()))}


def _minimal_prefixes(prefixes) -> set[str]:
    keep: set[str] = set()
    for p in sorted(set(prefixes), key=len):
        if not _prefix_hit(p, list(keep)):
            keep.add(p)
    return keep


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("select", "run"):
        p = sub.add_parser(name)
        p.add_argument("--base", default="origin/develop")
        p.add_argument("--repo", default=".")
        if name == "run":
            p.add_argument("pytest_args", nargs="*",
                           help="extra pytest arguments for the meta-test run, e.g. -n 4 inside a lab grant"
                                " (an -n run is heavy on a laptop)")
        if name == "select":
            p.add_argument("--json", action="store_true")
            p.add_argument("--names-from-stdin", action="store_true",
                           help="read the changed paths from stdin instead of git (a replay)")
    b = sub.add_parser("build")
    b.add_argument("--repo", default=".")
    b.add_argument("--jobs", type=int, default=0, help="files traced at once (default: the grant's or host's cores)")
    b.add_argument("pytest_args", nargs="*", help="extra pytest arguments, e.g. -n auto")
    args = parser.parse_args(argv)
    return {"select": cmd_select, "run": cmd_run, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
