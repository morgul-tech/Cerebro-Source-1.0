# Execution record — v0.1 bounded canary (`cerebro.v1.lifecycle.receipt`)

**STATUS: EXECUTED (reported). Credential closure UNKNOWN. Verifier provenance UNKNOWN. authority: NONE.**

Completion work for `claude/canary-v0.1-lifecycle-receipt`, requested by `PRINCIPAL_P22_758DEAED`
(scope: `BOUNDED_COMPLETION_ONLY`), internal owner `X1_KODEMESTER_W40D6`. Documentation, status and
handoff only: no code was changed, no canary was re-run, no credential was handled, nothing was pushed,
merged or deployed, and no Drive document was written by this work. Authored by `CLAUDE_EXTERNAL_SPECIALIST`;
a specialist contribution, not Cerebro truth, until X1 has consumed and reviewed it.

## Evidence strength used in this file

| Label | Meaning |
|---|---|
| `VERIFIED_IN_REPO` | Read from this branch or from git objects at the time of this work |
| `REPORTED` | Taken from Claude's own live-run result note of 2026-09-29 (project document `claude/canary-v0.1-live-run-result.md`); not re-proven by this work |
| `UNKNOWN` / `MISSING_REF` | Not located. Not reconstructed. Owner named below |

## Package and branch basis (X1 integration check)

- The ten Python files in the Human-delivered ZIP are byte-identical to the remote
  `claude/canary-v0.1-lifecycle-receipt` subtree at `2801f7323b803e3ddc71db46a709171e12c99f99`.
  `EVIDENCE_SCHEMA.md` is also identical. Only `RUNBOOK.md` and `component.yaml` differ from that
  remote subtree; this execution record is new there. The ZIP SHA-256 is
  `c2e1f906c3aa04e21d0da398ab7755610b464e3d3fb64492b8d5f2cacb5e0622`.
- Commit `7744898`, reported as the code that ran, is **not reachable in X1's current Source object
  database**. Its tree could not be compared here. Executed-code identity therefore remains
  `UNKNOWN`; matching the remote donor branch does not prove what ran live.
- The remote branch's last two commits are GitHub web uploads (`Add files via upload`), not the
  original local commits. The one-line `.gitignore` mentioned in the earlier report is absent in
  both the delivered ZIP and remote subtree. No code file was changed by this correction.

## Attempt history (both kept; the negative attempt is not overwritten)

| # | Date | Outcome | Strength | Notes |
|---|---|---|---|---|
| 1 | 2026-09-29 | **0/9, consumer timeout. No message was published.** | `REPORTED` | Execution error, not a transport or logic failure: the producer was never started before the consumer's 30 s receive limit (a mis-click in the computer-use navigation). Reported as not a PRINCIPAL on-failure event, since nothing was published. Its evidence file was reported as preserved and handed to Andreas. |
| 2 | 2026-09-29 | **9/9 published, 9/9 accepted, consumer `PASS`**, per-message SHA-256 identical between producer and consumer, all nine stages accepted in order by an unmodified `ReceiptTrail.emit()`. Verifier-reported runtime 17 s (within the 60 min cap). | `REPORTED` | Isolated non-production NATS, one subject, one producer, one consumer, synthetic messages, scoped credential, `NatsTransport` over `nats_stdlib_client.py`. Started via pre-written `.bat` launchers, double-clicked through computer use (no typing in a terminal). The private network address and the credential name are intentionally not recorded in the repository. |

## Evidence pointers (option A: no raw live evidence in this branch)

Raw evidence stays with its lawful evidence/receipt owner. This branch references it only. Producer and
consumer evidence are separate append-only JSON-lines files by construction (see `EVIDENCE_SCHEMA.md`).

| Attempt | Artifact | `run_id` | Immutable ref | SHA-256 | Provenance / owner |
|---|---|---|---|---|---|
| 1 | producer evidence | `MISSING_REF` | `MISSING_REF` | `MISSING_REF` | Reported held by Andreas; lawful evidence owner to be named by X1 |
| 1 | consumer evidence | `MISSING_REF` | `MISSING_REF` | `MISSING_REF` | same |
| 2 | producer evidence | `MISSING_REF` | `MISSING_REF` | `MISSING_REF` | same |
| 2 | consumer evidence | `MISSING_REF` | `MISSING_REF` | `MISSING_REF` | same |

No original evidence file was available to this work. Nothing above is reconstructed from the result note.
If an original cannot be located, it stays `MISSING_REF`.

