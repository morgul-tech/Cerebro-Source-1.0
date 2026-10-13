# Fixed ASSISTANT dressing journal

This client performs local journal operations and emits proposals. It opens no
network connection and grants no role. It reuses the completed B1 immutable
original/enrollment/SQLite/event/restart algorithm; its schema is a new separate
fixed-ASSISTANT original. Never open or convert the finished WORKER journal.

`request.schema.json` describes the frozen own request: one attempt, exact opaque
target, role ASSISTANT, expected public Source/method/projection and independent
owner expectation. `public-source-pins.schema.json` is public expected data, not
provider/currentness/owner proof. Real A1/B1 must first consume exact published
code/registry/schema/Source projections and D1 qualified admission. Empty Assistant
role refs are valid; common method remains mandatory. A new HEAD requires a new
qualified original or explicitly qualified server continuity, never journal rewrite.

The captured registry SHA is canonical LF Source identity; captured original bytes
are compiled once without hash-then-reopen. Opaque request fields are never trimmed
to create equality. Decoder rejects duplicate/nonfinite JSON and secret/seal keys.
Registry validation and stricter semantic checks accompany the JSON schemas.

## CLI: local preparation/observations, default missing-owner denial

Run the actual qualified Python with `-I -B` and the published file:

```text
role_dressing_client.py --source-pins <expected-public-pins.json> --db <new-protected-journal.sqlite> prepare --request <new-immutable-own-request.json>
role_dressing_client.py --source-pins <same-pins.json> --db <same-db> next --attempt <same-original> --fingerprint <separately-frozen-fingerprint>
role_dressing_client.py --source-pins <same-pins.json> --db <same-db> ingest --attempt <same-original> --fingerprint <same-fingerprint> --result <exact-host-read.json> --evidence-class HOST_PROVIDER_READ_NOT_VERIFIED_BY_CLIENT
role_dressing_client.py --source-pins <same-pins.json> --db <same-db> consume --attempt <same-original> --fingerprint <same-fingerprint> --method <exact-method> --decision APPLIED
role_dressing_client.py --source-pins <same-pins.json> --db <same-db> next --attempt <same-original> --fingerprint <same-fingerprint> --attach
role_dressing_client.py --source-pins <same-pins.json> --db <same-db> unknown --attempt <same-original> --fingerprint <same-fingerprint> --reason <actual-unknown-outcome>
role_dressing_client.py --source-pins <same-pins.json> --db <same-db> status --attempt <same-original> --fingerprint <same-fingerprint>
```

CLI deliberately has NO `--owner-admitted`, role switch, seal/token or network
option. Without a real constructor-bound owner dependency, `next` at PREPARED or
before attach returns ROLE_NOT_ADMITTED without a create/attach proposal/event.
After an already proposed/unknown effect it can propose exact-ref read without
fresh owner availability, preserving original recovery. `prepare` is only a local
durable original, not native birth/reservation. Client results use their own
client-result schema; ROLE_NOT_ADMITTED is not a success-shaped server reply.

## Qualified host-mediated API after real owner admission

```python
journal = Journal(new_protected_path,
                  source_pins=actual_expected_public_source_pins,
                  owner_gate=actual_constructor_bound_admission_checker)
```

`owner_gate.check_client_admission(request)` must raise on absent/stale/mismatching
admission. It is an installation-owned interface, not a caller JSON boolean.
`AssistantTrialOwnerResolver.check_client_admission` is the provided implementation
over the real protected reader/window/clock described in OWNER-CONSTRUCTION.md.
Actual D1/B1 bridging/custody of that dependency must be qualified: source code or
local expected tuple does NOT authenticate a caller. No actual adapter is supplied
or implicitly enabled by this package. Missing it is the exact local proposal
boundary; it does not prevent offline schema/journal preparation.

After a genuine owner check, `next_call` persists the proposal before returning
existing create/read or strict three-argument Assistant resume. B1 separately
invokes its existing authenticated host port under A1/D1 admission. A READY exact
read and APPLIED/NO_CHANGE consumption of the exact public method are required
before attach. NOT_APPLICABLE cannot skip mandatory common method. The actual host
revalidates its identity/session/current profile and seals internally.

After effect, ingest only an actual exact `read_boot_generation_state` result with
authenticated_binding and pre_role_generation. An isError wrapper is refused even
if it contains plausible state. Validate actual host success/refusal outputs
against their corresponding published tool schemas; the client additionally
validates registry/state/role/target/projection. Never substitute client result
schema for actual server schema. Attached state must match frozen ASSISTANT target
and projection. Client records observations as caller/provider-read inputs, not
native authentication. Actual B1/owner readback qualification remains independent.

Unknown effect persists before restart and permits only reading the same original;
no create/attach replay/new attempt/reset. Event replay fetches at most4097 to
detect a4096-event completeness bound; overflow refuses with unknown coverage.
It never truncates history to manufacture a clean state. Existing obligations,
enrollment and all events remain retained. The journal cannot be recreated when
an enrollment marker proves the missing database was previously established.

## Other roles and useful consumer

PM/Principal are documentation/negative coverage, not runtime role choices. PM
needs actual trusted governor/profile/control/state/task admission. Principal
needs its own contact/continuity/succession basis for READY_CURRENT; SHADOW_ONLY
authority is distinct from lifecycle readiness. No live grants or succession.

B1 is sole actual isolated Assistant consumer, A1 is orientation recipient.
After lawfully admitted dressing and exact pre/post-read, B1 reads a bounded public
current cut and provides an orientation result supporting an actual A1 decision.
Role/shadow/method is not job admission. A1's APPLIED/NO_CHANGE/NOT_APPLICABLE is a
real later consumer effect, not CLI output, dispatch or synthetic test PASS.
