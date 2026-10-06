# X9 PostgreSQL custody, C1157/P1771 REV2

Runtime home: providers.x9_postgres_custody.PostgresX9SessionAPI. Four methods are
identity/read_pointer_by_event_id/append_disposition_once/read_disposition_by_event_id.
The actual implementation performs PostgreSQL transactions; it is not a new Protocol.
Schema home: providers/x9_postgres_custody_v1.sql, ONE optional table within the
EXISTING selected Source control/owner PostgreSQL database. No separate service/store.
No global control migration/production Context PR9 deployment is authorized here.

Trusted host assembly only:
- Reuse the existing configured OAuthBearerAuthenticator with its REAL signature verifier,
  existing signed principal/tenant/workspace claims and valid read/transition grants.
- header_provider reads that principal's existing authorized credential privately;
  no token/password/DSN is in this package, tool output or receiver request.
- connection_factory is the SAME existing control-provider database factory, using
  transactional psycopg connections. The actor must not receive its database secret.
- Fresh authenticated principal must EXACTLY match the pinned X9 generation (receiver),
  or current PM generation (producer). Producer owns a SEPARATE authenticator/getter and
  its actual persisted producer_session_ref. Receiver session is pinned by existing port.
- EVERY operation freshly checks the existing control-session row for the verified
  tenant/workspace/principal/consumer/session and locks its current project/session rows.
  A missing/stale binding fails closed; this implementation NEVER creates that binding.
- X9 capability labels map allowed operations under the existing actual project_state
  grants: read -> pointer/disposition reads, transition -> receiver disposition append.
  This is the REV2 narrow admitted operation policy in the same provider; it does NOT
  add OAuth scopes, issue a credential, grant an identity or turn an actor/thread string
  into authentication. Previously reported REV1 missing operation remains historical.

Persistence/registration:
A1 after Source admission applies the EXACT SQL hash in a SINGLE transaction only to
this selected existing provider and verifies table/PK/FK/generated revision/RLS policies
and the existing runtime DB role privileges. No auto install. No GRANT or role/token
creation is in this package. Reapplying CREATE POLICY is intentionally an error; reconcile
existing schema definition before another install, never use IF NOT EXISTS as drift proof.
Runtime role must be qualified for these operations under existing provider custody;
privileged owner/BYPASSRLS is not evidence of actor authorization. RLS prevents update/delete
for ordinary roles; source runtime API supplies insert/select only. Runtime DB credentials
stay at the existing trusted provider boundary, not receiver input. SQL SET LOCAL scope
values originate ONLY in freshly verified OAuth claims, as in existing control provider.

Atomic semantics:
Unique (tenant,workspace,channel,kind,eventID) + INSERT ON CONFLICT DO NOTHING under
READ COMMITTED. Exact duplicate returns original payload/hash/provider revision; divergent
attempt/content never overwrites. DISPOSITION requires exact original pointer/attempt/hash
in the same transaction. Writer principal/session FK is to the existing control binding.
Commit uncertainty returns UNKNOWN_SEND with NO retry. An append acceptance is not
readback: public reads open an independent provider connection and verify stored canonical
bytes/hash/writer/revision. PostgreSQL generated provider_revision belongs to the ORIGINAL
receipt; duplicate calls cannot fabricate a newer version. Selected records are immutable
through this API. Ledger records have signal authority NONE and never replace PM truth.

Existing client composition:
providers.x9_postgres_custody.bind_existing_pm_x9(settings,ports,
 receiver_api=actual_receiver_api,producer_api=actual_producer_api,enabled=True)
reuses signalvev_client.pm_x9.build_binding and PmX9HostPorts. It verifies current
provider identities and wires X9SessionChannelPort + separate PM ChannelPort. It neither
starts listeners, installs SQL, sends hints nor invokes the X9 pulse. X1's pm_port and
pm_reread_port remain untouched. Default registration disabled. PM producer principal is
CURRENT_PM_PROJECT_MANAGER_C1A05B39; lawful succession must requalify this pin, not switch
it from an incoming signal.

Qualification limits:
8 changed-risk unit checks PASS Windows Python3.13.15 using real existing OAuth adapter
with SYNTHETIC signature-verifier/DB doubles. No real OAuth custody, PostgreSQL cross-process
concurrency, SQL install, original live durable receipt, actual scope/session or first-use
PASS is claimed. PostgreSQL executables were not available on this root's PATH; A1 owns
selected-host provider/config/schema qualification. Tests are NOT live DB substitutes.
Required next actual receipt: selected provider path/resource (without secrets), source/schema
hash, configured verifier, fresh authenticated X9+PM principals/current sessions and existing
grants, scoped role + PK/RLS/readback qualification; then the ONE real next-material PM flow
already authorized in REV2. PM10701/02 are historical references only, never replay inputs.

Way Home: packet2672E -> exact new receiver code/schema -> existing Source admission ->
A1 sole Liv selected provider registration/config readback -> genuine next PM transition ->
private D0 -> actual X9 consume -> fresh X1 PM reread -> immutable typed disposition ->
PM original receipt readback. MATERIAL_ROUTE requires actual next work/owner from X9.
External grant/custody deficiency, if ACTUALLY observed at that qualification, must name
exact existing resource/role/UI/action once; do not make a generic HOLD or invent a grant.
