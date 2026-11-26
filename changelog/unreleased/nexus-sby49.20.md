### Changed

- **The two older strict-guard markers no longer count.** A checkout with no `.nexus/project.json` now reads as the default (the short deny list) whatever `.nexus/dev-process.json` or `scripts/agent-worktree-preflight.sh` hold; `nx settings show` no longer names them. A checkout that relied on `.nexus/dev-process.json` for the strict guard now needs `.nexus/project.json` with `"subagent_git": "strict"`. The old `maintainer-profile` file under `~/.config/nexus` is still listed by `nx settings show` as having no effect. (nexus-sby49.20)
