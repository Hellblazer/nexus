### Fixed

- **The owner stamper's acknowledgement deadlines survive a slow engine start and a stamper fault.** The rebuild of the
  deadline timer after an engine start now retries, with a doubling delay capped at five minutes, until one full
  census of the tenants and their owner topics completes, instead of making one attempt five seconds after start. A
  `stamper_error` owner row now runs the owner deadline like every other owner row, so a red whose evaluation failed
  is handed on or escalated when nobody acknowledges it (nexus-igea9).
- **A client can no longer forge the engine's owner or clear state.** The owner decision counted a prior owner or clear
  row when its `from` dim said `engine`, a value any data-scope writer can set. It now counts one only when the engine
  wrote it, that is, when the row carries no sender session (nexus-igea9).
