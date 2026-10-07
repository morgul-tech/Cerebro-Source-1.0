# BK07 normal PM/X9 host entry

`signalvev-pm-x9-normal` provides three separate operations: `diagnose`,
`listen`, and `send pm-receipt:<id>`. It never calls `consume_one` or `pulse`;
those belong to X9's authenticated ordinary path. The listener remains alive
until stopped and deposits through the existing producer channel. The sender
uses the existing PM committed-ready reader and durable NATS sender ledger;
transport acceptance does not prove X9 work.

The host supplies four paths: the reviewed `cerebro-bk07-normal-use-profile/v1`
JSON, the existing `[pm_x9]` binding TOML with a current production
`ports_factory`, and separate sender/receiver `ClientConfig` TOML files.
The profile must be explicitly enabled for `send` or `listen`. Its literal
subject, server, credential paths and state directories must match both
client configs. The sender has no listen interest; the receiver has exactly
one `PM_READY_HINT` interest for the bound owner. Production rejects shared
credentials and shared evidence state. The host never reads credential bytes.

The binding TOML must name exactly `signalvev_client.pm_x9_live_host:make_ports`.
The Source host must call `compose(..., host_runtime=...)` with an exact
`LiveRuntimeBindings` instance from that reviewed provider module. A TOML
factory string or a structurally similar object does not confer this
capability. The command-line entry remains UNBOUND without a host-supplied
runtime; it cannot grant itself one from a file. The provider must supply
current authenticated PM owner read/reread and separately scoped producer/X9
channel ports. A NATS credential alone does not prove PM `MATERIAL_READY`.
If the concrete provider backing is absent, every operation refuses before
any NATS send or listener start.
Only a genuine owner-produced `receipt_ref` is accepted by `send`; no event ID
or revision is supplied by the caller. The factory must preserve the current
provider's authentication, atomic snapshot and owner-sequence checks.

The Context adapter is packaged with the client. It accepts only the exact
`PostgresPmOwner`, `X9ProducerHost`, `X9ReceiverHost` and two distinct
`McpToolCallContext` instances supplied in-process by a privileged host.
It uses Context's existing producer receipt verifier, current PM owner read,
PostgreSQL custody channels, and X9-scoped read transaction. Each composition
requires a new current `MATERIAL_READY` receipt whose owner, claim, packet,
queue, hash and commit reference match the host binding. Context rechecks
persisted role sessions and scopes. Caller JSON cannot provide a role, token,
provider result, or project readback as authority.

This adapter does not install a privileged host registration or durable
PM/X9 service sessions. A retained listener needs the host to maintain
valid receiver authentication throughout its lifetime; expired credentials
fail closed at the Context API. The local profile remains OFF until those
capabilities, separate NATS rights, and one genuine new receipt are reviewed
and tested. No old BK07 receipt is eligible for replay.
