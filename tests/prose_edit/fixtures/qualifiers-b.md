# Failover notes

Failover may take longer than 30 seconds. The standby waits for the full lease TTL because the primary does not release the lease while it is partitioned.

The lease TTL is 30 seconds.
