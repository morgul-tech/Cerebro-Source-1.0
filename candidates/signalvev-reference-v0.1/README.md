# Signalvev Reference Implementation Candidate v0.1

**STATUS: ISOLATED IMPLEMENTATION CANDIDATE. AUTHORITY: NONE.**

This is not an authoritative addition to Cerebro Source. It activates
nothing, migrates nothing, and does not compete with any existing
sannhetsflate, protocol, or transport binding. It is a reference
implementation of primitives that were already architecturally adopted
(Human ADOPT, 2026-09-24, Signalvev Transport Contract v0.1) and drafted
(Communication Stack v0.1) but had no executable, offline-testable Source
representation prior to this branch.

## Base commit

```
8c503cda639838d44f29dd6e555fed7dc6e0e2ee
```
(`main`, as of the shallow clone used to prepare this candidate; matches
`CURRENT_SOURCE_MAIN` referenced elsewhere in the Living documentation.)

## Branch

`claude/signalvev-reference-v0.1`

## Changed files (all additions, nothing modified or deleted)

```
A  candidates/signalvev-reference-v0.1/cerebro-message-v1-candidate.schema.json
A  candidates/signalvev-reference-v0.1/component.yaml
A  candidates/signalvev-reference-v0.1/receipt-taxonomy-candidate.yaml
A  candidates/signalvev-reference-v0.1/subject-registry-v0.1-candidate.json
A  candidates/signalvev-reference-v0.1/README.md   (this file)
A  tooling/validator/signalvev_reference_v01_validation.py
```

## Schema and registry version

- `cerebro-message-v1-candidate.schema.json`: `schema_version` const
  `"1.0.0-draft"`, `$id`
  `urn:cerebro:communication:message:v1:draft-no-authority`.
- `subject-registry-v0.1-candidate.json`: `schema`
  `cerebro.communication.subject-registry/v0.1-draft`, 7 entries.
- `receipt-taxonomy-candidate.yaml`: `schema`
  `cerebro-signalvev-receipt-taxonomy/v0.1-candidate`.

## Source references

| File | Upstream document | Drive ID | Section |
|---|---|---|---|
| `cerebro-message-v1-candidate.schema.json` | CEREBRO-Communication-Stack-v01-FULL-HANDOFF.md | `1qoQ0O53D5QVosQLo3-x2wsY8tGhQxXCU` | 3. CEREBRO_MESSAGE_V1 draft JSON Schema |
| `subject-registry-v0.1-candidate.json` | same | same | 4. Subject Registry v0.1 draft JSON |
| `receipt-taxonomy-candidate.yaml` (stage list) | same | same | 2. Felles primitive verktoy -> RECEIPT MODEL (Human text) |
| `receipt-taxonomy-candidate.yaml` (`negative_rules`) | same | same | 5. Viktige negative regler (Human text) |
| `receipt-taxonomy-candidate.yaml` (`falsifiers`) | same | same | X4 runtime-fagpass -> Falsifiers for one offline reference arc |

Each source file's own pinned byte-size and SHA256 (as stated in the
upstream handoff document) are carried in that file's own
`_provenance`/`$comment`/`provenance` block.

## Offline test results

```
$ PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_v01_validation.py selftest
signalvev_reference_v01_validation selftest: 18/18 PASS
$ echo $?
0
```

18 checks total:
- 6 schema-conformance canaries (positive case + 5 negative/rejection cases,
  including the `DURABLE_COMMAND` conditional requirements and the
  `payload_ref`/`payload_hash` co-presence rule)
- 12 falsifiers, numbered exactly as in the X4 runtime-fagpass source
  (`receipt-taxonomy-candidate.yaml#falsifiers`), each asserting that the
  reference implementation **rejects** the described failure scenario

All tests run fully offline: no socket, subprocess, filesystem write, or
network call outside reading the two data files in this directory.

## Compatibility note against existing L0/S0/S1 evidence

This candidate does **not** claim that nothing was previously implemented.
The correct statement, and the one this note makes:

