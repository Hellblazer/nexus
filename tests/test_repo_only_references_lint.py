# SPDX-License-Identifier: AGPL-3.0-or-later
"""Text that reaches a user, or a user's model, names no file only a nexus checkout has (nexus-z7ddd).

A user who installed ``conexus`` from PyPI and the plugin from the marketplace has no ``docs/``, ``tests/``,
``scripts/``, ``src/nexus/`` or ``service/`` tree. On 2026-11-04 a census of the 7.95.0 release candidate found
the plugin markdown, the resource files an agent opens with ``Read``, an MCP tool description and the CLI's
help and error text all sending that user to such a file. A reference they cannot open costs a wasted tool call
at best and, when an agent is told to run the file first, a failed task (the preflight script, nexus-z7ddd).

What counts as a channel, and how each is checked:

- **Plugin markdown** (``conexus/``, ``sn/``, ``prose-edit/``, ``tool-spill/``): every ``.md`` file, maintainer
  docs included, bar the two release ledgers. A repo path becomes a GitHub link or inline text.
- **Plugin files that are not markdown or Python** (JSON, YAML, plist, unit files, shell, TypeScript): the text a
  user can open, a comment in an installed plist included.
- **Files an agent opens with** ``Read``: Claude Code does not substitute ``${CLAUDE_PLUGIN_ROOT}`` in a file read
  that way (measured by the out-of-repo gate's canary on 2.1.318), fenced or not, and does not substitute the
  unbraced ``$CLAUDE_PLUGIN_ROOT`` anywhere. So a resource file, a skill's ``reference.md`` or other non-``SKILL.md``
  file, and a hook-printed ``.md`` hold no variable at all; they name a plugin file by a path relative to
  themselves. The unbraced ``$CLAUDE_PLUGIN_ROOT`` is banned in every plugin markdown file, except that a README
  (written for people, never opened by an agent) may name the variable in prose; a README path built on it is
  still flagged.
- **MCP tool descriptions and parameter descriptions**, from the live tool registry of both servers.
- **CLI help and option help**, from the live click command tree. A ``\\b`` example block counts like prose: an
  example that names a repo file misleads exactly as a sentence does. A URL must also survive click's wrapping
  at 80 and at 60 columns, counted per occurrence (see ``test_every_url_in_cli_help_renders_unbroken``): the same
  URL intact in an epilog does not excuse a copy broken in the body.
- **Every other string constant in ``src/nexus``** (error and hint text, ``click.echo`` text, doctor
  suggestions), found by AST. The same scan covers the plugins' Python (hook and launcher text). Docstrings and
  bare string statements are developer text, comments are not in the AST at all; the click and MCP docstrings
  that DO reach a user are covered by the two runtime scans above.
- **Engine strings**: every string literal in ``service/src/main/java`` (boot refusals, error bodies), the
  ``RAISE`` messages in the Liquibase changelogs, which reach an operator who has no checkout, and the ``doc:``
  blocks of the tuple templates (``service/src/main/resources/tuples/templates/*.yaml``), which
  ``tuple_registry(doc=true)`` hands to the user's model.
- **Packaged files under** ``src/nexus`` and ``mcpb/`` (``_install/*.sh`` echo text, ``*.md``, ``*.html``,
  ``*.toml`` and ``*.txt`` data files, the bundle's ``manifest.json``) and the bundle's Python. A ``#`` comment in a
  shell, TOML or text file is developer text; the rest can print.

A path is repo-only when it starts at a top-level directory of the nexus checkout and names something that exists
there (or has a file extension). Besides the plain ``tests/x.py`` form the scan sees: ``../tests/x.py`` (exists in
the checkout), ``$REPO/scripts/x.sh`` (a variable named for the repo or a checkout), ``~/git/nexus/scripts/x.sh``
and ``/opt/nexus/scripts/x.sh`` (a path ending in ``nexus/``), ``{x}/scripts/x.sh`` and a string that begins
``/scripts/`` (the tail of an f-string or a joined path), ``nexus/scripts/x.sh`` relative to anywhere,
``$PWD/scripts/x.sh`` and ``%CD%\\scripts\\x.sh`` (the working directory, as ``./`` is), ``%REPO%\\scripts\\x.sh``,
a Windows spelling (``tests\\x.py``), a path behind ``@`` or ``key:`` (``@docs/x.md``, ``path:scripts/x.sh``), a
glob with a name prefix (``src/nexus/tables/target-family-*.toml``), an existing nexus directory under ``docs/``
(``docs/agents/``; ``docs/exploration/`` and ``docs/runbooks/`` are the prose-edit genre map's, so they pass), a
bare name of one of the repo's own scripts (``mvnw-leased.sh``), and a root-level file (``AGENTS.md``) written in
the checkout's own terms. A URL with a period or a colon glued on is flagged too. ``docs/rdr/`` is
every project's own RDR directory and is left alone, except a concrete nexus RDR file that exists here; a path
inside a GitHub URL is left alone. Deliberate sites are in ``_ALLOWED``, each one line with the marker text that
identifies it and the reason an installed user is not misled; the live checks print what they scanned and floor it
(nexus-moht0: a scan that found nothing to check is a failure, not a pass).

**Why the links say ``blob/main``.** A plugin text and the file it links reach ``main`` in the same release (a
release promotes ``develop`` to ``main``), and an installed plugin is a release, so the link resolves for as
long as the file stays put. A tag-pinned link would 404 for any file added after the tag; a ``develop`` link
would 404 for a release that never carried it. The cost is a link that goes dead when a file later moves, which
this lint cannot see from an old install; ``test_every_github_link_this_lint_asks_for_names_something_in_the_tree``
catches the move on the PR that makes it.
"""
from __future__ import annotations

import ast
import functools
import importlib
import re
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import pytest

if TYPE_CHECKING:
    import click

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Top-level directories of the checkout that an installed user does not have. ``src/nexus`` rather than ``src``:
#: ``src/main/java/...`` is a generic example path.
_ROOTS = (
    "src/nexus", "tests", "scripts", "docs", "service", "conexus", "sn", "prose-edit", "tool-spill",
    ".claude", ".github", "web", "mcpb", "infra", "bench",
    "data", "dt", "archive", "papers", "blog", ".beads", ".plans", ".devcontainer",
)
#: These hold pages or names that running text uses for the USER'S own conventions (the prose-edit genre map,
#: ``docs/rdr/``, a project's ``data/`` or ``.beads/`` directory): only an exact existing file counts there,
#: never a directory or a glob.
_FILE_ONLY_ROOTS = frozenset({"docs", "web", "data", "dt", "archive", "papers", "blog", ".beads", ".plans", ".devcontainer"})
_USERS_OWN_DIRS = frozenset({"docs/exploration", "docs/runbooks"})
_FILE_EXTENSIONS = frozenset({".md", ".py", ".sh", ".yaml", ".yml", ".xml", ".toml", ".json", ".txt", ".html", ".java", ".sql"})
#: Root-level files of the checkout. Every project has some of these, so one counts only in text that says it is
#: the nexus checkout's own (see ``_ROOT_FILE_CONTEXT``).
_ROOT_FILES = ("AGENTS.md", "CLAUDE.md", "CHANGELOG.md", "pyproject.toml", "uv.lock", "LICENSING.md")
_ROOT_FILE_CONTEXT = re.compile(r"(?i)\bnexus\b")

