# SPDX-License-Identifier: AGPL-3.0-or-later
"""command_context -- shared substrate for ``nx command-context`` subcommands (RDR-130 P2).

P2.1: project-type detector consumed by all P2.x preamble subcommands.
P2.2: Click group + composable block helpers + proof subcommand (nexus-sg7hb).

The command-context group exposes per-command subcommands that print markdown
preamble context for the agent-relay slash commands.  Each subcommand accepts
``args`` (``nargs=-1``) so the ``--``/$ARGUMENTS pass-through contract works
exactly like ``nx rdr preamble <name>``.

No T2Database or chromadb opens here -- filesystem helpers use pathlib, git
and bd helpers shell out via subprocess with graceful fallback (RDR-128 lint
stays clean).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click


# ---------------------------------------------------------------------------
# P2.1: project-type detector (unchanged from P2.1 commit)
# ---------------------------------------------------------------------------

# Ordered list of (glob_pattern, label) pairs matching the bash _pt detector.
# Order is authoritative: polyglot output follows this sequence.
_MARKERS: list[tuple[str, str]] = [
    ("pyproject.toml", "Python"),
    ("setup.py", "Python (setup.py)"),
    ("Cargo.toml", "Rust"),
    ("go.mod", "Go"),
    ("package.json", "Node.js / TypeScript"),
    ("pom.xml", "Java/Kotlin (Maven)"),
    ("build.gradle*", "Java/Kotlin (Gradle)"),
    ("Gemfile", "Ruby"),
    ("composer.json", "PHP"),
    ("*.csproj", "C#/.NET"),
    ("CMakeLists.txt", "C/C++ (CMake)"),
    ("Package.swift", "Swift"),
    ("mix.exs", "Elixir"),
    ("build.sbt", "Scala (sbt)"),
    ("pubspec.yaml", "Dart/Flutter"),
    ("deps.edn", "Clojure"),
    ("project.clj", "Clojure (Leiningen)"),
    ("*.cabal", "Haskell"),
    ("stack.yaml", "Haskell (Stack)"),
    ("Project.toml", "Julia"),
    ("DESCRIPTION", "R"),
    ("build.zig", "Zig"),
    ("dune-project", "OCaml"),
    ("shard.yml", "Crystal"),
]

_UNKNOWN = "- Unknown (no recognized build/marker file)"

# Directories excluded from source-location scanning
_SOURCE_SCAN_EXCLUDE: frozenset[str] = frozenset({"node_modules", "target", ".git"})


def detect_project_types(root: Path) -> list[str]:
    """Return a list of ``"- <Stack>"`` strings for every marker found in *root*.

    Checks are performed in the canonical spec order so polyglot output is
    deterministic.  Returns a single-element list with the *Unknown* sentinel
    when no marker is found.

    Args:
        root: Directory to inspect.  Must be an absolute path; the function
              never consults ``os.getcwd()``.

    Returns:
        Ordered list of ``"- <Stack>"`` labels, or
        ``["- Unknown (no recognized build/marker file)"]``.
    """
    found: list[str] = []
    for pattern, label in _MARKERS:
        if any(True for _ in root.glob(pattern)):
            found.append(f"- {label}")
    return found if found else [_UNKNOWN]


# ---------------------------------------------------------------------------
# P2.2: composable block-builder helpers
# ---------------------------------------------------------------------------


def working_directory_block(cwd: Path) -> str:
    """Return a single markdown line for the working directory.

    Args:
        cwd: The directory to display.  Callers supply this explicitly;
             the helper never consults ``os.getcwd()`` internally.

    Returns:
        A string of the form ``"**Working directory:** <cwd>"``.
    """
    return f"**Working directory:** {cwd}"


def project_type_block(root: Path) -> list[str]:
    """Return the project-type block lines.

    Args:
        root: The project root to inspect.

    Returns:
        A list whose first element is ``"**Project type:**"`` followed by
        one ``"- <Stack>"`` line per detected type (or the Unknown sentinel).
    """
    return ["**Project type:**"] + detect_project_types(root)


def git_branch_block(root: Path) -> list[str]:
    """Return the git-branch block lines, or an empty list if not a git repo.

    Uses ``git -C <root> rev-parse --abbrev-ref HEAD`` and degrades
    gracefully on failure (non-git directory, git absent, etc.).

    Args:
        root: The directory to probe.

    Returns:
        ``["**Branch:** <branch>"]`` when successful, ``[]`` otherwise.
    """
    try:
        branch = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return [f"**Branch:** {branch}"]
    except Exception:
        return []


def beads_block(root: Path, args: list[str], heading: str) -> list[str]:
    """Return bead-list lines under *heading* by shelling to ``bd list <args>``.

    Degrades gracefully when ``bd`` is absent or exits non-zero: returns
    ``[heading, "- (bd unavailable or no results)"]``.

    Args:
        root: Working directory for the ``bd`` subprocess.
        args: Extra arguments forwarded verbatim to ``bd list``.
        heading: The markdown heading string to prepend (e.g. ``"### Beads"``).

    Returns:
        List of strings: heading, followed by bd output lines or a fallback.
    """
    try:
        output = subprocess.check_output(
            ["bd", "list"] + args,
            cwd=str(root),
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if output:
            return [heading] + output.splitlines()
        return [heading, "- (no beads found)"]
    except Exception:
        return [heading, "- (bd unavailable or no results)"]


def top_level_structure_block(root: Path) -> list[str]:
    """Return top-level directory structure lines.

    Lists up to 15 immediate subdirectories of *root*, sorted
    deterministically.  Uses pathlib only -- no shell ``ls`` or ``find``.
    Hidden directories (names starting with ``.``) are excluded to match the
    original ``ls -d */`` semantics, which the shell glob skips by default.

    Args:
        root: The project root.

    Returns:
        List starting with ``"### Top-level Structure"``, followed by
        ``"- <dirname>"`` lines (up to 15), or a fallback when empty.
    """
    header = "### Top-level Structure"
    try:
        dirs = sorted(
            p.name
            for p in root.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )[:15]
    except OSError:
        dirs = []
    if not dirs:
        return [header]
    return [header] + [f"- {d}" for d in dirs]


def source_locations_block(root: Path) -> list[str]:
    """Return source-location lines for directories named ``src`` under *root*.

    Walks *root* recursively, collects directories named ``src``, excludes
    ``node_modules``, ``target``, and ``.git`` from traversal, returns up to
    10 results sorted deterministically.

    Args:
        root: The project root.

    Returns:
        List starting with ``"### Source Locations"``, followed by
        ``"- <path>"`` lines (relative to root, up to 10), or a fallback.
    """
    header = "### Source Locations"
    src_dirs: list[str] = []
    try:
        for dirpath, dirnames, _ in os.walk(str(root)):
            # Prune excluded directories in-place so os.walk skips them
            dirnames[:] = [
                d for d in dirnames
                if d not in _SOURCE_SCAN_EXCLUDE
            ]
            p = Path(dirpath)
            if p.name == "src":
                try:
                    rel = str(p.relative_to(root))
                except ValueError:
                    rel = str(p)
                src_dirs.append(rel)
    except OSError:
        pass

    src_dirs = sorted(src_dirs)[:10]
    if not src_dirs:
        return [header]
    return [header] + [f"- {d}" for d in src_dirs]


# ---------------------------------------------------------------------------
# P2.2: shared-context composer (analyze-code preamble)
# ---------------------------------------------------------------------------


def render_shared_context(cwd: Path) -> str:
    """Compose the shared preamble context markdown string.

    Joins: ``## Context`` header, working-directory, project-type,
    top-level structure, and source locations.  This is the exact content
    that analyze-code's preamble block currently inlines.

    Args:
        cwd: The project root.  Passed explicitly so the function is
             cwd-independent and safe to call from tests with tmp_path.

    Returns:
        A multi-line markdown string.
    """
    parts: list[str] = []
    parts.append("## Context")
    parts.append("")
    parts.append(working_directory_block(cwd))
    parts.append("")
    parts.extend(project_type_block(cwd))
    parts.append("")
    parts.extend(top_level_structure_block(cwd))
    parts.append("")
    parts.extend(source_locations_block(cwd))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# P2.3: additional block helpers (git diff, test/pdf discovery, preflight)
# ---------------------------------------------------------------------------


def modified_files_block(root: Path) -> list[str]:
    """Return modified-files block lines from ``git diff --name-only HEAD``.

    Falls back gracefully when root is not a git repo or git is absent.

    Args:
        root: The project root to probe.

    Returns:
        ``["### Modified Files", ...]`` with filenames, or a fallback message.
    """
    header = "### Modified Files"
    try:
        output = subprocess.check_output(
            ["git", "-C", str(root), "diff", "--name-only", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        lines = output.splitlines()[:20] if output else []
        if lines:
            return [header] + lines
        return [header, "No uncommitted changes"]
    except Exception:
        return [header, "No uncommitted changes"]


def diff_stat_block(root: Path) -> list[str]:
    """Return diff-stat block lines from ``git diff --stat HEAD``.

    Falls back gracefully when root is not a git repo or git is absent.

    Args:
        root: The project root to probe.

    Returns:
        ``["### Diff Summary", ...]`` with the last 10 lines of git diff --stat,
        or a fallback message.
    """
    header = "### Diff Summary"
    try:
        output = subprocess.check_output(
            ["git", "-C", str(root), "diff", "--stat", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        lines = output.splitlines()[-10:] if output else []
        if lines:
            return [header] + lines
        return [header, "No diff available"]
    except Exception:
        return [header, "No diff available"]


def test_locations_block(root: Path) -> list[str]:
    """Return test-location lines by finding test/tests directories under *root*.

    Uses pathlib only -- no shell. Excludes ``node_modules`` and ``target``.

    Args:
        root: The project root to inspect.

    Returns:
        ``["### Test Locations", ...]`` with relative paths to test dirs, or fallback.
    """
    header = "### Test Locations"
    _TEST_NAMES = frozenset({"test", "tests", "__tests__"})
    found: list[str] = []
    try:
        for dirpath, dirnames, _ in os.walk(str(root)):
            dirnames[:] = [
                d for d in dirnames
                if d not in {"node_modules", "target"}
            ]
            p = Path(dirpath)
            if p.name in _TEST_NAMES:
                try:
                    rel = str(p.relative_to(root))
                except ValueError:
                    rel = str(p)
                found.append(rel)
    except OSError:
        pass

    found = sorted(found)[:5]
    if not found:
        return [header, "No test directories found"]
    return [header] + [f"- {d}" for d in found]


def pdf_files_block(root: Path) -> list[str]:
    """Return PDF files found under *root* (excluding .git), up to 20.

    Uses pathlib only -- no shell ``find``.

    Args:
        root: The directory to scan.

    Returns:
        ``["### PDF Files in Current Directory", ...]`` with relative paths,
        or a fallback when none found.
    """
    header = "### PDF Files in Current Directory"
    pdfs: list[str] = []
    try:
        for dirpath, dirnames, filenames in os.walk(str(root)):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            p = Path(dirpath)
            for fname in filenames:
                if fname.lower().endswith(".pdf"):
                    try:
                        rel = str((p / fname).relative_to(root))
                    except ValueError:
                        rel = str(p / fname)
                    pdfs.append(rel)
                    if len(pdfs) >= 20:
                        break
            if len(pdfs) >= 20:
                break
    except OSError:
        pass

    if not pdfs:
        return [header, "No PDF files found"]
    return [header] + pdfs


def tool_check_block(tool: str, section_heading: str, fail_message: str) -> list[str]:
    """Return a status block for a CLI tool presence check.

    Shells to ``<tool> --version`` and emits PASS/FAIL status.

    Args:
        tool: The tool binary name to check.
        section_heading: The markdown heading for this section.
        fail_message: Lines to emit after "Status: FAIL" when tool is absent.

    Returns:
        List of lines starting with *section_heading*, then version or fail info.
    """
    lines: list[str] = [section_heading, ""]
    try:
        version = subprocess.check_output(
            [tool, "--version"],
            stderr=subprocess.STDOUT,
            text=True,
        ).strip().splitlines()[0]
        lines += ["Status: PASS", f"Version: {version}"]
    except Exception:
        lines += ["Status: FAIL"] + fail_message.splitlines()
    lines.append("")
    return lines


def nx_doctor_block() -> list[str]:
    """Return the nx-doctor status block.

    Runs ``nx doctor`` and reports PASS/FAIL/SKIP based on exit code.
    Falls back to SKIP when nx is not installed.

    Returns:
        List of lines for the ### 2. nx configuration section.
    """
    heading = "### 2. nx configuration (nx doctor)"
    lines: list[str] = [heading, ""]
    try:
        proc = subprocess.run(
            ["nx", "doctor"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines += proc.stdout.splitlines()
        lines.append("")
        if proc.returncode == 0:
            lines.append("Status: PASS")
        else:
            lines.append("Status: FAIL - run 'nx doctor' for details")
    except FileNotFoundError:
        lines.append("Status: SKIP - nx not installed")
    lines.append("")
    return lines


def claude_md_block(root: Path) -> list[str]:
    """Return CLAUDE.md agent-readiness check lines.

    Inspects the CLAUDE.md file at *root* for language, build system, and
    test command mentions.

    Args:
        root: The project root to inspect.

    Returns:
        List of lines for the ### 6. CLAUDE.md Agent Readiness section.
    """
    heading = "### 6. CLAUDE.md Agent Readiness"
    lines: list[str] = [heading, ""]
    claude_md = root / "CLAUDE.md"
    if not claude_md.exists():
        lines += [
            "[ ] CLAUDE.md not found",
            "",
            "Status: WARN",
            "Agents work best when CLAUDE.md specifies language, build system, and test command.",
            "See: https://docs.anthropic.com/en/docs/claude-code/memory#claudemd",
        ]
        lines.append("")
        return lines

    lines.append("[x] CLAUDE.md exists")
    try:
        content = claude_md.read_text(encoding="utf-8")
        import re as _re
        lang_match = _re.search(
            r"Python|Java|Go|Rust|TypeScript|Node\.js|C\+\+|C#|Ruby|Kotlin|Swift|Scala",
            content,
            _re.IGNORECASE,
        )
        if lang_match:
            lines.append(f"[x] Language detected: {lang_match.group(0)}")
        else:
            lines.append("[?] Language: not found (optional - agents can detect from build files)")

        build_match = _re.search(
            r"uv|maven|mvn|cargo|go build|go mod|npm|yarn|pnpm|gradle|make|cmake",
            content,
            _re.IGNORECASE,
        )
        if build_match:
            lines.append(f"[x] Build system detected: {build_match.group(0)}")
        else:
            lines.append("[?] Build system: not found (optional)")

        test_match = _re.search(
            r"pytest|mvn test|go test|cargo test|npm test|jest|vitest|make test|uv run pytest",
            content,
            _re.IGNORECASE,
        )
        if test_match:
            lines.append(f"[x] Test command detected: {test_match.group(0)}")
        else:
            lines.append("[?] Test command: not found (optional)")

    except OSError:
        pass

    lines += ["", "Status: PASS (CLAUDE.md present)", ""]
    return lines


def render_nx_preflight(cwd: Path) -> str:
    """Compose the nx-preflight check markdown string.

    Runs dynamic checks for nx, bd, uv, npx, and CLAUDE.md readiness.

    Args:
        cwd: The project root (used for CLAUDE.md check).

    Returns:
        A multi-line markdown string.
    """
    parts: list[str] = ["## conexus Plugin Preflight Check", ""]

    # 1. nx CLI
    parts += tool_check_block(
        "nx",
        "### 1. nx CLI",
        "nx not found in PATH\n"
        "Install: uv tool install conexus  OR  pip install conexus\n"
        "Docs: https://github.com/Hellblazer/nexus",
    )

    # 2. nx doctor
    parts += nx_doctor_block()

    # 3. bd CLI
    parts += tool_check_block(
        "bd",
        "### 3. bd (Beads) CLI",
        "bd not found in PATH\n"
        "Install: https://github.com/BeadsProject/beads",
    )

    # 4. uv
    parts += tool_check_block(
        "uv",
        "### 4. uv (package manager)",
        "uv not found - nx can be installed with pip instead, but uv is recommended\n"
        "Install: curl -LsSf https://astral.sh/uv/install.sh | sh",
    )

    # 5. Node.js / npx
    parts += tool_check_block(
        "npx",
        "### 5. Node.js / npx (required by plugin MCP servers)",
        "npx not found in PATH - the plugin's sequential-thinking and context7 MCP\n"
        "servers are spawned via 'npx -y ...' and will silently fail to start.\n"
        "Install:\n"
        "  brew install node                         (macOS)\n"
        "  apt install nodejs npm                    (Ubuntu/Debian)\n"
        "  https://nodejs.org/                       (other platforms)",
    )

    # 6. CLAUDE.md
    parts += claude_md_block(cwd)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# P2.2: Click group + proof subcommand
# ---------------------------------------------------------------------------


@click.group("command-context")
def command_context() -> None:
    """Preamble context for agent-relay slash commands (RDR-130 P2).

    Each subcommand corresponds to one slash command and prints the
    markdown context block that the CC injection layer injects before
    the user's arguments.  Subcommands accept trailing args via
    ``-- $ARGUMENTS`` so the caller can pass through the user's input
    without interfering with nx option parsing.
    """


@command_context.command("analyze-code")
@click.argument("args", nargs=-1)
def analyze_code(args: tuple[str, ...]) -> None:
    """Print the shared preamble context for the analyze-code slash command.

    Outputs: working directory, project type, top-level structure, and
    source locations.  The *args* parameter accepts (and ignores) any
    trailing content after ``--``, mirroring the P1 preamble contract.
    """
    print(render_shared_context(Path.cwd()))


# ---------------------------------------------------------------------------
# P2.3: 14 additional subcommands (one per agent-relay command)
# ---------------------------------------------------------------------------


@command_context.command("architecture")
@click.argument("args", nargs=-1)
def architecture(args: tuple[str, ...]) -> None:
    """Print preamble context for the architecture slash command.

    Outputs: working directory, git branch, project structure (project type),
    active beads, pipeline position, and a tip.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    branch = git_branch_block(cwd)
    if branch:
        parts.extend(branch)
        parts.append("")
    parts.append("### Project Structure")
    parts.extend(project_type_block(cwd))
    parts.append("")
    parts.extend(beads_block(cwd, ["--status=in_progress", "--limit=5"], "### Active Beads"))
    parts.append("")
    parts.extend(beads_block(cwd, ["--type=epic", "--limit=3"], ""))
    parts.append("")
    parts.extend([
        "### Pipeline Position",
        "",
        "strategic-planner -> nx_plan_audit -> architect-planner -> developer",
        "",
        "### Tip",
        "",
        "The agent uses the search tool with corpus='code' and hybrid=true (30-50 results) for discovery,",
        "then LSP for precision navigation (documentSymbol, goToImplementation, findReferences).",
    ])
    print("\n".join(parts))


