# Queue notes

The queue is basically ordered, and entries leave in the order they arrived.

The worker really quite simply retries a failed job once before it gives up.

It should be noted that the retry path is unchanged from the previous release.

The scheduler just wakes every ten seconds and picks the oldest job.
