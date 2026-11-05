# Protected Principal source and ordinary PM operation — isolated successor r1

Owner selection: P24 7c37504da0b7d761f10e1253f0d6a40ff697993a1c82b6220768194a42db65f8. This document describes an UNACTIVATED, locally qualified UNPUBLISHED source candidate (manifest4c2883bcbc927f07d20bbbed53079011fe456d7a19c0334b8c347f89de6222f7). This release publishes documentation only, not the three new runtime-authority source files. Obtain exact owner-held material through current PM/Source owner if legitimately selected; no installed operative port is asserted. It changes no installed2872 or closed0afe artifact. It does not appoint a real successor or commission a signing root. All signing keys in the qualification are random ephemeral fixtures, never persisted or exported.

## Maintained protected factory

The existing Context/root custodian supplies `PrincipalAuthorityFactory` at its trusted composition root. Workers and intake JSON cannot commission it or choose its global spend database. There is one stable service-scoped custody directory, retained across rotations and intake folders. Never create a new store to recover a spent or uncertain operation. A different directory, copied backup or service replacement is not a replay escape; actual migration must preserve the authoritative epoch and complete operation history and needs its rightful installation disposition.

The explicit production provisioning consumer is `PrincipalAuthorityFactory.provision(absolute_custody_directory)`. It requires Linux root, root-owned ordinary non-symlink path components without group/other write, a private0700 custody directory, private0600 single-link `authority-config.json`, and an absent database. Its exact config fields are schema=`cerebro-protected-principal-source/v1`, `human_public_key` (Ed25519 public32bytes, hex), `tenant_ref`, `workspace_ref`, exact target `{project,environment,service,deployment}`, and `backend_sha256`. The custodian must commission this public trust anchor through actual verified Human appointment/change custody; repository text and public office history do not install it. The port generates no signing key and stores no Human/Principal signing secret. `cryptography` Ed25519 support is an explicit deployment dependency; no package is installed by this code.

Provisioning creates only the schema, with no Principal assignment or accepted operation. Normal service composition reads the same store with `PrincipalAuthorityFactory.from_protected_context(directory)`; absent/unprotected/uncommissioned storage fails closed. The directory/config come from the protected service composition, never the operation directory or worker arguments. The root verification key remains Human-controlled; a later Human root-key change is a separately verified commissioning/migration action, not a worker setting.

## Actual appointment and revocation consumer

`factory.register_human_appointment(signed_bytes, retained_decision_bytes)` verifies an Ed25519 Human signature over canonical payload JSON. The envelope is exactly `{payload,signature}`. Appointment fields are exactly those in `APPOINTMENT_FIELDS`: schema=`cerebro-human-principal-appointment/v1`; holder reference/generation/public verification key; exact next epoch and prior epoch; active boolean; tenant/workspace/target/backend scope matching the commissioned factory; validity; and SHA256 of the retained exact Human decision bytes. The original signed appointment and retained decision are durably preserved. Holder labels confer no authority. Only genuine Human signature and protected epoch insertion admit a holder. Replacement/revocation requires the next Human-signed epoch; old signed originals remain history. A signed active=false epoch revokes the office and fails later operations. No code edits or Pxx regex are used for successive holders.

## Separate external PM-operation acceptance producer

The currently appointed holder's own trusted authorization origin supplies an Ed25519-signed per-action payload. The service owns only its PUBLIC verifier. `factory.accept_operation(signed_bytes, owner_original_bytes=raw_original, raw_plan_bytes=raw_plan)` verifies current eligible holder/key/epoch and validity under the same store lock, exact schema/nonce/action, tenant/workspace, actual target/backend, exact raw original and plan SHA256, canonical plan and beforehead SHA256, candidate generation, lower operator and adapter byte hashes, operation ID and finite interval. STAGE and TRANSFER have separate signatures, IDs/nonces and acceptance records. The exact signed acceptance, original and raw plan are persisted before execution. Duplicate operation ID or nonce refuses, including after restart.

This is real external owner acceptance in the isolated implementation, not a JSON label or a verifier callback returning True. Genuine live acceptance still requires actual commissioned Human and holder keys at their rightful trusted origins. A private local test signature is not a live office grant. No API here signs on behalf of Human or Principal.

## Concrete isolated invocation and prerequisites for any later operative entry

Only after the isolated candidate is separately adopted/published, actually installed and genuinely commissioned with its dependency and current rights qualified may the existing root custodian use its already qualified runtime/migration connection factories to create the real existing `BackendWitness` with its actual expected backend and own nonce. No credential/environment lookup fallback is added. The exact invocation is:

