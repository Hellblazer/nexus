Verbatim text from the local RDR-231 halt rehearsal of 2026-10-29 (a 7.87.1 -> 7.88.0
upgrade over a database holding one chroma:// note row with a live nx-note:// twin).

engine_native_log_failed_boot.txt  lines 3170-3418 of logs/storage_service_native.log:
                                   the failed boot's ERROR line, its stack trace and the
                                   Caused by chain that ends in the RAISE, and the boot's
                                   trailing frames. Nothing edited.
supervisor_crash_line.txt          the line the supervisor wrote to logs/storage_service.crash.log.

engine_native_log_failed_boot_post_sink.txt  the same failure rendered through the RDR-167 sink pattern
                                   (logback %msgSafe/%threadSafe/%exSafe and the Liquibase UI channel's
                                   LineSafeUIService, nexus-dngtz.1.11, colophon 2026-11-18): every line
                                   break inside a message is a literal \x0a. The Liquibase UI line, then
                                   Main's ERROR line and its trace; the frames are synthetic, modelled on
                                   the file above, and the timestamps fixed.
engine_native_log_uncaught_post_sink.txt     an uncaught main-thread exception through the same sink: the
                                   UncaughtExceptionLog ERROR line and its escaped trace.
