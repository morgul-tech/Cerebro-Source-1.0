# Resource envelope and recovery notes (fixture only; production limits NOT approved)

## Envelope (D1Config defaults)

| Item | Fixture default | Notes |
|---|---|---|
| Stream | `X2_D1_TEST`, subject `cerebro.test.d1.pointer`, file storage, replicas 1, Limits, DiscardNew | literal, no wildcard |
| MaxAge / MaxMsgs / MaxBytes / MaxMsgSize | 600 s / 128 / 262144 / 4096 | DiscardNew rejects new, never evicts old |
| duplicate_window | 120 s | finite: owner/return idempotency still protects later replays |
| Consumer | `X2_D1_RECEIVER` durable pull, AckExplicit, DeliverAll, max_ack_pending 1, ack_wait 1 s, max_deliver 3 | no ordered/push/auto-ack |
| Pull | batch 1, bounded fetch timeout (1 s; tests 0.5 s) | `drain_once` max 8 items, no resident loop |
| Publication lookup | `lookup_bound` 256 get_msg calls and `lookup_time_budget_s` 10 s per call; `reconcile_once` shares one budget across its rows | a stream span larger than the remaining budget is never scanned: unresolved RECONCILE_HOLD |
| Send claim | `claim_stale_s` 5 s (> `publish_timeout_s` 2 s + 2 s transport margin) | younger SEND_CLAIMED attempts may still be in flight: never raced |
| HOLD NAK delay | 0.2 s | each NAK counts as a delivery toward max_deliver |
| Pointer TTL | ≤ 120 s and ≤ MaxAge | |
| In-progress extension | not used | processing is short; ack_wait bounds anything stuck |

Test overrides used (reported exactly by the tests): `max_msgs=2`, `max_bytes=2000`, `max_msg_size=512` (capacity);
`max_age_s=2, pointer_ttl_s=2, duplicate_window_s=1` (MaxAge expiry); `duplicate_window_s=1` (replay after window);
`pointer_ttl_s=1` (TTL reject); `lookup_bound=1` (incomplete lookup); `ack_wait_s=1, hold_nak_delay_s=0.2,
pull_timeout_s=0.5` (all integration tests).

## Recovery paths

| Situation | Behaviour |
|---|---|
| Owner committed, crash before the send claim | outbox intent INTENDED; `reconcile_once` publishes once (first publish, not a retry) |
| Crash after the durable send claim (before or after the broker stored it) | SEND_CLAIMED survives; restart → bounded lookup of id + exact digest: found → PUBLISHED (RECONCILED_BY_LOOKUP); proven absent and claim stale → one send under a new claim; absent but claim young → unchanged, no send |
| Claim write fails | CLAIM_NOT_DURABLE, intent unchanged, zero transport calls |
| Two publishers on one intent | compare-and-set on (state, claim token): one claim wins, the other sends nothing |
| No owner outbox | `end_to_end_claim()` = `LOCAL_TRANSPORT_PRESERVED_BUT_OWNER_END_TO_END_NO_LOSS_UNPROVEN` |
| PubAck lost / timeout | UNKNOWN_PENDING → bounded lookup (id + digest) → PUBLISHED; lookup unavailable/incomplete/over budget or stream with removals → RECONCILE_HOLD; never a blind second publish, independent of the broker duplicate window |
| D1 default OFF | publish/reconcile/metrics/drain make zero transport calls and change no publication state; owner commits keep their intents |
| Same Nats-Msg-Id, other bytes | broker answers duplicate=True; adapter reads the stored bytes → CONFLICT_HOLD |
| Lost optional wake | pointer stays pending in the stream; next safe-idle drain processes it |
| Active Human turn / local effect | no lease → defer, nothing pulled; lease lost mid-processing → NOACK, no ledger insert |
| Return append/readback or journal failure | NOACK (or ACK withheld); redelivery reproduces the same receipt |
| Crash after disposition before ACK | recorded disposition for that stream seq + bytes is honoured and ACKed (RECOVERED) |
| ACK lost | redelivery → recorded disposition → same receipt, no second materialisation |
| MaxAge expiry / max-delivery | owner outbox + receiver journal (+ captured advisory) surface EXPIRED_UNCLOSED / MAX_DELIVERY_EXHAUSTED after restart; no automatic republish |
| Capacity | REJECTED_CAPACITY in outbox, old pending kept, obligation visible |

## Remaining seams (first exact residual seam first)

1. Owner-side: the real Project owner must supply a durable outbox/unqueued-event feed with the same transaction as
   its commit, plus authenticated full-pointer route verification and current read; this candidate's owner is synthetic.
2. The pre-append fence is real only inside the owner's store; here it is a sqlite mirror of the PostgreSQL design.
3. Safe-boundary: the real host must provide the lease/epoch contract atomically with Human turn start.
4. Broker custody: replicas 1 on one local server proves restart on the same file store only — not replicated
   durability, production install, ACL or EDGE behaviour. The max-delivery advisory is ephemeral; durable
   bookkeeping is what survives restart.