_ROOT_ALT = "|".join(re.escape(r) for r in sorted(_ROOTS, key=len, reverse=True))
_SEG = r"[\w.*<>{}\[\]-]+"
_PATH = re.compile(r"(?P<p>(?:" + _ROOT_ALT + r")/" + _SEG + r"(?:/" + _SEG + r")*/?)")
_ROOT_FILE = re.compile(r"(?<![\w/.\-])(?P<p>" + "|".join(re.escape(f) for f in _ROOT_FILES) + r")(?![\w/\-])")
_BARE_SCRIPT = re.compile(r"(?<![\w/.\-])(?P<p>[A-Za-z0-9][\w.-]*\.(?:sh|py))(?![\w/\-])")
#: A markdown link to a URL: label and target both leave the scan (a label may show the path it links).
_URL_LINK = re.compile(r"\[[^\]]*\]\(https?://[^)]*\)")
_URL = re.compile(r"https?://\S+")
#: A URL that ends in a period or a colon: the sentence's punctuation glued to it pastes into a browser as part of
#: the address (``...engine-reaper.md: the refusal table`` pasted as ``...engine-reaper.md:``).
#: A colon counts only when something follows it: a string constant that ends ``http://127.0.0.1:`` is the front of
#: an f-string whose port comes next.
_URL_GLUED = re.compile(r"https?://[^\s\"'<>()\[\]`]*[a-zA-Z0-9/_-](?:\.(?=$|[\s\"')\]`])|:(?=[\s\"')\]`]))")
_LINE_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|::\w+)$")
#: A placeholder or variable that names the user's own place, not the checkout.
_USERS_OWN = re.compile(r"(?i)project|plugin|home|cwd|user|tmp|temp|work")
_REPO_VAR = re.compile(r"(?i)repo|checkout|nexus")
#: A working-directory variable: ``$PWD/scripts/x.sh`` means the same as ``./scripts/x.sh`` and ``scripts/x.sh``.
_CWD_VAR = re.compile(r"(?i)^(?:PWD|CWD|OLDPWD|CD)$")
#: What may stand directly before a path: ``@docs/x.md`` (an at-mention) and ``path:scripts/x.sh`` (a key) included.
_DELIMS = " \t\r\n`'\"([{<,;=|*>@:"


@functools.cache
def _own_script_names(repo_root: Path = REPO_ROOT) -> frozenset[str]:
    """Bare file names of the repo's own scripts: ``scripts/`` and the e2e helper library. A name that a plugin
    also ships (``install.sh``) is the plugin's, not the repo's."""
    names: set[str] = set()
    for d in ("scripts", "tests/e2e/lib"):
        for p in (repo_root / d).rglob("*"):
            if p.is_file() and p.suffix in (".sh", ".py") and not p.name.startswith("_") and "__pycache__" not in p.parts:
                names.add(p.name)
    shipped = {
        p.name
        for d in ("conexus", "sn", "prose-edit", "tool-spill", "src/nexus", "mcpb")
        for p in (repo_root / d).rglob("*")
        if p.is_file() and p.suffix in (".sh", ".py")
    }
    return frozenset(names - shipped)


def _prefix_kind(text: str, start: int) -> str | None:
    """How the root-directory match at *start* is attached to what precedes it.

    ``plain`` a free-standing path; ``up`` after ``../`` (counts only when it exists in the checkout);
    ``joined`` a tail glued to a placeholder, a leading ``/`` or a variable; ``checkout`` under a path that ends
    in ``nexus/``; ``None`` the middle of some other word or path (``hooks/scripts/x``, ``~/scripts/x``).
    """
    if start == 0:
        return "plain"
    prev = text[start - 1]
    if prev in _DELIMS:
        return "plain"
    if prev != "/":
        return None
    before = text[: start - 1]
    if before == "" or before[-1] in _DELIMS:
        return "joined"
    if re.search(r"(?:^|[^\w.])\.\.$", before):
        return "up"
    if re.search(r"(?:^|[^\w.])\.$", before):
        return "plain"
    if before.endswith(("}", ">")):
        m = re.search(r"\$?[{<]([^{}<>]*)[}>]$", before)
        name = m.group(1) if m else ""
        if _CWD_VAR.match(name):
            return "plain"
        return None if _USERS_OWN.search(name) else "joined"
    # A Windows variable, ``%REPO%\scripts\x.sh`` (the backslashes are already slashes here).
    m = re.search(r"(?:^|[^\w%])%([A-Za-z_]\w*)%$", before)
    if m:
        name = m.group(1)
        if _CWD_VAR.match(name):
            return "plain"
        return "joined" if _REPO_VAR.search(name) and not name.startswith("CLAUDE_") else None
    m = re.search(r"\$([A-Za-z_]\w*)$", before)
    if m:
        name = m.group(1)
        if _CWD_VAR.match(name):
            return "plain"
        return "joined" if _REPO_VAR.search(name) and not name.startswith("CLAUDE_") else None
    if before.endswith("~"):
        return None
    m = re.search(r"([~/][\w.\-/~]*)$", before)
    if m and re.search(r"(?:^|/)nexus(?:-wt/[\w.\-]+)?$", m.group(1)):
        return "checkout"
    # ``nexus/scripts/x.sh``: the checkout named by its directory, relative to somewhere.
    if re.search(r"(?:^|[\s`'\"(\[{<,;=|*>@:])nexus(?:-wt/[\w.\-]+)?$", before):
        return "checkout"
    return None


def _is_repo_only(token: str, repo_root: Path = REPO_ROOT, *, exists_only: bool = False) -> bool:
    t = token.rstrip(".,;:)'\"`")
    t = _LINE_SUFFIX.sub("", t)
    for mark in ("*", "<", "{", "["):
        if mark in t:
            t = t[: t.index(mark)]
            if mark == "*" and not t.endswith("/") and "/" in t:
                # A glob with a name prefix (``src/nexus/tables/target-family-*.toml``): its directory holds the match.
                t = t.rsplit("/", 1)[0]
    t = t.rstrip("/")
    if "/" not in t:
        return False
    path = repo_root / t
    if t == "docs/rdr" or t.startswith("docs/rdr/"):
        # Every project has its own docs/rdr/. A concrete nexus RDR file that exists here is still ours.
        return bool(re.match(r"docs/rdr/(?:rdr-)?\d{3}[-\w]*\.md$", t)) and path.is_file()
    if t.split("/", 1)[0] in _FILE_ONLY_ROOTS:
        # A directory counts too when it is a nexus one (``docs/agents/``); the prose-edit genre map names
        # ``docs/exploration/`` and ``docs/runbooks/`` for the USER'S own repository, so those names are theirs.
        return path.is_file() or (path.is_dir() and t not in _USERS_OWN_DIRS and not exists_only)
    if exists_only:
        return path.exists()
    return path.exists() or Path(t).suffix in _FILE_EXTENSIONS


def repo_only_paths(text: str, repo_root: Path = REPO_ROOT) -> list[str]:
    """The repo-only paths in *text*, as written."""
    text = _URL_LINK.sub(" ", text)
    text = _URL.sub(" ", text)
    # A Windows spelling (``tests\\x.py``, ``%REPO%\\scripts\\x.sh``) names the same file. A backslash escape such as
    # ``\\n`` in a string constant becomes a slash too, which cannot form a path that exists.
    text = text.replace("\\", "/")
    found: list[str] = []
    for m in _PATH.finditer(text):
        kind = _prefix_kind(text, m.start())
        if kind is not None and _is_repo_only(m.group("p"), repo_root, exists_only=(kind == "up")):
            found.append(m.group("p"))
    for m in _ROOT_FILE.finditer(text):
        if _ROOT_FILE_CONTEXT.search(text) and (repo_root / m.group("p")).is_file():
            found.append(m.group("p"))
    names = _own_script_names(repo_root)
    found += [m.group("p") for m in _BARE_SCRIPT.finditer(text) if m.group("p") in names]
    return found


def glued_url_tails(text: str) -> list[str]:
    """URLs in *text* that end in a sentence's closing period."""
    stripped = _URL_LINK.sub(" ", text)
    return _URL_GLUED.findall(stripped)


def scan_tokens(text: str, repo_root: Path = REPO_ROOT) -> list[str]:
    """Everything in *text* a user cannot use: repo-only paths and URLs with punctuation glued on."""
    return repo_only_paths(text, repo_root) + [f"{u} (period glued to the URL)" for u in glued_url_tails(text)]


def _fenced_lines(lines: list[str]) -> set[int]:
    """0-based indexes of the lines inside a fenced block (the fences included)."""
    inside: set[int] = set()
    start: int | None = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            if start is None:
                start = i
            else:
                inside.update(range(start, i + 1))
                start = None
    if start is not None:
        inside.update(range(start, len(lines)))
    return inside


class Site(NamedTuple):
    """One deliberate repo path: the token, text on its line that picks the site out, and why it is harmless."""

    token: str
    marker: str
    reason: str


class Hit(NamedTuple):
    rel: str
    lineno: int
    token: str
    text: str


