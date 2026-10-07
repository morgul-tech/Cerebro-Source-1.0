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

The binding TOML must name exactly `providers.pm_x9_live_host:make_ports`.
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

The PR85 host-entry and its live provider stack are still outside published
Source `main` at this change's base. This entry does not import candidate
provider code, reuse an old BK07 receipt, enable the local profile, or claim
a running normal listener. Those require a separately qualified current host
binding and a new eligible PM owner receipt.
