# Signalvev Reference Implementation Candidate v0.3 ("tredje bolge")

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

This is not an authoritative addition to Cerebro Source. It activates
nothing, migrates nothing, and does not compete with any existing
sannhetsflate, protocol, or transport binding. It follows on
`candidates/signalvev-reference-v0.1` (merged into `main` via PR #4) and
covers the **"tredje bolge"** primitives named in Communication Stack
v0.1 section 7 (Nåværende modningsrekkefølge): Communication Flight
Recorder, Workload Identity/Attestation, and Schema/version
compatibility. None of these had an executable, offline-testable Source
representation prior to this branch.

## Relationship to `candidates/signalvev-reference-v0.2`

This candidate is branched from `main` (post PR #4), **not** from
`claude/signalvev-reference-v0.2` (PR #6), because PR #6 was still open
and unmerged when this branch was prepared. This candidate therefore has
**no code dependency on v0.2** — its validator imports only from v0.1
(`STAGE_PREDECESSORS`, `ReceiptTrail`). Two source-citation comments in
`schema-version-compatibility-candidate.yaml` and the third-wave
falsifier suite mention v0.2's `request-reply-candidate.yaml`
`SCHEMA_MISMATCH` outcome for narrative continuity only — they are plain
comments, not imports, and this candidate's offline tests do not read
any v0.2 file. If PR #6 merges before this PR, there is no conflict:
`candidates/signalvev-reference-v0.3/` and
`candidates/signalvev-reference-v0.2/` occupy disjoint paths.

## Base commit

```
9695449 (Merge pull request #4 from morgul-tech/claude/signalvev-reference-v0.1)
```
(`main`, confirmed via `git fetch origin main` immediately before this
branch was created.)

## Branch

`claude/signalvev-reference-v0.3`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.3/flight-recorder-candidate.yaml
A  candidates/signalvev-reference-v0.3/workload-identity-attestation-candidate.yaml
A  candidates/signalvev-reference-v0.3/schema-version-compatibility-candidate.yaml
A  candidates/signalvev-reference-v0.3/component.yaml
A  candidates/signalvev-reference-v0.3/README.md   (this file)
A  tooling/validator/signalvev_reference_v03_validation.py
```

Nothing in `candidates/signalvev-reference-v0.1/` is modified. This PR's
validator **imports** (does not copy) `STAGE_PREDECESSORS` and
`ReceiptTrail` from
`tooling/validator/signalvev_reference_v01_validation.py`, and reads one
file from `candidates/signalvev-reference-v0.1/`
(`subject-registry-v0.1-candidate.json`) for a cross-version consistency
check only.

## Schema and data version

- `flight-recorder-candidate.yaml`: `schema`
  `cerebro-signalvev-flight-recorder/v0.1-candidate`.
- `workload-identity-attestation-candidate.yaml`: `schema`
  `cerebro-signalvev-workload-identity-attestation/v0.1-candidate`.
- `schema-version-compatibility-candidate.yaml`: `schema`
  `cerebro-signalvev-schema-version-compatibility/v0.1-candidate`.

`CEREBRO_MESSAGE_V1`'s `schema_version` const (`"1.0.0-draft"`) and the
Subject Registry (`cerebro.communication.subject-registry/v0.1-draft`)
are unchanged — this candidate is reference policy/logic for those
existing artifacts, not a new envelope version.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `flight-recorder-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> COMMUNICATION FLIGHT RECORDER; 2. X4 -> Trace paragraph |
| `workload-identity-attestation-candidate.yaml` | same | same | 1. Human text -> WORKLOAD IDENTITY / ATTESTATION; 2. X4 -> Envelope paragraph; 2. X4 -> L1 Identity crosswalk row |
| `schema-version-compatibility-candidate.yaml` | same | same | 1. Human text -> SUBJECT / CHANNEL REGISTRY; 2. X4 -> Registry paragraph |

Each file's own `_provenance` block carries the same component byte pins
used in v0.1/v0.2 (Human text: 8905 bytes, SHA256
`4431987c22dd2d4bada0ea4762e7373adebd4ee568868de79cbd203fb381517b`; X4
runtime-fagpass: 10397 bytes, SHA256
`0bfd76bb045f3610d9fd04d2ecb3389d9514f23a19b1bc27cb1597a7d3c4b8de`), as
stated by the upstream handoff document — see open deviation #1.

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v03_validation.py selftest
signalvev_reference_v03_validation selftest: 11/11 PASS
$ echo $?
0
```

Regression check — v0.1's own suite still passes unchanged after this PR:

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v01_validation.py selftest
signalvev_reference_v01_validation selftest: 18/18 PASS
```

11 checks total in this PR:
- 9 third-wave falsifiers (`TW1`–`TW9`, this candidate's own numbering —
  as with v0.2's `SW1`–`SW9`, the source document names the third-wave
  primitives and their governing sentences but does not itself enumerate
  third-wave falsifiers by number). Each test's docstring quotes the
  exact source sentence it encodes.
- 2 cross-version consistency canaries against
  `signalvev-reference-v0.1` (read only): that this candidate's
  `KNOWN_COMPATIBILITY_POLICIES` covers every `compatibility` value the
  v0.1 registry already uses, and that the imported `STAGE_PREDECESSORS`
  still carries all eleven receipt stages.

All tests run fully offline: no socket, subprocess, filesystem write
outside the repo checkout, or network call.

## What this candidate adds beyond v0.1/v0.2

- `FlightRecorder` — a real, read-only analysis class
  (`first_broken_edge`, `explain`) built directly on v0.1's
  `STAGE_PREDECESSORS`, rather than the inline `first_broken_edge()`
  closure v0.1's falsifier 11 used as a one-off test fixture. It exposes
  no emit/mutate/backfill method at all, making "read-only, never
  fabricates a receipt" a structural property, not just an assertion in
  a test.
- `AttestationBinder` — a real bind/reject contract for the gap X4's L1
  Identity crosswalk names explicitly ("No trusted workload attestation
  joining actor/generation to current machine/session/custody. Envelope
  `source` alone must remain a claim."). It does not close that gap live
  — see open deviation #4 — but it gives the gap an executable shape:
  what attestation would have to supply, and what happens when it is
  absent.
- `CompatibilityPolicy` — a real resolver over the five distinct
  `compatibility` policy strings the v0.1 registry already uses,
  replacing ad hoc per-entry assumptions with one typed-HOLD-by-default
  resolution function.

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from v0.1/v0.2's own compatibility notes, restated for this
PR:

- **S0** (private NATS ping/pong roundtrip, LEDGER row
  `EDGE-S0-LIV-EDGE-PINGPONG-B6E9D2-RECEIPT-001`) and **S1** (pointer ->
  authenticated Drive reread -> ACK_READ, LEDGER row
  `EDGE-S1-X1-2B84D7E1-ACKREAD-RECEIPT-002`) remain prior, independent,
  behavior-real transport evidence. This candidate does not re-run,
  re-prove, or supersede them, has no dependency on them, and takes no
  position on whether `PM_PRINCIPAL_CHANNEL` (PM8628) has currentized
  LEDGER row 181's evidence.
- This candidate is, like v0.1 and v0.2, a Source-repo-only, offline
  reference deliverable. It does not touch Google Drive, the
  Coordination Ledger, or any Living document.

## Open deviations and assumptions

1. **Hash verification of source citations not independently performed**
   (same caveat as v0.1/v0.2, open deviation #1 in both). The upstream
   handoff document pins exact byte-size/SHA256 for its two text
   components; this candidate was built from a Markdown-rendered
   Drive-API extraction, not raw bytes. Field-for-field content was
   preserved by direct transcription; byte-exact equality against the
   pinned hashes has not been checked.
2. **Third-wave falsifiers (TW1–TW9) are this candidate's own
   derivation, not a transcription** — same situation as v0.2's SW1–SW9.
   The selection and phrasing is Claude's own scoping choice, open to
   PRINCIPAL revision.
3. **`FlightRecorder`, `AttestationBinder`, and `CompatibilityPolicy`
   are new executable logic, not transcriptions.** They did not exist
   anywhere in Source prior to this branch and should be reviewed as a
   proposal, not trusted as a restatement of pre-existing code.
4. **`AttestationBinder` does not close the live attestation gap.**
   X4's L1 Identity crosswalk names "no trusted workload attestation"
   as a material gap. This candidate supplies the *contract* — required
   components, rejected identity sources, bind/reject semantics — not a
   live attestation mechanism (no PKI, no signing, no TPM/OS-level
   attestation). `workload-identity-attestation-candidate.yaml`'s own
   `current_gap_acknowledged` field says this explicitly.
5. **No code dependency on `candidates/signalvev-reference-v0.2`**, by
   construction (see "Relationship to v0.2" above) — this candidate was
   branched from `main` before PR #6 merged. The only cross-candidate
   code dependency in this PR is on v0.1, which is already merged.
6. **`CompatibilityPolicy.resolve()` collapses two of the five registry
   policies (`exact_schema_version_or_typed_hold` and
   `no_implicit_upgrade;unknown_version_holds`) to the same exact-match
   behavior**, differing only in their HOLD outcome label
   (`HOLD_VERSION_MISMATCH` vs `HOLD_NO_IMPLICIT_UPGRADE`). The registry
   names them as two distinct policy strings for two different subjects;
   this candidate does not currently encode a *behavioral* difference
   between them beyond the label, since the source text does not specify
   one. Flagged here rather than silently assumed away.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a claim that Flight Recorder, Workload Identity/Attestation, or
  Schema/version compatibility enforcement are live anywhere in Cerebro
  today — this is reference policy and logic for primitives the
  architecture has named, not a report that they are deployed.
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated — none have been, and none
  are touched by this PR.
- Not a modification of `candidates/signalvev-reference-v0.1/`,
  `candidates/signalvev-reference-v0.2/`, or anything merged by PR #4.