#: Deliberate repo paths, one line each: {repo-relative file: [Site]}. A site matches a hit when the token is the
#: same and the marker occurs in the hit's line (a Python string constant is one "line"). A guarded conditional
#: names a path that the project has or has not ("when the project has X"), so a project without it takes the
#: other branch; the user's own checkout path is the user's.
_ALLOWED: dict[str, list[Site]] = {
    "prose-edit/README.md": [
        Site(".claude/skills/site-page/SKILL.md", "A repository with `.claude/skills/site-page/SKILL.md` gets", "names the optional file a USER'S repository may carry; the sentence says a repository without it is fine"),
    ],
    "src/nexus/hooks/subagent_start.py": [
        Site("scripts/agent-worktree-preflight.sh", "run `scripts/agent-worktree-preflight.sh [required-sha]`", "the Preflight row text; emitted only when the project has the script (_worktree_preflight_row checks os.path.isfile)"),
        Site("scripts/agent-worktree-preflight.sh", "_WORKTREE_PREFLIGHT_SCRIPT: str =", "the path the hook probes with os.path.isfile in the project's directory, never printed on its own"),
    ],
    "src/nexus/catalog/types.py": [
        Site("tests/fixtures", 'FIXTURE_PATH_MARKER: str = "tests/fixtures"', "a path-fragment marker matched against indexed file paths, never shown"),
    ],
    "src/nexus/target_installer.py": [
        Site("src/nexus/_install", 'for rel in ("nexus/_install", "src/nexus/_install")', "a candidate directory probed relative to a root, never shown"),
    ],
    "src/nexus/tables/chunk_metadata_keys.py": [
        Site("src/nexus/catalog/note_ttl.py", "LEGACY_FALLBACK_MODULES: Final", "a module path compared with the repo's own scan results, never shown"),
    ],
    "src/nexus/pdf_stack.py": [
        Site("scripts/reinstall-tool.sh", "/scripts/reinstall-tool.sh", "the prefix is the install receipt's own checkout directory; reached only for an install built from a directory source, whose owner has that checkout"),
    ],
    "src/nexus/upgrade_finish.py": [
        Site("scripts/reinstall-tool.sh", "/scripts/reinstall-tool.sh", "the prefix is the uv receipt's own checkout directory; reached only for an install built from a local checkout"),
    ],
    "src/nexus/health.py": [
        Site("reinstall-tool.sh", "in a nexus checkout's scripts directory", "a second fix suggestion, labelled as the from-a-checkout alternative to the line before it, and qualified as living in a nexus checkout"),
    ],
    "src/nexus/commands/self_cmd.py": [
        Site("reinstall-tool.sh", "a dev checkout uses", "named for the dev-checkout case only, in a sentence conditional on running from one"),
        Site("reinstall-tool.sh", "For a checkout, run", "printed when nx runs from a source checkout, where the script is in the checkout's scripts directory"),
    ],
    "conexus/hooks/scripts/routing/credential_print_guard.py": [
        Site("claude_credentials.py", "blob/main/tests/e2e/lib/claude_credentials.py", "the message links the wrapper in its first sentence; the later mentions are the same file"),
    ],
    "src/nexus/storage_boundary_lint.py": [
        # Path constants the storage-boundary lint matches against its own checkout; none is printed.
        Site("src/nexus/catalog/", "CATALOG_PHASE_ALLOWLIST_PREFIX", "a prefix matched against scanned file paths, never shown"),
        Site("src/nexus/db/", '"src/nexus/db/",', "a prefix matched against scanned file paths, never shown"),
        Site("src/nexus/catalog/", '"src/nexus/catalog/",', "a prefix matched against scanned file paths, never shown"),
        Site("src/nexus/daemon/", '"src/nexus/daemon/",', "a prefix matched against scanned file paths, never shown"),
        *(
            Site(path, f'"{path}": ', "a key of an allowlist dict compared with the scan's own file paths, never shown")
            for path in (
                "src/nexus/collection_health.py", "src/nexus/context.py", "src/nexus/mcp_infra.py",
                "src/nexus/commands/_helpers.py", "src/nexus/commands/aspects.py", "src/nexus/commands/catalog.py",
                "src/nexus/commands/catalog_cmds/report.py", "src/nexus/commands/doc.py", "src/nexus/commands/enrich.py",
                "src/nexus/commands/index.py", "src/nexus/commands/rdr.py", "src/nexus/commands/search_cmd.py",
                "src/nexus/commands/taxonomy_cmd.py",
            )
        ),
    ],
    "conexus/commands/continuation.md": [
        Site("tests/e2e/lib/commit_scope_audit.sh", "when the project has", "a guarded conditional: the script runs only in a project that has it; any other project takes the by-hand branch on the same line"),
        Site("tests/e2e/lib/commit_scope_audit.sh", "<session-start-ref>..", "the command for the branch the conditional names; reached only when the project has the script"),
    ],
    "conexus/skills/composition-probe/SKILL.md": [
        Site("scripts/mvnw-leased.sh", "when the project has that script", "a guarded conditional: used only in a project that has the script; any other project takes the ./mvnw branch in the same paragraph"),
        Site("service/mvnw", "wraps `service/mvnw`", "describes what the nexus script wraps, inside the same guarded conditional"),
    ],
}

#: The two release ledgers name repo paths on purpose: one is the changelog of what changed, the other the list of
#: files that differ from the pinned tag.
_MARKDOWN_EXEMPT = frozenset({"conexus/CHANGELOG.md", "conexus/PENDING_RELEASE.md"})
#: Eval case directories (``conexus/evals/<case>/prompt.md`` and its graders) are fixtures the eval runner plays in a
#: nexus checkout, against nexus's own code: their prompts and grading criteria name that code on purpose and no
#: user's session reads them. The directory's README is maintainer prose and stays in scope.
_EVAL_CASE_PREFIX = "conexus/evals/"
_EVAL_README = "conexus/evals/README.md"
_PLUGIN_DIRS = ("conexus", "sn", "prose-edit", "tool-spill")
#: Directories whose Python ships to a user besides ``src/nexus`` (the .mcpb bundle included).
_SHIPPED_PYTHON_DIRS = ("conexus", "sn", "prose-edit", "tool-spill", "mcpb")

#: Plugin files that are not markdown or Python and that a user can open.
_PLUGIN_TEXT_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".plist", ".service", ".txt", ".toml"})
#: Plugin source whose comment lines are developer text but whose other lines print.
_PLUGIN_CODE_SUFFIXES = frozenset({".sh", ".ts", ".js"})
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".venv", "dist"})
#: Non-Python files that ship inside the ``nexus`` package or the .mcpb bundle. A ``#`` comment in a ``.sh``, ``.toml``
#: or ``.txt`` file is developer text; every other line can print (shell echo, an HTML template, a data file).
_PACKAGED_SUFFIXES = frozenset({".sh", ".md", ".toml", ".txt", ".html"})


def _walk(root: Path, suffixes: frozenset[str] | tuple[str, ...]) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix in suffixes and not (_SKIP_DIRS & set(p.relative_to(root).parts)):
            out.append(p)
    return out


def _is_comment(line: str) -> bool:
    s = line.strip()
    return s.startswith(("#", "//", "/*", "*"))


def _apply_allowed(hits: list[Hit]) -> tuple[list[str], set[tuple[str, int]]]:
    """(violations as ``file:line: token``, the (file, index) of every allowlist site that matched)."""
    bad: list[str] = []
    used: set[tuple[str, int]] = set()
    for h in hits:
        sites = _ALLOWED.get(h.rel, [])
        matched = [i for i, s in enumerate(sites) if s.token == h.token and s.marker in h.text]
        if matched:
            used.update((h.rel, i) for i in matched)
        else:
            bad.append(f"{h.rel}:{h.lineno}: {h.token}")
    return bad, used


# --------------------------------------------------------------------------------------------------------------
# Raw hits per channel
# --------------------------------------------------------------------------------------------------------------

def _plugin_markdown(repo_root: Path = REPO_ROOT) -> list[Path]:
    files = []
    for d in _PLUGIN_DIRS:
        files += sorted((repo_root / d).rglob("*.md"))
    rels = {p: p.relative_to(repo_root).as_posix() for p in files}
    return [
        p for p, rel in rels.items()
        if rel not in _MARKDOWN_EXEMPT
        and not (rel.startswith(_EVAL_CASE_PREFIX) and rel != _EVAL_README)
        and not (_SKIP_DIRS & set(Path(rel).parts))
    ]


def _line_hits(path: Path, repo_root: Path, *, skip_comments: bool = False) -> list[Hit]:
    rel = path.relative_to(repo_root).as_posix()
    hits = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if skip_comments and _is_comment(line):
            continue
        hits += [Hit(rel, lineno, t, line) for t in scan_tokens(line, repo_root)]
    return hits


def _markdown_hits(files: list[Path], repo_root: Path = REPO_ROOT) -> list[Hit]:
    return [h for p in files for h in _line_hits(p, repo_root)]


