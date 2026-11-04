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
- **Resource files** an agent opens with ``Read`` (``conexus/resources/``): Claude Code does not substitute
  ``${CLAUDE_PLUGIN_ROOT}`` in a file read that way, so the variable may appear only inside a fenced block (a
  template for an agent file, where it is substituted), never in running text. Plugin files are named by a
  path relative to the resource file.
- **MCP tool descriptions and parameter descriptions**, from the live tool registry of both servers.
- **CLI help and option help**, from the live click command tree.
- **Every other string constant in ``src/nexus``** (error and hint text, ``click.echo`` text, doctor
  suggestions), found by AST. Docstrings and bare string statements are developer text, comments are not in the
  AST at all; the click and MCP docstrings that DO reach a user are covered by the two runtime scans above.

A path is repo-only when it starts at a top-level directory of the nexus checkout and names something that exists
there (or has a file extension). ``docs/rdr/`` is every project's own RDR directory and is left alone, as is a
path inside a GitHub URL. Deliberate sites are in ``_ALLOWED``, each with the reason an installed user is not
misled; the live checks print what they scanned and floor it (nexus-moht0: a scan that found nothing to check is
a failure, not a pass).
"""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Top-level directories of the checkout that an installed user does not have. ``src/nexus`` rather than ``src``:
#: ``src/main/java/...`` is a generic example path.
_ROOTS = (
    "src/nexus", "tests", "scripts", "docs", "service", "conexus", "sn", "prose-edit", "tool-spill",
    ".claude", ".github", "web", "mcpb", "infra", "bench",
)
#: ``docs/`` and ``web/`` hold pages that are named in running text as the user's own conventions (the prose-edit
#: genre map, ``docs/rdr/``): only an exact existing file counts there, never a directory or a glob.
_FILE_ONLY_ROOTS = ("docs", "web")
_FILE_EXTENSIONS = frozenset({".md", ".py", ".sh", ".yaml", ".yml", ".xml", ".toml", ".json", ".txt", ".html", ".java", ".sql"})

_PATH = re.compile(
    r"(?<![\w/.:@~$}-])(?:\./)?"
    r"(?P<p>(?:" + "|".join(re.escape(r) for r in _ROOTS) + r")/[\w.*<>{}\[\]-]+(?:/[\w.*<>{}\[\]-]+)*/?)"
)
#: A markdown link to a URL: label and target both leave the scan (a label may show the path it links).
_URL_LINK = re.compile(r"\[[^\]]*\]\(https?://[^)]*\)")
_URL = re.compile(r"https?://\S+")
_LINE_SUFFIX = re.compile(r"(?::\d+(?:-\d+)?|::\w+)$")


def _is_repo_only(token: str, repo_root: Path = REPO_ROOT) -> bool:
    t = token.rstrip(".,;:)'\"`")
    t = _LINE_SUFFIX.sub("", t)
    for mark in ("*", "<", "{", "["):
        if mark in t:
            t = t[: t.index(mark)]
    t = t.rstrip("/")
    if "/" not in t:
        return False
    if t == "docs/rdr" or t.startswith("docs/rdr/"):
        return False
    path = repo_root / t
    if t.split("/", 1)[0] in _FILE_ONLY_ROOTS:
        return path.is_file()
    return path.exists() or Path(t).suffix in _FILE_EXTENSIONS


def repo_only_paths(text: str, repo_root: Path = REPO_ROOT) -> list[str]:
    """The repo-only paths in *text*, as written."""
    text = _URL_LINK.sub(" ", text)
    text = _URL.sub(" ", text)
    return [m.group("p") for m in _PATH.finditer(text) if _is_repo_only(m.group("p"), repo_root)]


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


#: Deliberate repo paths: {repo-relative file: {path as written: why an installed user is not misled}}.
_ALLOWED: dict[str, dict[str, str]] = {
    "prose-edit/README.md": {
        ".claude/skills/site-page/SKILL.md": "names the optional file a USER'S repository may carry; the sentence says a repository without it is fine",
    },
    "src/nexus/hooks/subagent_start.py": {
        "scripts/agent-worktree-preflight.sh": (
            "emitted only when the project has the script (_worktree_preflight_row checks os.path.isfile)"
        ),
    },
    "src/nexus/catalog/types.py": {
        "tests/fixtures": "a path-fragment marker matched against indexed file paths, never shown",
    },
    "src/nexus/target_installer.py": {
        "src/nexus/_install": "a candidate directory probed relative to a root, never shown",
    },
    "src/nexus/tables/chunk_metadata_keys.py": {
        "src/nexus/catalog/note_ttl.py": "a module path compared with the repo's own scan results, never shown",
    },
}

#: Files whose paths are the subject: a lint that names the checkout's own layout.
_WHOLE_FILE_ALLOWED: dict[str, str] = {
    "src/nexus/storage_boundary_lint.py": "path constants the storage-boundary lint matches against its own checkout",
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


# --------------------------------------------------------------------------------------------------------------
# Plugin markdown
# --------------------------------------------------------------------------------------------------------------

def _plugin_markdown(repo_root: Path = REPO_ROOT) -> list[Path]:
    files = []
    for d in _PLUGIN_DIRS:
        files += sorted((repo_root / d).rglob("*.md"))
    rels = {p: p.relative_to(repo_root).as_posix() for p in files}
    return [
        p for p, rel in rels.items()
        if rel not in _MARKDOWN_EXEMPT and not (rel.startswith(_EVAL_CASE_PREFIX) and rel != _EVAL_README)
    ]


def _markdown_violations(files: list[Path], repo_root: Path = REPO_ROOT) -> list[str]:
    bad = []
    for path in files:
        rel = path.relative_to(repo_root).as_posix()
        allowed = _ALLOWED.get(rel, {})
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for tok in repo_only_paths(line, repo_root):
                if tok not in allowed:
                    bad.append(f"{rel}:{lineno}: {tok}")
    return bad


# --------------------------------------------------------------------------------------------------------------
# Resource files
# --------------------------------------------------------------------------------------------------------------

_PLUGIN_ROOT_VAR = re.compile(r"\$\{?CLAUDE_PLUGIN_ROOT\}?")


def _resource_variable_violations(files: list[Path], repo_root: Path = REPO_ROOT) -> list[str]:
    bad = []
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        fenced = _fenced_lines(lines)
        for i, line in enumerate(lines):
            if i not in fenced and _PLUGIN_ROOT_VAR.search(line):
                bad.append(f"{path.relative_to(repo_root).as_posix()}:{i + 1}: plugin-root variable outside a fenced block")
    return bad


# --------------------------------------------------------------------------------------------------------------
# src/nexus string constants
# --------------------------------------------------------------------------------------------------------------

def _developer_text_ids(tree: ast.AST) -> set[int]:
    """Ids of string nodes that are developer text: docstrings and any bare string statement."""
    return {
        id(n.value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)
    }


def _string_violations(src_root: Path, repo_root: Path = REPO_ROOT) -> tuple[list[str], int]:
    """(violations, number of string constants examined) over every ``.py`` file under *src_root*."""
    bad: list[str] = []
    examined = 0
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(repo_root).as_posix()
        if rel in _WHOLE_FILE_ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _developer_text_ids(tree)
        allowed = _ALLOWED.get(rel, {})
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Constant) and isinstance(n.value, str)) or id(n) in skip:
                continue
            examined += 1
            for tok in repo_only_paths(n.value, repo_root):
                if tok not in allowed:
                    bad.append(f"{rel}:{n.lineno}: {tok}")
    return bad, examined


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
    ],
)
def test_a_repo_only_path_is_flagged(text: str) -> None:
    assert repo_only_paths(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Rules: https://github.com/Hellblazer/nexus/blob/main/docs/collections.md.",
        "[tests/test_x.py](https://github.com/Hellblazer/nexus/blob/main/tests/test_x.py)",
        "Write the RDR to docs/rdr/NNN-kebab-title.md",
        "the project's docs/guide.md and web/no-such-page.html",
        "genres: docs/exploration/ and docs/*.md",
        "a generic src/main/java/auth/AuthController.java example",
        "pytest tests/ -k x",
        "the service/PG pair and conexus/nexus store",
        "${CLAUDE_PLUGIN_ROOT}/resources/rdr/TEMPLATE.md",
        "{receipt.source}/scripts/reinstall-tool.sh",
    ],
)
def test_text_a_user_can_open_or_that_is_not_a_repo_path_passes(text: str) -> None:
    assert not repo_only_paths(text), text


def test_an_installed_users_own_file_of_the_same_name_is_still_a_repo_only_path_when_it_exists_here() -> None:
    """The rule is about the nexus checkout's tree, so it follows what exists there."""
    assert (REPO_ROOT / "docs" / "collections.md").is_file()
    assert repo_only_paths("docs/collections.md")
    assert not repo_only_paths("docs/this-page-does-not-exist-anywhere.md")


