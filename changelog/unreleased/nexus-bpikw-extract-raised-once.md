### Fixed

- **The aspect worker logs a failed extraction's traceback once (nexus-bpikw).** A retryable extraction failure on the tuple queue wrote `aspect_worker_extract_raised` twice, with the same traceback each time: once where the extractor raised and once when the failure was routed to its nack. Only the routing step logs it now, with the same collection, source path and traceback. A failed batch extraction no longer repeats the traceback on `aspect_worker_batch_extract_raised` (it names the error type instead), except when the batch falls back to extracting each document on its own.
