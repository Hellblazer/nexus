# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Ratchet: every text-mode subprocess call in shipped code names its encoding.

THE SHAPES THIS WATCHES::

    subprocess.run(argv, capture_output=True, text=True)
    subprocess.Popen(argv, stdout=PIPE, universal_newlines=True)
    subprocess.check_output(argv, errors="replace")
    subprocess.run(argv, **opts)             # may carry text=True; unread
    run_bounded(argv, timeout=5, encoding=None)

With ``text=True`` and no ``encoding``, Python decodes the child's pipes (and
encodes ``input=``) with the locale's preferred encoding. On macOS and Linux
that is UTF-8, so the omission is invisible. On Windows it is the ANSI code
page (cp1252 on an English install), so a non-ASCII path or message in a git,
uv or nx child's output garbles or raises ``UnicodeDecodeError``
(nexus-ikoqj, the sibling of the file-IO sweep nexus-ox0c0 and its lint,
``tests/test_text_io_encoding_lint.py``).

Unlike the file-IO sweep, the remedy is NOT one string everywhere: it depends
on the child, so each site was read and decided.

* git, uv, gh, bd, nx, the Java engine, psql and pg_ctl, launchctl, systemctl,
  ps, osascript: ``encoding="utf-8"``. ``nx`` writes UTF-8 to its pipes on
  Windows by itself (``nexus.cli`` reconfigures stdout and stderr at import).
  A bare Python child writes its pipes in its own locale encoding, so a spawn
  of one whose output can carry non-ASCII also sets ``PYTHONIOENCODING``.
* Native Windows tools (``schtasks``): the console code page, which is what
  ``locale.getpreferredencoding(False)`` names, with ``errors="replace"``;
  forcing UTF-8 there would garble their localised text.

``errors="replace"`` is added only where the text is shown or matched loosely.
A site that parses an exact value keeps the strict default, as it had on a
UTF-8 locale.

:func:`~nexus.bounded_subprocess.run_bounded` is the shared wrapper: it takes
``encoding`` (default ``"utf-8"``) and ``errors`` and passes them to ``Popen``
only in text mode, so its ~65 callers are decided there. The lint pins the
wrapper's own ``Popen`` call (it names ``encoding``) and flags a caller that
passes ``encoding=None``.

:data:`_EXEMPT` is empty and stays so: every file in scope is allowed zero.
With nothing left to find, the real-tree test would also pass with a detector
that matches nothing, so :func:`test_detector_recognises_the_shapes_it_claims_to`
pins the shapes and their near-misses on synthetic source and
:func:`test_the_walk_finds_a_planted_file` pins the walk.

What the scan cannot see is reported, not skipped: a ``**kwargs`` unpack into
a watched call that names no ``encoding`` is a finding of its own unless it
unpacks one of the two producers of ``Popen`` isolation flags
(:data:`_INERT_PRODUCERS`, pinned by :func:`test_the_inert_producers_return_only_isolation_flags`)
or it is named in :data:`_KWARGS_READ` with what reading it found.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

from tests.test_windows_release_legs_workflow import WINDOWS_TEST_SET

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Same roots as the file-IO lint: shipped Python, then the tests the Windows
# CI job runs, read from the pin on that job's list so the two cannot drift.
SCAN_ROOTS: tuple[str, ...] = ("src/nexus", "conexus", "sn") + tuple(
    dict.fromkeys(entry.split("::")[0].rstrip("/") for entry in WINDOWS_TEST_SET)
)

# "<repo-relative path>:<line>" -> why that call may stay encoding-less.
_EXEMPT: dict[str, str] = {}

# "<repo-relative path>:<line>" -> what reading the ``**kwargs`` found. These
# are unpacks into a watched call that names no ``encoding`` and sets no text
# mode of its own, so what they carry decides whether the child is decoded.
_KWARGS_READ: dict[str, str] = {
    "src/nexus/hooks/upgrade_auto.py:162": (
        "``common`` is a literal dict of stdin/stdout/stderr (DEVNULL or a binary "
        "log handle) and close_fds; the child is detached and nothing reads it as text"
    ),
    "src/nexus/hooks/upgrade_auto.py:165": "same ``common`` dict as :162",
    "src/nexus/hooks/upgrade_auto.py:168": "same ``common`` dict as :162",
    "tests/test_mcpb_bootstrap.py:187": (
        "a test double for ``bootstrap._sync_with_retry``'s ``run`` argument: the kwargs are the "
        "bootstrap's own, which carry encoding='utf-8' and errors='replace' and no text "
        "(pinned by test_sync_does_not_inherit_the_hosts_stdin_and_decodes_utf8)"
    ),
    "src/nexus/commands/command_context.py:97": (
        "``_check_output`` funnel into run_bounded: every caller passes timeout, stderr "
        "or text, none passes encoding, so the wrapper's utf-8 default decides; its "
        "children are git, bd, gh and nx"
    ),
    "src/nexus/daemon/installer.py:935": (
        "``_run_manager`` funnel into run_bounded: callers pass capture_output (dropped), "
        "text and check; ``_run_manager`` sets ``encoding`` itself before this call, per "
        "the manager (schtasks: console code page; launchctl, systemctl: utf-8)"
    ),
}