def test_the_markdown_scan_flags_a_planted_file_and_honours_the_allowlist(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "x.sh").write_text("")
    skill = tmp_path / "conexus" / "skills" / "s" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("ok line\nRun tests/x.sh now\n", encoding="utf-8")
    bad = _markdown_violations([skill], tmp_path)
    assert bad == ["conexus/skills/s/SKILL.md:2: tests/x.sh"], bad


def test_the_plugin_markdown_census_covers_all_four_plugins_and_skips_only_the_ledgers() -> None:
    files = {p.relative_to(REPO_ROOT).as_posix() for p in _plugin_markdown()}
    assert not files & _MARKDOWN_EXEMPT
    for d in _PLUGIN_DIRS:
        assert any(f.startswith(d + "/") for f in files), d
    assert "conexus/resources/agent-shared/MAINTENANCE.md" in files
    assert "conexus/README.md" in files
    assert "prose-edit/README.md" in files
    assert "conexus/evals/README.md" in files
    assert not any(f.startswith("conexus/evals/") and f != "conexus/evals/README.md" for f in files)


def test_a_plugin_root_variable_in_running_text_of_a_resource_file_is_flagged(tmp_path: Path) -> None:
    f = tmp_path / "R.md"
    f.write_text(
        "Read `${CLAUDE_PLUGIN_ROOT}/skills/x/SKILL.md` first.\n"
        "```markdown\n[Protocol](${CLAUDE_PLUGIN_ROOT}/resources/agent-shared/CONTEXT_PROTOCOL.md)\n```\n"
        "  ```markdown\n  indented ${CLAUDE_PLUGIN_ROOT} template\n  ```\n"
        "also $CLAUDE_PLUGIN_ROOT here\n",
        encoding="utf-8",
    )
    bad = _resource_variable_violations([f], tmp_path)
    assert [b.split(":")[1] for b in bad] == ["1", "8"], bad


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
        'OK = "https://github.com/Hellblazer/nexus/blob/main/docs/collections.md"\n',
        encoding="utf-8",
    )
    bad, examined = _string_violations(pkg, tmp_path)
    assert sorted(b.split(":")[1] for b in bad) == ["7", "8"], bad
    assert examined >= 4


