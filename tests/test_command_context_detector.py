# SPDX-License-Identifier: Apache-2.0
"""TDD tests for the project-type detector (RDR-130 P2.1).

Tests the detect_project_types(root: Path) -> list[str] helper that will
live in src/nexus/commands/command_context.py. These tests are written RED
first — the helper does not exist yet.

All assertions are exact (== or `in`) per feedback_exact_assertions_for_fixture_regression.
"""
from pathlib import Path

import pytest

from nexus.commands.command_context import detect_project_types


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_dir(tmp_path: Path, *filenames: str) -> Path:
    """Create filenames inside tmp_path and return tmp_path."""
    for name in filenames:
        (tmp_path / name).touch()
    return tmp_path


# ---------------------------------------------------------------------------
# Unknown / empty
# ---------------------------------------------------------------------------


def test_empty_dir_returns_unknown(tmp_path: Path) -> None:
    result = detect_project_types(tmp_path)
    assert result == ["- Unknown (no recognized build/marker file)"]


# ---------------------------------------------------------------------------
# 24 individual ecosystem tests (spec order)
# ---------------------------------------------------------------------------


def test_python_pyproject(tmp_path: Path) -> None:
    make_dir(tmp_path, "pyproject.toml")
    result = detect_project_types(tmp_path)
    assert "- Python" in result


def test_python_setup_py(tmp_path: Path) -> None:
    make_dir(tmp_path, "setup.py")
    result = detect_project_types(tmp_path)
    assert "- Python (setup.py)" in result


def test_rust(tmp_path: Path) -> None:
    make_dir(tmp_path, "Cargo.toml")
    result = detect_project_types(tmp_path)
    assert "- Rust" in result


def test_go(tmp_path: Path) -> None:
    make_dir(tmp_path, "go.mod")
    result = detect_project_types(tmp_path)
    assert "- Go" in result


def test_nodejs_typescript(tmp_path: Path) -> None:
    make_dir(tmp_path, "package.json")
    result = detect_project_types(tmp_path)
    assert "- Node.js / TypeScript" in result


def test_java_kotlin_maven(tmp_path: Path) -> None:
    make_dir(tmp_path, "pom.xml")
    result = detect_project_types(tmp_path)
    assert "- Java/Kotlin (Maven)" in result


def test_java_kotlin_gradle_exact(tmp_path: Path) -> None:
    """build.gradle (exact name) matches Gradle."""
    make_dir(tmp_path, "build.gradle")
    result = detect_project_types(tmp_path)
    assert "- Java/Kotlin (Gradle)" in result


def test_ruby(tmp_path: Path) -> None:
    make_dir(tmp_path, "Gemfile")
    result = detect_project_types(tmp_path)
    assert "- Ruby" in result


def test_php(tmp_path: Path) -> None:
    make_dir(tmp_path, "composer.json")
    result = detect_project_types(tmp_path)
    assert "- PHP" in result


def test_csharp_dotnet_exact(tmp_path: Path) -> None:
    """An exact .csproj file matches C#/.NET."""
    make_dir(tmp_path, "MyApp.csproj")
    result = detect_project_types(tmp_path)
    assert "- C#/.NET" in result


def test_c_cpp_cmake(tmp_path: Path) -> None:
    make_dir(tmp_path, "CMakeLists.txt")
    result = detect_project_types(tmp_path)
    assert "- C/C++ (CMake)" in result


def test_swift(tmp_path: Path) -> None:
    make_dir(tmp_path, "Package.swift")
    result = detect_project_types(tmp_path)
    assert "- Swift" in result


def test_elixir(tmp_path: Path) -> None:
    make_dir(tmp_path, "mix.exs")
    result = detect_project_types(tmp_path)
    assert "- Elixir" in result


def test_scala_sbt(tmp_path: Path) -> None:
    make_dir(tmp_path, "build.sbt")
    result = detect_project_types(tmp_path)
    assert "- Scala (sbt)" in result


def test_dart_flutter(tmp_path: Path) -> None:
    make_dir(tmp_path, "pubspec.yaml")
    result = detect_project_types(tmp_path)
    assert "- Dart/Flutter" in result


def test_clojure_edn(tmp_path: Path) -> None:
    make_dir(tmp_path, "deps.edn")
    result = detect_project_types(tmp_path)
    assert "- Clojure" in result


def test_clojure_leiningen(tmp_path: Path) -> None:
    make_dir(tmp_path, "project.clj")
    result = detect_project_types(tmp_path)
    assert "- Clojure (Leiningen)" in result


def test_haskell_cabal_exact(tmp_path: Path) -> None:
    """An exact .cabal file matches Haskell."""
    make_dir(tmp_path, "mylib.cabal")
    result = detect_project_types(tmp_path)
    assert "- Haskell" in result


def test_haskell_stack(tmp_path: Path) -> None:
    make_dir(tmp_path, "stack.yaml")
    result = detect_project_types(tmp_path)
    assert "- Haskell (Stack)" in result