@command_context.command("create-plan")
@click.argument("args", nargs=-1)
def create_plan(args: tuple[str, ...]) -> None:
    """Print preamble context for the create-plan slash command.

    Outputs: working directory, git branch, existing epics/features beads,
    and project structure (project type).
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    branch = git_branch_block(cwd)
    if branch:
        parts.extend(branch)
        parts.append("")
    parts.extend(beads_block(cwd, ["--type=epic", "--limit=5"], "### Existing Epics/Features"))
    parts.append("")
    parts.extend(beads_block(cwd, ["--type=feature", "--status=open", "--limit=5"], ""))
    parts.append("")
    parts.append("### Project Structure")
    parts.extend(project_type_block(cwd))
    print("\n".join(parts))


@command_context.command("implement")
@click.argument("args", nargs=-1)
def implement(args: tuple[str, ...]) -> None:
    """Print preamble context for the implement slash command.

    Outputs: working directory, git branch, plan-audit note, active work
    beads, and project info (project type).
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    branch = git_branch_block(cwd)
    if branch:
        parts.extend(branch)
        parts.append("")
    parts.append(
        "**Note:** Ensure plan has been validated by mcp__plugin_conexus_nexus__nx_plan_audit"
        " (RDR-080) before implementing."
    )
    parts.append("")
    parts.extend(beads_block(cwd, ["--status=in_progress", "--limit=5"], "### Active Work"))
    parts.append("")
    parts.append("### Project Info")
    parts.extend(project_type_block(cwd))
    print("\n".join(parts))