Way Home for each pointer: evidence owner -> immutable ref -> SHA-256 -> `EVIDENCE_SCHEMA.md` verifier checks 1-5.

## Credential closure: `UNKNOWN`

There is no fresh provider evidence that the scoped canary credential, its password and its ACL were revoked or
rotated after the live run. The 2026-09-29 result note records this only as "not confirmed". Do not call this canary
CLOSED on the credential axis until the lawful credential-custody owner (`X3_INFRAARKITEKT` per PRINCIPAL P22)
delivers the exact closure receipt. The repository may reference an immutable closure receipt or its hash
(`closure_receipt_ref: MISSING_REF`); it must never contain secret values or the credential itself.

## Verifier provenance: `UNKNOWN`

A distinct-verifier verdict `PASS_BOUNDED` ("9/9 published and received with identical ids/hashes, unique correct
stage order, no consumer errors, 17 s") is **reported** in the live-run result note. The original verifier text, the
verifier's role and the source have not been located. This completion therefore does not present the independent
verdict as freshly proven, and no verifier role or name is asserted. X1 or the evidence owner should locate the
original verifier receipt.

## What the run does and does not prove

Reported: the offline-proven `ReceiptTrail` partial order and `validate_envelope()` survived one real NATS round trip
for one golden-path linearization (`PRODUCED -> ... -> RELEASED`), unmutated, in order, without drops or duplicates,
and `nats_stdlib_client.py` spoke to a genuine `nats-server` once.
Not claimed: production readiness, JetStream, durability, `NO_EFFECT` / `EFFECT_UNKNOWN` / second-attempt `RELEASED`
mismatch over real transport (offline-only; a separate future bounded canary if ever earned), receipt attribution,
`nats-server` behaviour under load, partition or reconnect, or reachability of the target from other environments.

## Open deviations (kept)

1. **No `stage` field in the `CEREBRO_MESSAGE_V1` draft.** The canary encodes stage as `message_id` suffix
   (`<attempt_id>::STAGE::<stage>`). `LOCAL_DEVIATION_NE_GLOBAL_SCHEMA_REQUIREMENT`: this stays an open local schema
   observation in this branch. It is not raised as a `CEREBRO_MESSAGE_V1` schema programme by this work. Raise it later
   only if an independent, natural episode shows stage needs first-class semantics.
2. Full receipt attribution (issuer, `observed_at`, evidence reference, parent receipt) out of scope.
3. One golden-path linearization only.
4. ~~`nats_stdlib_client.py` never against a real `nats-server`~~ **Corrected:** it was run against one real
   `nats-server` (reported, attempt 2). Still not proven: other server versions, TLS, JetStream, clustering, or
   behaviour under adverse network conditions.
5. Execution environment: the run is reported to have executed on Andreas's machine, which implies raw TCP to the NATS
   address worked from there. Reachability from the cloud sandbox or the `device_bash` VM is still untested.
6. Credential closure `UNKNOWN`. 7. Verifier provenance `UNKNOWN`. 8. Raw evidence pointers `MISSING_REF`.

## Historical text left unchanged (code is not edited by this work)

These comments describe the pre-run state and are now out of date. They were left untouched so the code stays
byte-identical to what ran:

- `transport.py` (docstring around line 24): "it has never connected to an actual NATS server".
- `consumer.py` (docstring around line 21): "now exercised against real transport bytes for the first time".
- `end_to_end_selftest.py` (lines 12-14): "What remains genuinely untested until the live run is real `nats-server`'s own behavior".
- `offline_selftest.py` (lines 8-14): the "prepared and self-tested now" framing.

## LIVING_READINESS (candidate paragraph; not a promotion)

Wanted direction, per PRINCIPAL P22: `LIVING_PROMOTED_REFERENCE`, not Frozen and not production. Parked pending
assimilation/closure. Claude writes no Drive and promotes nothing.

| Precondition set by PRINCIPAL | Status after this work |
|---|---|
| Branch status corrected to lived reality | Done in this branch (this file, `RUNBOOK.md`, `component.yaml`); pending X1 review |
| Credential closure proven, or stated as bounded debt with a lawful owner | `UNKNOWN`; owner `X3_INFRAARKITEKT` / credential-custody owner |
| Verifier provenance located, or explicitly unresolved without overclaim | `UNKNOWN`; unresolved, no overclaim |
| X1 internal review and handback | Pending |

Boundaries a Living note would carry: not production; no JetStream; the PASS is `REPORTED` and bounded to one golden
path on one isolated server; `stage` stays a local deviation.