def _markdown_violations(files: list[Path], repo_root: Path = REPO_ROOT) -> list[str]:
    return _apply_allowed(_markdown_hits(files, repo_root))[0]


def _plugin_text_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    files: list[Path] = []
    for d in _PLUGIN_DIRS:
        files += _walk(repo_root / d, _PLUGIN_TEXT_SUFFIXES | _PLUGIN_CODE_SUFFIXES)
    return [p for p in files if not p.relative_to(repo_root).as_posix().startswith(_EVAL_CASE_PREFIX)]


def _plugin_text_hits(repo_root: Path = REPO_ROOT) -> list[Hit]:
    hits = []
    for p in _plugin_text_files(repo_root):
        hits += _line_hits(p, repo_root, skip_comments=p.suffix in _PLUGIN_CODE_SUFFIXES)
    return hits


def _developer_text_ids(tree: ast.AST) -> set[int]:
    """Ids of string nodes that are developer text: docstrings and any bare string statement."""
    return {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
    }


def _string_hits(src_root: Path, repo_root: Path = REPO_ROOT) -> tuple[list[Hit], int]:
    """(hits, number of string constants examined) over every ``.py`` file under *src_root*."""
    hits: list[Hit] = []
    examined = 0
    for path in sorted(src_root.rglob("*.py")):
        if _SKIP_DIRS & set(path.relative_to(src_root).parts):
            continue
        rel = path.relative_to(repo_root).as_posix()
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)
        skip = _developer_text_ids(tree)
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in skip:
                continue
            examined += 1
            toks = scan_tokens(n.value, repo_root)
            if toks:
                # A site is picked out by the constant or by the source line it sits on.
                line = lines[n.lineno - 1] if n.lineno <= len(lines) else ""
                hits += [Hit(rel, n.lineno, t, n.value + "\n" + line) for t in toks]
    return hits, examined


def _string_violations(src_root: Path, repo_root: Path = REPO_ROOT) -> tuple[list[str], int]:
    hits, examined = _string_hits(src_root, repo_root)
    return _apply_allowed(hits)[0], examined


def _java_string_literals(src: str) -> list[tuple[int, str]]:
    """(line, text) of each string literal and text block in Java source; comments and char literals are skipped."""
    out: list[tuple[int, str]] = []
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
        elif src.startswith('"""', i):
            j = src.find('"""', i + 3)
            j = n if j < 0 else j
            out.append((line, src[i + 3 : j]))
            line += src.count("\n", i, j)
            i = j + 3
        elif c == '"':
            j = i + 1
            while j < n and src[j] != '"' and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out.append((line, src[i + 1 : j]))
            i = j + 1
        elif c == "'":
            j = i + 1
            while j < n and src[j] != "'" and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            i = j + 1
        else:
            i += 1
    return out


_RAISE_MESSAGE = re.compile(r"RAISE\s+(?:EXCEPTION|NOTICE|WARNING|INFO)\s+'((?:[^']|'')*)'", re.IGNORECASE)


def _engine_hits(repo_root: Path = REPO_ROOT) -> tuple[list[Hit], int]:
    """(hits, strings examined) over the engine's Java string literals and its changelogs' ``RAISE`` messages."""
    hits: list[Hit] = []
    examined = 0
    java = repo_root / "service" / "src" / "main" / "java"
    for path in sorted(java.rglob("*.java")):
        rel = path.relative_to(repo_root).as_posix()
        for lineno, text in _java_string_literals(path.read_text(encoding="utf-8")):
            examined += 1
            hits += [Hit(rel, lineno, t, text) for t in scan_tokens(text, repo_root)]
    for path in sorted((repo_root / "service" / "src" / "main" / "resources").rglob("*.xml")):
        rel = path.relative_to(repo_root).as_posix()
        src = path.read_text(encoding="utf-8")
        for m in _RAISE_MESSAGE.finditer(src):
            examined += 1
            lineno = src.count("\n", 0, m.start()) + 1
            hits += [Hit(rel, lineno, t, m.group(1)) for t in scan_tokens(m.group(1), repo_root)]
    # The tuple templates' ``doc:`` blocks reach the user's model through ``tuple_registry(doc=true)``.
    for path in sorted((repo_root / "service" / "src" / "main" / "resources" / "tuples" / "templates").glob("*.yaml")):
        rel = path.relative_to(repo_root).as_posix()
        for lineno, line in _yaml_doc_lines(path.read_text(encoding="utf-8")):
            examined += 1
            hits += [Hit(rel, lineno, t, line) for t in scan_tokens(line, repo_root)]
    return hits, examined


def _yaml_doc_lines(text: str) -> list[tuple[int, str]]:
    """(line number, text) of every non-comment line inside the top-level ``doc:`` block of a template."""
    out: list[tuple[int, str]] = []
    inside = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if line.strip() and not line.startswith((" ", "\t")):
            inside = line.split(":", 1)[0].strip() == "doc"
            continue
        if inside and line.strip() and not line.lstrip().startswith("#"):
            out.append((lineno, line))
    return out


def _packaged_text_hits(repo_root: Path = REPO_ROOT) -> tuple[list[Hit], int]:
    """Shell echo text and markdown data files that ship inside the ``nexus`` package."""
    hits: list[Hit] = []
    # AGENTS.md and CLAUDE.md in a module directory are contributor guidance for work inside a checkout. Hatch
    # packages them with the module, but no session reads them from site-packages and no command prints them.
    files = [
        p for p in _walk(repo_root / "src" / "nexus", _PACKAGED_SUFFIXES)
        if p.name not in ("AGENTS.md", "CLAUDE.md")
    ]
    # The .mcpb bundle (mcpb/manifest.json is what the desktop installer shows, mcpb/pyproject.toml its metadata).
    files += _walk(repo_root / "mcpb", _PACKAGED_SUFFIXES | {".json"})
    for p in files:
        hits += _line_hits(p, repo_root, skip_comments=p.suffix in (".sh", ".toml", ".txt"))
    return hits, len(files)


def _resource_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    """Files the model opens with ``Read`` (or that a hook prints): the variable is never substituted in them."""
    files = set((repo_root / "conexus" / "resources").rglob("*.md"))
    for d in _PLUGIN_DIRS:
        for p in (repo_root / d / "skills").rglob("*.md") if (repo_root / d / "skills").is_dir() else []:
            if p.name != "SKILL.md":
                files.add(p)
        hooks = repo_root / d / "hooks"
        if hooks.is_dir():
            files.update(p for p in hooks.rglob("*.md") if p.name != "README.md")
    return sorted(p for p in files if not (_SKIP_DIRS & set(p.relative_to(repo_root).parts)))


_BRACED_VAR = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}")
_UNBRACED_VAR = re.compile(r"\$CLAUDE_PLUGIN_ROOT(?![\w{])")
_UNBRACED_PATH = re.compile(r"\$CLAUDE_PLUGIN_ROOT/")


def _resource_variable_violations(files: list[Path], repo_root: Path = REPO_ROOT) -> list[str]:
    bad = []
    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if _BRACED_VAR.search(line) or _UNBRACED_VAR.search(line):
                bad.append(f"{rel}:{i}: plugin-root variable in a file the model Reads (never substituted there)")
    return bad


def _unbraced_variable_violations(files: list[Path], repo_root: Path = REPO_ROOT) -> list[str]:
    bad = []
    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # A README is for people: naming the variable in prose harms no one, a path built on it does.
            if (_UNBRACED_PATH if path.name == "README.md" else _UNBRACED_VAR).search(line):
                bad.append(f"{rel}:{i}: unbraced $CLAUDE_PLUGIN_ROOT (Claude Code substitutes only the braced form)")
    return bad


