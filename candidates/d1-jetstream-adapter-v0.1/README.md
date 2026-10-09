# d1-jetstream-adapter-v0.1 — selective JetStream D1 adapter for the existing D1 gateway (BK11)

AUTHORITY = LOCAL_SYNTHETIC_IMPLEMENTATION_ONLY. Default OFF. One adapter, one receiver, one disposable stream.
Not a scheduler, owner engine, truth store, production config, or a change to the Signalvev Core NATS client.

## What it adds (the missing delta)

* `d1js/nats_transport.py` – real nats-py JetStream binding: publish with PubAck + stable `Nats-Msg-Id`,
  one durable pull consumer `X2_D1_RECEIVER` on stream `X2_D1_TEST`, literal subject `cerebro.test.d1.pointer`,
  `ack_sync` explicit ACK, NAK for HOLD, bounded stream lookup, max-delivery advisory capture.
* `d1js/port.py` – `JetStreamPullAckPort` implementing the historical gateway's `PullAckPort` (`pull_one/ack/noack`):
  delivery_id = `<stream>/<consumer>/<receiver>/s<stream_seq>/c<consumer_seq>/d<num_delivered>`; strict pointer
  validation; **disposition write + independent exact readback before every ACK** (terminal or negative);
  recorded disposition honoured on redelivery; compatible-HINT coalescing per drain; lease re-checks.
* `d1js/composition.py` – thin composition: gateway + port + lease + synthetic owner/return ports.
  `drain_once()` returns, per item, the gateway's **original** `GatewayResult` plus D1 disposition, transit ACK state
  and unresolved-obligation marker (separately). `reconcile_once()` and `metrics()` are bounded callables.
* `d1js/publisher.py` – owner outbox intent → **durable send claim** (atomic compare-and-set INTENDED → SEND_CLAIMED,
  one winner) → publish → PubAck recorded → optional wake (loss is harmless). Every ambiguous attempt
  (SEND_CLAIMED / UNKNOWN_PENDING / RECONCILE_HOLD, incl. after a process death) is resolved only by a **bounded**
  lookup (stable id + exact digest; ≤ `lookup_bound` get_msg calls and ≤ `lookup_time_budget_s` per call, a span the
  budget cannot cover is an unresolved RECONCILE_HOLD); a retry only after a complete lookup proves absence and the
  last claim is older than `claim_stale_s`. CONFLICT_HOLD on same id/other bytes. Default OFF: no publish/lookup/
  stream-state/advisory call and no publication-state change from any public entry (correction r1.1).
* `d1js/reference_root.py` – imports the historical gateway (cbbc315b) from an **explicit reference root**, every
  file SHA-256 pinned; nothing copied or reimplemented. Project return reference (a8f1780f) is hash-checked only.
* SYNTHETIC (labelled) ports: `owner.py` (owner commit/outbox/head/closure ledger, mirrors the reference return
  semantics incl. pre-append head fence and independent readback), `owner_ports.py` (gateway bindings),
  `boundary.py` (safe-boundary host + lease/epoch guard), `journal.py` (receiver bookkeeping), `faults.py`.

Mapping: `SELECTED_RETURN_CLOSED → CONSUMED_SELECTED_RETURN` (only after the exact return receipt is independently
readable), `STALE_SUPERSEDED → STALE`, `READ_NOT_MATERIAL → NO_MATERIAL_DELTA`; `NOT_APPLICABLE`, `EXPIRED`,
`REJECT_*` and every HOLD reason are kept. ACK is transit only — never work, effect or truth.

## Setup and commands (from the candidate root)

Reference roots are passed explicitly (no guessing): set
`D1JS_GATEWAY_ROOT=<input package>/materials/historical-gateway/candidates/d1-hybrid-gateway-v0.1` and
`D1JS_PROJECT_RETURN_ROOT=<input package>/materials/historical-project-return`
(or write them to `reference_roots.local.json` as `{"gateway_root": ..., "project_return_root": ...}`).

```
python -B -m unittest discover -s tests -p 'test_unit*.py'
python -B fixture/run_jetstream_tests.py --nats-server <bundled nats-server> --work <NEW private fixture dir>
```
Correction r1.1 focused tests: `tests/test_unit_publisher_correction.py` (synthetic, part of the unit discover) and
class `J_PublisherCorrection` in `tests/integration/test_js_d1.py` (real broker, part of the harness run).
Install nats-py only from the supplied wheel: `pip install --no-index --find-links <pkg>/dependencies
--require-hashes -r requirements-fixture.txt`. The harness refuses an existing work dir, binds 127.0.0.1 on a free
port, owns only its own server PID, restarts the SAME file store for restart proof, keeps `<work>/logs` and
`<work>/evidence`, and removes only `<work>/store` and `<work>/state`. Exit 0 = pass on the real broker, 1 = fail,
3 = UNRUN (exact reason in `evidence/results.json`), never a stub PASS.

Unit tests use a SYNTHETIC in-memory transport and are not broker evidence; the integration suite
(`tests/integration/test_js_d1.py`) runs only under the harness against the real nats-server.

## Future internal integration (not done here)

1. Source integration chooses how the pinned gateway bytes enter the tree; `load_gateway()` then binds that path.
2. Replace the SYNTHETIC owner/outbox/return ports with the real Project owner route verifier, current read,
   append-time fence (`PostgresProjectD1ClosureReturnPort.append_and_confirm` + an exact `read_exact`) and the
   owner-side unqueued-event reconciliation feed.
3. Replace the SYNTHETIC safe-boundary host with the genuine host capability (same lease/epoch contract).
4. Broker owner supplies least-privilege stream/consumer/ACL/custody; production limits are not approved here.
See `RESOURCE_AND_RECOVERY.md` for the envelope, recovery paths and remaining seams.
