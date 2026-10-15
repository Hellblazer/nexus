# Cutting a release, full text

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); AGENTS.md keeps only the per-turn rules and links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

### Cutting a release (version bump + tag-push to PyPI)

**The `release` skill is the authority and the executable checklist.** Invoke it
(`/conexus:release`, or `Skill` on `release`); this section is a pointer, not a
parallel copy. `docs/contributing.md#release-process` is the long form for
rollback and one-time setup.

The authority used to run the other way and the two drifted, which is why it was
inverted (2026-09-20). Measured on 2026-09-19: this file recorded the
remediation-commit gate as RETIRED while the skill listed it as a live preflight
leg 57 lines after carrying that retirement inside an HTML comment; the skill's
`git add` block ended in a trailing backslash that swallowed its own
`git commit`, so it staged files and never committed; the skill described a
SQLite backend deleted at RDR-158 in the present tense; and "the human pushes
the tag" had widened in the skill into "human by default, OR the AI pushes it
when authorized". Two copies of a procedure decay until the stale one wins an
argument, and a reader had no way to tell which was current.

What did NOT move, and why this is not a mechanical rule: § Engine-service
release ABOVE is not a pointer and should not become one. It is not a summary
of its skill — it is the incident archive that skill defers to, and the
`engine-release` skill's own back-references name this file as the authority on
wording conflicts. A pointer cannot win a wording conflict.

[2026-10-14, nexus-2ziaz: the paragraph above describes the layout before the move. § Engine-service release now lives in full in [engine-service-release.md](engine-service-release.md), and THAT file is the authority on wording conflicts; AGENTS.md carries a short rules-only section under the same heading.]

**Step 0 stays here because other sections cite it.** The engine-freshness gate
is BLOCKING and is a command, not a prose eyeball-check (nexus-i5c2u):

```bash
uv run python scripts/check_engine_release_floor.py
```

Non-zero means STOP. It fails in both directions — a cloud behind the pinned
identity, and a gated engine tag that was never pinned — and the remedy differs
per direction. The skill's Step 0 carries both, the `--paired-deploy` form for a
paired release (nexus-k1c08), and the post-tag verify obligation. `ONE engine
identity per release` and the `REQUIRED_ENGINE_VERSION` -> `PINNED_SERVICE_TAG`
derivation are stated in § Engine-service release above.
