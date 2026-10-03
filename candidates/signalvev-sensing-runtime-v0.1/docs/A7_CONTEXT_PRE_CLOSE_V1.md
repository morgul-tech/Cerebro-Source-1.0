# A7 Context Learning -> Operational Pulse PRE_CLOSE v1

Status: default-off local Source candidate. Authority NONE. No live Context storage, key, config, deploy, watcher or consumer.

## Existing seam
Source main basis: 66f3c711a4d3dbe533eab058a185bd84105c0463.
Existing Source already defines Operational Pulse stages FRESH_WORLD, CURRENT_CONTACT and PRE_CLOSE.
PRE_CLOSE fail-closes unknown/stale currentness and relevant debt. complete_project_control_event consumes the pulse, verifies persistence/readback, and only then activates navigation/next action.
This candidate does not edit those MCP files.

## Learning persistence
OwnerContextLearningSink implements the merged ContextLearningSink port over an injected owner port.
LEARNING_COMMITTED requires committed owner append plus fresh exact readback of learning_key, semantic fingerprint, pending_id and owner revision.
Malformed/unavailable evidence remains unknown outcome. machine_liveness_credit must remain false.
Production Context custody is not supplied by this candidate.

## PRE_CLOSE projection
Before pointer emission, owner readback must match actor_ref, generation_ref, role, objective, scope, interest, owner revision and visibility.
Wrong/stale generation, cross-scope, privacy/interest mismatch or unreadable owner state returns HOLD_TARGET_SCOPE_UNVERIFIED with zero pointer emission.
This consumes R2 C1003 / PM9474.

## Actor disposition
Transport delivery or ACK is never consumption.
A separate actor-owned disposition receipt must bind projection id, learning key, pending id, actor generation and owner revision.
Allowed dispositions: APPLIED, NO_CHANGE, NOT_APPLICABLE, HOLD_STALE, DEFER_BUSY.
APPLIED, NO_CHANGE and NOT_APPLICABLE consume only after exact disposition readback plus pending ACK/readback.
HOLD_STALE and DEFER_BUSY keep the pending obligation.
This consumes R1 C1002 / PM9472.

## C1001 provenance canary
Machine PM9451 remains UNKNOWN_SEND. Human-direct PM9454 is the actor START. PM9455 is terminal and PM9457 release.
The learning fixture preserves origin=HUMAN and machine_liveness_credit=false. PROS690 is advisory only, not Context append/readback.
Learning commit precedes later pointer projection; exact actor disposition precedes consumption.
This consumes R4 C1005 / PM9476.

## PM9480 eligibility canary
Historical retained/unreleased claim residue is preserved as history but is not current competing work without fresh exact activity on the present actor generation and carrier.
Only current_verified active work matching actor_ref + generation_ref + carrier_ref can produce HOLD_CURRENT_COLLISION.
PM9480 remains advisory registration only and creates no Context learning or C4 work credit.

## Tests
test_a7_context_preclose.py covers owner append/readback, false receipt, duplicate, stale generation, cross-scope privacy, visibility/interest, Human-origin UNKNOWN_SEND, PROS690 advisory-only, transport ACK not consumption, all five dispositions, no encounter, PRE_CLOSE readback, and PM9480 eligibility currentness.

## Proof ceiling
Local contract candidate only.
First production gap remains CURRENT_CONTEXT_PRODUCTION_CUSTODIAN_ACTOR_REF: no verified current actor_ref + generation + delegation/readback for production Context configuration, issuer and storage is bound here.
No C4, Room B, Claude, live provider/storage/key/deploy/global watcher.
## Recovery-cut authoritative obligation currentness

PRE_CLOSE projection now has a mandatory owner-currentness phase before the
existing consumer/privacy phase.

The local pending row identifies only which obligation must be reread. It is
not evidence that the obligation is still open. The injected owner port must
return a fresh CurrentObligationReadback bound to:

- stable obligation_id,
- stable provenance_ref,
- authoritative status OPEN|CONSUMED|REVOKED,
- current actor_ref + generation_ref + scope_ref + binding_ref,
- owner head ref + monotonic head revision,
- owner revision,
- owner fence,
- durable disposition/tombstone state,
- verified owner readback reference.

Projection is allowed only for authoritative OPEN and only when owner
head/revision/fence are at least the pending-pointer basis. Any missing,
unverified, unknown, consumed, revoked, changed binding/provenance or stale
head/revision/fence yields HOLD_OWNER_UNAVAILABLE_OR_STALE with zero pointer,
zero consumption and zero effect.

The recovery falsifier uses two byte-equivalent restored rev10 local pending
histories. In one history fresh owner truth is still OPEN rev10. In the other,
the owner records CONSUMED rev11 and a generation/binding change at rev12.
Only the fresh owner readback may distinguish them. Local pending,
transport ACK, old snapshot absence and current=true are explicitly
non-authoritative for OPEN.

This closes the A4 P1589 PM9497 recovery-cut finding locally. It does not prove
that a production CurrentObligationReader or production Context custodian is
deployed or authorized.