# Calls that return only ``start_new_session`` or ``creationflags``.
_INERT_PRODUCERS: frozenset[str] = frozenset({"isolation_popen_kwargs", "_isolation_kwargs"})

# Module-level dicts in the tests that hold only those same two keys: where each
# is defined, so :func:`test_the_inert_names_hold_only_isolation_flags` can read it.
_INERT_NAME_HOMES: dict[str, tuple[str, ...]] = {
    "OWN_GROUP": ("tests/daemon/_children.py",),
    "_OWN_GROUP": ("tests/daemon/test_rdr149_lifecycle_conformance.py",),
}
_INERT_NAMES: frozenset[str] = frozenset(_INERT_NAME_HOMES)

# ``subprocess.<name>`` entry points. ``getoutput`` and ``getstatusoutput`` are
# always text and take no encoding at all.
_SUBPROCESS_ATTRS: frozenset[str] = frozenset({"run", "Popen", "check_output", "check_call", "call"})
_ALWAYS_TEXT: frozenset[str] = frozenset({"getoutput", "getstatusoutput"})
# Bare names that only mean subprocess's after ``from subprocess import``.
_BARE: frozenset[str] = frozenset({"Popen", "check_output", "check_call"}) | _ALWAYS_TEXT
# Popen's positional index of ``universal_newlines`` (args, bufsize, executable,
# stdin, stdout, stderr, preexec_fn, close_fds, shell, cwd, env, universal_newlines).
_UNIVERSAL_NEWLINES_AT = 11


def _watched(call: ast.Call) -> str | None:
    """Which kind of watched call *call* is, or ``None``."""
    func = call.func
    if isinstance(func, ast.Attribute):
        receiver = func.value.id if isinstance(func.value, ast.Name) else None
        if receiver == "subprocess" and func.attr in _SUBPROCESS_ATTRS | _ALWAYS_TEXT:
            return f"subprocess.{func.attr}"
        if receiver == "os" and func.attr == "popen":
            return "os.popen"
        if func.attr == "run_bounded":
            return "run_bounded"
    elif isinstance(func, ast.Name):
        if func.id in _BARE:
            return func.id
        if func.id == "run_bounded":
            return "run_bounded"
    return None


def _truthy_or_unknown(node: ast.expr) -> bool:
    """Not provably off: anything but a literal ``False`` / ``None``."""
    return not (isinstance(node, ast.Constant) and node.value in (False, None))


def _only_inert_unpacks(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg is not None:
            continue
        value = keyword.value
        if isinstance(value, ast.Name):
            if value.id not in _INERT_NAMES:
                return False
            continue
        name = value.func.id if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) else None
        if name not in _INERT_PRODUCERS:
            return False
    return True


def unencoded_subprocess_text(source: str) -> list[tuple[int, str]]:
    """(line, kind) for every watched subprocess call in *source* that decodes
    with the locale's encoding, or whose ``**kwargs`` cannot be read."""
    tree = ast.parse(source)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kind = _watched(node)
        if kind is None:
            continue
        keywords = {k.arg: k.value for k in node.keywords if k.arg is not None}
        starred = any(k.arg is None for k in node.keywords)
        encoding = keywords.get("encoding")
        if isinstance(encoding, ast.Constant) and encoding.value is None:
            found.append((node.lineno, f"{kind} with encoding=None"))
            continue
        if kind == "run_bounded":
            # The wrapper defaults to utf-8; only a ``**`` unpack hides what it gets.
            if starred and not _only_inert_unpacks(node):
                found.append((node.lineno, f"{kind} with **kwargs"))
            continue
        if encoding is not None:
            continue
        text_mode = (
            kind.rsplit(".", 1)[-1] in _ALWAYS_TEXT
            or kind == "os.popen"
            or any(_truthy_or_unknown(keywords[name]) for name in ("text", "universal_newlines") if name in keywords)
            or "errors" in keywords
            or (kind.endswith("Popen") and len(node.args) > _UNIVERSAL_NEWLINES_AT)
        )
        if text_mode:
            found.append((node.lineno, f"{kind} text mode, no encoding"))
        elif starred and not _only_inert_unpacks(node):
            found.append((node.lineno, f"{kind} with **kwargs"))
    return sorted(found)


