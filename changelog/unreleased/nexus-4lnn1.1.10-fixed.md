### Fixed

- A recall-canary, query-ring or REINDEX-phase failure now logs the database's SQLState and its own error message. It used to log jOOQ's message, whose leading SQL text filled the 200-character cap, so the cloud's canary failures never showed their cause. (nexus-4lnn1.1.10)
- The recall canary's exact leg, its brute-force reference search, now runs under the canary's two-minute run limit instead of the 30-second serving search bound. On a production copy it ran 25.6 s cold, close to that bound; the canary failures in the cloud after a boot and after a REINDEX are taken to be this timeout, inferred from the timing because their error code was not logged. The HNSW leg keeps the serving bound. (nexus-4lnn1.1.10)
