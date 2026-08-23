---
name: debugging-intermittent-npe
---

The nightly job intermittently throws a NullPointerException in the
connection pool — I already tried adding a null check and bumping the
pool size, and it still fails randomly, maybe one run in five.
