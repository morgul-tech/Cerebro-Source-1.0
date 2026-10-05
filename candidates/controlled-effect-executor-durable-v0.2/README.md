# Controlled Effect Executor — durable V0.2 (PostgreSQL adapter)

**Status:** isolated implementation candidate, `AUTHORITY=NONE`, local and default-off. Work order
`EXT-CLAUDE-CONTROLLED-EFFECT-EXECUTOR-DURABLE-V02` rev 1.0. Proves local durable-adapter behaviour against a
disposable PostgreSQL. It does **not** prove real provider credentials, live authority, a production owner store,
credential isolation or any deployment. A completed build or a green run authorises nothing.

## C1128 focused offline repair

`BoundedRecoverySweep.next_page()` reads at most 500 unresolved admissions at a
time. After the tail it resets to the beginning; a fresh process also starts at
the beginning. Callers must keep running sweeps while late admissions can become
visible. A persisted `admit_seq` cursor alone can silently miss a lower sequence
that commits late. This is read-only discovery, not automatic execution or an
owner disposition. `FENCED`, `IN_FLIGHT` and `UNKNOWN_EFFECT` remain distinct.

The V0.1 regression launcher now returns the child's actual exit code in both
modes. An installed source-only scan failure is reported as such, not converted
to a green wrapper. Its temporary-tree cleanup checks the resolved path before
removal on Windows. Run the focused no-DB tests with
`python -B tests/test_offline_corrections.py`; these are safe on Windows and do
not launch PostgreSQL.

Final `NO_COMMIT` needs explicit `SETTLED_ABSENT` with authoritative and
authenticated settlement plus a provider evidence reference. Empty complete
history alone remains `UNKNOWN_EFFECT`. The `authenticated` field is a trusted
adapter contract asserted by the synthetic test provider; this candidate does
not bind or prove a real provider credential/readback. Ledger digests prove
local consistency only, not external authenticity.

Admission's same-row-lock owner read is conditional on a real owner implementing
`OwnerTransactionPort` inside the same PostgreSQL transaction. This candidate
contains no production owner binding.

## What it adds (and nothing else)

The V0.1 in-memory executor lost the admitted task and any unfinished attempt when the process died. V0.2 keeps the
V0.1 package **byte-identical** (`src/controlled_effect_executor`, hash-pinned in `tests/V01_PIN.json` against the
received V0.1 return) and adds `src/controlled_effect_executor_durable`:

| Need | Where |
|---|---|
| Admission + attempt storage in one real DB transaction/lock boundary | `PgAdmissionStore.admit`, `begin_attempt` |
| Bounded, read-only listing so a fresh process recovers digest, task refs, identities, state without the old pointer | `PgAdmissionStore.list_unresolved` |
| Restart path that never calls the provider's mutation path | `DurableControlledEffectExecutor.recover` |
| Honest ambiguity: crash / lost response stays `UNKNOWN_EFFECT`; final `NO_COMMIT` needs explicit provider settlement evidence | `settlement.py`, `recover` |
| Disposable-schema DDL with DB-enforced immutability / hash-chain / legal transitions | `schema.py` |
| Stdlib `ctypes` libpq driver (psycopg could not be installed here — network policy; see ASSUMPTIONS) | `pg_libpq.py` |
| SYNTH owner + provider used only by the tests | `synthetic_pg.py` |
| Reproducible wheel (self-contained PEP 427 writer; host setuptools is broken on 3.13, see its docstring) | `packaging/build_wheel.py`, `pyproject.toml` |

No scheduler, daemon, transport, retry policy, expiry/grace/timeout policy, new owner role or new idempotency key.
`test_component_inventory.py` and all tooling are untouched; there is no `component.yaml`.

## Run it

See `TEST_COMMANDS.md` (short rerun instruction). Needs a local PostgreSQL server binary set (`initdb`, `pg_ctl`),
the system `libpq5`, Python >= 3.11, and an explicit flag:

```bash
TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 python3 tests/run_pg_tests.py --confirm-disposable-test-cluster
```

The runner refuses without the flag, scrubs every `PG*` / `DATABASE_URL` / `*_DSN` variable, creates its own cluster
(generated password, Unix socket in a private temp directory, no TCP listener), and removes only what it created.

## Historical producer evidence (not rerun or bundled here)

* The original return reported **67 tests, 0 failures, 0 skipped on disposable PostgreSQL 16.15**, two or more independent OS processes (not threads) for
  contention, SIGKILL, and whole-server crash/restart (`pg_ctl -m immediate`, WAL recovery). These are prior producer results, not tests run for this Source candidate.
* The original return reported the same suite from an **installed wheel** in a clean venv outside the source tree.
* The original return reported V0.1 regression (76 checks) against the carried package and installed wheel; the installed source-only scan must now retain its failing exit for honest interpretation.
* The original return reported a negative unlocked-owner control and eight caught mutants. The present PR carries code and tests, not the earlier run logs.
* Independent adversarial review (separate agent, read-only) found 3 serious + several minor points; all were fixed or
  documented, see `ARCHITECTURE.md` §7.

## Compatibility with V0.1

Unchanged: `BatchSpec`, canonical text/digests, `AdmissionReceipt`/`LedgerEvent`/`ExecutionResult` shapes, the state
vocabulary and `TRANSITIONS`, grant minting, ports. Changed where persistence or honesty required it:

1. `execute(receipt, spec)` is unchanged in signature and result; an additional `recover(admission_ref)` and
   `list_unresolved(...)` exist. `reconcile` is an alias of `recover`.
2. **NO_COMMIT is stricter.** V0.1 accepted an authoritative, complete applied-history that lacked the correlation
   (e.g. `fail_before_commit`) as final. V0.2 downgrades that to `UNKNOWN_EFFECT` unless the provider's readback also
   returns explicit authoritative `SETTLED_ABSENT` for that correlation (`ProviderSettlementReadbackPort`). A readback
   without that port can never produce a final NO_COMMIT.
3. The store is PostgreSQL, scope-bound, and the owner views must be readable in the same database transaction
   (`OwnerTransactionPort`); V0.1's in-memory owner port is not a drop-in for the shared-transaction fence.
4. V0.1 source-file guards that scan the package tree cannot run against an installed artifact; this is explained
   case by case in `TEST_COMMANDS.md` / `evidence/v01_regression_installed.txt`.

## First unproven edge

How a *real* provider reaches `SETTLED_ABSENT` (its own timeout, fencing token, tombstone) is not designed or proven here.
Until a provider can say that, a lost response ends in `UNKNOWN_EFFECT` with an owner decision, never in NO_COMMIT.
