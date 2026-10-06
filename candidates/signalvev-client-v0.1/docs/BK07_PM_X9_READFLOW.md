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
| `load_ports(settings, *, host_runtime=None)` | resolves `ports_factory = "module:callable"`; passes an explicit runtime only when supplied |
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

## BK07 X6 current-session host composition

`providers.pm_x9_live_host:make_ports` composes the existing PM projection, Postgres producer/X9
custody, X9 receiver channel, and existing NATS `ClientConfig`. The client loader passes
`host_runtime` only when the live host explicitly supplies it. The runtime object is created by the
host in memory; provider readbacks, credentials, headers, and connection factories are not written
to the TOML file. `enabled=True` is an explicit host setting and is not a role grant or session bind.

Before returning ports, the factory requires fresh `read_project_control_state` v2 readbacks for PM
and X9, provider-bound caller identity, a current persisted session for each caller, matching
transport/persisted session refs, matching project revisions, distinct sessions, and exact agreement
with the provider-owned PM projection and trusted X9 producer/receiver tuples. Shared OAuth principal
metadata is permitted, but never substitutes for these bindings. No identity probe, role bind, NATS
connect, database operation, install, event send, or listener start occurs during this check. Missing
data returns `PM_X9_CURRENT_HOST_COMPOSITION_UNAVAILABLE` with exact missing fields; other exceptions
are reduced to a safe type-only code.

The fresh RYG396 PM10880 and X9 PM10867 reads for this task report
`NO_CURRENT_PERSISTED_SESSION` and `persisted_session=null`. Their transport refs cannot establish
roles. The expected safe result is therefore a fail-closed composition diagnostic naming the missing
persisted session tuple fields. No first PM event is authorized until the existing PM and X9 providers
return actual current persisted bindings; do not create or bind one to clear this preflight.

### A1 install and first-run handoff

1. Install the reviewed Source package from the draft PR commit and use this example TOML as the
   non-secret configuration template. Keep existing NATS, OAuth, header/secret, Postgres, PM-owner,
   and X9 role providers in their current host; inject them through `LiveRuntimeBindings`.
2. Before enabling any operation, take fresh PM/X9 control-state readbacks and invoke
   `load_ports(settings, host_runtime=bindings)`, then `build_binding(settings, ports)`. If the current
   persisted PM/X9 session tuples are absent, stale, duplicate, or mismatched, stop on the returned
   diagnostic. Do not call `open_sender`, `start_listener`, `send_hint`, or `pulse`.
3. Rollback: restore the prior package version and its prior TOML/config selection; keep the new
   runtime composition disabled. No schema installation or credential migration is part of this
   change.
4. The first eligible live edge, after A1 independently verifies both real persisted bindings and
   the existing provider readiness, is one existing genuine PM-ready event through the current
   owner receipt path, preserving its event identity. Continue D0 → X9 deposit/ordinary consume →
   fresh PM reread → typed disposition/readback, then stop and record that single result. Never
   synthesize, replay, duplicate, or restart an event for this qualification.

A1 retains install ownership and X6 retains the operative PM→X9 chain. This PR supplies the selected
non-secret config template, runner factory, schema reuse reference (`providers/x9-role-binding-config.schema.json`),
rollback recipe, and exact first edge. It does not authorize deployment or a live event by itself.

Room C rev2 (C2 → C3 → C4) remains the current operational case. This PM/X9 composition does
not authorize a first PM/X9 live trial, install, NATS connection, deployment or actor work.