# --------------------------------------------------------------------------------------------------------------
# Planted violations: every rule fires (nexus-moht0)
# --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "Rules: docs/collections.md.",
        "run scripts/reinstall-tool.sh",
        "see tests/test_plugin_structure.py::TestX",
        "see src/nexus/search_engine.py:149",
        "the file `conexus/registry.yaml`",
        "the file ./scripts/mvnw-leased.sh now",
        "under service/src/main/resources/db/changelog/ add",
        "the rule .claude/rules/releases-and-engine.md",
        "dir tests/e2e/** is scanned",
        "a new file tests/brand_new_helper.py",
        "up one: ../../scripts/reinstall-tool.sh",
        "with a variable $REPO/scripts/reinstall-tool.sh",
        "with a braced variable ${NEXUS_REPO}/scripts/reinstall-tool.sh",
        "from home ~/git/nexus/scripts/reinstall-tool.sh",
        "from an install /opt/nexus/scripts/reinstall-tool.sh",
        "a worktree /home/u/nexus-wt/feature/scripts/reinstall-tool.sh",
        "a placeholder {receipt.source}/scripts/reinstall-tool.sh",
        "a placeholder <checkout>/scripts/reinstall-tool.sh",
        "/scripts/reinstall-tool.sh",
        "a bare script mvnw-leased.sh runs the build",
        "the project's AGENTS.md in the nexus repo",
        "the nexus CHANGELOG.md",
        "concrete RDR docs/rdr/rdr-227-per-collection-hnsw-above-the-router-threshold.md",
        # False cleans found in the confirmation round (nexus-z7ddd, r2): each of these used to pass.
        "see src/nexus/tables/target-family-*.toml",
        "read @docs/collections.md first",
        "path:scripts/reinstall-tool.sh",
        "file:docs/collections.md",
        "run $PWD/scripts/reinstall-tool.sh",
        "run %CD%/scripts/reinstall-tool.sh",
        "run %REPO%\\scripts\\reinstall-tool.sh",
        "run %NEXUS_CHECKOUT%/scripts/reinstall-tool.sh",
        "edit tests\\test_plugin_structure.py",
        "run nexus/scripts/reinstall-tool.sh from the parent",
        "the notes in docs/agents/ describe it",
        "the notes in docs/agents describe it",
    ],
)
def test_a_repo_only_path_is_flagged(text: str) -> None:
    assert repo_only_paths(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Rules: https://github.com/Hellblazer/nexus/blob/main/docs/collections.md",
        "[tests/test_x.py](https://github.com/Hellblazer/nexus/blob/main/tests/test_x.py)",
        "Write the RDR to docs/rdr/NNN-kebab-title.md",
        "the project's docs/rdr/rdr-001-example.md",
        "the project's docs/guide.md and web/no-such-page.html",
        "genres: docs/exploration/ and docs/*.md",
        "a generic src/main/java/auth/AuthController.java example",
        "pytest tests/ -k x",
        "the service/PG pair and conexus/nexus store",
        "${CLAUDE_PLUGIN_ROOT}/resources/rdr/TEMPLATE.md",
        "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/x.py and hooks/scripts/y.py",
        "{project_path}/scripts/deploy.sh",
        "$HOME/scripts/deploy.sh and ~/scripts/deploy.sh",
        "../skills/orchestration/SKILL.md",
        "the project's own pyproject.toml and CHANGELOG.md",
        "a user script run-the-thing.sh",
        "the user's $HOME/scripts/deploy.sh and %USERPROFILE%\\scripts\\deploy.sh",
        "the project's own ${PROJECT}/scripts/deploy.sh and %PROJECT_DIR%/scripts/deploy.sh",
        "a mail to dev@docs/ is not a path",
        "genres: docs/runbooks/ and docs/exploration/",
        "a newline escape in tests\\nfoo",
        "a user's nexus-notes/scripts/x.sh",
    ],
)
def test_text_a_user_can_open_or_that_is_not_a_repo_path_passes(text: str) -> None:
    assert not repo_only_paths(text), text


def test_a_root_level_file_and_a_data_directory_are_judged_by_what_exists_here(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "present.csv").write_text("")
    (tmp_path / "AGENTS.md").write_text("")
    assert repo_only_paths("load data/present.csv", tmp_path)
    assert not repo_only_paths("load data/absent.csv", tmp_path)
    assert repo_only_paths("the nexus AGENTS.md", tmp_path)
    assert not repo_only_paths("the project's AGENTS.md", tmp_path)
    assert not repo_only_paths("the nexus CHANGELOG.md", tmp_path), "absent here: nothing to point at"


def test_a_url_with_punctuation_glued_on_is_flagged() -> None:
    assert glued_url_tails("see https://github.com/Hellblazer/nexus/blob/main/docs/collections.md.")
    assert not glued_url_tails("(https://example.com/a, then)")
    assert not glued_url_tails("see https://github.com/Hellblazer/nexus/blob/main/docs/collections.md")
    assert not glued_url_tails("see [the rules](https://github.com/Hellblazer/nexus/blob/main/docs/collections.md).")
    assert not glued_url_tails("go to (https://example.com/a) now")
    # A colon glued to the URL pastes into a browser the same way (the doctor fix suggestions did this).
    assert glued_url_tails("see https://github.com/Hellblazer/nexus/blob/main/docs/operations/engine-reaper.md: the table")
    assert glued_url_tails('"https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md: the policy"')
    assert not glued_url_tails("see https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md (the policy)")
    assert not glued_url_tails("a port at http://127.0.0.1:8080/x and the front of an f-string http://127.0.0.1:")
    assert not glued_url_tails("[the table](https://github.com/Hellblazer/nexus/blob/main/docs/architecture.md): the policy")


def test_the_nexus_checkout_is_the_only_thing_that_makes_a_bare_script_name_repo_only() -> None:
    names = _own_script_names()
    assert {"mvnw-leased.sh", "reinstall-tool.sh", "build-gate-jar.sh", "commit_scope_audit.sh"} <= names
    assert not {"__init__.py", "conftest.py", "install.sh", "nx_launch.py"} & names


