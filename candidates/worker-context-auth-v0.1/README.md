# C1006 WORKER Context/Auth candidate

Default-off, isolated Source candidate based on X4 C995/C999 NAL head
`d64ef7813070b024b3ffa5ea6f4b53c997efabc3`. It does not rerun the NAL
PostgreSQL fixture or bind production Context, PM, Human, database, or key ports.

## Server-held ports

`mcp/worker_context_auth.py` composes three **injected owner readers**:

1. Human intent: current, nonrevoked `BOOT_WORKER` receipt bound to the exact
   principal, session, Boot generation, source/method and intended WORKER actor.
2. Parent PM mandate: current, Human-granted, generation/actor/source-scoped
   mandate. A PM task decision cannot create or expand this mandate.
3. PM claim reader: an explicit generation-scoped zero-claim owner readback and
   a `hold_zero_claim` fence. The fence must cover the atomic Context role attach
   and readback. Empty actor shadow is never zero-claim evidence.

The owner adapters themselves are absent in production. Their authentication,
durability, issuer custody, currentness and cross-provider fence are the first
remaining production proof. The local fixture uses independent in-memory ports.

`resume_ready_unbound_worker_overlay` accepts only generation, expected
revision and source revision. The server reads the owner ports, checks source
method continuity, seals the exact generic `attach_role_overlay` payload with
an internally injected issuer, and verifies the same-generation WORKER READY
`SHADOW_ONLY` state, preserved birth receipt and zero-claim fence. Client
attestation or role/payload choices are rejected. A second attach cannot replay
birth or attach.

`read_worker_operative_task` is a separate read-only entrypoint. It checks
fresh attached WORKER state and method continuity, then reads an **already
issued** PM task decision whose claim/packet/queue, generation, method and
scope fit the current parent mandate. Its response is not WORKER START,
terminal, scheduler authority or a live effect. Any later START needs its own
fresh owner decision and effect gate.

## Local acceptance

Run from Source root:

```text
python -B tooling/validator/worker_overlay_way_home_validation.py
```

The fixture checks G0 role-null READY_UNBOUND, G1 server-held policy and
fenced zero-claim, G2 same-generation attach/readback, and G3 current PM task
within mandate. It rejects client seals, PM self-grant, wrong/revoked Human
intent, stale/wrong mandate, false zero claim, race before fence, concurrent
claim issuance through fence, generation/revision/source/method drift,
out-of-mandate task, and second attach after host restart. A new host session
may read the same current task after handoff; transport readback does not grant
START. G4 terminal and PM admission remain outside this candidate.

No Source push/merge, migration, provider binding, deployment, Boot, live
WORKER creation, C4 operation or real PM claim was performed by this candidate.


## P1603 · PM authority v0.2 local consumer seam

P1603 adds a **default-off local contract only** in
\`mcp/worker_context_auth.py::BoundPmAuthorityV02\`. It does not create a real
Human mandate, PM delegation, provider identity, claim, task, Worker, effect,
configuration, key, database, deployment, or boot.

The seam is deliberately downstream of the existing WORKER role-attach and
\`read_worker_operative_task\` owner reads:

1. **Versioned Human rights:** an injected owner reader returns one current,
   nonrevoked Human-owned PM mandate with immutable semantic revision, original
   Human decision references, supported operation classes, per-operation
   scopes, validity and a basis fingerprint. A current owner may place one
   operation class on HOLD without widening that HOLD to unrelated classes.
2. **PM generation binding:** a separate injected administrative owner path
   bootstraps or technically renews the current PM actor/generation against that
   mandate, followed by an independent delegation readback. The bind receipt
   must say \`HUMAN_ADMIN_OWNER\`; a PM cannot authorize its own bootstrap.
   Same-actor/same-generation renewal requires the explicit
   \`SAME_ACTOR_SAME_GENERATION_TECHNICAL_RENEWAL\` rule. A new generation
   requires explicit succession.
3. **Exact operative decision:** the existing trusted WORKER task reader still
   supplies the already-issued task. v0.2 additionally binds exact task
   revision/SHA-256, operation, authority scope, PM actor/generation,
   delegation, effect target and idempotency key. Task scope must remain a
   subset of both current Human rights and current delegation.
4. **Ordinary effect fence:** an injected owner fence covers the actual local
   representative operation, not merely role attach. Revocation/currentness is
   checked before the owner effect call. Revoke-before-commit denies the new
   effect. A commit already recorded before later revocation remains an
   immutable linked receipt.
5. **Ambiguous outcomes:** once the owner effect port has been invoked,
   unavailable or malformed outcome proof is \`UNKNOWN_COMMIT\`, never silent
   zero effect. Reconciliation reads authoritative owner outcome. Only a
   verified \`NO_COMMIT\` may be called no-commit; a verified committed receipt
   resolves to the exact prior result without replay.
6. **Continuity without semantic regrant:** the immutable effect basis contains
   Human semantic mandate basis, stable delegation id, PM actor/generation and
   exact task/decision/idempotency. Mutable delegation owner/fence revisions and
   progress revisions are outside that identity. Therefore a same-actor chat
   rollover may refresh technical evidence under an explicit renewal rule while
   retaining the same semantic Human mandate. A completed prior decision
   remains the same effect; a distinct next operation requires a new
   decision/idempotency.

Local acceptance:

\`\`\`text
python -B tooling/validator/pm_authority_v02_validation.py
\`\`\`

The fixture uses synthetic in-memory owner ports and supported historical
decision references only as **test data**. It proves mechanics such as
self-grant denial, wrong scope/generation, revoked/stale rights/delegation,
effect-fence ordering, \`UNKNOWN_COMMIT\` reconciliation, immutable basis and
same-actor continuity. It does not install or authenticate those rights in any
production owner.

The first production gap remains the lawful authenticated owner/provider chain:
current Human semantic mandate readback -> current PM actor/generation
delegation/readback -> exact issued decision/currentness -> enforced ordinary
effect fence -> durable provider/owner result readback. X3 production-plan
control and PR33 publication reconciliation remain separate gates.
