# Controlled Effect Executor v0.2 Source candidate tests

From `candidates/controlled-effect-executor-durable-v0.2`, run the focused
no-DB repair tests on Windows or Linux with Python >=3.11:

```sh
python -B tests/test_offline_corrections.py
```

They create no provider effect, PostgreSQL cluster or credential. The commands
below describe the retained broader Linux suite from the original return;
they were not rerun for this Source candidate.

Prerequisites: Linux, Python >= 3.11, PostgreSQL server binaries under `/usr/lib/postgresql/<v>/bin` (`initdb`,
`pg_ctl`), system `libpq5`. Do **not** point this at any existing database. All data is SYNTH, all credentials are
generated per run.

```bash
cd candidates/controlled-effect-executor-durable-v0.2
export TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1

# 1. source-tree run (needs the explicit flag; creates and deletes its own throw-away cluster; ~30 s)
python3 tests/run_pg_tests.py --confirm-disposable-test-cluster

# 2. V0.1 regression against the carried (byte-identical) V0.1 package: expect 76/76 PASS
python3 tests/v01_regression/run_v01_regression.py --mode vendored

```

The original producer reported 67 tests OK / 0 skipped on a disposable Linux
cluster. An installed-wheel V0.1 regression now propagates its inner nonzero
exit if source-file checks fail; the exact failing names must be interpreted,
not converted to wrapper success.

Windows PostgreSQL route: NOT_RUN. The focused offline tests above do run on Windows. psycopg path: UNRUN (the adapter uses only a DBAPI subset — `cursor.execute` with `%s`, dict rows,
explicit BEGIN/COMMIT in autocommit — so a psycopg connection factory should fit, but nothing here proves it).

To run against an operator-supplied disposable endpoint instead: set one variable named `CEE_V02_TEST_<anything>` to
JSON `{"host","port","dbname","user","password"}` with `dbname` starting `cee_v02_test`, and pass
`--external-conn-env CEE_V02_TEST_<anything>`. Restart/crash tests then report UNRUN (they need the runner's own cluster).
