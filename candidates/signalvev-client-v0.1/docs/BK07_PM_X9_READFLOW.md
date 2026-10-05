# BK07 — PM-to-X9 read-only flow (binding seam)

Authority NONE. Default OFF. Candidate code for internal review; not deployed, no live PM/channel/broker contacted.

## Route

```
PM receipt ─PmOwnerCommitReader.read_verified (authenticated, committed, read-back, consistent; reread SAME)─▶
  read_trusted_owner_event ─▶ OwnerEvent (POINTER, referent PM_READY_HINT / pm-ready:…) ─SendClient (durable ledger)─▶ D0
  ─Core NATS─▶ ListenClient (bounded Dispatcher, off the event loop) ─▶ SensingReceiver
  ─PmRereadResolver (fresh PM reread, owner-judged relation)─▶ ACK_READ closure
  ─X9DepositSink─▶ X9ChannelIngress.deposit (producer-scoped port, exact pointer readback) ─▶ PulseInbox (bounded)
  ─explicit X9 pulse─▶ consume_one ─PmHintCutReader (fresh hint-bound PM reread)─▶ STALE | NO_MATERIAL_DELTA | MATERIAL_ROUTE
```

## Public API (`signalvev_client.pm_x9`)

| Operation | Meaning |
|---|---|
| `parse_settings(mapping) -> PmX9Settings` | bounded non-secret `[pm_x9]` table; unknown keys and inline secrets refused; `mode` defaults to `OFF` |
| `PmX9HostPorts(pm_port, producer_channel, x9_channel, clock, client_config, connect_fn=None, pm_reread_port=None)` | what the host binds |
| `diagnose(settings, ports) -> [{port,status,detail}]` | per-port `BOUND / MISSING / INVALID / SYNTHETIC_IN_PRODUCTION / NOT_SYNTHETIC_IN_TEST_MODE / DEFAULT_OFF`, no I/O |
| `build_binding(settings, ports) -> PmX9Binding` | **the one factory entry point**; raises `PmX9Unbound(code, diagnostics)` before any I/O |
| `load_ports(settings)` | resolves `ports_factory = "module:callable"` |
| `binding.open_sender()` / `binding.send_hint(receipt_ref) -> HintSendResult` | operation 1 |
| `binding.start_listener()` | operation 2: `ListenClient(resolver=PmRereadResolver, sink=X9DepositSink)` |
| `binding.consume_one(event_id, prior_material_sha256=None, now=None) -> ConsumeResult` | operation 3 (the X9 pulse calls it) |
| `binding.pulse(*, human_conversation_active, prior_material_sha256=None, now=None) -> PulseReport` | one explicit pulse over queued ingress; deferred untouched while a Human conversation is active |
| `binding.status()` / `binding.close()` | honest counters / release |

CLI: `signalvev-pm-x9 diagnose --config FILE` (0 bound, 2 config error, 3 off/unbound) and
`signalvev-pm-x9 selftest` (SYNTHETIC_TEST_ONLY whole flow; 0 pass, 4 fail).

## Mapping

PM reread (`PmCurrentRead`, projection required) → receiver `ResolverResult`:
`source_ref=owner_ref`, `referent_type=PM_READY_HINT`, `referent_id`, `current_revision`, `revision_relation=relation`,
`observed_sha256=snapshot_sha256`, `owner_seq`, `grounding={snapshot_ref, source_cut, ready_state}`.
SAME with a packet hash different from the trusted host coordinate fails closed (HOLD_UNREADABLE).

`PmCurrentRead` → X9 `PMOwnerCut`: `owner_ref, claim_ref, packet_ref, queue_ref, packet_sha256` (projection),
`revision=current_revision`, `relation_to_hint=relation`, `material_sha256`, `source_cut`,
`committed_readback=readback_verified`, `authenticated`, `material_ready = ready_state == "MATERIAL_READY"`.

`PointerContext` = validated D0 (event_id, owner_ref, referent type/id, revision, expected hash, owner_seq, way_home) +
trusted host coordinates (claim/packet/queue, packet_sha256, producer, X9 session, attempt) + receive-time owner
`source_cut` + `expires_at = clock + pointer_ttl_seconds`.

## Minimal interface changes (allowed seam)

