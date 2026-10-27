# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-231 (nexus-bivz3.14): nothing under ``src/nexus`` spells a note scheme except the definitions.

A note's ``source_uri`` is ``nx-note://<collection>/<title>``, minted by ``aspect_readers.note_uri`` and
recognised by ``is_note_uri`` / ``canonical_note_uri``. The legacy spelling ``chroma://`` survives only
where something reads or repairs old rows. This lint stops a new site from spelling either scheme
itself (RDR Risks and Mitigations: "nothing else spells a scheme"). It replaced the interim
``f"chroma://`` line census that ``tests/test_rdr231_client_mints.py`` carried until nexus-bivz3.14.

What is flagged, by AST walk over every ``*.py`` under ``src/nexus``:

* a string that builds ``chroma://`` or ``nx-note://``: a ``Constant``, an f-string, or a ``+``
  concatenation, folded so ``"chro" + "ma://"`` and ``"nx-note" + "://"`` read as the scheme;
* a string that names either scheme with a colon after it, however much regex punctuation sits between
  (nexus-bivz3.15, code review S1): ``r"^(?:chroma|nx-note)://"``, ``startswith("chroma:")``;
* a ``Constant`` exactly equal to ``"chroma"`` (a scheme key: dict key, set member, ``== "chroma"``,
  ``"scheme": "chroma"``) unless it is an operand of ``/`` (``config_dir / "chroma"`` is a directory
  name: ``config.py``, ``stranded_install.py``, ``db/minilm_direct.py``).

What is not flagged, and why:

* **Docstrings** (module, class, function) and **comments**. They describe the scheme; they cannot mint
  one. Comments are not in the AST at all. The consequence is that ``engine_version.py``, whose
  mentions are release-history comments only, needs no allowlist entry (an entry would be dead and the
  stale-entry test would fail on it).
* **Message text is NOT exempt.** A help string, an error message or a ``click.echo`` is an ordinary
  string constant; telling it apart from a mint syntactically would be a heuristic a mint could hide
  inside (``log.info(f"nx-note://{c}/{t}")``). A message that has to name the scheme interpolates
  ``NOTE_URI_SCHEME`` (see ``commands/catalog.py`` and ``db/serving_engine_version.py``), or its file
  carries an allowlist entry with the reason.
