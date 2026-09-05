# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fix E5 / nexus-eae0: _extract_context unit tests.

Tests _extract_context from nexus.indexer (introduced in Fix A / nexus-2tob):
- Python class + method: returns ('MyClass', 'my_method')
- Java class + method: returns ('BankAccount', 'deposit')
- Unsupported language: returns ('', '')
- Chunk spanning two sibling methods: returns ('Foo', '')
- Decorated Python function: returns method name from wrapped function_definition
"""
import pytest

try:
    from tree_sitter_language_pack import get_parser  # noqa: F401
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

from nexus.indexer import _extract_context

pytestmark = pytest.mark.skipif(
    not TREE_SITTER_AVAILABLE,
    reason="tree-sitter-language-pack not available",
)


# ── Python ─────────────────────────────────────────────────────────────────────

def test_extract_context_python() -> None:
    """Python class + method: chunk inside method body returns both names."""
    source = b"""class MyClass:
    def my_method(self):
        x = 1
        return x
"""
    # 0-indexed: class spans 0-3, method spans 1-3, chunk is lines 2-3
    result = _extract_context(source, "python", 2, 3)
    assert result == ("MyClass", "my_method"), f"Got {result!r}"


# ── Java ───────────────────────────────────────────────────────────────────────

def test_extract_context_java() -> None:
    """Java class + method: chunk inside method body returns both names."""
    source = b"""public class BankAccount {
    private double balance;

    public void deposit(double amount) {
        if (amount <= 0) {
            throw new IllegalArgumentException("Deposit must be positive");
        }
        balance += amount;
    }
}
"""
    # class spans 0-9, deposit method spans 3-8, chunk is lines 5-7
    result = _extract_context(source, "java", 5, 7)
    assert result == ("BankAccount", "deposit"), f"Got {result!r}"


# ── Unsupported language ───────────────────────────────────────────────────────

def test_extract_context_unsupported_language() -> None:
    """Unknown language key returns ('', '') without raising."""
    result = _extract_context(b"x = 1\ny = 2\n", "cobol", 0, 1)
    assert result == ("", ""), f"Expected ('', ''), got {result!r}"


# ── Multi-method span ──────────────────────────────────────────────────────────

def test_extract_context_multi_method_span() -> None:
    """Chunk spanning two sibling methods yields class but empty method name.

    When neither method fully encloses the chunk, method_name stays ''.
    Only the enclosing class should be returned.
    """
    source = b"""class Foo:
    def alpha(self):
        return 1

    def beta(self):
        return 2
"""
    # alpha spans lines 1-2, beta spans lines 4-5
    # chunk 1-5 spans both — no single method fully encloses it
    result = _extract_context(source, "python", 1, 5)
    assert result[0] == "Foo", f"Expected class='Foo', got {result!r}"
    assert result[1] == "", f"Expected method='', got {result!r} (chunk spans two methods)"


# ── Decorated Python functions ─────────────────────────────────────────────────

def test_extract_context_decorated_python_function() -> None:
    """Decorated Python function: chunk inside body returns correct method name.

    Regression for C3: decorated_definition nodes have no direct identifier
    child — the name is on the wrapped function_definition.  _extract_name_from_node
    must recurse into the inner node to return the correct name.
    """
    source = b"""class MyService:
    @staticmethod
    def process(data):
        return data.strip()
"""
    # decorated_definition spans lines 1-3, function_definition spans lines 2-3
    # chunk is lines 3 (return statement)
    result = _extract_context(source, "python", 3, 3)
    assert result == ("MyService", "process"), (
        f"Decorated method name not extracted: got {result!r}"
    )


# ── _is_import_only_chunk (RDR-200 Phase 1c evidence hygiene, nexus-4jj40) ──────
#
# A chunk consisting only of a package/module declaration plus import
# statements is structural, not evidentiary -- near-identical header
# boilerplate crowds out real matches in cross-repo semantic search. This
# classification is per CHUNK CONTENT only: no path markers, no category
# rules (the two approaches this bead already tried and reverted).

from nexus.code_indexer import _is_import_only_chunk  # noqa: E402


def test_import_only_chunk_java_header() -> None:
    """Java: package + import declarations only -> import-only."""
    source = b"""package com.example.foo;

import java.util.List;
import static java.util.Map.Entry;

public class Foo {
    void bar() {}
}
"""
    # lines 0-3 (0-indexed): package_declaration, blank, import, import static
    assert _is_import_only_chunk(source, "java", 0, 3) is True


def test_import_only_chunk_java_mixed_is_not_import_only() -> None:
    """Java: a chunk that also covers the class declaration is mixed."""
    source = b"""package com.example.foo;

import java.util.List;

public class Foo {
    void bar() {}
}
"""
    # lines 0-6: package + import + the whole class declaration
    assert _is_import_only_chunk(source, "java", 0, 6) is False


def test_import_only_chunk_python_header() -> None:
    """Python: __future__ import, import, from-import only -> import-only."""
    source = b"""from __future__ import annotations
