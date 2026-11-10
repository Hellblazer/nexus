### Fixed

- **`nx doctor` no longer scans every document to decide whether an upgrade step is pending** (nexus-mjmjl). On a local engine, the upgrade-ladder row ran a rung's dry-run scan of the whole catalog (a profile put about 210 s of a 207-239 s doctor run there, longer under index-build load). Doctor now makes only bounded reads for that row, on any engine: a rung answers from its completion record or one engine count, and a rung that cannot is reported "not checked", with `nx upgrade --dry-run` named as the full check (a read of every document) and `nx upgrade` to check and fill.