* **A bare ``"nx-note"``** (no ``://``). It is the canonical scheme NAME, used as a registry key
  (``_READERS``, ``_KNOWN_URI_SCHEMES``). It mints nothing, and flagging it would make the catalog's
  lightweight ``types`` module import ``aspect_readers`` for no protection. The bead's requirement
  names only the legacy ``"chroma"`` key; the legacy spelling must not resurface as a key.

Allowlist entries are per FILE, each with a reason AND the number of hit lines the file has today
(nexus-bivz3.15): a new mint inside an allowlisted file changes the count and fails
``test_the_allowlisted_files_hold_exactly_their_pinned_hits``, so the allowlist cannot become a licence
for the whole file. A file with no remaining hit fails the stale-entry test. The counts (files scanned, hits, allowlisted hits) are in the assertion message of
``test_every_scheme_literal_is_in_an_allowlisted_file`` when it fails; ``python -c "from tests.test_rdr231_note_scheme_literals_lint import _scan; print(_scan())"``
prints them on a green tree.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parent.parent
_SRC_REL = Path("src") / "nexus"

#: A part the fold cannot resolve (a variable, a call, a formatted value) becomes this character.
_UNKNOWN = "\x00"

_SCHEME = re.compile(r"(?:chroma|nx-note)://")
#: The scheme name followed by a colon, with up to six punctuation characters between (a regex group's
#: closing paren, a quote, a bracket): ``(?:chroma|nx-note)://``, ``chroma:``. Prose that merely
#: contains the word has no colon straight after it and is not matched.
_SCHEME_COLON = re.compile(r"(?:chroma|nx-note)[^\w\s]{0,6}:")
_LEGACY_KEY = "chroma"

#: Fewer files than this means the walk found the wrong directory.
_MIN_FILES_SCANNED = 300

#: path -> (reason, hit lines in that file today). Re-measure with
#: ``python -c "from tests.test_rdr231_note_scheme_literals_lint import _scan; print(_scan())"``.
_ALLOWED: dict[str, tuple[str, int]] = {
    "src/nexus/aspect_readers.py": (
        "owns the definitions: NOTE_URI_SCHEME, the legacy chroma:// spelling canonical_note_uri still "
        "accepts on input, and the reader and stat registries keyed by the scheme",
        2,
    ),
    "src/nexus/commands/catalog_cmds/backfill.py": (
        "`nx catalog backfill` finds and repairs stored chroma:// rows under file-routed collections by "
        "prefix; it builds a pattern to FIND residue, never an identity to store",
        6,
    ),
    "src/nexus/catalog/recovery_bundle.py": (
        "the residue counter counts bundle rows still carrying the legacy chroma:// spelling under a "
        "file-routed collection; it recognises the spelling and mints nothing",
        1,
    ),
}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for n in ast.walk(tree):
        if (
            isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(getattr(n.body[0], "value", None), ast.Constant)
        ):
            ids.add(id(n.body[0].value))
    return ids


def _fold(node: ast.AST) -> str | None:
    """The string an expression builds, ``_UNKNOWN`` for parts it cannot resolve, None when the
    expression is not string-shaped. Folds ``"a" + "b"`` (a Constant-only walk misses a split scheme)
    and an f-string operand of ``+``."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else _UNKNOWN
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _fold(node.left), _fold(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else _UNKNOWN) + (right if right is not None else _UNKNOWN)
    return None


def _hit_lines(path: Path) -> set[int]:
    """Line numbers of the scheme literals and legacy scheme keys in one file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    doc = _docstring_ids(tree)
    parent: dict[int, ast.AST] = {}
    for n in ast.walk(tree):
        for child in ast.iter_child_nodes(n):
            parent[id(child)] = n
    lines: set[int] = set()
    for n in ast.walk(tree):
        if id(n) in doc:
            continue
        if isinstance(n, (ast.Constant, ast.BinOp)):
            folded = _fold(n)
            if folded is not None and (_SCHEME.search(folded) or _SCHEME_COLON.search(folded)):
                lines.add(n.lineno)
        if isinstance(n, ast.Constant) and n.value == _LEGACY_KEY:
            up = parent.get(id(n))
            if isinstance(up, ast.BinOp) and isinstance(up.op, ast.Div):
                continue  # a path part: config_dir / "chroma"
            lines.add(n.lineno)
    return lines


def _scan(repo: Path = REPO) -> tuple[int, dict[str, int]]:
    """``(files scanned, {relative path: hit count})`` over ``src/nexus``. ``repo`` is a resolved path."""
    files = sorted((repo / _SRC_REL).rglob("*.py"))
    found: dict[str, int] = {}
    for p in files:
        n = len(_hit_lines(p))
        if n:
            found[p.relative_to(repo).as_posix()] = n
    return len(files), found


def test_every_scheme_literal_is_in_an_allowlisted_file() -> None:
    scanned, found = _scan()
    hits = sum(found.values())
    allowlisted = sum(n for f, n in found.items() if f in _ALLOWED)
    offenders = {f: n for f, n in found.items() if f not in _ALLOWED}
    assert not offenders, (
        f"files scanned={scanned}, hits={hits}, allowlisted={allowlisted}. These files spell a note scheme "
        "(chroma:// or nx-note://, or a bare \"chroma\" key) outside the definitions. Mint with "
        "aspect_readers.note_uri, classify with is_note_uri, normalise with canonical_note_uri, and "
        "interpolate NOTE_URI_SCHEME in a message. Only a file that recognises or repairs legacy residue "
        f"belongs in _ALLOWED, with the reason: {offenders}"
    )


def test_the_allowlisted_files_hold_exactly_their_pinned_hits() -> None:
    """A new scheme literal inside an allowlisted file is a new mint site the per-file allowlist would
    otherwise wave through (code review S1, nexus-bivz3.15). Re-measure and edit the pin only for a literal
    that recognises or repairs residue, with the reason in the entry."""
    _, found = _scan()
    drift = {f: (found.get(f, 0), pinned) for f, (_reason, pinned) in _ALLOWED.items() if found.get(f, 0) != pinned}
    assert not drift, f"allowlisted files whose scheme-literal line count moved (now, pinned): {drift}"


def test_the_allowlist_has_no_stale_entries_and_every_reason_is_real() -> None:
    _, found = _scan()
    stale = sorted(set(_ALLOWED) - set(found))
    assert not stale, f"allowlisted files with no scheme literal left (remove the entry): {stale}"
    for path, (reason, _pinned) in _ALLOWED.items():
        assert (REPO / path).is_file(), f"{path}: allowlisted file does not exist"
        assert len(reason.strip()) > 40, f"{path}: reason too short to be a justification"


def test_the_scan_is_not_vacuous() -> None:
    scanned, found = _scan()
    assert scanned >= _MIN_FILES_SCANNED, f"scanned only {scanned} files; the walk is broken"
    for owner in _ALLOWED:
        assert found.get(owner, 0) >= 1, f"the scan must see the definitions in {owner}"
    # A path part named chroma is a directory, never a scheme: all three sites exist and none is flagged.
    for dirname_site in ("src/nexus/config.py", "src/nexus/stranded_install.py", "src/nexus/db/minilm_direct.py"):
        assert '"chroma"' in (REPO / dirname_site).read_text(encoding="utf-8"), (
            f"{dirname_site} no longer names the chroma directory; the negative control below lost its subject"
        )
        assert dirname_site not in found, f"{dirname_site} uses \"chroma\" as a path part and must not be flagged"


_PLANTS = {
    "chroma_literal": 'URI = "chroma://c/x"\n',
    "nx_note_literal": 'URI = "nx-note://c/x"\n',
    "nx_note_fstring": 'def u(c):\n    return f"nx-note://{c}/x"\n',
    "chroma_fstring": 'def u(c):\n    return f"chroma://{c}/x"\n',
    "split_concat": 'URI = "chro" + "ma://"\n',
    "split_name_and_separator": 'URI = "nx-note" + "://"\n',
    "fstring_operand": 'def u(c):\n    return "nx-note:/" + f"/{c}"\n',
    "dict_key": 'READERS = {"chroma": print}\n',
    "set_member": 'SCHEMES = frozenset({"file", "chroma"})\n',
    "compare": 'def u(s):\n    return s == "chroma"\n',
    "scheme_value": 'ROW = {"scheme": "chroma"}\n',
    "regex_alternation": 'import re\nR = re.compile(r"^(?:chroma|nx-note)://")\n',
    "regex_alternation_reversed": 'import re\nR = re.compile(r"(?:nx-note|chroma)://")\n',
    "prefix_test_with_a_colon": 'def u(s):\n    return s.startswith("chroma:")\n',
    "nx_note_prefix_test_with_a_colon": 'def u(s):\n    return s.startswith("nx-note:")\n',
    "startswith_in_a_message": 'def u(log):\n    log.info("row is chroma://c/x")\n',
}

_CLEAN = {
    "path_part_div": 'from pathlib import Path\nD = Path.home() / "chroma"\nE = Path.home() / ".cache" / "chroma" / "onnx"\n',
    "bare_scheme_name": 'READERS = {"nx-note": print}\n',
    "prose_without_separator": 'MSG = "chroma was retired"\n',
    "docstring_only": 'def f():\n    """Mints nx-note://c/x; reads chroma://c/x."""\n',
    "module_docstring": '"""A chroma:// row is residue."""\nX = 1\n',
    "comment_only": "# a chroma:// row is residue; mint nx-note://c/x through note_uri\nX = 1\n",
}


@pytest.mark.parametrize("name", sorted(_PLANTS))
def test_a_planted_scheme_literal_is_flagged(tmp_path: Path, name: str) -> None:
    mod = tmp_path / "src" / "nexus" / "scratch_mod.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(_PLANTS[name])
    scanned, found = _scan(tmp_path.resolve())
    assert (scanned, found) == (1, {"src/nexus/scratch_mod.py": 1}), name


@pytest.mark.parametrize("name", sorted(_CLEAN))
def test_a_directory_name_a_docstring_a_comment_and_a_bare_scheme_name_are_not_flagged(
    tmp_path: Path, name: str,
) -> None:
    mod = tmp_path / "src" / "nexus" / "clean_mod.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(_CLEAN[name])
    assert _scan(tmp_path.resolve()) == (1, {}), name
