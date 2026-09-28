# Signalvev Reference Implementation Candidate v0.2 ("andre bolge")

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

This is not an authoritative addition to Cerebro Source. It activates
nothing, migrates nothing, and does not compete with any existing
sannhetsflate, protocol, or transport binding. It follows directly on
`candidates/signalvev-reference-v0.1` (merged into `main` via PR #4) and
covers the **"andre bolge"** primitives named in Communication Stack v0.1
section 7 (Nåværende modningsrekkefølge): Presence/Service Directory,
Request/Reply, Selective Durable Inbox, and Artifact Pointer/File
Transfer. None of these had an executable, offline-testable Source
representation prior to this branch.

## Base commit

```
9695449 (Merge pull request #4 from morgul-tech/claude/signalvev-reference-v0.1)
```
(`main`, confirmed via `git fetch origin main` immediately before this
branch was created — this is the post-merge `main`, so this candidate
already includes v0.1's schema/registry/receipt-taxonomy files as
ordinary repository content, not as part of this PR's diff.)

## Branch

`claude/signalvev-reference-v0.2`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.2/presence-directory-candidate.yaml
A  candidates/signalvev-reference-v0.2/request-reply-candidate.yaml
A  candidates/signalvev-reference-v0.2/selective-durable-inbox-candidate.yaml
A  candidates/signalvev-reference-v0.2/artifact-pointer-transfer-candidate.yaml
A  candidates/signalvev-reference-v0.2/component.yaml
A  candidates/signalvev-reference-v0.2/README.md   (this file)
A  tooling/validator/signalvev_reference_v02_validation.py
```

Nothing in `candidates/signalvev-reference-v0.1/` is modified. This PR's
validator reads one file from it
(`subject-registry-v0.1-candidate.json`) for a cross-version consistency
check only.

## Schema and data version

- `presence-directory-candidate.yaml`: `schema`
  `cerebro-signalvev-presence-directory/v0.1-candidate`.
- `request-reply-candidate.yaml`: `schema`
  `cerebro-signalvev-request-reply/v0.1-candidate`.
- `selective-durable-inbox-candidate.yaml`: `schema`
  `cerebro-signalvev-selective-durable-inbox/v0.1-candidate`.
- `artifact-pointer-transfer-candidate.yaml`: `schema`
  `cerebro-signalvev-artifact-pointer-transfer/v0.1-candidate`.

These do not extend or version-bump `CEREBRO_MESSAGE_V1` or the Subject
Registry (`schema_version` `"1.0.0-draft"` and
`cerebro.communication.subject-registry/v0.1-draft` respectively remain
exactly as delivered in v0.1). This candidate is reference policy/logic
for message classes and subjects v0.1 already registered
(`PRESENCE`, `REQUEST`, `RESPONSE`, `DURABLE_COMMAND`,
`ARTIFACT_POINTER`), not a new envelope.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `presence-directory-candidate.yaml` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 1. Human text -> 2. Felles primitive verktoy -> PRESENCE / SERVICE DIRECTORY |
| `request-reply-candidate.yaml` | same | same | 1. Human text -> REQUEST / REPLY PRIMITIVE; 2. X4 runtime-fagpass -> Request/reply and pointers |
| `selective-durable-inbox-candidate.yaml` | same | same | 1. Human text -> SELECTIVE DURABLE INBOX; 2. X4 runtime-fagpass -> One sequencing correction |
| `artifact-pointer-transfer-candidate.yaml` | same | same | 1. Human text -> ARTIFACT POINTER / FILE TRANSFER; 2. X4 runtime-fagpass -> Request/reply and pointers |

Each file's own `_provenance` block carries the same component byte pins
already used in v0.1 (Human text: 8905 bytes, SHA256
`4431987c22dd2d4bada0ea4762e7373adebd4ee568868de79cbd203fb381517b`; X4
runtime-fagpass: 10397 bytes, SHA256
`0bfd76bb045f3610d9fd04d2ecb3389d9514f23a19b1bc27cb1597a7d3c4b8de`), as
stated by the upstream handoff document — see open deviation #1.

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v02_validation.py selftest
signalvev_reference_v02_validation selftest: 12/12 PASS
$ echo $?
0
```

Regression check — v0.1's own suite still passes unchanged after this PR:

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v01_validation.py selftest
signalvev_reference_v01_validation selftest: 18/18 PASS
```

12 checks total in this PR:
- 9 second-wave falsifiers (`SW1`–`SW9`, this candidate's own numbering —
  the source document does not itself enumerate second-wave falsifiers,
  unlike the twelve first-wave falsifiers v0.1 transcribed verbatim from
  X4). Each test's docstring cites the specific doctrine sentence it
  encodes (Presence-not-authority, service-directory-not-scheduler,
  request-requires-timeout, stale-response-handling, effectful-timeout-
  not-silently-retried, ephemeral-class-never-deduplicated, durable-class-
  replay-collapses-to-one-effect, artifact-hash-verified-before-use,
  pointer-never-carries-inline-bytes).
- 3 cross-version consistency canaries against
  `signalvev-reference-v0.1/subject-registry-v0.1-candidate.json` (read
  only), checking that this candidate's `DURABILITY_CLASSES` vocabulary
  covers every `durability_class` value the v0.1 registry already uses,
  and that `cerebro.v1.work.command` / `cerebro.v1.artifact.pointer`
  carry the specific classes the source document's own text implies for
  them.

All tests run fully offline: no socket, subprocess, filesystem write
outside the repo checkout, or network call.

## What this candidate adds beyond v0.1

v0.1 delivered the envelope, registry, receipt taxonomy and the twelve
X4 falsifiers — but four of those twelve (3, 6, 7, 9, 10, 12 per v0.1's
own open-deviation #4) were **simulated** against small synthetic
in-process scenarios rather than backed by a real, reusable reference
class. This candidate supplies the actual reference implementations the
doctrine's second wave names:

- `PresenceDirectory` — a real state container for the six presence
  states, with no authority-bearing operation reachable on it at all
  (not just a test asserting presence and work-admission stages are
  disjoint sets, as v0.1's falsifier 6 did).
- `RequestReplyMatcher` — real correlation-id tracking, timeout
  resolution, and stale-response detection, rather than a single
  standalone `treat_as_canonical()` function (v0.1's falsifier 9).
- `DurableInboxPolicy` — a real classify-then-dedup policy across all
  four durability classes the v0.1 registry already uses, rather than a
  single inline `admit()` closure (v0.1's falsifier 4, which this
  candidate's `test_sw7` generalizes to the full class vocabulary).
- `make_pointer()` / `verify_and_consume()` — a real artifact-pointer
  construction and verification path that also rejects inline payload
  bytes at construction time, not only a hash-mismatch check (v0.1's
  falsifier 10).

## Compatibility note against existing L0/S0/S1 evidence

Unchanged from v0.1's own compatibility note, restated for this PR:

- **S0** (private NATS ping/pong roundtrip, LEDGER row
  `EDGE-S0-LIV-EDGE-PINGPONG-B6E9D2-RECEIPT-001`) and **S1** (pointer ->
  authenticated Drive reread -> ACK_READ, LEDGER row
  `EDGE-S1-X1-2B84D7E1-ACKREAD-RECEIPT-002`) remain prior, independent,
  behavior-real transport evidence. This candidate does not re-run,
  re-prove, or supersede them, has no dependency on them, and takes no
  position on whether `PM_PRINCIPAL_CHANNEL` (PM8628) has currentized
  LEDGER row 181's evidence.
- This candidate is, like v0.1, a Source-repo-only, offline reference
  deliverable. It does not touch Google Drive, the Coordination Ledger,
  or any Living document.

## Open deviations and assumptions

1. **Hash verification of source citations not independently performed**
   (same caveat as v0.1, open deviation #1). The upstream handoff
   document pins exact byte-size/SHA256 for its two text components; this
   candidate was built from a Markdown-rendered Drive-API extraction, not
   raw bytes. Field-for-field content was preserved by direct
   transcription of the PRESENCE / REQUEST-REPLY / SELECTIVE-DURABLE-
   INBOX / ARTIFACT-POINTER sections; byte-exact equality against the
   pinned hashes has not been checked.
2. **Second-wave falsifiers (SW1–SW9) are this candidate's own
   derivation, not a transcription.** Unlike v0.1's twelve falsifiers,
   which exist verbatim as a numbered list in the X4 source, the source
   document names the second-wave primitives and their governing
   sentences but does not itself enumerate second-wave falsifiers by
   number. Each SW test's docstring quotes the exact source sentence it
   encodes so this can be checked against the source directly; the
   selection and phrasing of "nine falsifiers" is Claude's own scoping
   choice, open to PRINCIPAL revision.
3. **`RequestReplyMatcher` and `DurableInboxPolicy` are new executable
   logic, not transcriptions.** As with v0.1's `ReceiptTrail`, these
   classes did not exist anywhere in Source prior to this branch and
   should be reviewed as a proposal, not trusted as a restatement of
   pre-existing code.
4. **No dependency on live transport for any of the four primitives.**
   `PresenceDirectory`, `RequestReplyMatcher`, `DurableInboxPolicy`, and
   `verify_and_consume()` are all purely in-process; none of them are
   wired to NATS, WireGuard, JetStream, or any VPS/EDGE-01 state. Live
   integration is a distinct, later verification step, exactly as it was
   for v0.1.
5. **`DurableInboxPolicy` durability storage is a Python dict for the
   life of one process.** No persistence, restart-survival, or
   JetStream-class mechanism is implemented or implied — the file's own
   `implementation_note` says this explicitly. What is delivered is the
   *classification and dedup policy* a durable inbox implementation must
   satisfy, not a durable inbox itself.
6. **Cross-version canaries read but do not modify
   `signalvev-reference-v0.1/subject-registry-v0.1-candidate.json`.** If
   PRINCIPAL or a later PR changes that file's `durability_class` values,
   `tooling/validator/signalvev_reference_v02_validation.py`'s three
   `test_v01_registry_*` checks will need a matching update; that
   coupling is intentional (it is what "second wave builds on first
   wave" means concretely) and is called out here so it is not
   mistaken for an accidental cross-file dependency.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a claim that Presence, Request/Reply, Durable Inbox, or Artifact
  Pointer are live anywhere in Cerebro today — this is reference policy
  and logic for primitives the architecture has named, not a report that
  they are deployed.
- Not a statement that `PM_PRINCIPAL_CHANNEL`, the Coordination Ledger,
  or any Living document has been updated — none have been, and none
  are touched by this PR.
- Not a modification of `candidates/signalvev-reference-v0.1/` or of
  anything merged by PR #4.