def _census(repo: pathlib.Path, roots: tuple[str, ...]) -> tuple[int, dict[str, str]]:
    """(files scanned, "<path>:<line>" -> kind). *repo* and *roots* are
    parameters only so :func:`test_the_walk_finds_a_planted_file` can point
    the walk at a planted tree."""
    files = 0
    findings: dict[str, str] = {}
    for root in roots:
        base = repo / root
        for path in [base] if base.is_file() else sorted(base.rglob("*.py")):
            files += 1
            rel = path.relative_to(repo).as_posix()
            for line, kind in unencoded_subprocess_text(path.read_text(encoding="utf-8")):
                findings[f"{rel}:{line}"] = kind
    return files, findings


def test_subprocess_text_calls_name_their_encoding() -> None:
    files, findings = _census(REPO_ROOT, SCAN_ROOTS)
    assert files > 300, f"the walk reached only {files} files; scan roots moved?"
    unexpected = {
        site: kind
        for site, kind in findings.items()
        if site not in _EXEMPT and site not in _KWARGS_READ
    }
    assert not unexpected, (
        "subprocess calls that decode with the locale encoding (cp1252 on Windows) or hide "
        "their kwargs; name an encoding after reading the child, see this module's docstring:\n"
        + "\n".join(f"  {site}  {kind}" for site, kind in sorted(unexpected.items()))
    )
    stale = sorted((set(_EXEMPT) | set(_KWARGS_READ)) - set(findings))
    assert not stale, f"_EXEMPT / _KWARGS_READ name sites that are no longer findings: {stale}"
    assert _EXEMPT == {}, "this ratchet allows zero; fix the call instead of exempting it"


_MUST_MATCH: tuple[tuple[str, str], ...] = (
    ("subprocess.run(a, capture_output=True, text=True)", "subprocess.run text mode, no encoding"),
    ("subprocess.run(a, universal_newlines=True)", "subprocess.run text mode, no encoding"),
    ("subprocess.run(a, text=flag)", "subprocess.run text mode, no encoding"),
    ("subprocess.run(a, errors='replace')", "subprocess.run text mode, no encoding"),
    ("subprocess.check_output(a, text=True, stderr=DEVNULL)", "subprocess.check_output text mode, no encoding"),
    ("subprocess.check_call(a, text=True)", "subprocess.check_call text mode, no encoding"),
    ("subprocess.call(a, text=True)", "subprocess.call text mode, no encoding"),
    ("subprocess.Popen(a, stdout=PIPE, text=True)", "subprocess.Popen text mode, no encoding"),
    (
        "subprocess.Popen(a, 0, None, None, None, None, None, True, False, None, None, True)",
        "subprocess.Popen text mode, no encoding",
    ),
    ("Popen(a, text=True)", "Popen text mode, no encoding"),
    ("check_output(a, text=True)", "check_output text mode, no encoding"),
    ("subprocess.getoutput(cmd)", "subprocess.getoutput text mode, no encoding"),
    ("subprocess.getstatusoutput(cmd)", "subprocess.getstatusoutput text mode, no encoding"),
    ("os.popen(cmd)", "os.popen text mode, no encoding"),
    ("subprocess.run(a, text=True, encoding=None)", "subprocess.run with encoding=None"),
    ("subprocess.run(a, **opts)", "subprocess.run with **kwargs"),
    ("subprocess.run(a, text=True, **opts)", "subprocess.run text mode, no encoding"),
    ("subprocess.Popen(a, **common)", "subprocess.Popen with **kwargs"),
    ("run_bounded(a, timeout=5, encoding=None)", "run_bounded with encoding=None"),
    ("bounded_subprocess.run_bounded(a, timeout=5, **rest)", "run_bounded with **kwargs"),
)

_MUST_NOT_MATCH: tuple[str, ...] = (
    "subprocess.run(a, capture_output=True, text=True, encoding='utf-8')",
    "subprocess.run(a, text=True, encoding='utf-8', errors='replace')",
    "subprocess.run(a, text=True, encoding=enc)",
    "subprocess.run(a, encoding='utf-8')",
    "subprocess.run(a, capture_output=True)",
    "subprocess.run(a, text=False)",
    "subprocess.run(a, universal_newlines=False)",
    "subprocess.run(a, text=None)",
    "subprocess.check_output(a, text=True, encoding='utf-8')",
    "subprocess.Popen(a, stdout=PIPE)",
    "subprocess.Popen(a, text=False, **isolation_popen_kwargs())",
    "subprocess.Popen(a, **_isolation_kwargs(isolation_popen_kwargs(), extra))",
    "subprocess.Popen(a, text=text, encoding=encoding, **isolation_popen_kwargs())",
    "subprocess.run(a, **opts, encoding='utf-8')",
    "subprocess.Popen(a, stdout=PIPE, **OWN_GROUP)",
    "subprocess.Popen(a, **_OWN_GROUP)",
    "run_bounded(a, timeout=5)",
    "run_bounded(a, timeout=5, text=True)",
    "run_bounded(a, timeout=5, encoding='utf-8')",
    "run_bounded(a, timeout=5, **isolation_popen_kwargs())",
    "subprocess.CompletedProcess(args, 0, out, err)",
    "subprocess.TimeoutExpired(a, 5)",
    "TextContent(type='text', text='x')",
    "Document(text='x')",
    "os.getenv('x')",
    "pool.run(a, text=True)",
    "def run(self, text=True): pass",
)


