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
