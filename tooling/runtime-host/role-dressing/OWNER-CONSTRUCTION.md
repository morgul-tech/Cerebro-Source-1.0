# One isolated Assistant owner cut

Actual current owner binding is UNKNOWN_OWNER_BINDING. This construction is code
and a dependency contract, not installation or a grant. No Source file/env loader,
default profile, real private values, key generation or credential is supplied.

These are server effect-time constructor dependencies. The Windows proposal
journal does not receive this protected server object, reader, DB or signing key.
Its former owner_gate prerequisite has been removed after the actual D1/B1
unsupported-interface finding. Local proposals grant no authority and execute no
provider; A1 operational release and the actual authenticated host controls remain
mandatory for every real create/attach effect. Existing D1 host_args are legitimate
in the server process. No new client RPC or copied admission flag replaces them.
The resolver's existing check_client_admission method is unchanged as an optional
in-host check; it is not a required Windows binding or a serving transport.

Construct `AssistantTrialOwnerResolver` from the qualified Source
`mcp/assistant_trial_owner.py` module closure with:

- An explicit profile_ref for exactly one deliberately installed trial profile.
- An installation-owned protected admission_reader with `hold_current(profile_ref)`
  and `assert_current(profile_ref, window_ref, owner_revision)`.
- The actual qualified server clock, explicitly injected, with finite current
  time and profile not_before/not_after eligibility. No default current/unexpired.

`hold_current` yields exactly window_ref and profile. Its actual implementation
must serialize profile changes/revocation/inflight operations across the full
handler read/continuity/resolve/seal/atomic attach/readback. It must raise if that
guarantee/current admission is absent. The Source class deepcopies the one profile
and uses a per-invocation ContextVar; it is not an observed distributed lock.
`assert_current` must raise on changed/revoked/unqualified owner state, not return
an untrusted current=true flag. Schema/profile metadata alone is insufficient.

The exact private profile schema is owner-profile.schema.json: owner original/ref/
revision, enabled explicit true, scope BUILD7_SINGLE_ISOLATED_ASSISTANT, actual signed
caller tenant/workspace/principal/consumer and transport session, one generation/
Source/method/revision2, fixed owner-chosen overlay/actor-generation/target and A1
orientation recipient, explicit owner time window. No wildcard/allowed-role set,
PM/Principal/WORKER grant or task authority. Raw opaque values compare exactly.
Actual READY receipt/state fingerprints come from the server state, not profile
fabrication. A planned generation/target is a frozen owner choice, not evidence of
an existing generation. Only a real READY_UNBOUND candidate can resolve/attach.

Source handler change is deliberately narrow: strict existing Assistant resume
requires the resolver's held owner-window interface, holds it through readback,
and checks currentness before seal and before atomic attach. Existing seal issuer,
matching attestation verifier, method continuity, payload/receipt checks and CAS
are reused. All other roles/handlers/scopes remain unchanged. A generic resolver
without the required window refuses rather than silently skipping owner fencing.
The resolver returns only the existing exact ALLOW decision shape and ASSISTANT
payload. No caller seal or new control/grant engine is introduced.

Admission time is checked before effect entry; holding an immutable owner cut
does NOT establish instantaneous expiry rollback or mid-commit distributed
revocation. The real D1 owner must qualify its serialized admission/no-change/
inflight rules and whether a queued revocation is effective after the current
already-admitted atomic operation. If that contract is insufficient, retain the
precise owner fence gap; never claim an observed lock or borrow WORKER zero-claim.
Unknown commit/readback remains an original obligation requiring exact read.

Supported current construction seam: existing
`assemble_postgres_control_context_remote_runtime[_from_connection_factory]`
already accepts assistant_overlay_control_resolver and
assistant_overlay_attestation_issuer; it requires the actual matching
resolution_attestation_verifier. Supply this resolver, the REAL server-held issuer/
verifier pair and existing source continuity verifier. The reference/test HMAC
attestor used in local controls is NOT production implementation/key custody.

Exact matched later install cut: new assistant_trial_owner.py + reviewed modified
control_context_tools.py into the actual current Context composition, with genuine
private profile/reader/clock/issuer/verifier/current caller/fence inputs and protected
constructor readback. Do not restore old PM policy/env or replace unrelated modules.
D1 owns current target and narrow controlled installation/admission; A1 owns byte
publication. B1 consumes exact published CLI/schemas/projection and real owner
original; no speculative birth solely to see whether the owner exists.

No actual protected admission-reader implementation or matching production issuer
is invented here. Those existing-owner inputs are the concrete qualification rest,
not a reason for a new global port or for blocking local code/S1/full BK11 work.
Public source/readback/recipe never proves installed owner or current native grant.
