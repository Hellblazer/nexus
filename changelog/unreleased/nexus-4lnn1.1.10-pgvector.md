### Changed

- The local PostgreSQL bundle now builds pgvector 0.8.7, the version the cloud runs, instead of 0.8.2. pgvector 0.8.3 fixed possible index corruption during HNSW vacuuming, and 0.8.4 fixed the "hnsw graph not repaired" error and errors with inserts during HNSW vacuuming. The fixes are in the extension's library, so an install that takes the new bundle gets them, while its catalog keeps reporting extension version 0.8.2 until `ALTER EXTENSION vector UPDATE` runs. (nexus-4lnn1.1.10)
