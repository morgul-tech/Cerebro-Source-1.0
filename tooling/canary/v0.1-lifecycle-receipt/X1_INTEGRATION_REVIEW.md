# X1 integration review · bounded lifecycle receipt canary v0.1

**Disposition: PASS_BOUNDED_INTEGRATION_CANDIDATE; no live adoption claim.**

Human-delivered ZIP SHA-256: `c2e1f906c3aa04e21d0da398ab7755610b464e3d3fb64492b8d5f2cacb5e0622`. X1 checked all 14 paths stay in the expected subtree. The ten Python files and `EVIDENCE_SCHEMA.md` match the existing remote Claude branch at `2801f7323b803e3ddc71db46a709171e12c99f99` byte for byte. The delivery changes `RUNBOOK.md` and `component.yaml` status text and adds `EXECUTION_RECORD.md`; X1 corrected the latter's overstrong claim about run-code identity. Commit `7744898` is not reachable in the current Source object database, so exact code identity to the reported live run remains `UNKNOWN`.

Current Source `main` at `aacd164b360ebc8cafc9cecdd3d89c582678008e` had no canary subtree. Integration therefore adds the bounded 14-file subtree, plus this review, rather than merging the divergent Claude branch. The repository already contains the imported `tooling/validator/signalvev_reference_v01_validation.py` dependency. X1 changed no Python or canary logic bytes from the delivered ZIP.

Local checks in the clean `main` worktree: offline 4/4 PASS (including expected negative falsifiers), NATS protocol loopback 1/1 PASS, end-to-end fake-server loopback 1/1 PASS. These checks do not recreate the 2026-09-29 live run.

The record preserves both reported attempts: first 0/9 with no publish; second 9/9 reported PASS. Raw producer/consumer evidence references and hashes remain `MISSING_REF`; distinct verifier provenance remains `UNKNOWN`; credential closure remains `UNKNOWN` with X3/custody owner. No code claim converts those debts into PASS. `stage` in `message_id` remains a local deviation, not a global schema mandate.

This is a draft Source candidate for distinct review. It grants no Claude authority, Source promotion, new live run, credential handling, merge, deployment or production readiness. A Living reference may cite the reported bounded run only with the typed debts and explicit provenance limits above.

Way Home: Human ZIP → PM8883 / PROSJEKTMANN:514 → this review → draft PR → X3/evidence-owner receipts → distinct Source/Living disposition.
