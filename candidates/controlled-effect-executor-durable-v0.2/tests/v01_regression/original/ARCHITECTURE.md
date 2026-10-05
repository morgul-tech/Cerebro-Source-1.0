# ARCHITECTURE — controlled-effect-executor-v0.1

Isolert kandidat, `authority: NONE`. Lokal, default-off, leverandørnøytral. Bevisgrensen er: **API/candidate boundary, not production credential isolation proof.**

## 1. Årsakskjeden som lukkes (og det som ikke lukkes)

| Problem i bestillingen | Hva kandidaten gjør | Hva som IKKE er lukket |
|---|---|---|
| Credential kan fortsatt kalles etter revokering | Provider-adapteren godtar bare en `ExecutionGrant` preget av executoren; executoren starter bare fra en `FENCED` admission som matcher den lagrede BatchSpec | Et ekte credential utenfor denne prosessen kan kalle provider direkte (credential-isolasjon er utenfor scope og ubevist) |
| Fersk lesing før mutasjon er ikke nok (revokering kan komme etter lesingen) | Lesing og admission-innsetting skjer i én eier-linearisert seksjon (+ en currentness-sjekk ved innsetting som tripwire); revokering ordnes strengt før eller etter fencen | En revokering etter admission stanser ikke den admitterte batchen (bestillingen tillater at «den eksakte admitterte batchen» fortsetter) |
| Tapt respons ⇒ UNKNOWN | Alle feil etter forsøksstart ⇒ `UNKNOWN_EFFECT`; kun readback kan løse; ingen vei tilbake til provider-kall | NO_COMMIT kan være feil hvis provider committer *etter* readback (krever provider-side fencing-token) |

## 2. Dataflyt

```
Owner (SyntheticOwner / produksjonseier)       AdmissionStore (reference)                ControlledEffectExecutor      Provider (mock)
  DelegationView, ApprovalView  --atomic_read-->  admit(spec, presented_digest)
                                                    1 digest == spec.digest ?   (ellers DENIED)
                                                    2 SYNTH-refs?               (referanse-guard)
                                                    3 idempotency: replay | konflikt | ny
                                                    4 sjekkrekkefølge (se §4) + currentness CAS
                                                    5 AdmissionReceipt(FENCED) + ledger-event #1
                                                                              execute(receipt, spec)
                                                                                verifiser kvittering = lagret admission
                                                                                utløp ikke passert
                                                                                begin_attempt (CAS FENCED->IN_FLIGHT) ──────────► (ledger-event #2, FØR kall)
                                                                                ExecutionGrant (kun executor kan prege) ──apply──► mutasjon / tapt respons
                                                                                feil ⇒ UNKNOWN_EFFECT ; ack ⇒ readback
                                                                              reconcile(ref)  ──read_target_state──────────────► TargetObservation (read-only)
```

## 3. Tilstandsmaskin

```
            admit (ok)            begin_attempt              provider feilet/uten respons
 (ingen) ───────────────► FENCED ─────────────► IN_FLIGHT ─────────────────────────────► UNKNOWN_EFFECT ◄──┐ (ny readback uten avklaring)
    │                                              │  ack + readback matcher                  │  │          │
    │ DENIED (resultat, ingen admission,           ▼                                          │  └──────────┘
    │  bruker ikke opp nøkkelen)             COMMITTED_READBACK ◄──── readback matcher ───────┤
                                                   (terminal)                                 └── readback beviser ingen commit ─► NO_COMMIT (terminal)
 IN_FLIGHT --owner: declare_in_flight_lost--> UNKNOWN_EFFECT
```

`states.TRANSITIONS` er den eneste kilden: `FENCED→IN_FLIGHT`; `IN_FLIGHT→{IN_FLIGHT, UNKNOWN_EFFECT, COMMITTED_READBACK}`; `UNKNOWN_EFFECT→{UNKNOWN_EFFECT, COMMITTED_READBACK, NO_COMMIT}`; terminale tilstander har ingen etterfølger. Ingen kant går tilbake til `FENCED`/`IN_FLIGHT` fra tvetydighet: **automatisk retry er ikke representerbar**. En ny forsøk er en *ny* BatchSpec med ny idempotency-nøkkel som owner må admittere på nytt.

`IN_FLIGHT` har ingen fast frist (bestillingen adopterer ingen 30-minutters grense). Når en forsøk erklæres tapt er owners beslutning (`declare_in_flight_lost`).

## 4. Admission-sjekker (første feil vinner; stabil rekkefølge)

`SPEC_INVALID` → `DIGEST_MISMATCH` → `NON_SYNTHETIC_REF_REFUSED` → *(under owner-seksjon + store-lås)* `EXACT_REPLAY` | `IDEMPOTENCY_KEY_CONFLICT` → `DELEGATION_NOT_FOUND` → `DELEGATION_REVOKED` → `DELEGATION_EXPIRED` → `STALE_DELEGATION_REVISION` | `DELEGATION_REVISION_MISMATCH` → `ACTOR_MISMATCH` → `ACTOR_GENERATION_MISMATCH` → `DELEGATION_EXPIRY_MISMATCH` → `OPERATION_OUT_OF_SCOPE` → `TARGET_OUT_OF_SCOPE` → `VERSION_OUT_OF_SCOPE` → `APPROVAL_NOT_FOUND` → `APPROVAL_NOT_ACTIVE` → `APPROVAL_BINDING_MISMATCH` → `OWNER_MOVED_DURING_ADMISSION` (CAS ved innsetting).

