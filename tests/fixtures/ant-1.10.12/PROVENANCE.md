# Apache Ant 1.10.12 (`ant-1.10.12.jar`, `ant-launcher-1.10.12.jar`)

Fixtures for `tests/test_service_jooq_codegen_skip_behaviour.py` (nexus-9st84 follow-up),
which runs the real Ant steps of `service/pom.xml` under the Ant version
maven-antrun-plugin 3.1.0 declares. The test used to download these two jars from Maven
Central when `~/.m2` did not hold them, and the hosted `pytest (lint markers)` jobs (no
Maven build, so an empty `~/.m2`) failed on a 404 and on HTTP 429 from both mirrors
(CI runs 39720885850, 40250553442, 41199117325, 43937508362). They are committed so the test
makes no network fetch.

## Source

Maven Central, `org/apache/ant/ant/1.10.12/ant-1.10.12.jar` and
`org/apache/ant/ant-launcher/1.10.12/ant-launcher-1.10.12.jar`, unmodified.

| File | Bytes | SHA-256 |
|---|---|---|
| `ant-1.10.12.jar` | 2254148 | `5c6a438c3ebe7a306eba452b09fa307b0e60314926177920bca896c4a504eaf6` |
| `ant-launcher-1.10.12.jar` | 18705 | `42a7ca7e7bb777fe7ee75a5ed4cc529c766bc43486367fdaad0ab4a32ee9c249` |

The same digests are pinned in `_ANT_JARS` in the test, which resolves Ant from these files
only (never `~/.m2`, so every host runs the path the hosted lint job runs) and checks each jar
against them.

## License

Apache License 2.0. Each jar carries its own `META-INF/LICENSE.txt` and `META-INF/NOTICE.txt`
(Copyright 1999-2021 The Apache Software Foundation), which satisfies the redistribution
terms; the files are committed unmodified. `.gitattributes` keeps `tests/fixtures/**` byte-exact
on every checkout.

## Updating

Change `_ANTRUN_PLUGIN_VERSION`, `_ANT_VERSION` and `_ANT_JARS` in the test together with the
jars here, fetching from Maven Central and comparing against the `.sha1` Central publishes. A test
reads `service/pom.xml` and fails when its maven-antrun-plugin version is not the one pinned.
