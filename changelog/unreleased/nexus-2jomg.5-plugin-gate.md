### Changed

- **The plugin ledger is a gate on the pull request into main (Sam, 2026-11-27).** `plugin-drift-ledger.yml` is now a called workflow and `ci.yml`'s `plugin-ledger-gate` is its one caller, so `pytest-gate` reads its result: blocking when the event is a pull request into main, a `::warning::` otherwise, the shape of `mcpb-resolve-gate`. The job takes a runner only when `scripts/plugin_surface.py` finds the plugin surface in the diff, and always on a pull request into main. The workflow no longer triggers itself, so the tree is tested once.
