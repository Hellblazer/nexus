### Added

- **`conexus/hooks/audience.yaml`, its lint and a behaviour leg.** Every hook registration, module and injected constant declares who it is for (every user, the nexus checkout, or the maintainer), `tests/test_plugin_audience_lint.py` fails on one that does not, and `tests/e2e/plugin-out-of-repo-gate.sh` now runs the plugin's hooks in a scratch user repository, a marked nexus repository and a flagged account, with and without `nx`. `docs/agents/plugin-audiences.md` explains the model. (nexus-sby49.3)
