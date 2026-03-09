# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unified language registry for code indexing.

Single source of truth for extension-to-language mapping, consumed by
chunker.py (AST splitting), indexer.py (context extraction), and
classifier.py (CODE vs PROSE classification).
"""

LANGUAGE_REGISTRY: dict[str, str] = {
    # Core web/scripting
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",  # tree-sitter has separate tsx grammar
    # Systems languages
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".rs": "rust",
    ".go": "go",
    # JVM family
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sc": "scala",
    # .NET
    ".cs": "c_sharp",
    # Shell / scripting
    ".sh": "bash",
    ".bash": "bash",
    # Mobile / cross-platform
    ".swift": "swift",
    ".m": "objc",
    # Interpreted
    ".rb": "ruby",
    ".php": "php",
    ".r": "r",
    ".lua": "lua",
    # Protocol / schema
    ".proto": "proto",
    # BEAM ecosystem
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hrl": "erlang",
    # Functional languages
    ".hs": "haskell",
    ".clj": "clojure",
    ".cljs": "clojure",
    ".cljc": "clojure",
    ".ml": "ocaml",
    ".mli": "ocaml_interface",
    ".el": "elisp",
    # Modern systems / app languages
    ".dart": "dart",
    ".zig": "zig",
    ".jl": "julia",
    # Scripting
    ".pl": "perl",
    ".pm": "perl",
}

# Extensions classified as CODE but not in LANGUAGE_REGISTRY
# (no tree-sitter grammar or AST chunking needed).
GPU_SHADER_EXTENSIONS: frozenset[str] = frozenset({
    ".cl", ".comp", ".frag", ".vert", ".metal", ".glsl", ".wgsl", ".hlsl",
})
