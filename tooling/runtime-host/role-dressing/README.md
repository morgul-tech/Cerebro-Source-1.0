# Fixed ASSISTANT dressing journal

This client performs local journal operations and emits proposals. It opens no
network connection and grants no role. It reuses the completed B1 immutable
original/enrollment/SQLite/event/restart algorithm; its schema is a new separate
fixed-ASSISTANT original. Never open or convert the finished WORKER journal.

`request.schema.json` describes the frozen own request: one attempt, exact opaque
target, role ASSISTANT, expected public Source/method/projection and independent
owner expectation. `public-source-pins.schema.json` is public expected data, not
provider/currentness/owner proof. Real A1/B1 must first consume exact published
code/registry/schema/Source projections. Actual effects additionally require A1
release and D1 qualified host admission. Empty Assistant
role refs are valid; common method remains mandatory. A new HEAD requires a new
qualified original or explicitly qualified server continuity, never journal rewrite.

The captured registry SHA is canonical LF Source identity; captured original bytes
are compiled once without hash-then-reopen. Opaque request fields are never trimmed
to create equality. Decoder rejects duplicate/nonfinite JSON and secret/seal keys.
Registry validation and stricter semantic checks accompany the JSON schemas.

## CLI: local proposals and observations, authenticated host effects

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

CLI has no `--owner-admitted`, role switch, seal/token or network option.
`next` at PREPARED can journal and emit a role-neutral create proposal; after an
exact READY read and method decision it can emit strict Assistant resume arguments.
Each proposal is authority NONE, native_effect false and
requires_authenticated_host_gate true. It is not admission, a successful server
reply or permission to invoke a provider. Operational A1 effect release stays
separate. `prepare` is a local durable original, not native birth/reservation.
Client results keep their own client-result schema; a host refusal is not success.

## Non-authoritative Windows journal API

```python
journal = Journal(new_protected_path,
                  source_pins=actual_expected_public_source_pins)
```

The prior `owner_gate` constructor/checker prerequisite is removed. A server-only
protected profile/reader/clock object cannot be transported into this Windows
journal through the existing interface. No replacement boolean, caller admission
flag, copied profile, fake signature or new RPC is accepted or required. Frozen
owner_expectation remains unverified caller data for exact observation/target
comparison; it authenticates nothing. No server DB or signing credential belongs
in the client. Existing server host_args remain legitimate at their own boundary.

`next_call` durably records a proposal before returning existing create/read or
strict three-argument Assistant resume. B1 invokes the existing authenticated host
port only after the separately qualified A1 effect disposition. The host controls
real identity/scope/Source/verifier/issuer for role-neutral birth and all protected
Assistant owner/profile/currentness/window/seal checks for attachment. Client
proposal or expected JSON cannot replace these actual host controls. Source
publication alone does not install or admit anything.

A READY exact read and APPLIED/NO_CHANGE consumption of the exact public method
are still required before attach. NOT_APPLICABLE cannot skip mandatory common
method. Unknown/restart can only propose reading the same original, never replay
create/attach or invent a new attempt. No local checker result is called admission.

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
