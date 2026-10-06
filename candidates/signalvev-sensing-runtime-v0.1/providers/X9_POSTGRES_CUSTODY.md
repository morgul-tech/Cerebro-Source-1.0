# X9 PostgreSQL custody role and tuple contract

Runtime home: `providers.x9_postgres_custody.PostgresX9SessionAPI`. The optional
candidate SQL is `providers/x9_postgres_custody_v1.sql`; it is not installed and
does not create role assignments or grant runtime privileges.

Each provider instance requires an explicit `receiver` or `producer` selector
and a read-only `TrustedCustodyConfigurationPort`. That host port must return a
validated `TrustedCustodyConfiguration` with separate assignment refs and
source refs, plus each role's complete tuple: tenant, workspace, project and
revision, OAuth principal and consumer, actual session ref, persisted binding
ID and revision, and binding fingerprint. The configuration contract is also
defined in `x9-role-binding-config.schema.json`.

Role labels come from the independently governed host assignment source. The
implementation does not derive them from an OAuth principal, shared consumer,
thread name, display alias, or session string. The tuples may share principal
and consumer values, but their session refs and assignment refs must differ.
The host adapter remains unimplemented; no production values are checked in.

For every operation, the existing OAuth verifier must authenticate the
configured tenant/workspace/principal/consumer tuple. The database read then
locks the exact configured session binding and active project and compares
project revision, session binding ID/revision, and fingerprint. Any absent,
stale, or mismatched value fails closed. No operation creates a binding.

The SQL candidate adds an independent role-assignment relation and immutable
receipt rows tied to writer and peer assignments. RLS requires the current
configured tuple. Runtime INSERT/UPDATE/DELETE policies for role assignments
are deliberately absent; a separate trusted host-control operation must
populate them. The script has no aliases, credentials, grants, roles, seed rows,
or automatic migration. Do not install it before the role adapter and both
current persisted bindings are available and the selected provider is admitted.

`bind_existing_pm_x9` remains disabled by default. When enabled by a qualified
host, it reuses `signalvev_client.pm_x9.build_binding`, wires the configured
receiver and producer ports, and does not start listeners, send hints, install
SQL, or invoke the X9 pulse. Existing OAuth read/transition grants continue to
govern capabilities; this package adds no OAuth scope or credential.

## Current qualification limit

A1's deployed caller/v2 normal read shows transport metadata available on its
connection and no persisted session. A1 also reports a fresh X9 caller/v2 read
with a distinct transport session and no persisted session. Those observations
do not establish role assignment or current PM/X9 binding. The PM's own normal
caller/v2 read after a genuine ordinary project event is still required to
expose its tuple and current persisted binding. The existing host-control
owner must then provide an independent receiver/producer assignment source
that returns both exact current tuples and assignment provenance.

Focused synthetic checks cover the channel adapter (8 tests) and custody API
(10 tests), using the existing OAuth adapter with synthetic verifier/database
doubles. The role configuration JSON parses. No real OAuth custody, live
PostgreSQL syntax/privilege/concurrency, schema install, persisted PM/X9
bindings, role assignment, or first-use flow has been verified. PostgreSQL
qualification and any installation remain with A1 after the trusted role
adapter and both bindings exist.
