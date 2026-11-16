### Added

- **The engine gains `nexus.data_moves`, the registry of background data moves (RDR-246).** One row per registered move: its DML template, driving table, chunk size, throttle, verification query, optional finishing SQL, supersession link and state. It is a global table with no row-level security, read and written only by the engine's migrating role (`nexus_admin`); `nexus_svc` holds no privilege on it, and the runAlways service-grant loop now skips it. Schema only, no data effect (nexus-f3yqd.2.12).