@command_context.command("debug")
@click.argument("args", nargs=-1)
def debug(args: tuple[str, ...]) -> None:
    """Print preamble context for the debug slash command.

    Outputs: working directory, recent test failures (filesystem scan),
    and active beads.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    # Recent test failures: check for known report directories (pathlib only)
    parts.append("### Recent Test Failures")
    surefire = cwd / "target" / "surefire-reports"
    reports = cwd / "reports"
    if surefire.is_dir():
        failures = sorted(
            str(p.relative_to(cwd))
            for p in surefire.glob("*.txt")
            if p.is_file()
        )[:5]
        if failures:
            parts.extend(failures)
        else:
            parts.append("No recent failures in surefire-reports")
    elif reports.is_dir():
        xmls = sorted(
            str(p.relative_to(cwd))
            for p in reports.glob("*.xml")
            if p.is_file()
        )[:5]
        parts.extend(xmls) if xmls else parts.append("No XML reports found")
    elif (cwd / "pytest.xml").exists() or (cwd / "test-results.xml").exists():
        parts.append("Test results file found")
    else:
        parts.append("No test output found - run the project's test command (check CLAUDE.md)")
    parts.append("")
    parts.extend(beads_block(cwd, ["--status=in_progress", "--limit=3"], "### Active Beads"))
    print("\n".join(parts))


@command_context.command("deep-analysis")
@click.argument("args", nargs=-1)
def deep_analysis(args: tuple[str, ...]) -> None:
    """Print preamble context for the deep-analysis slash command.

    Outputs: working directory, git branch, active beads, and a tip.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    branch = git_branch_block(cwd)
    if branch:
        parts.extend(branch)
        parts.append("")
    parts.extend(beads_block(cwd, ["--status=in_progress", "--limit=5"], "### Active Beads"))
    parts.append("")
    parts.extend([
        "### Tip",
        "",
        "The deep-analyst uses mcp__plugin_conexus_sequential-thinking__sequentialthinking:"
        " hypothesis -> evidence -> evaluation -> conclusion.",
        "For cross-cutting issues, this agent explores multiple components before converging on root cause.",
    ])
    print("\n".join(parts))


