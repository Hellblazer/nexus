---
title: Queue notes
summary: This very basically explains how the queue is really quite reliable in practice.
tags: [queue]
---

# Queue notes

The scheduler basically hands each job to one worker, and it is really quite important to note that a worker which dies mid-job simply releases the lease. It should be noted that the lease is very basically a row with an expiry time.

> Basically, the original design note said the queue is really quite simple, and it is important to note that this was very deliberately kept simple.

```python
# This is basically a very simple loop that is really quite fast.
def take(queue):
    return queue.pop()
```

| Column | Meaning |
| --- | --- |
| lease | A very basically important expiry column that is really quite short. |
| owner | The worker that basically holds the row. |

The retry path is in fact identical to the first attempt, which is to say that a retried job is really the same job.
