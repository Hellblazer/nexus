### Fixed

- A recall-canary, query-ring or REINDEX-phase failure now logs the database's SQLState and its own error message. It used to log jOOQ's message, whose leading SQL text filled the 200-character cap, so the cloud's canary failures never showed their cause. (nexus-4lnn1.1.10)
