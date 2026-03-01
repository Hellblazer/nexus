# Post-Mortem: RDR-011 — PDF Ingest Test Coverage

**Date:** 2026-03-01
**Close reason:** implemented
**PR:** https://github.com/Hellblazer/nexus/pull/47

## What Was Delivered

38 new tests across 5 new files, all passing without API keys in 1.4s (budget: 30s).

| File | Tests | ACs |
|------|-------|-----|
| `test_pdf_chunker_integration.py` | 11 | AC-U1–U11 |
| `test_pdf_extractor_integration.py` | 6 | AC-U12–U17 |
| `test_pdf_subsystem.py` | 7 | AC-S1–S6 |
| `test_pdf_e2e.py` | 4 | AC-E1–E4 |
| `test_indexer_e2e.py` (augmented) | 1 | AC-E5 |

Production fix: `_pdf_chunks` now uses 1-based page numbers from character-offset boundaries.

## Divergences from Plan

1. **No `tests/fixtures/` directory**: The plan said to create a `tests/fixtures/` directory and commit `type3_font.pdf` as a binary. Instead, `_make_type3_pdf()` was added to `conftest.py` as a session-scoped generator that builds the binary at test time. This is cleaner — avoids a committed binary and keeps all fixture logic in one place.

2. **`pdf_subject` / `pdf_keywords` metadata fields**: The plan referenced adding `pdf_subject` and `pdf_keywords` as new fields mapped from PDF document metadata. These were not added; the existing field set proved sufficient for all ACs. The implementation plan item 10 ("add `test_pdf_metadata_schema_complete`") was not implemented as a standalone test — coverage is already provided by `test_simple_pdf_full_metadata` (AC-S1).

3. **Phase numbering shift**: The plan used "Phase 0–4" with a different numbering from the plan document's "Phase 1–5". Commits used the 0-based scheme, which aligns with the RDR acceptance criteria numbering (AC-E1 etc.).

## Key Lessons

- **`insert_text` vs `insert_textbox`**: PyMuPDF's `insert_text` clips to one line (~95 chars). E2E page attribution only works with `insert_textbox` which wraps text across the full page rectangle (~2000 chars/page). This caused AC-E2 to pass with `[1, 1]` page numbers until the fixture was fixed.
- **Credential patching scope**: Patching `_has_credentials` is insufficient when the callee also calls `get_credential` directly. Patching `nexus.config.get_credential` covers both sites and is the correct approach for credential-isolation tests.
- **Staleness guard + embedding model**: `_local_embed` must return the input `model` unchanged (not a stub like `"test-local"`); otherwise the hash+model pair doesn't match on re-index and the guard doesn't fire.