def test_the_markdown_scan_flags_a_planted_file_and_honours_the_allowlist(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.sh").write_text("")
    skill = tmp_path / "conexus" / "skills" / "s" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("ok line\nRun tests/x.sh now\n", encoding="utf-8")
    bad = _markdown_violations([skill], tmp_path)
    assert bad == ["conexus/skills/s/SKILL.md:2: tests/x.sh"], bad


def test_an_allowlist_site_covers_only_the_line_its_marker_picks_out() -> None:
    hits = [
        Hit("f.md", 1, "scripts/x.sh", "when the project has scripts/x.sh, run it"),
        Hit("f.md", 2, "scripts/x.sh", "now run scripts/x.sh unconditionally"),
    ]
    saved = dict(_ALLOWED)
    try:
        _ALLOWED["f.md"] = [Site("scripts/x.sh", "when the project has", "guarded")]
        bad, used = _apply_allowed(hits)
    finally:
        _ALLOWED.clear()
        _ALLOWED.update(saved)
    assert bad == ["f.md:2: scripts/x.sh"] and used == {("f.md", 0)}


def test_the_plugin_markdown_census_covers_all_four_plugins_and_skips_only_the_ledgers() -> None:
    files = {p.relative_to(REPO_ROOT).as_posix() for p in _plugin_markdown()}
    assert not files & _MARKDOWN_EXEMPT
    for d in _PLUGIN_DIRS:
        assert any(f.startswith(d + "/") for f in files), d
    assert "conexus/resources/agent-shared/MAINTENANCE.md" in files
    assert "conexus/hooks/scripts/routing/README.md" in files
    assert "prose-edit/README.md" in files
    assert "conexus/evals/README.md" in files
    assert not any(f.startswith("conexus/evals/") and f != "conexus/evals/README.md" for f in files)


def test_a_plugin_root_variable_in_a_file_the_model_reads_is_flagged_fenced_or_not(tmp_path: Path) -> None:
    f = tmp_path / "R.md"
    f.write_text(
        "Read `${CLAUDE_PLUGIN_ROOT}/skills/x/SKILL.md` first.\n"
        "```markdown\n[Protocol](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CONTEXT_PROTOCOL.md)\n```\n"
        "also $CLAUDE_PLUGIN_ROOT here\n"
        "a placeholder <plugin-root>/resources/x.md is fine\n",
        encoding="utf-8",
    )
    bad = _resource_variable_violations([f], tmp_path)
    assert [b.split(":")[1] for b in bad] == ["1", "3", "5"], bad


_MD_LINK_TARGET = re.compile(r"\]\(([^)\s]+)\)")


def _escaping_link_violations(files: list[Path], repo_root: Path = REPO_ROOT) -> list[str]:
    """Relative markdown links that resolve outside the plugin the file belongs to.

    ``[Nexus CLI](../README.md)`` in ``conexus/README.md`` points at the repository README, which an installed plugin
    does not have; the root-level-file rule cannot see it because a ``../`` prefix hides the name."""
    import os  # noqa: PLC0415

    bad = []
    for path in files:
        rel = path.relative_to(repo_root)
        plugin = repo_root / rel.parts[0]
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _MD_LINK_TARGET.finditer(line):
                target = m.group(1).split("#", 1)[0]
                if not target or "://" in target or target.startswith(("mailto:", "/")):
                    continue
                resolved = Path(os.path.normpath(path.parent / target))
                if not resolved.is_relative_to(plugin):
                    bad.append(f"{rel.as_posix()}:{i}: link {m.group(1)} leaves the plugin")
    return bad


def test_a_link_that_leaves_the_plugin_is_flagged(tmp_path: Path) -> None:
    f = tmp_path / "conexus" / "README.md"
    f.parent.mkdir()
    (tmp_path / "conexus" / "skills").mkdir()
    f.write_text(
        "see [CLI](../README.md) and [skill](skills/x/SKILL.md) and [up](skills/../agents/a.md)\n"
        "and [web](https://example.com/../x) and [anchor](#top) and [out](../docs/a.md#frag)\n",
        encoding="utf-8",
    )
    bad = _escaping_link_violations([f], tmp_path)
    assert bad == [
        "conexus/README.md:1: link ../README.md leaves the plugin",
        "conexus/README.md:2: link ../docs/a.md#frag leaves the plugin",
    ], bad


def test_no_plugin_markdown_links_outside_its_plugin() -> None:
    files = _plugin_markdown()
    bad = _escaping_link_violations(files)
    assert len(files) >= 84
    assert not bad, "\n".join(bad)


def test_a_plugin_root_placeholder_block_names_one_concrete_file_for_the_real_spelling() -> None:
    """The resource files cannot carry the plugin-root variable, so their template blocks write ``<plugin-root>`` and
    say where to copy the real spelling from. A directory is not an answer (an agent opens it and still has to pick a
    file); one concrete agent file that carries the variable is."""
    spelled_in = "agents/developer.md"
    carriers = [p for p in _resource_files() if "<plugin-root>" in p.read_text(encoding="utf-8")]
    assert len(carriers) >= 4, [p.name for p in carriers]
    for p in carriers:
        text = p.read_text(encoding="utf-8")
        assert f"[{spelled_in}](../../{spelled_in})" in text, f"{p.name}: the placeholder block does not name {spelled_in}"
        assert "[../../agents/](../../agents/)" not in text, f"{p.name}: points at a directory"
    assert "${CLAUDE_PLUGIN_ROOT}" in (REPO_ROOT / "conexus" / spelled_in).read_text(encoding="utf-8"), (
        f"{spelled_in} no longer spells the variable, so the pointer is stale"
    )


def test_the_unbraced_variable_is_flagged_and_the_braced_one_is_not(tmp_path: Path) -> None:
    f = tmp_path / "S.md"
    f.write_text("Run ${CLAUDE_PLUGIN_ROOT}/a.sh\nRun $CLAUDE_PLUGIN_ROOT/a.sh\nRun $CLAUDE_PLUGIN_ROOT_X\nuses $CLAUDE_PLUGIN_ROOT here\n", encoding="utf-8")
    assert [b.split(":")[1] for b in _unbraced_variable_violations([f], tmp_path)] == ["2", "4"]
    readme = tmp_path / "README.md"
    readme.write_text("uses $CLAUDE_PLUGIN_ROOT here\nbut not $CLAUDE_PLUGIN_ROOT/a.sh\n", encoding="utf-8")
    assert [b.split(":")[1] for b in _unbraced_variable_violations([readme], tmp_path)] == ["2"]


def test_a_string_constant_scan_flags_code_text_and_skips_developer_text(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "collections.md").write_text("")
    pkg = tmp_path / "src" / "nexus"
    pkg.mkdir(parents=True)
    (pkg / "m.py").write_text(
        '"""Module doc: see docs/collections.md."""\n'
        "# comment docs/collections.md\n"
        "def f():\n"
        '    """Doc: docs/collections.md."""\n'
        '    x = "attr"\n'
        '    """Attribute doc: docs/collections.md."""\n'
        '    raise RuntimeError("rules: docs/collections.md")\n'
        'HINT = f"read {x} then docs/collections.md"\n'
        'OK = "https://github.com/Hellblazer/nexus/blob/main/docs/collections.md"\n'
        'JOINED = f"{x}/scripts/reinstall-tool.sh"\n'
        'GLUED = "see https://github.com/Hellblazer/nexus/blob/main/docs/collections.md."\n',
        encoding="utf-8",
    )
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "reinstall-tool.sh").write_text("")
    bad, examined = _string_violations(pkg, tmp_path)
    assert sorted(b.split(":")[1] for b in bad) == ["10", "11", "7", "8"], bad
    assert examined >= 4


def test_java_literals_are_found_and_comments_and_char_literals_are_not() -> None:
    src = (
        '// a comment with "docs/a.md"\n'
        '/* block "docs/b.md" */\n'
        'String a = "See docs/configuration.md" + \'"\' + "tail";\n'
        'String t = """\n  text block docs/c.md\n  """;\n'
        'char q = \'"\';\n'
        'String e = "esc \\" docs/d.md";\n'
    )
    lits = _java_string_literals(src)
    texts = [t for _, t in lits]
    assert "See docs/configuration.md" in texts and "tail" in texts
    assert any("docs/c.md" in t for t in texts) and any("docs/d.md" in t for t in texts)
    assert not any("docs/a.md" in t or "docs/b.md" in t for t in texts)
    assert dict((t, ln) for ln, t in lits)["See docs/configuration.md"] == 3


def test_the_engine_scan_reads_java_strings_and_changelog_raise_messages(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "configuration.md").write_text("")
    java = tmp_path / "service" / "src" / "main" / "java" / "A.java"
    java.parent.mkdir(parents=True)
    java.write_text('class A { String m = "See docs/configuration.md"; // docs/configuration.md\n}\n', encoding="utf-8")
    xml = tmp_path / "service" / "src" / "main" / "resources" / "db" / "changelog" / "x.xml"
    xml.parent.mkdir(parents=True)
    xml.write_text("<sql>RAISE EXCEPTION 'halted; see docs/configuration.md', n;</sql>", encoding="utf-8")
    tpl = tmp_path / "service" / "src" / "main" / "resources" / "tuples" / "templates" / "t.yaml"
    tpl.parent.mkdir(parents=True)
    tpl.write_text(
        "# docs/configuration.md in a comment is developer text\n"
        "name: t\n"
        "see: docs/configuration.md outside the doc block\n"
        "doc:\n"
        "  summary: s\n"
        "  # docs/configuration.md in a comment inside the block\n"
        "  writers:\n"
        "    - a session per docs/configuration.md\n"
        "  see_also:\n"
        "    - https://github.com/Hellblazer/nexus/blob/main/docs/configuration.md\n"
        "other: docs/configuration.md\n",
        encoding="utf-8",
    )
    hits, examined = _engine_hits(tmp_path)
    assert sorted((h.rel.rsplit("/", 1)[-1], h.token, h.lineno) for h in hits) == [
        ("A.java", "docs/configuration.md", 1), ("t.yaml", "docs/configuration.md", 8), ("x.xml", "docs/configuration.md", 1),
    ]
    assert examined == 2 + 5, "two strings, and the five non-comment lines of the doc block"


def test_the_packaged_scan_reads_html_toml_txt_and_the_mcpb_bundle_and_skips_hash_comments(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "configuration.md").write_text("")
    pkg = tmp_path / "src" / "nexus"
    (pkg / "console").mkdir(parents=True)
    (pkg / "console" / "p.html").write_text("<p>see docs/configuration.md</p>\n", encoding="utf-8")
    (pkg / "t.toml").write_text('# docs/configuration.md in a comment\ntitle = "docs/configuration.md"\n', encoding="utf-8")
    (pkg / "c.txt").write_text("# docs/configuration.md in a comment\nfoo==1\n", encoding="utf-8")
    (pkg / "AGENTS.md").write_text("docs/configuration.md\n", encoding="utf-8")
    (tmp_path / "mcpb").mkdir()
    (tmp_path / "mcpb" / "manifest.json").write_text('{"description": "see docs/configuration.md"}\n', encoding="utf-8")
    hits, n = _packaged_text_hits(tmp_path)
    assert sorted((h.rel, h.lineno) for h in hits) == [
        ("mcpb/manifest.json", 1), ("src/nexus/console/p.html", 1), ("src/nexus/t.toml", 2),
    ]
    assert n == 4, "html, toml, txt and the manifest; the module's AGENTS.md is contributor guidance"


