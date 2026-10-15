# Release cadence policy

Moved from AGENTS.md on 2026-10-14 (nexus-2ziaz); AGENTS.md keeps only the per-turn rules and links here. A `§ <heading>` reference below names a section of AGENTS.md or of a sibling file; [README.md](README.md) maps each heading to its file.

### Release cadence policy (nexus-mkj6u)

Six rules borrowed from the global `marketplace-pinned-source-playbook`:

1. **Releases are hand-cut.** CI does not publish on merge. Tag-push triggers publish. Merges to main between releases do not affect installed users (marketplace.json's `source.ref` stays pinned to the previous tag).
2. **`source.ref` only ever points at immutable release tags.** Never at a branch, never at main HEAD. Optional `source.sha` for force-push protection.
3. **One channel until proven otherwise.** No `-dev` / `-rc` / `-canary` suffix variants. If a beta channel becomes necessary, file an RDR.
4. **Bump cadence matches user-visible impact, not commit volume.** Many internal PRs can land on develop and then on main without bumping the version. The version bumps when users would see something change.
5. **Releaser is human. AI prepares; human cuts.** AI can draft the release PR, bump manifests, write the CHANGELOG entry. The human runs `gh pr merge` + `git tag` + `git push origin vX.Y.Z`.
6. **Parity tests stay strict.** Any drift between `pyproject.toml` version and the other SIX version surfaces (`mcpb/pyproject.toml`, `mcpb/manifest.json`, marketplace.json's `plugins[].version` field for every plugin the file lists, marketplace.json's `plugins[].source.ref` for every plugin, every plugin's own `<plugin>/.claude-plugin/plugin.json` version, `uv.lock`) fails CI — seven surfaces total counting `pyproject.toml` itself, same enumeration `docs/contributing.md` § Release Process Step 7 carries (nexus-smsau, 2026-09-27 — this line previously said "six" while ALSO naming only `sn/.claude-plugin/plugin.json`, which had no parity test for `conexus/.claude-plugin/plugin.json` at all, and appending `uv.lock` outside the six it did count; the two files disagreed on both the number and the membership. Fixed by adding `test_every_plugins_own_plugin_json_version_matches_pyproject`, which loops over every plugin's own `plugin.json` instead of hardcoding "sn" — conexus is now enforced too, and so is any plugin added later). No `# noqa` escape hatches.