def test_julia(tmp_path: Path) -> None:
    make_dir(tmp_path, "Project.toml")
    result = detect_project_types(tmp_path)
    assert "- Julia" in result


def test_r_language(tmp_path: Path) -> None:
    make_dir(tmp_path, "DESCRIPTION")
    result = detect_project_types(tmp_path)
    assert "- R" in result


def test_zig(tmp_path: Path) -> None:
    make_dir(tmp_path, "build.zig")
    result = detect_project_types(tmp_path)
    assert "- Zig" in result


def test_ocaml(tmp_path: Path) -> None:
    make_dir(tmp_path, "dune-project")
    result = detect_project_types(tmp_path)
    assert "- OCaml" in result


def test_crystal(tmp_path: Path) -> None:
    make_dir(tmp_path, "shard.yml")
    result = detect_project_types(tmp_path)
    assert "- Crystal" in result


# ---------------------------------------------------------------------------
# Glob pattern cases
# ---------------------------------------------------------------------------


def test_gradle_kts_glob(tmp_path: Path) -> None:
    """build.gradle.kts matches the build.gradle* pattern."""
    make_dir(tmp_path, "build.gradle.kts")
    result = detect_project_types(tmp_path)
    assert "- Java/Kotlin (Gradle)" in result


def test_csproj_glob_named(tmp_path: Path) -> None:
    """Foo.csproj matches *.csproj."""
    make_dir(tmp_path, "Foo.csproj")
    result = detect_project_types(tmp_path)
    assert "- C#/.NET" in result


def test_cabal_glob_named(tmp_path: Path) -> None:
    """mylib.cabal matches *.cabal."""
    make_dir(tmp_path, "mylib.cabal")
    result = detect_project_types(tmp_path)
    assert "- Haskell" in result


# ---------------------------------------------------------------------------
# Polyglot: multiple markers present -> all labels in spec order
# ---------------------------------------------------------------------------


def test_polyglot_all_labels_present(tmp_path: Path) -> None:
    """A directory with multiple markers returns ALL matching labels."""
    make_dir(tmp_path, "pyproject.toml", "Cargo.toml", "go.mod")
    result = detect_project_types(tmp_path)
    assert "- Python" in result
    assert "- Rust" in result
    assert "- Go" in result
    # No Unknown when matches exist
    assert "- Unknown (no recognized build/marker file)" not in result


def test_polyglot_spec_order(tmp_path: Path) -> None:
    """Labels appear in spec order, not filesystem order."""
    # Cargo.toml (index 2) and pyproject.toml (index 0) — Python must come first
    make_dir(tmp_path, "Cargo.toml", "pyproject.toml")
    result = detect_project_types(tmp_path)
    assert result.index("- Python") < result.index("- Rust")


def test_polyglot_no_unknown_when_matches(tmp_path: Path) -> None:
    """Unknown line must NOT appear when at least one marker matches."""
    make_dir(tmp_path, "package.json", "pom.xml")
    result = detect_project_types(tmp_path)
    assert "- Unknown (no recognized build/marker file)" not in result


def test_polyglot_full_kitchen_sink(tmp_path: Path) -> None:
    """All 24 markers present -> 24 labels, no Unknown."""
    make_dir(
        tmp_path,
        "pyproject.toml",
        "setup.py",
        "Cargo.toml",
        "go.mod",
        "package.json",
        "pom.xml",
        "build.gradle",
        "Gemfile",
        "composer.json",
        "MyApp.csproj",
        "CMakeLists.txt",
        "Package.swift",
        "mix.exs",
        "build.sbt",
        "pubspec.yaml",
        "deps.edn",
        "project.clj",
        "mylib.cabal",
        "stack.yaml",
        "Project.toml",
        "DESCRIPTION",
        "build.zig",
        "dune-project",
        "shard.yml",
    )
    result = detect_project_types(tmp_path)
    assert len(result) == 24
    assert "- Unknown (no recognized build/marker file)" not in result
    expected_labels = [
        "- Python",
        "- Python (setup.py)",
        "- Rust",
        "- Go",
        "- Node.js / TypeScript",
        "- Java/Kotlin (Maven)",
        "- Java/Kotlin (Gradle)",
        "- Ruby",
        "- PHP",
        "- C#/.NET",
        "- C/C++ (CMake)",
        "- Swift",
        "- Elixir",
        "- Scala (sbt)",
        "- Dart/Flutter",
        "- Clojure",
        "- Clojure (Leiningen)",
        "- Haskell",
        "- Haskell (Stack)",
        "- Julia",
        "- R",
        "- Zig",
        "- OCaml",
        "- Crystal",
    ]
    assert result == expected_labels


# ---------------------------------------------------------------------------
# Pure function: does not depend on cwd
# ---------------------------------------------------------------------------


def test_accepts_explicit_root_not_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """detect_project_types uses the root argument, not os.getcwd()."""
    make_dir(tmp_path, "Cargo.toml")
    # Change cwd to a different dir (one with no markers)
    monkeypatch.chdir(Path("/tmp"))
    result = detect_project_types(tmp_path)
    assert "- Rust" in result
