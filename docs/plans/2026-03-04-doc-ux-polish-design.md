# Design: White-Glove Documentation and UX Polish

**Date**: 2026-03-04
**Status**: approved

## Scope

Six areas, approved by user:

### A. Fix Inconsistencies
- `storage-tiers.md`: CHROMA_TENANT listed as required → optional
- `getting-started.md`: "14 agents, 27 skills" → "15 agents, 28 skills"
- `README.md`: "12 languages" → "19 languages (27 file types)"

### B. Config Init Wizard — UX Rewrite
- `chroma_database` signup entry: remove fake URL/nav-path; since init auto-provisions, just tell user to pick a name
- Add visual separator between credential collection and provisioning phases
- Fix provisioning failure block formatting
- Print "Next steps" block at end: `nx doctor` → `nx index repo .`

### C. `nx doctor` Improvements
- `bd` check: move GitHub URL to a `Fix:` line
- Remove `uv` from doctor (install-time, not runtime)
- "db not reachable" fix block: replace generic dashboard advice with "Run `nx config init` to provision databases automatically — no dashboard visit needed"
- Doctor footer already says "run config init" — ensure text describes what init does

### D. Troubleshooting Section in `getting-started.md`
After the "Verify" section, new "Troubleshooting first-run issues":
- Credentials not set → `nx config init`
- Provisioning failed (plan restriction) → create 4 databases manually
- `nx index repo .` fails "credentials not set" → T3 required; local commands still work
- Rate limit on first large index → `--monitor` flag

### E. Memory List Output
Change `[id] title (agent, timestamp)` → `[id] project/title  (agent, timestamp)`

### F. Minor Copy Polish
- `config init` intro: note config file location more clearly upfront
- `doctor` CHROMA_TENANT line: add "(set explicitly only for multi-workspace)"
- `store put` docstring: add stdin pipe example