* `pm_owner_commit.PmCurrentRead`: optional projection fields `claim_ref, packet_ref, queue_ref, packet_sha256,
  ready_state, material_sha256, source_cut, consistent_snapshot` (default `None`). `reread_current(...,
  require_projection=False)`: when required or present, all must be present, consistent, typed, and on the expected
  claim/packet/queue (`PM_REREAD_PROJECTION_MISSING / _SPLIT_SNAPSHOT / _PROJECTION_INVALID / _COORDINATE_MISMATCH`).
* `x9_channel_ingress`: `PointerContext`/`PointerRecord` gain `referent_id` and `owner_seq` (default `None`). With
  `referent_id` set, the closure must name exactly that referent; `claim_ref` stays the separate coordinate. `None`
  keeps the legacy claim-referent check. `X9ChannelIngress(pm_hint_reader=...)` (`read_current_for_pointer(pointer)`)
  is used for the fresh owner read when bound; the legacy `pm_reader.read_current(claim, packet, queue)` is unchanged.

## Packaging

The existing client wheel builder now also bundles the two adapters byte-identical as package `signalvev_adapters`
(pinned in `RESOURCE_MANIFEST.json`, verified by `_bootstrap` at import) and exposes `signalvev-pm-x9`.
`signalvev_client._adapters` binds the same module names to the source directory in a checkout; the installed route
never falls back to a source path. `packaging/verify_pm_x9_install.py` proves the installed route in a clean venv.

Example configurations live in `examples/pm_x9/` (a subdirectory, so the existing client example-config test, which
parses `examples/*.toml` as client configs, is unaffected).
# Internal reconciliation — C1146

This returned composition is integrated on Source `510e1c04a0b33a9f1fa16b78b102dd0ff8e5db63`,
which already contains the newer PR51 PM provider, PR52 X9 session provider and PR50 external
package authority guards. Those provider and guard bytes are unchanged. The returned PM
consumer adapter (absent from this main) is added as supplied; it does not replace PR51's server.

Concurrent P22 active-HOLD policy is retained. The hint-bound fresh PM projection carries the
owner's `active_hold` unchanged to the current X9 policy, including due action/escalation and
invalid-binding refusal. It is not synthesized from the event or used to bypass authority.

The client and PR52 now share canonical adapter module objects under both `signalvev_adapters`
and `adapters`. Installed imports use pinned bundled bytes and never source fallback; conflicting
preloaded modules refuse. PR52 keeps its pinned principal/session, fresh identity/ACL checks,
receiver-only pointer-write refusal and exact readback checks. The host provider implementations
are not bundled in the client wheel.

The raw PR51 server is **not** a client port: it requires host-custodied credentials and exact
expected sequence/hash inputs, and returns `PmReadyRecord` rather than the client's typed
committed/current projection. `diagnose` rejects that incompatible signature without I/O. The
host factory must supply a trusted adapter preserving every PR51 authentication/currentness check
and an owner-produced same-snapshot material digest/source cut projection; these fields must not
be guessed from packet bytes or independently read rows. Until that adapter/atomic owner backend
and credentials/ACL are qualified, leave production OFF. Producer ChannelPort and X9's real
session API/atomic disposition readback/pulse hook remain independent qualification boundaries.

Recent ingress results, deposit diagnostics and resolver thread-ID diagnostics each retain at
most 256 entries. Total ingress/deposit/call counters are independent of tail eviction;
`wait_for_ingress(count)` waits on the total handled count. Use `ingress_count` rather than the
length of `ingress_results()` when setting the next wait target. Eviction does not remove durable
provider receipts, dedupe identities or the pulse pending queue. These diagnostics are not a
receipt archive; durable pending/restart proof still belongs to the real host/provider.

The original external ZIP/wheel and X2's completed source77/installed49/pins42 evidence remain
immutable. Internal code changes need distinct review; the old wheel is not evidence of the new
integrated bytes. Only targeted reconciliation/retention tests are new here. The separate X2
Windows verifier correction preserves `SystemRoot` in its subprocess environment.

Room C rev2 (C2 → C3 → C4) remains the current operational case. This PM/X9 composition does
not authorize a first PM/X9 live trial, install, NATS connection, deployment or actor work.
