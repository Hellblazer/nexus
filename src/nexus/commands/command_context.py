# SPDX-License-Identifier: Apache-2.0
"""command_context — shared substrate for `nx command-context` subcommands (RDR-130 P2).

P2.1: project-type detector consumed by all P2.x preamble subcommands.
P2.2: Click group + subcommand scaffolding (added by nexus-sg7hb).
"""
from pathlib import Path

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