# --------------------------------------------------------------------------------------------------------------
# Live checks
# --------------------------------------------------------------------------------------------------------------

def test_plugin_markdown_names_no_repo_only_file() -> None:
    files = _plugin_markdown()
    print(f"plugin markdown: scanned {len(files)} files")
    assert len(files) >= 84, f"only {len(files)} plugin markdown files: wrong root?"
    bad = _markdown_violations(files)
    assert not bad, "\n".join(bad)


def test_plugin_files_that_are_not_markdown_or_python_name_no_repo_only_file() -> None:
    files = _plugin_text_files()
    print(f"plugin text files: scanned {len(files)} files")
    assert len(files) >= 28, f"only {len(files)} plugin text files: wrong root?"
    bad, _ = _apply_allowed(_plugin_text_hits())
    assert not bad, "\n".join(bad)


def test_packaged_shell_and_markdown_under_src_nexus_name_no_repo_only_file() -> None:
    hits, n = _packaged_text_hits()
    print(f"src/nexus shell and markdown: scanned {n} files")
    assert n >= 24, f"only {n} files: wrong root?"
    scanned = {p.suffix for p in _walk(REPO_ROOT / "src" / "nexus", _PACKAGED_SUFFIXES)}
    assert {".sh", ".md", ".html", ".toml", ".txt"} <= scanned, scanned
    assert (REPO_ROOT / "mcpb" / "manifest.json").is_file() and any(
        p.name == "manifest.json" for p in _walk(REPO_ROOT / "mcpb", _PACKAGED_SUFFIXES | {".json"})
    ), "the .mcpb bundle is a scanned root"
    bad, _ = _apply_allowed(hits)
    assert not bad, "\n".join(bad)


def test_resource_files_hold_no_plugin_root_variable() -> None:
    files = _resource_files()
    print(f"files the model Reads: scanned {len(files)}")
    assert len(files) >= 16, f"only {len(files)} Read-opened files: wrong root?"
    assert any(p.name == "CONTEXT_PROTOCOL.md" for p in files)
    bad = _resource_variable_violations(files)
    assert not bad, "\n".join(bad)


def test_no_plugin_markdown_uses_the_unbraced_plugin_root_variable() -> None:
    files = _plugin_markdown()
    print(f"unbraced-variable scan: {len(files)} files")
    assert len(files) >= 84
    bad = _unbraced_variable_violations(files)
    assert not bad, "\n".join(bad)


_SERVER_MODULES = (("nexus.mcp.core", "nexus"), ("nexus.mcp.catalog", "nexus-catalog"))


def test_mcp_tool_and_parameter_descriptions_name_no_repo_only_file() -> None:
    bad: list[str] = []
    tools = params = 0
    for module_path, server in _SERVER_MODULES:
        mod = importlib.import_module(module_path)
        for name, tool in sorted(mod.mcp._tool_manager._tools.items()):
            tools += 1
            bad += [f"{server}.{name}: {t}" for t in scan_tokens(tool.description or "")]
            for pname, schema in (tool.parameters.get("properties") or {}).items():
                params += 1
                bad += [f"{server}.{name}({pname}): {t}" for t in scan_tokens(schema.get("description") or "")]
    print(f"mcp: scanned {tools} tool descriptions and {params} parameter descriptions")
    assert tools >= 50 and params >= 237, (tools, params)
    assert not bad, "\n".join(bad)


def test_cli_help_names_no_repo_only_file() -> None:
    import click  # noqa: PLC0415 — deferred: importing the CLI is the point of this test

    from nexus.cli import main  # noqa: PLC0415

    bad: list[str] = []
    seen = 0

    def walk(cmd: click.Command, path: list[str]) -> None:
        nonlocal seen
        seen += 1
        texts = [("help", cmd.help or ""), ("short_help", cmd.short_help or ""), ("epilog", cmd.epilog or "")]
        texts += [(f"--{p.name}", getattr(p, "help", "") or "") for p in cmd.params]
        for where, text in texts:
            # A ``\b`` example block is scanned like the prose around it: an example that names a repo file
            # misleads as a sentence does (the examples in the tree use generic paths such as src/app/main.py).
            bad.extend(f"{' '.join(path)} {where}: {t}" for t in scan_tokens(text))
        if isinstance(cmd, click.Group):
            for name, sub in sorted(cmd.commands.items()):
                walk(sub, [*path, name])

    walk(main, ["nx"])
    print(f"cli: scanned {seen} commands")
    assert seen >= 263, f"only {seen} commands: the click tree did not load"
    assert not bad, "\n".join(bad)


def test_no_string_constant_in_src_nexus_names_a_repo_only_file() -> None:
    hits, examined = _string_hits(REPO_ROOT / "src" / "nexus")
    bad, _ = _apply_allowed(hits)
    print(f"src/nexus: examined {examined} string constants, "
          f"{sum(map(len, _ALLOWED.values()))} allowlisted sites")
    assert examined >= 54000, f"only {examined} string constants examined: wrong root?"
    assert not bad, "\n".join(bad)


def test_no_string_constant_in_the_plugins_python_names_a_repo_only_file() -> None:
    """Hook and launcher text reaches the user's model too (the SessionStart and SubagentStart injections)."""
    hits: list[Hit] = []
    examined = 0
    for d in _SHIPPED_PYTHON_DIRS:
        found, n = _string_hits(REPO_ROOT / d)
        hits += found
        examined += n
    bad, _ = _apply_allowed(hits)
    print(f"plugin python: examined {examined} string constants")
    assert examined >= 6400, f"only {examined} string constants examined: wrong roots?"
    assert not bad, "\n".join(bad)


def test_the_engines_strings_name_no_repo_only_file() -> None:
    """A boot refusal reaches an operator with a PyPI install and no checkout (ServiceRoleBypassRlsCheck, PgVersionFloor)."""
    hits, examined = _engine_hits()
    bad, _ = _apply_allowed(hits)
    templates = sorted((REPO_ROOT / "service" / "src" / "main" / "resources" / "tuples" / "templates").glob("*.yaml"))
    doc_lines = sum(len(_yaml_doc_lines(p.read_text(encoding="utf-8"))) for p in templates)
    print(f"engine: examined {examined} string literals, RAISE messages and template doc lines "
          f"({doc_lines} doc lines in {len(templates)} templates)")
    assert examined >= 11700, f"only {examined} engine strings examined: wrong root?"
    assert len(templates) >= 7 and doc_lines >= 150, (len(templates), doc_lines)
    assert not bad, "\n".join(bad)


@functools.cache
def _every_file_hit() -> list[Hit]:
    hits = _markdown_hits(_plugin_markdown())
    hits += _plugin_text_hits()
    hits += _packaged_text_hits()[0]
    hits += _string_hits(REPO_ROOT / "src" / "nexus")[0]
    for d in _SHIPPED_PYTHON_DIRS:
        hits += _string_hits(REPO_ROOT / d)[0]
    hits += _engine_hits()[0]
    return hits


def test_every_allowlist_entry_is_still_needed() -> None:
    """A stale entry hides nothing today and a future use of the path tomorrow."""
    _, used = _apply_allowed(_every_file_hit())
    for rel, sites in _ALLOWED.items():
        assert (REPO_ROOT / rel).is_file(), rel
        for i, site in enumerate(sites):
            assert site.reason.strip() and len(site.reason.split()) >= 3, (rel, site.token)
            # An empty marker is a substring of every line, so the entry would excuse the token anywhere in the file.
            assert site.marker.strip(), f"{rel}: the site for {site.token!r} has no marker; pick out its line"
            assert (rel, i) in used, f"{rel}: allowlisted {site.token!r} (marker {site.marker!r}) is no longer there"


# --------------------------------------------------------------------------------------------------------------
# A URL in help text must survive click's wrapping
# --------------------------------------------------------------------------------------------------------------

_HELP_URL = re.compile(r"https?://[^\s)\"'<>]+")


def _intact_url(raw: str) -> str:
    """The URL as an author meant it: a sentence's closing period or comma is not part of it."""
    return raw.rstrip(".,;:")


def _help_texts(cmd: "click.Command") -> list[str]:
    """The texts of *cmd* that ``--help`` renders: its body, its epilog and each option's help. ``short_help`` is
    the line in the PARENT's command list, not part of this command's own help."""
    return [cmd.help or "", cmd.epilog or "", *[getattr(p, "help", "") or "" for p in cmd.params]]


