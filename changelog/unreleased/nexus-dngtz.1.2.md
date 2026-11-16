### Fixed

- **The correlation id now reaches the worker threads of `nx index repo`.** The three thread pools it makes engine calls from (the per-file "nx-index" pool, the chunk batcher's "nx-flush" pool and the PDF pipeline's three stage threads) submitted their work bare, and a bare `ThreadPoolExecutor.submit` does not carry a `ContextVar` into the worker. Each submit now runs in its own copied Context, so a log line emitted in a worker carries the id the invocation bound. Other thread-starting code is unchanged. RDR-167, Phase 1.
