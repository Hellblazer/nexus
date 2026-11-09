### Added

- **`nx doctor` shows each supervised daemon's own memory.** The storage service supervisor and the aspect workers write their resident memory, peak, CPU seconds and thread count into their lease on each heartbeat (sampled at most every 30 s, standard library only, nothing spawned), and log once when resident memory crosses `NX_FOOTPRINT_WARN_MB` (default 1024). A new "Process footprint" doctor row, shown in the console's health panel too, lists them and warns past the threshold. Memory reads on Linux and macOS; Windows reports CPU and threads only (nexus-3ceda.3).