Executor-avslag (ingen provider-kall): `RECEIPT_FORGED_OR_MALFORMED`, `ADMISSION_NOT_FOUND`, `RECEIPT_NOT_THE_STORED_ADMISSION`, `RECEIPT_SPEC_MISMATCH`, `ADMISSION_EXPIRED_BEFORE_START`.

Tolkningsvalg (flagget i RETURN.md): en `FENCED` admission hvis i batchen bundne delegasjonsutløp har passert **avvises ved start** (`ADMISSION_EXPIRED_BEFORE_START`). Utløpet er bundet i BatchSpec og er ikke ny policy; revokering etter admission avviser derimot *ikke* (jf. bestillingen).

## 5. Readback-klassifisering (`classify_observation`, ren funksjon)

| Observasjon | Resultat |
|---|---|
| ikke `TargetObservation` / ikke autoritativ / feil mål | `INDETERMINATE` → forblir `UNKNOWN_EFFECT` |
| korrelasjonen (= admission_ref) er anvendt og artefaktversjon = godkjent | `COMMITTED` → `COMMITTED_READBACK` med original admission-/attempt-identitet |
| anvendt, men annen artefaktversjon | `INDETERMINATE` |
| ikke anvendt, historikk **ikke** komplett | `INDETERMINATE` (NO_COMMIT kan ikke påstås) |
| ikke anvendt, historikk komplett, **ingen ack mottatt** | `NO_COMMIT` (returneres til owner; ingen retry-policy) |
| ikke anvendt, historikk komplett, men provider **ackete** | `INDETERMINATE` (`ACK_CONTRADICTED_BY_READBACK`, HOLD) |

En ack er aldri bevis; `COMMITTED_READBACK` kreves også etter ack. Unntaksmeldinger fra provider persisteres aldri (kun klassenavn).

## 6. Uforanderlighet og provenance

`BatchSpec`, `AdmissionReceipt` og `LedgerEvent` er `frozen`. Fremdrift er kun en append-only, hash-lenket event-liste (første event peker på kvitteringens fingerprint). Ledger-skriving krever en engangs-kapabilitet (`issue_writer_key`, tatt av executoren; API-grense, ikke isolasjon). Hver skriveoperasjon må bære admissionens eksakte `batch_digest` (og forsøkets ref) og avvises ellers med `LedgerBasisError`; kun kantene i `TRANSITIONS` finnes. `verify_provenance` regner om alle identiteter fra bunnen og melder koder (`EVENT_FINGERPRINT:n`, `EVENT_CHAIN:n`, `EVENT_SEMANTICS:n`, `ADMISSION_RECEIPT_FINGERPRINT` m.fl.). En `RECONCILIATION` må være *utledet* av klassifikasjonen (COMMITTED ⇒ `COMMITTED_READBACK` + observasjons-digest; NO_COMMIT ⇒ digest og ingen tidligere ack; ellers `UNKNOWN_EFFECT`). Dette beviser ledger-konsistens, ikke at en readback er ekte (ingen signaturer).

## 7. Eksakte ikke-mål

Ingen Railway/live provider; ingen credentials/secrets; ingen deploy, merge, push, PR eller Source-main-endring; ingen NAL parent/child revoke-reparasjon; ingen PM-autoritet, WORKER-rolle, A7-læring, PR33-reconciliation eller Signalvev-transport; ingen universell Authority Service, scheduler, daemon, watcher, ny produksjonsstore eller migrasjon; ingen produksjonsdelegasjoner; ingen valg av Human-policy, produksjons-utløpsvarighet, in-flight-timeout eller endelig operasjonsmengde; X3-profilhasher brukes ikke som batch-hash; ingen redesign av Railway-custody eller operasjonell credential-isolasjon; ingen bred sikkerhetsreview.

## 8. Det en produksjonsadapter må levere (isolert minste grensesnitt, ikke implementert)

1. `OwnerFencePort.atomic_read` som én owner-transaksjon (radlås/serialisérbar) som også omfatter admission-innsettingen, **eller** en betinget innsetting utført atomisk av eierens egen store (f.eks. `INSERT ... WHERE currentness = :read`). Kandidatens `currentness()`-sjekk er check-then-act og dekker ikke dette.
2. Persistent, append-only ledger med de samme hash-lenkede eventene og CAS for `FENCED→IN_FLIGHT`.
3. Provider-side fencing-token (`correlation_ref`/`admission_ref` sendes allerede med) slik at en forsinket commit etter en NO_COMMIT-readback kan avvises av provider.
4. Provider-side idempotens/fencing på `correlation_ref` (mocken gjør det; en grant er engangs men kun innenfor prosessen).
5. En owner-policy for (a) avbrytelse av uonstartede `FENCED` admissions etter revokering og (b) når `IN_FLIGHT` erklæres tapt. Ingen av dem er valgt her.
