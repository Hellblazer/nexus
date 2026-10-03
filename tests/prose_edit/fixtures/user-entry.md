# Queue notes

A worker takes one job from the queue and holds a lease while it runs. A worker that dies mid-job releases the lease, and the scheduler hands the job to another worker.

The retry path is the same for every job. The scheduler waits ten seconds between attempts.