import os
from typing import Any


def foo():
    pass
"""
    # lines 0-2: future_import_statement, import_statement, import_from_statement
    assert _is_import_only_chunk(source, "python", 0, 2) is True


def test_import_only_chunk_python_mixed_is_not_import_only() -> None:
    """Python: a chunk that also covers the function definition is mixed."""
    source = b"""from __future__ import annotations
import os


def foo():
    pass
"""
    assert _is_import_only_chunk(source, "python", 0, 4) is False


def test_import_only_chunk_typescript_header() -> None:
    """TypeScript: import statements only -> import-only."""
    source = b"""import { foo } from "./foo";
import type { Bar } from "./bar";

export function baz() {}
"""
    assert _is_import_only_chunk(source, "typescript", 0, 1) is True


def test_import_only_chunk_typescript_mixed_is_not_import_only() -> None:
    """TypeScript: a chunk that also covers the exported function is mixed."""
    source = b"""import { foo } from "./foo";

export function baz() {}
"""
    assert _is_import_only_chunk(source, "typescript", 0, 2) is False


def test_import_only_chunk_unsupported_language_never_classified() -> None:
    """A language absent from ``_IMPORT_NODE_TYPES`` is never import-only,
    matching ``_extract_context``'s fail-conservative design -- an
    unlisted language's chunks are left alone rather than guessed at."""
    assert _is_import_only_chunk(b"use foo;\n", "cobol", 0, 0) is False


def test_import_only_chunk_no_overlapping_node_is_not_import_only() -> None:
    """A chunk range with no overlapping top-level node (e.g. past the end
    of the file) is never classified as import-only -- there is nothing to
    classify, so this must not vacuously return True."""
    source = b"import os\n"
    assert _is_import_only_chunk(source, "python", 100, 200) is False


# ── Comment handling (round 4 fold-in, T2 critique [24606] Critical 1) ──────────
#
# A comment must never disqualify an otherwise import-only chunk (a BSD/SPDX
# license header preceding package+imports, or a trailing Javadoc summary),
# but a chunk of comments ALONE (no header statement) must still be "mixed".

def test_import_only_chunk_java_license_header_does_not_disqualify() -> None:
    """A block-comment license header preceding package+imports must not
    flip the chunk to 'mixed' -- this is the exact CHOAM.java failure mode
    T2 [24606] Critical 1 found (a BSD-3-Clause header made the chunk
    classify False pre-fix)."""
    source = b"""/*
 * Copyright (c) 2026 Example Corp. All rights reserved.
 * Licensed under the BSD 3-Clause License.
 */
package com.example.foo;

import java.util.List;
"""
    # lines 0-6 (0-indexed): block_comment, package_declaration, blank, import
    assert _is_import_only_chunk(source, "java", 0, 6) is True


def test_import_only_chunk_java_trailing_javadoc_does_not_disqualify() -> None:
    """A trailing Javadoc comment (the class's one-line summary, sitting
    in the SAME chunk as the tail imports but before the class body
    starts) must not flip the chunk to 'mixed' either -- the other half
    of the CHOAM.java failure mode."""
    source = b"""import java.util.List;
import java.util.Map;

/**
 * Foo does the thing.
 */
"""
    assert _is_import_only_chunk(source, "java", 0, 5) is True


def test_import_only_chunk_java_comments_only_is_not_import_only() -> None:
    """A chunk of comments alone, with no package/import statement at
    all, must still classify False -- this function finds import-only
    chunks specifically; a bare comment block is not one, even though it
    is separately non-evidentiary."""
    source = b"""/*
 * Copyright (c) 2026 Example Corp. All rights reserved.
 */
"""
    assert _is_import_only_chunk(source, "java", 0, 2) is False


def test_import_only_chunk_python_module_docstring_counts_as_evidence() -> None:
    """A module docstring is real, author-written content (module
    purpose, usage notes) -- unlike a comment, it usually carries
    evidentiary value, so a chunk mixing a docstring with imports must
    stay 'mixed' and never be stamped 'imports' (documented design
    decision, T2 critique [24606] item 1)."""
    source = b'"""Module docstring explaining purpose."""\nimport os\nimport sys\n'
    assert _is_import_only_chunk(source, "python", 0, 2) is False


def test_import_only_chunk_python_comment_header_does_not_disqualify() -> None:
    """A '#' comment header (e.g. a license notice) preceding imports
    must not disqualify the chunk, matching the Java case."""
    source = b"# Copyright (c) 2026 Example Corp.\n# SPDX-License-Identifier: MIT\nimport os\nimport sys\n"
    assert _is_import_only_chunk(source, "python", 0, 3) is True


# ── Real-file end-to-end split (round 4 fold-in): a license-headed Java ─────────
# file large enough for the REAL CodeSplitter (~1500-char budget, T2 [24606]
# Observation 2) to separate the header from the class body into distinct
# chunks -- not hand-picked line ranges. Reproduces the shape of the bead's
# own cited evidence (delos/choam/CHOAM.java): a multi-line license header,
# package, many imports, and a class Javadoc split so the header chunk
# stands alone.

