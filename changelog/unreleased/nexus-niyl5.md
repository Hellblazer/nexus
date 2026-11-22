### Fixed

- Tests: every psql child the suite starts reads and writes UTF-8. `tests/conftest.py` sets `PGCLIENTENCODING=UTF8` and each text-mode psql call names `encoding="utf-8"`, so non-ASCII SQL no longer fails on Windows, where stdin took the console code page. A lint keeps every psql call naming its encoding (nexus-niyl5).
