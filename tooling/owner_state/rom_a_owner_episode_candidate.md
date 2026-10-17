# Rom A owner admission — inactive Source candidate

This is the corrected PR95 admission prototype integrated against Source
`8b83398981970019444d0d8dcc3268759d497192`. It is available for explicit import
from `tooling.owner_state.rom_a_owner_episode_candidate`; normal owner-state
persistence retains its Project, Quality and Convergence scope.

## Integration decision

The A1 episode and admission ledger have their own semantic identity. The
candidate therefore has a separate module and reuses the current
`PostgresOwnerStatePersistencePort` transaction and existing row/hash/error
helpers. Its episode validator and port body preserve the corrected PR95
implementation. It does not import PR93/94's historical host or file adapters.

No runtime adapter is installed: the selected effect is Source integration of
the inactive candidate, and the current host has no selected A1 admission
consumer. An explicit caller must provide the constructor-bound authenticated
ingress and `current_fence`; the fence covers current owner/mandate readback,
gate evaluation, SQL CAS and commit. It must serialize all relevant owner
changes. The module supplies no sender or `send_selected_under_owner_fence`.

The existing candidate SQL and `control_context_rom_a_owner_migrations.json`
are retained byte-for-byte from PR95. The normal migration manifest is
unchanged and never selects them. Their presence creates no database, runtime
role, migration duty or additional Project/Quality/Convergence requirement.

## Preserved behavior

Admission returns `ADMITTED_UNCERTAIN`, not delivery or recipient-use proof.
Currentness drift preserves historical admission debt and returns
`OWNER_BASIS_DRIFT` / `STALE`; it does not erase the original basis/revision or
authorize another admission. Missing fences, stale revisions, closed gates and
uncertain commits retain their corrected failure boundaries.

## Provenance and bounded verification

- Original PR95: `d4b9749c2c69f0767d0e95c383d68318a4fc8e23`.
- Reused correction: `fb4804fd3d01a329858f1a6490ffb890a0d85c01`.
- Correction patch SHA-256:
  `499f651fed14920a178d477d468eb453c1c7f22ab045bebf44348b980514b843`.
- The prior 20/20 scripted DB-API/fence tests and 8/8 owner-state tests are
  producer evidence, not a PostgreSQL or runtime-admission claim.
- The integration reuses the corrected candidate tests against current main's
  helpers, adds one check that normal owner/migration selection stays unchanged,
  and checks cold package/direct imports. The integrated batch passed 21/21 on
  Python 3.12.14. The candidate state-validator and port ASTs match the corrected
  original; Python 3.10 grammar was checked without a Python 3.10 runtime claim.

Run the bounded, database-free candidate checks with:

```sh
python -B tooling/validator/rom_a_owner_transaction_validation.py
```

These checks do not exercise real PostgreSQL locks, RLS or crash durability,
authenticate an A1 owner, migrate a database, dispatch work, or affect BK11.
