# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""No tracked file writes a `.credentials.json`, reads either forbidden
keychain item, or assigns `CLAUDE_CODE_OAUTH_TOKEN` a literal, outside the
one shared picker (nexus-galkv.19, widened RDR-219 Phase 3 Step 2a,
nexus-wauo1.23).

THE ORIGINAL INCIDENT. More than one macOS Keychain item can carry the
service name ``Claude Code-credentials`` — on this box an ``acct="unknown"``
item is an empty husk (``accessToken ""``, ``refreshToken ""``,
``expiresAt 0``) alongside the live item the CLI actually refreshes.
``security find-generic-password -s 'Claude Code-credentials' -w`` with no
``-a`` returns an ARBITRARY match, not necessarily the live one. The fix,
``tests/e2e/lib/claude_credentials.py`` (RDR-219: ``run``/``status``), is the
one file allowed to actually perform the three forbidden actions below.

RDR-219 WIDENS THIS LINT (Phase 3 Step 2, Technical Design "The lint") past
``find-generic-password`` calls to every tracked text file, markdown
included, and past one forbidden shape to three:

1. **Writing a ``.credentials.json`` file** — the operator's own
   interactive-login credential file, which a harness must never copy
   (RDR-219 rule 1).
2. **Reading either forbidden keychain item** — ``Claude Code-credentials``
   (the operator's interactive login, read by nothing in this repository)
   or ``nexus-automation-oauth-token`` (the harness's own automation
   identity, read by nothing outside the shared tool).
3. **Assigning ``CLAUDE_CODE_OAUTH_TOKEN`` a literal** — the token travels
   only through a child process's environment (RDR-219 rule 2); a literal
   assignment (as opposed to `=$VAR`/`=$(cmd)` expansion, or the bare
   `-e CLAUDE_CODE_OAUTH_TOKEN` docker shape that reads the value from the
   *invoking* process's own environment) is exactly the shape that would put
   the token on a process's argv or into a file.

SCOPE. Every file ``git ls-files`` returns that decodes as UTF-8 text
(binary files, e.g. fixtures under ``tests/e2e/migration-rehearsal/``, are
excluded by construction: `.decode("utf-8")` fails and the file is skipped),
minus the shared tool itself and a small, closed exemption list (below).
Markdown is in scope: RDR-219 Phase 2 removed every live credential-file
write from this repository, so the only things a tracked ``.md`` file can
legitimately still say about these three shapes are prose, history, a test
asserting their absence, or (in the RDR itself) a forbidding rule — never a
live recipe that performs one.

DISTINGUISHING A MENTION FROM A LIVE ACTION. A comment line (``#``-prefixed,
every scanned language except markdown, where ``#`` starts a heading, not a
comment) never trips this lint. Beyond that, this codebase's own convention
— used throughout its docstrings and markdown prose — is to name a forbidden
shape inline inside a single pair of backticks on one line (e.g. `` `cp
/creds/.credentials.json ...` ``): that is documentation, not code, so
``.py`` and ``.md`` files have every such single-line backtick span stripped
before the three detectors below run (``.sh`` files do not get this
treatment — a backtick there is live command substitution, not a
documentation marker). A write is further required to look like one: a
copy/move/install/tee/rsync/`docker cp` verb followed by REAL whitespace and
a real next token, a Python file-write call, or a redirection whose target
leads directly into ``.credentials.json`` with no intervening whitespace or
quote — a verb WORD appearing only inside a quoted grep/regex-pattern
string (e.g. ``grep -qE '(cp\\s+|...)...'``, as this repository's own
``tests/e2e/run_sh_credentials_test.sh`` contains, checking for exactly this
incident) is not followed by real whitespace and so does not match. A
``CLAUDE_CODE_OAUTH_TOKEN=`` assignment is flagged only when a genuine,
non-empty literal follows — never a `$VAR`/backtick expansion, and never the
empty "value" a naive quote-strip would see in a Python string like
``"CLAUDE_CODE_OAUTH_TOKEN=" not in text`` (an assertion that the shape is
ABSENT, common throughout this repo's own RDR-219 test suite).

THE FORBIDDEN-NAME REGISTRY. Every credential-shaped NAME this lint looks
for lives in exactly one constant, ``FORBIDDEN_CREDENTIAL_NAMES``, mapping
each name to the repo-relative paths (beyond the shared tool and the
exemption list, which are skipped entirely) where that SPECIFIC name may
legitimately appear. A future amendment (nexus-wauo1.35) is expected to add
a harness-only token name, probably ``NX_HARNESS_CLAUDE_OAUTH_TOKEN``,
legitimately named in harness ``mcp.json`` configs and ``src/nexus``'s own
claude-dispatch module — sites that still need checking for the OTHER two
forbidden shapes and so cannot be whole-file exempted the way the shared
tool is. Adding that name's allowed sites is a one-line, reviewed change to
this one dict; no other part of this lint changes.

KNOWN LIMIT (line-based, not data-flow). Every detector above operates
ONE LINE at a time (``_violations``'s ``for line in text.splitlines()``
loop) and looks for the write verb and the credential-shaped name/path
literal TOGETHER on that same line. It does not track a path held in a
variable and interpolated elsewhere, e.g.::

    p = ".credentials.json"
    open(p, "w").write(payload)

Neither line carries both the write call and the literal path, so this
shape is NOT flagged (nexus-wauo1.26 code review finding 5, confirmed via
a direct ``_violations()`` call returning empty hits). Accepted, not
fixed here: a full data-flow analysis -- tracking a string literal
through an arbitrary variable before it reaches a write call -- is out of
proportion to what a cheap, line-based lint can do, and RDR-219's
defense-in-depth does not rest on this one layer alone. The credential
janitor (``tests/test_credential_janitor.py``, wired into the release
battery per nexus-wauo1.24) finds the RESULTING ``.credentials.json``
file on disk by walking the filesystem for its FILENAME, regardless of
how the write that created it reached that name -- so an indirected write
this lint misses is still caught the moment it actually lands a file.

THE EXEMPTION LIST (closed; see ``_EXEMPT_PATHS``/``_EXEMPT_PREFIXES``, and
``test_exempt_list_is_exactly_this`` which pins it): this lint's own file
(its docstring and kill-control fixtures plant the forbidden shapes on
purpose — DATA, not live code), ``CHANGELOG.md`` (its RDR-219 entry names
the forbidden command it replaced), and ``docs/rdr/**`` (the RDR itself
quotes the forbidden ``security find-generic-password`` command in its own
Minimum Viable Validation), and the P3.1 guard's test file
(nexus-wauo1.22), whose positive controls quote every command the guard
denies, as data. The guard script itself needs no exemption: it matches
those shapes with patterns, never with a literal forbidden command line.
"""
from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: The one file allowed to actually perform the three forbidden actions.
_SHARED_TOOL = REPO_ROOT / "tests" / "e2e" / "lib" / "claude_credentials.py"

#: This lint's own file — excluded from its own scan (see module docstring).
_SELF = pathlib.Path(__file__).resolve()


def _rel(path: pathlib.Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


#: Repo-relative EXACT paths exempt from every check in this lint, closed
#: (see module docstring "THE EXEMPTION LIST"; pinned by
#: ``test_exempt_list_is_exactly_this``).
_EXEMPT_PATHS: frozenset[str] = frozenset({
    _rel(_SELF),
    "CHANGELOG.md",
    "tests/test_routing_credential_print_guard.py",
})

#: Repo-relative path PREFIXES exempt from every check in this lint.
_EXEMPT_PREFIXES: tuple[str, ...] = ("docs/rdr/",)

#: Forbidden credential-shaped NAMES this lint looks for, kept in ONE
#: constant (see module docstring "THE FORBIDDEN-NAME REGISTRY").
FORBIDDEN_CREDENTIAL_NAMES: dict[str, frozenset[str]] = {
    #: The operator's interactive-login keychain item — read by nothing in
    #: this repository (RDR-219 rule 1).
    "Claude Code-credentials": frozenset(),
    #: The harness's own automation identity — read only by the shared tool.
    "nexus-automation-oauth-token": frozenset({_rel(_SHARED_TOOL)}),
    #: The environment variable the token travels in — never legitimately
    #: a LITERAL assignment anywhere, not even in the shared tool (which
    #: sets it from a variable, never a literal).
    "CLAUDE_CODE_OAUTH_TOKEN": frozenset(),
}

#: A non-comment line invoking `find-generic-password` and naming a
#: forbidden keychain item on the literal same line, in either quoting
#: style. Quotes/brackets/commas are normalized to whitespace first (same
#: technique as the git-config-global lint) so a Python argv list literal
#: (`["security", "find-generic-password", "-s", "Claude Code-credentials"]`)
#: is matched by the identical pattern.
_NOISE_RE = re.compile(r"""["'\[\],]""")
_KEYCHAIN_ITEM_ALTERNATION = "|".join(
    re.escape(name) for name in ("Claude Code-credentials", "nexus-automation-oauth-token")
)
_KEYCHAIN_READ_RE = re.compile(
    r"\bfind-generic-password\b.*\b(" + _KEYCHAIN_ITEM_ALTERNATION + r")\b"
)

#: Inline single-backtick code spans on ONE line — this repo's convention
#: for MENTIONING a forbidden shape as documentation. Stripped before
#: scanning `.py` and `.md` files only (never `.sh`, where a backtick is a
#: live command-substitution operator).
_INLINE_BACKTICK_SPAN_RE = re.compile(r"`[^`\n]*`")
_BACKTICK_STRIP_SUFFIXES = (".py", ".md")

#: `.credentials.json` mentioned anywhere on the (post-backtick-strip) line.
_CREDENTIALS_JSON_RE = re.compile(r"\.credentials\.json")

#: A write-verb pattern taking a REAL argument: `cp`/`mv`/`install`/`tee`/
#: `rsync`/`docker cp` followed by actual whitespace and a real next token
#: (excludes a verb word appearing only inside a quoted regex-pattern
#: string, e.g. `cp\s+|...`, where no real whitespace follows "cp" — see
#: module docstring), or a Python file-write call.
_WRITE_VERB_RE = re.compile(
    r"\b(?:cp|mv|install|tee|rsync)\b[ \t]+\S"
    r"|\bdocker\b[ \t]+cp\b[ \t]+\S"
    r"|\.write_text\("
    r"|\.write\("
    r"""|open\([^)]*["']\w*w"""
)

#: Shell redirection whose target leads DIRECTLY (no intervening whitespace
#: or quote) into a `.credentials.json` path.
_CREDENTIALS_JSON_REDIRECT_RE = re.compile(r""">{1,2}\s*['"]?[^\s'"]*\.credentials\.json""")

#: `CLAUDE_CODE_OAUTH_TOKEN=<literal>` — a quoted or bare NON-EMPTY literal,
#: never a `$VAR`/backtick expansion and never an empty "value" (the shape a
#: Python string like `"CLAUDE_CODE_OAUTH_TOKEN=" not in x` would otherwise
#: look like). Two alternatives: a quoted literal whose first character
#: (immediately after the opening quote) is neither `$` nor the SAME quote
#: character again (which would mean an empty string); or a bare literal
#: whose first character is none of whitespace/quote/`$`/backtick.
_TOKEN_LITERAL_ASSIGN_RE = re.compile(
    r"\bCLAUDE_CODE_OAUTH_TOKEN\s*=\s*(?:"
    r"""(["'])(?!\$)(?!\1)\S"""
    r"""|(?![\s"'$`])\S"""
    r")"
)


def _normalize(line: str) -> str:
    return _NOISE_RE.sub(" ", line)


def _strip_inline_backticks(line: str) -> str:
    return _INLINE_BACKTICK_SPAN_RE.sub(" ", line)


def _is_comment_line(line: str, suffix: str) -> bool:
    """`#` starts a comment in every scanned language except markdown,
    where it starts a heading -- markdown lines are never skipped by this
    check (headings carry no live shapes anyway)."""
    if suffix == ".md":
        return False
    return line.lstrip().startswith("#")


def _violations(text: str, rel_path: str = "synthetic.sh", suffix: str = ".sh") -> list[str]:
    hits: list[str] = []
    strip_backticks = suffix in _BACKTICK_STRIP_SUFFIXES
    for line in text.splitlines():
        if _is_comment_line(line, suffix):
            continue
        scan_line = _strip_inline_backticks(line) if strip_backticks else line
        if not scan_line.strip():
            continue

        # 1. Writing a `.credentials.json` file.
        if _CREDENTIALS_JSON_RE.search(scan_line) and (
            _WRITE_VERB_RE.search(scan_line) or _CREDENTIALS_JSON_REDIRECT_RE.search(scan_line)
        ):
            hits.append(line.strip())
            continue

        # 2. Reading either forbidden keychain item.
        keychain_match = _KEYCHAIN_READ_RE.search(_normalize(scan_line))
        if keychain_match:
            name = keychain_match.group(1)
            if rel_path not in FORBIDDEN_CREDENTIAL_NAMES.get(name, frozenset()):
                hits.append(line.strip())
                continue

        # 3. Assigning CLAUDE_CODE_OAUTH_TOKEN a literal.
        if _TOKEN_LITERAL_ASSIGN_RE.search(scan_line):
            if rel_path not in FORBIDDEN_CREDENTIAL_NAMES.get(
                "CLAUDE_CODE_OAUTH_TOKEN", frozenset()
            ):
                hits.append(line.strip())
    return hits


def _git_tracked_all() -> list[pathlib.Path]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        check=True, capture_output=True, text=True,
    ).stdout
    return sorted(REPO_ROOT / rel for rel in out.split("\0") if rel)


def _is_exempt(rel_path: str) -> bool:
    if rel_path in _EXEMPT_PATHS:
        return True
    return any(rel_path.startswith(prefix) for prefix in _EXEMPT_PREFIXES)


def _tracked_text_corpus() -> list[tuple[pathlib.Path, str, str, str]]:
    """Every git-tracked file that decodes as UTF-8 text, except the shared
    tool and the exempt paths/prefixes above. Returns (absolute path,
    repo-relative posix path, suffix, text) tuples."""
    shared_tool_resolved = _SHARED_TOOL.resolve()
    corpus: list[tuple[pathlib.Path, str, str, str]] = []
    for path in _git_tracked_all():
        if ".git" in path.parts:
            continue
        if path.resolve() == shared_tool_resolved:
            continue
        rel_path = _rel(path)
        if _is_exempt(rel_path):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        corpus.append((path, rel_path, path.suffix, text))
    return corpus


def test_scan_is_non_vacuous() -> None:
    """A broken glob (wrong root) or a scan that silently walked zero files
    would make the real assertion below pass on an empty set -- pin a floor
    so that reads as a failure, not a clean bill of health (a scan that
    checked nothing is a failure, not a pass)."""
    corpus = _tracked_text_corpus()
    assert len(corpus) >= 1000, (
        f"only found {len(corpus)} tracked, text-decodable files under {REPO_ROOT} -- "
        "the scan may be broken (wrong root, exemption list swallowing "
        "everything) rather than the repo genuinely shrinking that far"
    )
    # Markdown must genuinely be in scope, not merely counted.
    md_count = sum(1 for _, rel, suffix, _ in corpus if suffix == ".md")
    assert md_count >= 20, (
        f"only found {md_count} tracked, non-exempt .md files -- markdown "
        "must be in scope per RDR-219 Phase 3 Step 2a"
    )


def test_exempt_list_is_exactly_this() -> None:
    """Pins the closed exemption list (module docstring "THE EXEMPTION
    LIST") -- an uncontrolled addition here silently widens what this lint
    will never check."""
    assert _EXEMPT_PATHS == frozenset({
        "tests/test_claude_credentials_single_source_lint.py",
        "CHANGELOG.md",
        "tests/test_routing_credential_print_guard.py",
    })
    assert _EXEMPT_PREFIXES == ("docs/rdr/",)


def test_shared_tool_exists_and_is_excluded_from_the_scan() -> None:
    assert _SHARED_TOOL.is_file(), (
        f"expected the shared picker at {_SHARED_TOOL} -- if it moved, "
        "update _SHARED_TOOL here too"
    )
    assert _SHARED_TOOL.resolve() not in {p for p, _, _, _ in _tracked_text_corpus()}


# ---------------------------------------------------------------------------
# Category 2: reading a forbidden keychain item (original shape, widened to
# the second item).
# ---------------------------------------------------------------------------


def test_detector_flags_the_pre_fix_auth_login_shape() -> None:
    """Kill control, proved on a synthetic fixture -- never on a real repo
    file, so this can never pass vacuously because the tree happens to
    already be clean. Reproduces the EXACT line ``tests/e2e/auth-login.sh``
    carried before this fix (double-quoted service argument)."""
    synthetic = (
        'creds=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null || true)\n'
    )
    hits = _violations(synthetic)
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_the_pre_fix_run_sh_shape() -> None:
    """The single-quoted variant `tests/e2e/migration-rehearsal/run.sh`
    carried in both the --fullstack and --shakeout-e2e legs before this
    fix."""
    synthetic = (
        "FRESHCREDS=\"$(security find-generic-password -s 'Claude Code-credentials' -w 2>/dev/null || true)\"\n"
    )
    hits = _violations(synthetic)
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_a_python_subprocess_list_argv() -> None:
    """The same call assembled as a Python list-literal argv, never a
    contiguous shell substring, must be caught by the same pattern."""
    synthetic = (
        'subprocess.run(["security", "find-generic-password", "-s", '
        '"Claude Code-credentials", "-w"])\n'
    )
    hits = _violations(synthetic, rel_path="src/example.py", suffix=".py")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_automation_token_keychain_read_outside_the_shared_tool() -> None:
    """Widened shape: reading the OTHER forbidden item -- the harness's own
    automation identity -- is also forbidden outside the shared tool. A
    positive control in a `.py`-shaped file, per the bead's required kill
    control."""
    synthetic = (
        'subprocess.run(["security", "find-generic-password", "-a", user, '
        '"-s", "nexus-automation-oauth-token", "-w"])\n'
    )
    hits = _violations(synthetic, rel_path="src/example.py", suffix=".py")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_ignores_automation_token_read_at_the_shared_tools_own_path() -> None:
    """The identical call shape, attributed to the shared tool's own
    repo-relative path, must NOT be flagged -- it is the one legitimate
    reader."""
    synthetic = (
        'subprocess.run(["security", "find-generic-password", "-a", user, '
        '"-s", "nexus-automation-oauth-token", "-w"])\n'
    )
    hits = _violations(synthetic, rel_path=_rel(_SHARED_TOOL), suffix=".py")
    assert hits == []


def test_detector_ignores_comment_lines() -> None:
    """A comment documenting the forbidden shape (exactly what the fixed
    files now carry) must not itself trip this lint."""
    synthetic = (
        "# never a bare, unscoped `security find-generic-password -s "
        "'Claude Code-credentials' -w` -- see CRED_TOOL note above\n"
    )
    assert _violations(synthetic) == []


def test_detector_ignores_the_shared_tool_own_call_shape() -> None:
    """The shared tool's own real call builds the service name as an argv
    element referencing a module-level constant, not the literal string on
    the same line as `find-generic-password` -- this proves the detector
    does not accidentally flag that shape too."""
    synthetic = (
        'SERVICE = "Claude Code-credentials"\n'
        'cmd = ["security", "find-generic-password", "-s", SERVICE]\n'
    )
    assert _violations(synthetic, rel_path="src/example.py", suffix=".py") == []


def test_detector_ignores_keychain_item_split_across_two_lines_by_word_wrap() -> None:
    """Markdown prose word-wraps a backtick span across a line break (as
    `tests/cc-validation/README.md` does): each physical line then contains
    only ONE of the two required substrings, never both on the same line --
    this must not be flagged, matching this lint's existing line-at-a-time
    scanning."""
    synthetic = (
        "Keychain item (`Claude Code-credentials`) via a bare, unscoped `security\n"
        "find-generic-password` and cached the result at\n"
    )
    assert _violations(synthetic, rel_path="docs/example.md", suffix=".md") == []


# ---------------------------------------------------------------------------
# Category 1: writing a `.credentials.json` file.
# ---------------------------------------------------------------------------


def test_detector_flags_credentials_json_write_in_a_shell_script() -> None:
    """Required positive control: a planted `.credentials.json` write in a
    `.sh` file fails the lint."""
    synthetic = 'cp "$SRC" ~/.claude/.credentials.json\n'
    hits = _violations(synthetic, rel_path="tests/e2e/example.sh", suffix=".sh")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_credentials_json_write_in_a_markdown_recipe() -> None:
    """Required positive control: a planted `.credentials.json` write inside
    a markdown fenced recipe fails the lint."""
    synthetic = (
        "Run this to seed a local copy:\n\n"
        "```bash\n"
        "cp foo ~/.claude/.credentials.json\n"
        "```\n"
    )
    hits = _violations(synthetic, rel_path="docs/example.md", suffix=".md")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_credentials_json_redirect() -> None:
    synthetic = 'echo "$TOKEN_JSON" > "$HOME/.claude/.credentials.json"\n'
    hits = _violations(synthetic, rel_path="tests/e2e/example.sh", suffix=".sh")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_credentials_json_write_via_python_write_text() -> None:
    synthetic = 'pathlib.Path(dest, ".credentials.json").write_text(payload)\n'
    hits = _violations(synthetic, rel_path="src/example.py", suffix=".py")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_ignores_credentials_json_prose_and_assertions() -> None:
    """The exact shapes this repo's own RDR-219 test suite carries: an
    assert checking the string is ABSENT, an error message using the
    English word "write"/"writes" (not a `.write(` call), and this repo's
    own `tests/e2e/run_sh_credentials_test.sh` -- a hand-written checker
    whose grep PATTERN mentions `cp` and `.credentials.json` together as
    DATA (a regex string), never as a live invocation (no real whitespace
    follows the `cp` inside that pattern string)."""
    synthetic = (
        'assert ".credentials.json" not in script_text\n'
        '"the ladder must never write a home/.claude/.credentials.json file"\n'
        "if grep -qE '(cp\\s+|>\\s*\"?\\$?\\{?TEST_HOME|-f\\s+\"?\\$)[^#]*\\.credentials\\.json' \"$TARGET\"; then\n"
    )
    assert _violations(synthetic, rel_path="tests/example.py", suffix=".py") == []
    assert _violations(synthetic, rel_path="tests/e2e/example.sh", suffix=".sh") == []


def test_detector_ignores_credentials_json_mentioned_only_inside_backticks() -> None:
    """A `.py`/`.md` docstring naming the forbidden shape inline, inside a
    single pair of backticks, is documentation -- the exact shape
    `tests/scripts/test_hook_surface_shakeout_automation_token.py` and
    `tests/cc-validation/README.md` carry."""
    synthetic = (
        "the `cp /creds/.credentials.json ...` copy into the container's "
        "own `.claude` directory\n"
    )
    assert _violations(synthetic, rel_path="tests/example.py", suffix=".py") == []
    assert _violations(synthetic, rel_path="docs/example.md", suffix=".md") == []


# ---------------------------------------------------------------------------
# Category 3: assigning CLAUDE_CODE_OAUTH_TOKEN a literal.
# ---------------------------------------------------------------------------


def test_detector_flags_token_literal_assignment_quoted() -> None:
    synthetic = 'CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-not-a-real-token" claude -p "hi"\n'
    hits = _violations(synthetic, rel_path="tests/e2e/example.sh", suffix=".sh")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_token_literal_assignment_bare() -> None:
    synthetic = "env -i CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-not-a-real-token claude\n"
    hits = _violations(synthetic, rel_path="tests/e2e/example.sh", suffix=".sh")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_flags_token_literal_assignment_docker_dash_e() -> None:
    synthetic = 'docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-abc image\n'
    hits = _violations(synthetic, rel_path="tests/e2e/example.sh", suffix=".sh")
    assert len(hits) == 1, f"expected 1 flagged line, got {hits}"


def test_detector_ignores_token_variable_expansion() -> None:
    """The allowed shapes: `$VAR`, `${VAR}`, `$(cmd)`, and the bare `-e
    NAME` docker shape (no `=value` at all, reads from the invoking
    process's own environment -- this repo's actual, correct shape)."""
    synthetic = (
        "CLAUDE_CODE_OAUTH_TOKEN=$TOKEN claude\n"
        'CLAUDE_CODE_OAUTH_TOKEN="${TOKEN}" claude\n'
        "CLAUDE_CODE_OAUTH_TOKEN=$(fetch_token) claude\n"
        'docker run --rm -e CLAUDE_CODE_OAUTH_TOKEN image\n'
    )
    assert _violations(synthetic, rel_path="tests/e2e/example.sh", suffix=".sh") == []


def test_detector_ignores_token_env_dict_equality_check() -> None:
    """`env["CLAUDE_CODE_OAUTH_TOKEN"] == _FAKE_TOKEN` is a comparison, not
    an assignment -- no `=` immediately follows the name (only `] ==`)."""
    synthetic = 'assert env["CLAUDE_CODE_OAUTH_TOKEN"] == _FAKE_TOKEN\n'
    assert _violations(synthetic, rel_path="tests/example.py", suffix=".py") == []


def test_detector_ignores_token_absence_assertion() -> None:
    """`"CLAUDE_CODE_OAUTH_TOKEN=" not in run_sh` is an assertion that the
    shape is ABSENT -- the empty "value" a naive quote-strip would produce
    must not be treated as a literal."""
    synthetic = 'assert "CLAUDE_CODE_OAUTH_TOKEN=" not in run_sh\n'
    assert _violations(synthetic, rel_path="tests/example.py", suffix=".py") == []


def test_detector_ignores_token_mentioned_only_inside_backticks() -> None:
    """The exact docstring shape `tests/test_run_ladder_credentials.py`
    carries: a backtick-quoted illustration of the pre-fix bad shape,
    inside prose explaining what must never happen."""
    synthetic = (
        "not a literal `env -i CLAUDE_CODE_OAUTH_TOKEN=...` argument to a "
        "transient `env` process, and not a tmux client argv like "
        "`-e CLAUDE_CODE_OAUTH_TOKEN=...`.\n"
    )
    assert _violations(synthetic, rel_path="tests/example.py", suffix=".py") == []


# ---------------------------------------------------------------------------
# The full, real-repo scan.
# ---------------------------------------------------------------------------


def test_no_tracked_source_violates_the_single_source_of_credentials() -> None:
    bad: list[str] = []
    for _, rel_path, suffix, text in _tracked_text_corpus():
        for line in _violations(text, rel_path=rel_path, suffix=suffix):
            bad.append(f"{rel_path}: {line}")
    assert not bad, (
        "tracked file(s) write a .credentials.json file, read a forbidden "
        "keychain item, or assign CLAUDE_CODE_OAUTH_TOKEN a literal -- this "
        "can silently select an arbitrary keychain item (nexus-galkv.19, "
        "nexus-qs1g6) or put the token on argv/into a file (RDR-219): route "
        "through tests/e2e/lib/claude_credentials.py's `run`/`status` "
        "instead:\n" + "\n".join(bad)
    )