- **S0** (private NATS ping/pong roundtrip, Liv-i-Hagen <-> EDGE-01,
  LEDGER row `EDGE-S0-LIV-EDGE-PINGPONG-B6E9D2-RECEIPT-001`, admitted at
  PM8571) and **S1** (pointer -> authenticated Drive reread -> ACK_READ,
  LEDGER row `EDGE-S1-X1-2B84D7E1-ACKREAD-RECEIPT-002`) are prior,
  independent, **behavior-real** implementation evidence, produced by a
  separate, non-canonical candidate (the ephemeral, locally-scoped
  nats-py / JWT-NKey client referenced at PM8448-8452, C854/C855).
- What was missing, and what this candidate supplies, is not "any
  implementation" -- it is the **canonical, schema-validated, Source-repo
  reference** for the message envelope, subject registry, and receipt
  state machine that S0/S1-class transport work can be checked against
  going forward.
- This candidate does not re-run, re-prove, or supersede S0/S1. It has no
  dependency on them and they have no dependency on it. Reconciling
  S0/S1's transport-level evidence against this candidate's envelope/
  registry/receipt model (e.g. does the nats-py client's message shape
  conform to `cerebro-message-v1-candidate.schema.json`?) is explicitly
  **out of scope** for this PR and listed as an open deviation below.
- Per LEDGER181's own disposition note: that evidence has not yet been
  currentized back into `PM_PRINCIPAL_CHANNEL` (PM8628 still reads
  NO_GO). This candidate does not attempt to resolve that cross-surface
  inconsistency -- it is a Source-repo-only deliverable and takes no
  position on PM_PRINCIPAL_CHANNEL state.

## Open deviations and assumptions

1. **Hash verification not independently performed.** The upstream
   handoff document pins exact byte-size/SHA256 values for each source
   section. This candidate was built from a Markdown-rendered extraction
   of that document (tool-mediated conversion, which is known to alter
   literal byte sequences such as escaped underscores and brackets), not
   from the raw file bytes. Field-for-field content was preserved by
   direct transcription, but byte-exact equality against the pinned
   hashes has not been checked and should not be assumed.
2. **Receipt-taxonomy state-machine encoding is new work, not a
   transcription.** The stage list and both rule lists (`negative_rules`,
   `falsifiers`) are sourced verbatim. The executable state machine
   (`ReceiptTrail`, `STAGE_PREDECESSORS`) and the twelve falsifier test
   functions are new code written for this candidate -- they did not
   exist as executable logic anywhere in Source prior to this branch.
   Anyone reviewing this PR should treat the enforcement logic as a
   proposal to critique, not a transcription to trust by default.
3. **No jsonschema library dependency.** `validate_envelope()` hand-rolls
   the checks in `cerebro-message-v1-candidate.schema.json` rather than
   depending on an external JSON Schema validator, matching the
   no-third-party-dependency pattern observed elsewhere in
   `tooling/validator/`. It has not been cross-checked against a real
   JSON Schema Draft 2020-12 validator; a residual risk exists that a
   subtle case (e.g. an edge in the `allOf`/`if`/`then` conditionals) is
   not identically enforced.
4. **Falsifiers 3, 6, 7, 9, 10, 12 are simulated, not transport-integrated.**
   They exercise the reference logic against synthetic in-process
   scenarios (a fabricated stale snapshot, a fabricated hash mismatch,
   etc.), not live Signalvev/NATS behavior -- consistent with this PR's
   boundary (no NATS/WireGuard/tunnel operations). Live-transport
   falsification is a distinct, later verification step.
5. **`candidates/` as a new top-level directory is this candidate's own
   proposal**, chosen because an existing `-candidate.schema.json`
   filename convention was found in `engines/presentation/`, but no
   existing top-level directory for isolated, non-integrated reference
   work was found in the repository. PRINCIPAL may prefer a different
   placement; nothing about the schema/registry/state-machine content
   depends on this specific path.
6. **README/validator rule-count drift (421 vs 422) is intentionally
   NOT included here**, per instruction -- it is scoped to a separate,
   small follow-up PR.

## Explicitly not claimed

- Not production-ready.
- Not authoritative.
- Not a runtime activation of anything.
- Not a statement that PM_PRINCIPAL_CHANNEL, LEDGER, or any Living
  document has been updated -- none have been, and none are touched by
  this PR.