def _license_headed_java_source(import_count: int = 60) -> str:
    license_header = (
        "/*\n"
        " * Copyright (c) 2026 Example Corp. All rights reserved.\n"
        " * Licensed under the BSD 3-Clause License.\n"
        " * See the LICENSE file for details.\n"
        " */\n"
    )
    package_line = "package com.example.widget;\n\n"
    imports = "\n".join(
        f"import com.example.widget.pkg{i}.Thing{i};" for i in range(import_count)
    ) + "\n\n"
    javadoc = "/**\n * WidgetProcessor handles the processing of widgets.\n */\n"
    class_body = (
        "public class WidgetProcessor {\n"
        "    private int count;\n\n"
        "    public WidgetProcessor() {\n"
        "        this.count = 0;\n"
        "    }\n\n"
        "    public void process(String input) {\n"
        "        if (input == null) {\n"
        "            throw new IllegalArgumentException(\"input must not be null\");\n"
        "        }\n"
        "        count += 1;\n"
        "    }\n\n"
        "    public int getCount() {\n"
        "        return count;\n"
        "    }\n"
        "}\n"
    )
    return license_header + package_line + imports + javadoc + class_body


def test_real_file_license_header_chunk_stamped_class_chunk_not(tmp_path) -> None:
    """End-to-end through the REAL chunk_file() AST splitter -- not a
    hand-picked line range. A license-headed, twenty-plus-import Java
    file big enough to force a real chunk boundary between the header
    and the class body: every chunk before the class declaration must
    classify import-only, and the chunk containing the class declaration
    must not."""
    from nexus.chunker import chunk_file

    content = _license_headed_java_source()
    source_bytes = content.encode("utf-8")
    java_file = tmp_path / "WidgetProcessor.java"
    java_file.write_text(content)

    chunks = chunk_file(java_file, content)
    # Non-vacuity guard: this fixture is sized to force a real split (a
    # future CodeSplitter/llama-index change that raised the byte budget
    # enough to collapse this back to one chunk would otherwise let this
    # test pass by having nothing left to check.
    assert len(chunks) >= 2, (
        f"fixture must produce >= 2 chunks to exercise the header/class "
        f"split; got {len(chunks)} -- widen the fixture (more imports)"
    )

    class_chunk_seen = False
    for chunk in chunks:
        is_import_only = _is_import_only_chunk(
            source_bytes, "java", chunk["line_start"] - 1, chunk["line_end"] - 1,
        )
        if "class WidgetProcessor" in chunk["text"]:
            class_chunk_seen = True
            assert is_import_only is False, (
                f"class-declaration chunk wrongly classified import-only: "
                f"{chunk['text'][:80]!r}"
            )
        else:
            assert is_import_only is True, (
                f"header-region chunk wrongly classified mixed: "
                f"{chunk['text'][:80]!r}"
            )
    assert class_chunk_seen, "fixture's class declaration must land in some chunk"


def test_import_only_chunk_csharp_header() -> None:
    """C#: using directives only -> import-only."""
    source = b"""using System;
using System.Collections.Generic;

namespace Foo {
    class Bar {}
}
"""
    # lines 0-1 (0-indexed): using_directive, using_directive
    assert _is_import_only_chunk(source, "c_sharp", 0, 1) is True


def test_import_only_chunk_csharp_mixed_is_not_import_only() -> None:
    """C#: a chunk that also covers the namespace/class is mixed."""
    source = b"""using System;
using System.Collections.Generic;

namespace Foo {
    class Bar {}
}
"""
    assert _is_import_only_chunk(source, "c_sharp", 0, 5) is False


def test_import_only_chunk_kotlin_header() -> None:
    """Kotlin: package header + import list only -> import-only."""
    source = b"""package com.example

import java.util.List

class Foo {}
"""
    # lines 0-2 (0-indexed): package_header, blank, import_list
    assert _is_import_only_chunk(source, "kotlin", 0, 2) is True


def test_import_only_chunk_kotlin_mixed_is_not_import_only() -> None:
    """Kotlin: a chunk that also covers the class declaration is mixed."""
    source = b"""package com.example

import java.util.List

class Foo {}
"""
    assert _is_import_only_chunk(source, "kotlin", 0, 4) is False


def test_import_only_chunk_rust_header() -> None:
    """Rust: a use declaration alone -> import-only."""
    source = b"""use std::collections::HashMap;

fn main() {}
"""
    assert _is_import_only_chunk(source, "rust", 0, 0) is True


def test_import_only_chunk_rust_mixed_is_not_import_only() -> None:
    """Rust: a chunk that also covers the function item is mixed."""
    source = b"""use std::collections::HashMap;

fn main() {}
"""
    assert _is_import_only_chunk(source, "rust", 0, 2) is False
