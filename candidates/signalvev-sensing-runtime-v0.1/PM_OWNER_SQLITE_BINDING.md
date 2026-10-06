# BK07 PM owner persistence · C1179/P1793 rev2

This adds a real SQLite transaction implementation under the existing PM
owner provider. It does not move SOL history, mutate Drive truth or create a
daemon. One new PM admission becomes an atomic owner projection here. Existing
claim/packet/queue rows are provenance references; they are not retroactively
called an atomic external snapshot.

## Actual source composition

Load the admitted Source checkout's `candidates/signalvev-sensing-runtime-v0.1`
on the control host's module path. For the existing client factory, import
`signalvev_client._adapters` first, preserving its canonical adapter aliases.
Providers are Source modules, not automatically part of the existing client
wheel. Do not silently use an older or independently copied provider module.

Callables in `providers.pm_owner_sqlite`:

- `ContextPmCredentialPort`: binds the existing
  `mcp/control_context_remote_service.py:OAuthBearerAuthenticator.authenticate`
  and `tooling/context/control_context_state_postgres.py:PostgresControlContextStatePort.read_session`.
- `SqlitePmOwnerStore.initialize`: explicit authorized one-time schema creation.
- `SqlitePmOwnerStore.commit_ready`: bounded PM producer operation with CAS,
  durable idempotency, immutable receipt and source/material/hold in one commit.
- `SqlitePmOwnerStore.read_receipt/read_current`: PR66 `AtomicProjectionPort`.
- `SqlitePmOwnerStore.source_port`: exact registration of the PR66 reader;
  plug this into existing `PmX9HostPorts.pm_port`/`pm_reread_port`.
  The surrounding factory/receiver belongs to X4; these files do not edit it.

```python
from providers.pm_owner_sqlite import (
    ContextPmCredentialPort, PmContextCustody, SqlitePmOwnerStore,
)

# Actual values come from the current owner/custodian; no fixture substitutes.
custody = PmContextCustody(
    owner_ref=owner_ref, audience=pm_audience,
    tenant_ref=tenant_ref, workspace_ref=workspace_ref, project_ref=project_ref,
    principal_ref=principal_ref, consumer_ref=consumer_ref, session_ref=session_ref,
    allowed_actions=allowed_pm_actions,
)
credentials = ContextPmCredentialPort(
    custody=custody, authenticator=existing_oauth_authenticator,
    state_port=existing_context_postgres_port,
    credential_reader=existing_credential_reader,
)
store = SqlitePmOwnerStore(
    database_path=owner_local_database_path,
    owner_ref=owner_ref, provider_ref=pm_provider_ref, audience=pm_audience,
    principal_ref=principal_ref, session_ref=session_ref,
    credentials=credentials, credential_reader=existing_credential_reader,
    session_check=credentials.session_check, enabled=True,
)
# Producer only, when separately authorized:
# store.initialize()
# cut = store.commit_ready(the_actual_new_pm_transition)
# Reader only, after the original commit has actually succeeded:
# port = store.source_port(expectation=exact_expectation, receipt_ref=cut.record.receipt_ref)
```

`allowed_pm_actions` is an explicit custodian binding to existing PM authority,
not authority inferred from a generic Context token. Reader actions are
`read_receipt/read_current`. Producer actions `initialize/commit_ready` also
require existing `project_state:transition` and a current Context session;
`read_session` itself requires `project_state:read`. Use separate reader and
producer bindings. A reader token alone cannot create a commit.

## Finite host qualification and next owner

Suggested concrete PM control-host database path, subject to owner placement:
`C:/Users/morgu/Documents/Codex/2026-10-01/google-drive-boot-pm/work/pm-owner.sqlite3`.
No DB has been created there by this candidate. A1 selects the corresponding
protected local owner path on Liv after Source admission. SQLite data and its
journal must remain on one local owner's disk with existing custody; do not
put it in Drive/NFS/sync storage or pretend this file is a distributed database.
SQLite is included with Python; no new service installation is needed.

First external qualification edge is precise: actual PM producer principal,
Context tenant/workspace/project/consumer/session, currently valid scoped
credential reference and custodian's PM action binding must be supplied to the
constructor above. The existing OAuth authenticator and Postgres `read_session`
must successfully return those exact values, not a credential file name alone.
X1 cannot infer that a generic Context session is the PM producer. No new scope
or credential is issued here. If the selected current credential lacks an
already authorized scope, report that exact token/target operation via the
existing custodian; do not synthesize rights or copy secrets into reports.

PM selects the next genuine material-ready/available transition (not historical
PM10701/10702). Supply a `PmReadyTransition` containing the actual source refs,
exact packet/material bytes, source decision pointer, explicit hold/no-hold,
Way Home, unique attempt and last owner revision. Provider assigns the sequence,
revision, event, receipt, commit/readback and snapshot hashes only when writing.
This is a new admitted owner projection, not automatic lifecycle ownership.

A1's bounded qualification after admission: actual interpreter/module origins,
constructor values without secrets, authenticated current Context session,
producer commit, independent reader's receipt/current readback, restart readback,
packet/material/source-cut/hold match. PM then orchestrates the authorized one
private D0/X9 use, with X4 receiver bound, and fresh durable PM result readback.

## Failure and recovery

`BEGIN IMMEDIATE`, full synchronous commit and SQL uniqueness/CAS serialize
writers across processes. Reads return one database snapshot. Existing receipts
remain immutable in the application API and current pointers advance atomically.
An unchanged attempt returns its original receipt after restart; a changed
attempt payload conflicts. CAS conflict consumes no sequence. Missing, stale,
wrong-owner/project/session or revoked credentials deny reads/writes. Missing
database reads fail without creating a replacement.

If a commit succeeds but its subsequent authenticated readback fails, the outcome
is unresolved to the caller. Preserve the same attempt and input; reconcile that
attempt when custody is restored. Never create a new attempt or send D0 merely
because an exception occurred. A recovered historical receipt is not itself
current: the existing PR66 reader rereads currentness and refuses stale sends.

Checksums detect storage/representation drift, not malicious edits by an owner
of the DB. Existing host ACL/custody remains necessary. Tokens are not stored.
Cross-store session revocation and SQLite commit have no distributed transaction;
the second authorization at the commit/read boundary is a fresh check, not a
claim of atomic revocation across both databases.

## Evidence scope

New focused tests use real local SQLite disk and separate writer processes;
credential/Context objects are labeled offline synthetic. They prove persistence,
CAS, replay conflict, rollback, representation checks and adapter composition.
They do not prove real OAuth, real PM admission, Liv host fit or D0/X9 first use.
Reuse earlier PR51/PR66 tests; no old-suite replay is needed.