@pytest.mark.parametrize(("source", "kind"), _MUST_MATCH)
def test_detector_recognises_the_shapes_it_claims_to(source: str, kind: str) -> None:
    assert unencoded_subprocess_text(source) == [(1, kind)]


@pytest.mark.parametrize("source", _MUST_NOT_MATCH)
def test_detector_passes_the_near_misses(source: str) -> None:
    assert unencoded_subprocess_text(source) == []


def test_the_walk_finds_a_planted_file(tmp_path: pathlib.Path) -> None:
    pkg = tmp_path / "pkg" / "sub"
    pkg.mkdir(parents=True)
    (pkg / "clean.py").write_text(
        "subprocess.run(a, text=True, encoding='utf-8')\n", encoding="utf-8"
    )
    (pkg / "dirty.py").write_text("x = 1\nsubprocess.run(a, text=True)\n", encoding="utf-8")
    (tmp_path / "single.py").write_text("subprocess.Popen(a, **opts)\n", encoding="utf-8")
    files, findings = _census(tmp_path, ("pkg", "single.py"))
    assert files == 3
    assert findings == {
        "pkg/sub/dirty.py:2": "subprocess.run text mode, no encoding",
        "single.py:1": "subprocess.Popen with **kwargs",
    }


def test_the_inert_producers_return_only_isolation_flags() -> None:
    """The two names :data:`_INERT_PRODUCERS` lets a ``**`` unpack skip must
    never grow a ``text`` or ``encoding`` key."""
    from nexus.bounded_subprocess import _isolation_kwargs
    from nexus.util.process_group import isolation_popen_kwargs

    allowed = {"start_new_session", "creationflags"}
    assert set(isolation_popen_kwargs()) <= allowed
    assert set(_isolation_kwargs(isolation_popen_kwargs(), 0x8)) <= allowed
    assert _INERT_PRODUCERS == {"isolation_popen_kwargs", "_isolation_kwargs"}


def test_the_inert_names_hold_only_isolation_flags() -> None:
    """:data:`_INERT_NAMES` lets ``**OWN_GROUP`` through unread. Each name is a
    module-level dict whose every string key is ``creationflags`` or
    ``start_new_session``."""
    allowed = {"creationflags", "start_new_session"}
    for name, homes in _INERT_NAME_HOMES.items():
        for home in homes:
            tree = ast.parse((REPO_ROOT / home).read_text(encoding="utf-8"))
            values = [
                node.value
                for node in tree.body
                if isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
                and node.value is not None
            ]
            assert len(values) == 1, f"{home} does not define {name} exactly once"
            keys = {
                key.value
                for dict_node in ast.walk(values[0])
                if isinstance(dict_node, ast.Dict)
                for key in dict_node.keys
                if isinstance(key, ast.Constant)
            }
            assert keys and keys <= allowed, (home, name, keys)


def test_run_bounded_decodes_utf8_and_leaves_binary_mode_alone() -> None:
    """The wrapper's contract the callers rely on: text mode defaults to
    UTF-8, and ``text=False`` still returns bytes (passing ``encoding`` to
    ``Popen`` unconditionally would have switched it to text)."""
    from nexus.bounded_subprocess import run_bounded

    code = "import sys; sys.stdout.buffer.write('\\u00e9\\u4e2d'.encode('utf-8'))"
    text = run_bounded([sys.executable, "-c", code], timeout=30)
    assert text.stdout == "é中"
    raw = run_bounded([sys.executable, "-c", code], timeout=30, text=False)
    assert raw.stdout == "é中".encode()
    legacy = run_bounded([sys.executable, "-c", code], timeout=30, encoding="cp1252", errors="replace")
    assert legacy.stdout != "é中"
    # A byte that is not UTF-8 (0x97 is an em dash in cp1252) is replaced by default, not raised.
    bad = "import sys; sys.stdout.buffer.write(b'a\\x97b')"
    assert run_bounded([sys.executable, "-c", bad], timeout=30).stdout == "a\ufffdb"
    with pytest.raises(UnicodeDecodeError):
        run_bounded([sys.executable, "-c", bad], timeout=30, errors="strict")
