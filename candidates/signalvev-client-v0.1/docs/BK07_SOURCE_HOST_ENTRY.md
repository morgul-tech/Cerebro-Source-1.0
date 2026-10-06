# BK07 Source-host entry

`signalvev_client.pm_x9_host` is an in-process host entry stacked on PR83. It is
default off. `signalvev-pm-x9 diagnose` remains a diagnostic CLI and does not
load live host bindings.

The host passes a `PmX9Settings` value and an actual
`providers.pm_x9_live_host.LiveRuntimeBindings` instance to `compose` or
`run_one_existing_ready_receipt`. It must obtain PM and X9
`read_project_control_state` v2 results from each actor's own authenticated
route, a PM-owned atomic `PmProjectionBinding` with Context session custody,
trusted producer/receiver X9 assignments on distinct current sessions, and
existing OAuth, Postgres, NATS and clock objects. None can be assembled from
the example TOML or a channel row. The host sets `enabled=True` and the
explicit `host_authorized=True` only after its own activation decision; the
one-receipt call also requires `receiver_consume_authorized=True`.

The call is `run_one_existing_ready_receipt(settings, bindings, receipt_ref,
host_authorized=True, receiver_consume_authorized=True)`. It starts the listener
and sender, sends that one existing owner receipt, waits at most 30 seconds for
the same event's deposit, and calls `consume_one` only after exact deposited
readback. A new disposition decision performs a fresh hint-bound PM reread
through the existing binding. The returned `HostRunResult` retains typed send, deposit and
consume results. `DISPOSITION_READBACK` requires the same event ID and an
actual fresh disposition record. `ALREADY_DISPOSED` keeps its separate top-level
state: it confirms a prior durable disposition without claiming a new PM reread.
`UNKNOWN_SEND`, replay refusal, missing ingress or
deposit, and exceptions are returned without an automatic retry. Opened
resources are closed on every path. An uncertain result requires owner-led
reconciliation of the original event identity, never a new event or blind
resend.

The Liv candidate at `D:\Cerebro\Run\Signalvev\BK07\candidates\PR83-63218e7e-r2`
remains disabled and is a rollback source, not a running installation.
The previously selected package and configuration remain the rollback target.
The concrete host constructor and provider instances are still required; this
code does not create PM/X9 sessions, role assignments, credentials or schema.
