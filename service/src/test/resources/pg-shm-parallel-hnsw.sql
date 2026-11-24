-- Row 24 (nexus-p2m1x.22): a parallel HNSW build takes one shared area of maintenance_work_mem in /dev/shm.
-- At 256MB it fails on Docker's 64 MB default ("could not resize shared memory segment ... No space left on device")
-- and builds on the test container's 1 GiB. Run by PgContainerTmpfsGateTest through psql -f inside the container.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE shm_probe(v vector(3));
INSERT INTO shm_probe SELECT ARRAY[random(), random(), random()]::vector FROM generate_series(1, 50000);
ALTER TABLE shm_probe SET (parallel_workers = 2);
SET maintenance_work_mem = '256MB';
SET max_parallel_maintenance_workers = 2;
SET client_min_messages = debug1;
CREATE INDEX ON shm_probe USING hnsw (v vector_l2_ops);