@command_context.command("enrich-plan")
@click.argument("args", nargs=-1)
def enrich_plan(args: tuple[str, ...]) -> None:
    """Print preamble context for the enrich-plan slash command.

    Outputs: working directory, and related (open epic) beads.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    parts.extend(
        beads_block(cwd, ["--type=epic", "--status=open", "--limit=5"], "### Related Beads")
    )
    print("\n".join(parts))


@command_context.command("knowledge-tidy")
@click.argument("args", nargs=-1)
def knowledge_tidy(args: tuple[str, ...]) -> None:
    """Print preamble context for the knowledge-tidy slash command.

    Outputs: working directory, existing knowledge note, recently completed
    beads, and storage standards.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    parts.extend([
        "### Existing Knowledge",
        "",
        "Use **store_list** tool: collection='knowledge' to list existing knowledge entries.",
        "",
    ])
    parts.extend(beads_block(cwd, ["--status=done", "--limit=5"], "### Recently Completed Beads"))
    parts.append("")
    parts.extend([
        "### Storage Standards",
        "",
        "Title conventions: research-{topic}, decision-{component}-{name}, pattern-{name}, debug-{component}-{issue}",
        "All entries stored via store_put tool: collection='knowledge'",
    ])
    print("\n".join(parts))


@command_context.command("pdf-process")
@click.argument("args", nargs=-1)
def pdf_process(args: tuple[str, ...]) -> None:
    """Print preamble context for the pdf-process slash command.

    Outputs: working directory, PDF files in cwd (pathlib scan), existing
    indexed collections note, and a tip.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    parts.extend(pdf_files_block(cwd))
    parts.append("")
    parts.extend([
        "### Existing Indexed Collections",
        "",
        "Use **store_list** tool to list existing indexed collections.",
        "",
        "### Tip",
        "",
        "Specify PDF paths or a directory. nx index pdf extracts text, chunks content,",
        "and indexes into T3 for semantic search via the search tool.",
    ])
    print("\n".join(parts))


@command_context.command("plan-audit")
@click.argument("args", nargs=-1)
def plan_audit(args: tuple[str, ...]) -> None:
    """Print preamble context for the plan-audit slash command.

    Outputs: working directory, static instruction line, and related beads.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    parts.append("Provide the plan to audit in the arguments or reference existing documentation.")
    parts.append("")
    parts.extend(
        beads_block(cwd, ["--type=epic", "--status=open", "--limit=3"], "### Related Beads")
    )
    print("\n".join(parts))