```python
factory = PrincipalAuthorityFactory.from_protected_context(COMMISSIONED_CONTEXT_CUSTODY)
# A genuine signed acceptance has already been registered by the protected owner port.
witness = BackendWitness(runtime_factory=EXISTING_RUNTIME_FACTORY,
                         migration_factory=EXISTING_MIGRATION_FACTORY,
                         expected_backend_sha256=ACTUAL_BACKEND_SHA256,
                         nonce=OWN_EXPECTED_NONCE)
try:
    result = execute_intake(ROOT_PRIVATE_OPERATION_DIRECTORY,
                            EXACT_INTAKE_MANIFEST_SHA256,
                            witness=witness,
                            authority_factory=factory,
                            accepted_operation_id=ALREADY_ACCEPTED_OPERATION_ID)
finally:
    witness.close()
```

These uppercase values are actual protected composition/owner inputs, not defaults or values inferred from a worker. `execute_intake` retains the existing root `/run/cerebro-pm-warm-*` exact four-file intake: intake-manifest.json pins owner-original.json, candidate-readback.json and owner-plans.json. It checks exact issuer-independent warm original/schema, dynamic outgoing, all five caller values, native birth and source pins, actual service target/backend, current candidate phase and exact accepted operation. The warm constructor and lower consumer share one concrete `AcceptedPmOperation` loaded from the commissioned factory; a worker cannot substitute its issuer label for that object.

The lower wrapper durably claims that accepted operation globally BEFORE any backend action. It then holds the same authoritative SQLite epoch write lock across backend entry, precommit authority recheck, actual SQL commit and independent readback. Human epoch changes use the same lock and cannot interleave. Every check revalidates the signed acceptance against current epoch/time and calls the actual `BackendWitness`. Failure or unknown outcome leaves the durable claim spent/NO_RETRY even if the intake directory is changed. Successful result is recorded in that same global store. The legacy installed P24 call shape remains its separately scoped existing branch; ordinary non-P24 calls require the concrete accepted authority. The bounded2872 generation cannot use the new ordinary branch.

## Isolated qualification and remaining boundaries

`qualify_authority.py` exercises actual Ed25519 verification, retained decision/rawplan binding, actual SQLite registration/acceptance/global spend/restart/epoch locks, both real consumer classes, two successive fictitious holders, unknown/stale/revoked/signature/target/expiry/refusal/resume cases and legacyP24 callback-branch compatibility. SQL/backend/witness, time and birth/role domain state are explicit doubles/in-memory fixtures. No real native tool, provider, claim, database, live credential or current transfer runs. The legacy compatibility fixture's callback is explicitly simulated; it is not the new ordinary authority producer.

Windows test uses `isolated_test_custody` with an explicit test-only mark; the lower consumer requires `isolated_test=True`, and production `execute_intake` refuses such a factory. This mode does not qualify POSIX root/modes. D1 may place this fixture home under its existing current-user+SYSTEM NTFS protection, but that is still local test custody. Actual Linux provisioning, genuine root commissioning, authoritative deployment wiring, production Ed25519 dependency, real backend/locks and current rights remain owned installation/acceptance prerequisites. No actual factory is wired into deployment/app.py by this package.

The earlier missing-producer finding remains true for the old inspected runtime. The new isolated producer and both consuming successors have A1 local30-case evidence and D1 independent19-case evidence; E2 scoped review d1e1d5c5056739f79411cd3e646c585767b7cdd6d4eca3768d5e33124f4f72b5 found no concrete source defect. Exact current-user+SYSTEM child custody during the D1 Windows fixture was NOT PROVEN: child files had token-default Administrators/OWNER RIGHTS, with tightening only afterward. Retain that qualification ceiling; no retroactive custody PASS. Crypto/register/closure evidence is separate from Linux/root custody, production commissioning/wiring/dependency/backend/current rights and actual ordinary first-reader/use. No publication-as-installation or localPASS-as-liveauthority claim.


Current isolated successor: manifest87d595c2b331ff526b55b2202fed88b15725bf8d045f03fe7a7c41d37298786c changes only the ordinary lower producer-fit helper. Exact code-pinned legacy4ca14 retains validated five fields; currenta4717 requires validated six-field method projection, with all core bindings preserved. E2 aa3d1e review found no source issue. D1 actual first-reader course98c95aa1 continued only unfinished TRANSFER from its STAGE checkpoint with an explicitly simulated authority/backend/SQL premise after loss of ephemeral fixture keys; it is component proof, not a new genuine signature or global-spend proof. Earlier genuine crypto/register proofs and the original r1 custody gap retain their own scope. Both source candidates remain UNPUBLISHED/UNACTIVATED.
