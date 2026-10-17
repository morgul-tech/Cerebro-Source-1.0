# Control Metabolism vNext · inert proposal library

Historical basis: Human Bordslag PM8851–8853; Living Modern PM `END_P22_CONTROL_METABOLISM_VNEXT_FAGOCYTOSE_CANDIDATE_01`; P22 CUT03. The original PR29 branch started at Source `aacd164b360ebc8cafc9cecdd3d89c582678008e`.

The corrected library is integrated in Source as an explicit, non-authoritative
API. It is not registered in the MCP manifest, canonical resolver, runtime or
dispatch paths. Source inclusion makes the reviewed implementation available;
it does not select or activate a new control route.

## Historical donor inventory — 2026-10-01

The table below preserves the 2026-10-01 design disposition. The 2026-10-17
reconciliation at the end supersedes it as a description of current main.

| Existing surface | Disposition | Reason |
| --- | --- | --- |
| `mcp/project_manager_control_governor.py` trusted profile, exact generation, no self authority, mutation readback, typed interrupt, local HOLD | KEEP | Required safety boundaries at real mutation edges. |
| `mcp/control_resolution.py` optional PM governance hook | REFINE LATER | PM should be invoked only for `PM_NEEDED` and true governance; host-bound facts are needed before wiring this candidate into live resolution. |
| `mcp/project_manager_control_governor.py` universal worker terminal admission | REFINE LATER | Routine exact terminal closure belongs to Terminalsluse; retain independent admission only where it changes lawful continuation or material truth. |
| `mcp/control_resolution_host.py` PM same-turn command chain and governor self-next-owner progress | RETIRE AS DEFAULT LATER | Intent/resource envelope must bound continuation; no open-ended PM loop for routine work. Do not change current runtime without a trusted host migration path. |
| Dirigent/Hybrid send and Terminalsluse release | DEFER LIVE PROOF | Provider-direct `send_message_to_thread` can target an existing Codex conversation outside this Source checkout. Physical Hybrid driver qualification and a callable Terminalsluse release writer remain unproven. The candidate performs no effect. |

## Contract and authority

`control_metabolism_vnext.py` is a pure classifier. Inputs are a bounded Intent Envelope, matching Project Lease, and facts that a future trusted host must bind. Outputs: `QUIET`, `PM_NOT_NEEDED_DISPATCH_GRANT`, `PM_NEEDED`, `HUMAN_GATE`, or `HOLD_EXACT`. Every output has `authority=NONE` and `effect_authorized=false`. The dispatch grant is a *candidate* for Dirigent; it is never a send permit.

Deferred qualification activates at its declared trigger. Thus bounded Drive navigation may orient, while a reader result used to authorize effect requires the stronger proof. A known exact terminal may be classified for local drain without PM cognition; this module does not release a lease or admit an effect. Optional Human acceleration cannot become a required heartbeat.

For C9, a future adapter would resolve semantic target to an exact existing thread, verify lease/currentness and use an already authorized provider message route. Submission acceptance and worker consumption are separate receipts. This library classifies those axes; acceptance alone never proves consumption or physical Hybrid start. The historical Dirigent owner return recorded `live_transport_authorized=false`, `driver_qualification=NOT_PROVEN`, `AUTO_WAKE=false`; that dated return is not a current runtime observation.

For C10, [KARTOTEK row 1299](https://docs.google.com/spreadsheets/d/1mXgu5OcezcQrb-Uz1cdCtp_ZeBuo3m5fukZgArqugmI/edit#gid=1975681016&range=A1299:R1299) describes the existing Terminalsluse semantics: exact provider-visible terminal, claim and packet lineage, deterministic transition, no conflict/gate/ambiguity, admission receipt, close/release and refill signal. This candidate checks exact identity and eligibility shape but cannot write the release. No callable provider writer or independent sluse automation is evidenced here.

`tooling/validator/control_metabolism_vnext_validation.py` freezes C0–C12 as contract canaries plus one exact-identity rejection. Run:

```text
python tooling/validator/control_metabolism_vnext_validation.py
```

## First behavior-real edge

Any later activation needs a concrete existing caller and its owner-selected
effect before new integration work is justified. The caller must supply bound,
current facts and distinguish exact target, provider submission and worker
consumption. A real terminal transition additionally needs the existing owner's
admission/release writer and receipts. Validate only that selected integration
edge, reusing unaffected evidence. Source inclusion creates no PM, runtime
connection, scheduler, role change or resume of paused work.

## Reconciliation against main — 2026-10-17

Review basis: main `8b83398981970019444d0d8dcc3268759d497192`; original
candidate `1aa9d55ec455e57e2df37b3e60913aeced299717`, based on
`aacd164b360ebc8cafc9cecdd3d89c582678008e`. Local review commit
`3e7c6a669a80ad31fa6f20073280f8df762cede3` corrected the original candidate.
The subsequent Source integration imports those corrected module and validator
bytes unchanged onto the reviewed main basis. This document is updated for
their Source home and preserves the original implementation/review provenance.

Main already has an optional `project_manager_governance_candidate` hook in
`mcp/control_resolution.py`, canonical owner sequencing in
`mcp/control_owner_routing.py`, and a bounded PM command chain in
`mcp/control_resolution_host.py` (default 8 steps, range 1–32, fresh state after
effects and explicit stop conditions). `tooling/return-bridge/rom_a_normal_path.py`
already separates `SENT_ACCEPTED` from `recipient_use=NOT_OBSERVED`. Existing
terminal/admission gates remain in `mcp/project_manager_control_governor.py`.
These are existing code contracts; this review does not qualify their live use.

No concrete missing caller or active routing need was identified that requires
this five-way compiler. The historical plan to replace PM continuation or add a
new host/Dirigent/Terminalsluse path is therefore deferred. The intent, lease and
resource envelope remain optional design material, with no dependency imposed on
Assistant, BK11, CAP or the Development Toolkit.

The candidate remains default-off: no runtime registration, automatic caller,
scheduler or adapter is added. Direct function calls only classify input shapes.
`PM_NOT_NEEDED_DISPATCH_GRANT` is the retained proposal label, not a send permit;
its nested object also carries `authority=NONE` and `effect_authorized=false`.

Two local result-contract defects are corrected:

- Reported `CONFIRMED` consumption plus a nonempty proof reference can no longer
  set `work_consumed_proven=true`. Receipt fields and the reported proof reference
  are retained as `UNVERIFIED_INPUT`; separate host verification is required.
- A terminal proposal retains all four compared identity fields and the reported
  provider revision in its fingerprint. Different claims or generations cannot
  collapse into the same proposal merely because attempt and packet match.

Neither an identity comparison, `provider_visible=true`, a revision string, a
local fingerprint nor a `KNOWN_TERMINAL` route establishes provider currentness,
worker consumption, terminal truth, admission or release. The terminal output is
also `UNVERIFIED_INPUT`. The compiler does not interpret free-text lease rules or
authenticate the caller's gate/collision facts; its terminal axes remain reported
input values. These limits are preserved rather than expanded into a new runtime.

Validation extends the existing 15 synthetic canaries with four focused checks
for claimed consumption, terminal subject retention, subject-sensitive
fingerprints and non-authoritative public returns. C9/C10 remain local shape
checks; no provider send, worker readback, lease transition or integration runs.

The original 19/19 local canary result is retained as producer evidence for the
unchanged implementation. Main integration checks only source composition,
importability and the absence of an active caller or registration; it does not
repeat that campaign or qualify a live transport/terminal effect.