@command_context.command("research")
@click.argument("args", nargs=-1)
def research(args: tuple[str, ...]) -> None:
    """Print preamble context for the research slash command.

    Outputs: working directory, available knowledge sources, and a tip.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    parts.extend([
        "### Available Knowledge Sources",
        "",
        "- **nx store**: Semantic search across stored knowledge",
        "- **Web**: Current information from web search",
        "- **Codebase**: Relevant code examples and patterns",
        "- **nx memory**: Session context and prior work",
        "",
        "### Tip",
        "",
        "The agent will first search the T3 store for existing research on this topic.",
        "Prior findings will be incorporated into the synthesis.",
    ])
    print("\n".join(parts))


@command_context.command("review-code")
@click.argument("args", nargs=-1)
def review_code(args: tuple[str, ...]) -> None:
    """Print preamble context for the review-code slash command.

    Outputs: working directory, git branch (if repo), modified files and
    diff summary (if repo), otherwise a note and recently modified files,
    and active beads.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    branch = git_branch_block(cwd)
    if branch:
        parts.extend(branch)
        parts.append("")
        parts.extend(modified_files_block(cwd))
        parts.append("")
        parts.extend(diff_stat_block(cwd))
    else:
        parts.append("**Note:** Not a git repository")
        parts.append("")
        parts.append("### Modified Files")
        parts.append("No uncommitted changes")
    parts.append("")
    parts.extend(beads_block(cwd, ["--status=in_progress", "--limit=3"], "### Active Beads"))
    print("\n".join(parts))


