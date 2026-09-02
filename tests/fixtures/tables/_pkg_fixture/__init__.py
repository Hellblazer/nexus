"""A minimal importable package used only to exercise
``nexus.tables.load.load_packaged_table``'s ``importlib.resources`` path in
tests/tables/test_check.py (RDR-201 P1.1 review follow-up, IMPORTANT (a)).
Not part of the ``nexus`` distribution; reached only via
``monkeypatch.syspath_prepend`` in the test that needs it.
"""
