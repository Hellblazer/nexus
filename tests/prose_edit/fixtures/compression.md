# The reservation store

The reservation store holds one record for each job a worker has claimed. This page explains how leases expire and how the store raises alarms.

## Leases

A lease gives one worker the right to run a job until the lease expires. The lease renewal failure retry count resets after a successful renewal. A worker renews its lease every ten seconds.

A claim that sits in the table for hours because its worker crashed holds the job back. The retry policy wants every job to finish within two attempts.

A sweep is a pass that frees every expired lease. The lease sweep interval is the time between two sweeps. An operator sets the lease sweep interval in the store configuration.

The scheduler passes the deadline to `acquire_lease_with_retry_budget` unchanged. The worker crash recovery path replays every unfinished claim.

## Requests

The parser fails when a record has no owner field. The store indexes each record by its owner.

The lookup returns the newest row for a claim. The proxy logs each rejected call to the audit file.

The gateway refuses a request that carries no token. The router handles one request at a time.

The validator checks each field against the schema. The session scratch holds notes for the length of one session.

## Reports and alarms

The dashboard badges each stale lease in red. The audit job spreadsheets each day's skipped claims.

The module docstring promises a stable order for claims. Operators confirm the first worker held the lease when the second worker started the job.

The on-call engineer learns the sweep has not run in the last ten minutes. The retry counter, which the worker resets after every successful renewal of its lease, drives the backoff delay.

The pager message is dense on purpose, so the on-call engineer reads the whole state at a glance.

Queue depth alarm threshold breach, lease age alarm threshold breach, sweep lag alarm threshold breach.
