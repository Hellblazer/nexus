# Classifier SKIP Extension — Design

**Date:** 2026-03-02
**Status:** Approved

## Problem

`docs__` collections contain noise: GPU shader files (`.cl`, `.comp`, `.frag`, `.vert`,
`.metal`), Protobuf schemas (`.proto`), build configs (`pom.xml`), `LICENSE`, `mvnw`,
`.json`, `.yml`, `.html`, `.css`, etc. are classified as PROSE because the classifier
has no SKIP class and treats every non-CODE/non-PDF file as prose.

Root cause: `_CODE_EXTENSIONS` is missing several code-like extensions, and there is no
concept of "known-useless" files that should not be indexed at all.

## Decision

Minimal fix only (YAGNI):

1. **Expand `_CODE_EXTENSIONS`** — add missing code-like extensions:
   `.proto`, `.cl`, `.comp`, `.frag`, `.vert`, `.metal`, `.glsl`, `.wgsl`, `.hlsl`

2. **Add `ContentClass.SKIP`** — new enum value for known-noise extensions that
   should not be indexed. The indexer silently ignores SKIP files.

3. **Define `_SKIP_EXTENSIONS`** — frozenset of extensions to skip:
   - Build/config: `.xml`, `.json`, `.yml`, `.yaml`, `.toml`, `.properties`,
     `.ini`, `.cfg`, `.conf`, `.gradle`
   - Web/markup: `.html`, `.htm`, `.css`, `.svg`
   - Shell/batch scripts (non-code): `.cmd`, `.bat`, `.ps1`
   - Lock files: `.lock`
   - Extensionless files: skip unless shebang-detected as code

4. **Shebang detection for extensionless files** — read first 2 bytes; if `#!`,
   classify as CODE (shell/python/etc.), else SKIP.

## Out of scope

- Haiku-based classification for unknown extensions (deferred)
- Metadata-based query filtering / semantic reranking (deferred)
- Fine-grained `doc_type` or `file_category` metadata fields (deferred, YAGNI)

## Files affected

- `src/nexus/classifier.py` — add `ContentClass.SKIP`, `_SKIP_EXTENSIONS`,
  expand `_CODE_EXTENSIONS`, update `classify()` logic
- `src/nexus/indexer.py` — handle `ContentClass.SKIP` in `_walk_repo_files()`
  (or wherever files are dispatched to indexing)
- `tests/test_classifier.py` — new tests for SKIP class and shebang detection

## Success criteria

- `ContentClass.SKIP` returned for `.xml`, `.json`, `.yml`, `.html`, `.css`, etc.
- `.proto`, `.cl`, `.comp`, `.frag`, `.vert`, `.metal` return `ContentClass.CODE`
- Extensionless file with `#!/usr/bin/env python` returns `ContentClass.CODE`
- Extensionless file with no shebang returns `ContentClass.SKIP`
- SKIP files are not passed to `_index_code_file` or `_index_prose_file`
- All existing tests continue to pass
