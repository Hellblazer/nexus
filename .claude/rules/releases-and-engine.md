---
paths:
  - "pyproject.toml"
  - "uv.lock"
  - "mcpb/**"
  - ".claude-plugin/**"
  - "conexus/.claude-plugin/**"
  - "sn/.claude-plugin/**"
  - "CHANGELOG.md"
  - "conexus/PENDING_RELEASE.md"
  - "service/**"
  - "src/nexus/engine_version.py"
  - "scripts/check_engine_release_floor.py"
  - "scripts/cut_plugin_release.py"
  - "scripts/plugin_cut_back_merge.sh"
  - "scripts/mvnw-leased.sh"
  - "tests/e2e/release-battery.sh"
  - ".claude/skills/release/**"
  - ".claude/skills/engine-release/**"
---

## Release cadence policy (nexus-mkj6u)

Full text: [release-cadence](../../docs/agents/release-cadence.md).

1. **Releases are hand-cut.** CI does not publish on merge; a tag push publishes.
2. **`source.ref` only ever points at immutable release tags.** Never a branch, never main HEAD.
3. **One channel until proven otherwise.** No `-dev` / `-rc` / `-canary` variants.
4. **Bump cadence matches user-visible impact, not commit volume.**
5. **Releaser is human. AI prepares; human cuts** (merge, tag, push tag).
6. **Parity tests stay strict.** `pyproject.toml` version must match `mcpb/pyproject.toml`, `mcpb/manifest.json`, marketplace.json `plugins[].version` and `source.ref`, every plugin's `plugin.json`, and `uv.lock`. No `# noqa` escape hatches.

## Independent plugin release channel (RDR-197)

Full text: [plugin-release-channel](../../docs/agents/plugin-release-channel.md). Plugin-surface content (`conexus/`, `sn/`, `.claude-plugin/marketplace.json`) ships without a client release via an anchored tag `plugin-v{X.Y.Z}-{n}`. The cut is `scripts/cut_plugin_release.py`; the post-cut back-merge is `scripts/plugin_cut_back_merge.sh`, never a bare merge. Never cut while a release PR is open; batch related plugin work. **Rehearse before every real cut:** `tests/e2e/plugin-cut-rehearsal/run.sh` must end `PLUGIN-CUT REHEARSAL PASSED`.

## Engine-service release (a SECOND lifecycle — decoupled from the PyPI release)

**Authority and the full incident archive: [engine-service-release](../../docs/agents/engine-service-release.md)** (the full text, not a summary). Invoke the `engine-release` skill as the executable checklist.

- The Java engine-service binary is cut by an `engine-service-vX.Y.Z` tag, separate from the PyPI release and not gated by it. **Prep is AI, the tag push is human (Sam).** Version is tag-stamped; there is no manifest to bump.
- Build or test the engine only through `scripts/mvnw-leased.sh`, never a bare `./mvnw`/`mvn` (one builder at a time). On a lab pool host pass `--lab-run <class>` rather than wrapping it in `lab-run`: the lease comes before the grant, so a build waiting its turn holds no cores (nexus-iqmih).
- **ONE engine identity per release:** bump `REQUIRED_ENGINE_VERSION` in `src/nexus/engine_version.py` unconditionally to the engine the release was built and gated with. It also derives `PINNED_SERVICE_TAG`. Local-mode installs get only what that constant names.
- **Paired release:** cut the engine tag FIRST (a tag cut is never blocked); the client release bumps the pin in the same release; the deploy fires before or at client-tag push, never "eventually". `scripts/check_engine_release_floor.py --client-precondition engine-service-vX.Y.Z` gates the DEPLOY, never the tag cut.
- Deploy and cloud-gate are conexus-side: surface "relay: deploy `engine-service-vX.Y.Z` + re-gate" to Sam, never as autonomous.
- After every cloud deploy run `tests/e2e/cloud-client-path-gate.sh` from a cloud-mode box with `NX_EXPECTED_OWNERLESS_WRITE_MODE=log-only` (or `=enforce` after the flip); plain, it FAILS leg B3.

## Cutting a release (version bump + tag-push to PyPI)

**The `release` skill is the authority and the executable checklist.** Invoke it (`/conexus:release`, or `Skill` on `release`); this section is a pointer. `docs/contributing.md#release-process` is the long form for rollback and one-time setup. Full text: [cutting-a-release](../../docs/agents/cutting-a-release.md).

**Step 0 stays here because other sections cite it.** The engine-freshness gate is BLOCKING and is a command, not a prose eyeball-check (nexus-i5c2u):

```bash
uv run python scripts/check_engine_release_floor.py
```

Non-zero means STOP. It fails in both directions — a cloud behind the pinned identity, and a gated engine tag that was never pinned — and the remedy differs per direction. The skill's Step 0 carries both, the `--paired-deploy` form for a paired release (nexus-k1c08), and the post-tag verify obligation.