# --------------------------------------------------------------------------------------------------------------
# Live checks
# --------------------------------------------------------------------------------------------------------------

def test_plugin_markdown_names_no_repo_only_file() -> None:
    files = _plugin_markdown()
    print(f"plugin markdown: scanned {len(files)} files")
    assert len(files) >= 80, f"only {len(files)} plugin markdown files: wrong root?"
    bad = _markdown_violations(files)
    assert not bad, "\n".join(bad)


def test_resource_files_use_the_plugin_root_variable_only_in_fenced_templates() -> None:
    files = sorted((REPO_ROOT / "conexus" / "resources").rglob("*.md"))
    print(f"resource files: scanned {len(files)}")
    assert len(files) >= 8, f"only {len(files)} resource files: wrong root?"
    fenced_uses = 0
    for p in files:
        lines = p.read_text(encoding="utf-8").splitlines()
        fenced = _fenced_lines(lines)
        fenced_uses += sum(1 for i in fenced if _PLUGIN_ROOT_VAR.search(lines[i]))
    assert fenced_uses >= 2, "no fenced agent-file template names the variable: the fence rule is unexercised"
    bad = _resource_variable_violations(files)
    assert not bad, "\n".join(bad)


_SERVER_MODULES = (("nexus.mcp.core", "nexus"), ("nexus.mcp.catalog", "nexus-catalog"))


