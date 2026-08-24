# SQLite compatibility fixtures

`v0.0.0.sqlite3` is a populated database created with the production SQLite
adapter from OpenEngine v0.0.0 at main commit `482087d`. It contains the
standalone chat and completed workflow asserted by
`persisted-navigation.spec.ts`.

SHA-256: `bb09ee6db190f9af9eaff7faec9823606a99c485bde8786631b26d2863650dc1`

The browser harness and `tests/test_sqlite_compatibility_fixtures.py` copy this
file into a disposable state directory; they never regenerate or edit the
checked-in artifact. The current application then opens the copy, so schema
migrations and persisted-data compatibility are covered before the tests read
the old records and create new ones.

Add a new immutable, version-named artifact when a release changes the SQLite
schema or serialized domain data. Do not replace an existing version fixture.