def _wrapped_urls(cmd: "click.Command", path: list[str], width: int = 80) -> list[str]:
    """URLs in *cmd*'s help that the rendered ``--help`` at *width* columns breaks.

    Counted per occurrence: a URL written twice (once in the body, once in an epilog that click prints as written)
    must come out intact twice. ``url in rendered`` is satisfied by the epilog's copy and passed ``nx init`` with its
    body copy broken (``docs/cli-`` newline ``reference.md``)."""
    import collections  # noqa: PLC0415

    import click  # noqa: PLC0415

    ctx = click.Context(cmd, info_name=path[-1], terminal_width=width, max_content_width=width)
    rendered = cmd.get_help(ctx)
    written = collections.Counter(_intact_url(m.group(0)) for text in _help_texts(cmd) for m in _HELP_URL.finditer(text))
    return [
        f"{' '.join(path)} at {width} columns: {url} (written {n}x, intact {rendered.count(url)}x)"
        for url, n in sorted(written.items())
        if rendered.count(url) < n
    ]


_RENDER_WIDTHS = (80, 60)


def test_a_url_that_wraps_in_rendered_help_is_flagged() -> None:
    import click  # noqa: PLC0415

    @click.command()
    @click.option("--a-long-option-name-here", help="Rules: see https://github.com/Hellblazer/nexus/blob/main/docs/collections.md for the rules.")
    def wraps(a_long_option_name_here: str) -> None:
        """Does a thing."""

    @click.command(epilog="\b\nhttps://github.com/Hellblazer/nexus/blob/main/docs/exploration/taxonomy-projection-tuning.md")
    def holds() -> None:
        """Does a thing."""

    assert _wrapped_urls(wraps, ["nx", "wraps"]), "an option help URL wraps at 80 columns"
    assert not _wrapped_urls(holds, ["nx", "holds"])


def test_a_url_broken_in_the_body_is_flagged_though_the_epilog_prints_it_whole() -> None:
    """The ``nx init`` case (nexus-z7ddd): the same URL in the docstring and in the epilog. The body copy wraps at the
    column edge and the epilog copy does not, so a membership test passes and a count does not."""
    import click  # noqa: PLC0415

    url = "https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md"

    @click.command(epilog=f"\b\nSteps:\n{url}")
    def twice() -> None:
        """Provisions things (the paragraph is nx init's docstring as 7.95.0 shipped it).

        ``--embedder`` selects bge-768 vs minilm-384 for the local service-embedder
        step (minilm-384 gets an advisory, this step always fetches the bge-768 ONNX).
        A separate opt-in (nexus-umm29) switches the running service to Voyage
        instead, see https://github.com/Hellblazer/nexus/blob/main/docs/cli-reference.md "Local mode with Voyage" for the
        ``local.embed_model`` + ``voyage_api_key`` + restart steps.
        """

    @click.command(epilog=f"\b\nSteps:\n{url}")
    def once() -> None:
        """Provisions things; the steps are linked at the end of this help."""

    ctx = click.Context(twice, info_name="twice", terminal_width=80, max_content_width=80)
    rendered = twice.get_help(ctx)
    assert url in rendered, "the epilog copy is whole: this is what made the old check pass"
    assert rendered.count(url) == 1
    bad = _wrapped_urls(twice, ["nx", "twice"])
    assert len(bad) == 1 and "written 2x, intact 1x" in bad[0], bad
    assert not _wrapped_urls(once, ["nx", "once"])


def test_a_url_that_fits_80_columns_and_not_60_is_flagged_at_60() -> None:
    import click  # noqa: PLC0415

    @click.command()
    @click.option("--url", help="Base URL, default https://api.conexus-nexus.com for the service.")
    def probe(url: str) -> None:
        """Does a thing."""

    assert not _wrapped_urls(probe, ["nx", "probe"], 80)
    assert _wrapped_urls(probe, ["nx", "probe"], 60)


@pytest.mark.parametrize("width", _RENDER_WIDTHS)
def test_every_url_in_cli_help_renders_unbroken(width: int) -> None:
    """A wrapped URL pastes into a browser broken (``docs/collection`` newline ``s.md``). Put it in an epilog or a
    ``\\b`` block, which click does not rewrap. Checked at the usual 80 columns and at 60, a narrow split pane."""
    import click  # noqa: PLC0415

    from nexus.cli import main  # noqa: PLC0415

    bad: list[str] = []
    checked = urls = 0

    def walk(cmd: click.Command, path: list[str]) -> None:
        nonlocal checked, urls
        checked += 1
        urls += sum(len(_HELP_URL.findall(t)) for t in _help_texts(cmd))
        bad.extend(_wrapped_urls(cmd, path, width))
        if isinstance(cmd, click.Group):
            for name, sub in sorted(cmd.commands.items()):
                walk(sub, [*path, name])

    walk(main, ["nx"])
    print(f"cli help urls at {width} columns: {urls} urls over {checked} commands")
    assert checked >= 263 and urls >= 17, (checked, urls)
    assert not bad, "\n".join(bad)


# --------------------------------------------------------------------------------------------------------------
# The replacement must not rot: a GitHub link stands in for a repo path, so its target must exist in this tree
# --------------------------------------------------------------------------------------------------------------

_GITHUB_TARGET = re.compile(r"https://github\.com/Hellblazer/nexus/(?:blob|tree)/main/([\w./-]+)")


def _missing_github_targets(texts: list[tuple[str, str]], repo_root: Path = REPO_ROOT) -> list[str]:
    missing = []
    for where, text in texts:
        for m in _GITHUB_TARGET.finditer(text):
            target = m.group(1).rstrip(".")
            if not (repo_root / target).exists():
                missing.append(f"{where}: {target}")
    return missing


def test_a_github_link_to_a_missing_target_is_flagged(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "here.md").write_text("")
    texts = [("a", "see https://github.com/Hellblazer/nexus/blob/main/docs/here.md."),
             ("b", "see https://github.com/Hellblazer/nexus/blob/main/docs/gone.md."),
             ("c", "[d](https://github.com/Hellblazer/nexus/tree/main/docs)")]
    assert _missing_github_targets(texts, tmp_path) == ["b: docs/gone.md"]


def test_every_github_link_this_lint_asks_for_names_something_in_the_tree() -> None:
    texts: list[tuple[str, str]] = []
    for path in _plugin_markdown():
        texts.append((path.relative_to(REPO_ROOT).as_posix(), path.read_text(encoding="utf-8")))
    py_files = [p for d in ("src/nexus", *_SHIPPED_PYTHON_DIRS) for p in sorted((REPO_ROOT / d).rglob("*.py"))]
    for path in py_files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _developer_text_ids(tree)
        texts += [
            (f"{path.relative_to(REPO_ROOT).as_posix()}:{n.lineno}", n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
        ]
    for path in sorted((REPO_ROOT / "service" / "src" / "main" / "java").rglob("*.java")):
        texts += [
            (f"{path.relative_to(REPO_ROOT).as_posix()}:{ln}", t)
            for ln, t in _java_string_literals(path.read_text(encoding="utf-8"))
        ]
    n_links = sum(len(_GITHUB_TARGET.findall(t)) for _, t in texts)
    print(f"github links: {n_links} checked")
    assert n_links >= 72, f"only {n_links} links: the pattern saw nothing"
    missing = _missing_github_targets(texts)
    assert not missing, "\n".join(missing)


def test_the_credential_guard_deny_message_links_the_wrapper_it_names() -> None:
    """The guard ships to every user of the plugin; the wrapper it names exists only in the nexus repo, so the
    message carries the GitHub link (a bare file name reads as something to run; the bare-name rule sees it too)."""
    src = (REPO_ROOT / "conexus" / "hooks" / "scripts" / "routing" / "credential_print_guard.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    messages = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "claude_credentials.py status" in n.value
        and "Harnesses never read" in n.value
    ]
    assert len(messages) == 1, messages
    assert "https://github.com/Hellblazer/nexus/blob/main/tests/e2e/lib/claude_credentials.py" in messages[0]


def test_an_installed_users_own_file_of_the_same_name_is_still_a_repo_only_path_when_it_exists_here(tmp_path: Path) -> None:
    """The rule is about the nexus checkout's tree, so it follows what exists there. Built in a scratch root so a
    rename of any real page cannot fail this for the wrong reason."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "collections.md").write_text("")
    assert repo_only_paths("docs/collections.md", tmp_path)
    assert not repo_only_paths("docs/this-page-does-not-exist-anywhere.md", tmp_path)