def test_mcp_tool_and_parameter_descriptions_name_no_repo_only_file() -> None:
    bad: list[str] = []
    tools = params = 0
    for module_path, server in _SERVER_MODULES:
        mod = importlib.import_module(module_path)
        for name, tool in sorted(mod.mcp._tool_manager._tools.items()):
            tools += 1
            bad += [f"{server}.{name}: {t}" for t in repo_only_paths(tool.description or "")]
            for pname, schema in (tool.parameters.get("properties") or {}).items():
                params += 1
                bad += [f"{server}.{name}({pname}): {t}" for t in repo_only_paths(schema.get("description") or "")]
    print(f"mcp: scanned {tools} tool descriptions and {params} parameter descriptions")
    assert tools >= 50 and params >= 150, (tools, params)
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
            # Docstring examples (``\b`` blocks) show a path the user supplies, so only the prose around it counts.
            bad.extend(f"{' '.join(path)} {where}: {t}" for t in repo_only_paths(text))
        if isinstance(cmd, click.Group):
            for name, sub in sorted(cmd.commands.items()):
                walk(sub, [*path, name])

    walk(main, ["nx"])
    print(f"cli: scanned {seen} commands")
    assert seen >= 200, f"only {seen} commands: the click tree did not load"
    assert not bad, "\n".join(bad)


def test_no_string_constant_in_src_nexus_names_a_repo_only_file() -> None:
    bad, examined = _string_violations(REPO_ROOT / "src" / "nexus")
    skipped_files = sorted(_WHOLE_FILE_ALLOWED)
    print(f"src/nexus: examined {examined} string constants, skipped whole files {skipped_files}, "
          f"{sum(map(len, _ALLOWED.values()))} allowlisted tokens")
    assert examined >= 20000, f"only {examined} string constants examined: wrong root?"
    assert not bad, "\n".join(bad)


def test_every_allowlist_entry_is_still_needed() -> None:
    """A stale entry hides nothing today and a future use of the path tomorrow."""
    for rel, reason in _WHOLE_FILE_ALLOWED.items():
        assert (REPO_ROOT / rel).is_file() and reason.strip(), rel
    for rel, entries in _ALLOWED.items():
        path = REPO_ROOT / rel
        assert path.is_file(), rel
        found: set[str] = set()
        if path.suffix == ".py":
            tree = ast.parse(path.read_text(encoding="utf-8"))
            skip = _developer_text_ids(tree)
            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
                    found.update(repo_only_paths(n.value))
        else:
            for line in path.read_text(encoding="utf-8").splitlines():
                found.update(repo_only_paths(line))
        for token, why in entries.items():
            assert why.strip(), (rel, token)
            assert token in found, f"{rel}: allowlisted {token!r} is no longer there"


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
    for path in sorted((REPO_ROOT / "src" / "nexus").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _developer_text_ids(tree)
        texts += [
            (f"{path.relative_to(REPO_ROOT).as_posix()}:{n.lineno}", n.value)
            for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip
        ]
    n_links = sum(len(_GITHUB_TARGET.findall(t)) for _, t in texts)
    print(f"github links: {n_links} checked")
    assert n_links >= 60, f"only {n_links} links: the pattern saw nothing"
    missing = _missing_github_targets(texts)
    assert not missing, "\n".join(missing)