@command_context.command("substantive-critique")
@click.argument("args", nargs=-1)
def substantive_critique(args: tuple[str, ...]) -> None:
    """Print preamble context for the substantive-critique slash command.

    Outputs: working directory, git branch (if repo), modified files (if
    repo), active beads, and a tip.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    branch = git_branch_block(cwd)
    if branch:
        parts.extend(branch)
        parts.append("")
        parts.extend(modified_files_block(cwd))
        parts.append("")
    parts.extend(beads_block(cwd, ["--status=in_progress", "--limit=3"], "### Active Beads"))
    parts.append("")
    parts.extend([
        "### Tip",
        "",
        "The substantive-critic analyzes structure, logical consistency, completeness, and spec conformance.",
        "Findings are prioritized: Critical > Significant > Minor.",
    ])
    print("\n".join(parts))


@command_context.command("test-validate")
@click.argument("args", nargs=-1)
def test_validate(args: tuple[str, ...]) -> None:
    """Print preamble context for the test-validate slash command.

    Outputs: working directory, test locations, recently modified files
    (if git repo), and active beads.
    """
    cwd = Path.cwd()
    parts: list[str] = ["## Context", ""]
    parts.append(working_directory_block(cwd))
    parts.append("")
    parts.extend(test_locations_block(cwd))
    parts.append("")
    branch = git_branch_block(cwd)
    if branch:
        parts.append("### Recently Modified Files")
        try:
            output = subprocess.check_output(
                ["git", "-C", str(cwd), "diff", "--name-only", "HEAD~5"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
            src_lines = [
                ln for ln in output.splitlines()
                if ln.endswith((".java", ".py", ".ts", ".js"))
            ][:10]
            if src_lines:
                parts.extend(src_lines)
            else:
                parts.append("No recent source changes")
        except Exception:
            parts.append("No recent source changes")
        parts.append("")
    parts.extend(beads_block(cwd, ["--status=in_progress", "--limit=3"], "### Active Beads"))
    print("\n".join(parts))


@command_context.command("nx-preflight")
@click.argument("args", nargs=-1)
def nx_preflight(args: tuple[str, ...]) -> None:
    """Print the conexus plugin preflight check output.

    Runs dynamic checks for nx CLI, nx doctor, bd, uv, npx, and CLAUDE.md
    agent readiness.  Reports PASS/FAIL/WARN per dependency.
    """
    print(render_nx_preflight(Path.cwd()))
