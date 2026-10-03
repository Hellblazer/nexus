# Release notes

## [2.4.0] - 2026-03-02

### Changed

- The scheduler now basically hands each job to one worker, and it is really quite fast. (#101)
- The retry path is in fact identical to the first attempt. (#102)

## [2.3.0] - 2026-02-10

### Changed

- The lease table has a very simple expiry column that is really quite short. (#97)
- The compactor just runs once a day, which is basically fine. (#98)
- It should be noted that the drain loop is unchanged. (#99)

### Fixed

- A worker that basically dies mid-job now releases its lease. (#95)

## [2.2.0] - 2026-01-15

### Changed

- The queue is really quite ordered, and the cache just returns entries. (#90)
