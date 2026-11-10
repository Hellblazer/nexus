### Fixed

- **`nx init` says why a re-run with no running service still exits 0** (nexus-f3yqd.6, F9). When the autostart unit was registered before the run and the service has not published its lease yet, `nx init` leaves bring-up to the unit and exits 0, while a fresh install in the same state exits 1. The message now says so, and names the supervisor and engine logs to read if the service is still not running after a minute.
