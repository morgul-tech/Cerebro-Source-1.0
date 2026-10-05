# controlled-effect-executor-v0.1 — CONTROLLED_EFFECT_EXECUTOR_V01

**Status:** isolert implementasjonskandidat, lokal og default-off. **authority: NONE.** Ikke deployet, ingen kontakt med Railway eller noen live provider, ingen credentials, ingen ny tjeneste/daemon/store/migrasjon, ingen produksjonsklarhetspåstand. Bestilling: `EXT-CLAUDE-CONTROLLED-EFFECT-EXECUTOR-V01` rev 1.0. Claude = `CLAUDE_EXTERNAL_SPECIALIST`; resultatet er et produsentbidrag, ikke Cerebro-sannhet. Adopsjon, Source-publisering og enhver live-effekt har egne gater som denne leveransen ikke gir.

## Hva dette er

Den minste lokale mekanismen som gjør at en provider-effekt **ikke kan starte bare fordi et credential fortsatt virker**:

```
OWNER DELEGATION -> ONE-USE ADMISSION FENCE -> CONTROLLED EXECUTOR -> PROVIDER RESULT/UNKNOWN -> AUTHORITATIVE READBACK
```

- **BatchSpec**: kanonisk, deterministisk identitet for én eksakt batch (ordnet operasjonsliste, mål, forutsatt målversjon, godkjent artefaktversjon, aktør + generasjon, delegasjon + revisjon + utløp, Human-approval-referanse, idempotency-nøkkel, valgfri rollback-id). `digest` = SHA-256 over kanonisk JSON (samme konvensjon som `tooling/owner_state` og `mcp/control_owner_effect_receipt`). Ingen profil-hash er en batch-identitet.
- **Atomisk ett-bruks admission fence** (`InMemoryAdmissionStore.admit`): leser owner-state og setter inn admission i *én* eier-linearisert seksjon. En currentness-sjekk ved innsetting finnes som tripwire for eiere uten eksklusjon; den er check-then-act og **ikke** en erstatning for seksjonen. Maks én `FENCED`-kvittering per idempotency-identitet; eksakt replay returnerer samme kvittering; avslag skaper ingen admission og bruker ikke opp nøkkelen.
- **ControlledEffectExecutor**: tar kun imot en gyldig `FENCED`-kvittering som matcher den lagrede BatchSpec, registrerer forsøket **før** provider kalles, kaller injisert adapter **maks én gang** (grant er engangs; mocken er idempotent per korrelasjon; en produksjonsleverandør må selv håndheve idempotens på `correlation_ref`), og avgjør alt via skrivebeskyttet autoritativ readback. Ingen retry-policy.
- **Tilstander:** `FENCED, IN_FLIGHT, COMMITTED_READBACK, NO_COMMIT, UNKNOWN_EFFECT, DENIED` (se `ARCHITECTURE.md`).
- **Provenance:** `delegation -> batch -> admission -> attempt -> provider result -> readback` kan rekonstrueres og verifiseres (`verify_provenance`); basisen kan ikke omskrives av mutabel fremdrift.

## Liten offentlig API

```python
from controlled_effect_executor import (BatchSpec, Operation, DelegationView, TargetScopeEntry, ApprovalView,
                                        InMemoryAdmissionStore, ControlledEffectExecutor, verify_provenance)

decision = store.admit(spec, spec.digest)      # AdmissionDecision: FENCED | DENIED (+ reason_code), replayed
result   = executor.execute(decision.receipt, spec)   # ExecutionResult: innenfor denne pakken den eneste veien som driver en provider-adapter (API-grense, ikke isolasjon)
result   = executor.reconcile(decision.receipt.admission_ref)   # kun lesing; UNKNOWN_EFFECT -> COMMITTED_READBACK | NO_COMMIT | forblir UNKNOWN_EFFECT
executor.declare_in_flight_lost(ref, owner_reason_ref=...)      # owner eskalerer IN_FLIGHT -> UNKNOWN_EFFECT (ingen timeout finnes)
verify_provenance(store.provenance(ref))       # () = kjeden er intakt
```

Merk: én `ControlledEffectExecutor` per `AdmissionStore` (executoren tar store-ens skrivekapabilitet). Porter (`typing.Protocol`): `OwnerFencePort` (+ `OwnerSnapshot`), `ProviderEffectPort`, `ProviderReadbackPort`. Syntetiske fixtures ligger i `controlled_effect_executor.synthetic` (`SyntheticOwner`, `SyntheticProvider`, `FakeClock`), er merket REFERENCE-ONLY og re-eksporteres ikke fra pakkeroten. Referanse-storen **avviser** autoritetsbærende refs som ikke starter med `SYNTH-`.

## Bevisgrense (proof ceiling) — les dette

Beståtte tester beviser **kun lokal kandidatsemantikk** mot syntetiske fixtures. De beviser **ikke** produksjonsautoritet, provider-custody, credential-isolasjon, deploy-sikkerhet eller live revokering.

- **T12 / bypass:** pakkens offentlige API eksponerer ingen provider-mutasjon som omgår executoren; adapteren nekter alt som ikke er en executor-preget `ExecutionGrant`. Dette er en **API/candidate boundary, not production credential isolation proof**: i én Python-prosess kan den som importerer private moduler forfalske en grant, og den som holder et ekte provider-credential kan fortsatt kalle provider direkte.
- **Fence ≠ stopp av revokering etter admission.** Som bestillingen sier (`admission-before-revoke may permit at most the exact already-admitted frozen batch`) kan en `FENCED`, ennå ikke startet batch kjøres etter en senere revokering. Fencen gjør racen *deterministisk og begrenset til den eksakte admitterte batchen*; den fjerner den ikke. En ekte stans etter admission krever provider-side fencing/credential-revokering (ubevist her).
- **NO_COMMIT** er bare så sterkt som providerens readback (komplett anvendt-historikk uten forsøkets korrelasjon). En forsinket provider-commit *etter* en NO_COMMIT-readback er ikke utelukket av dette uten provider-side fencing-token (se `ARCHITECTURE.md`, første ubevist kant).
- Atomisiteten er bevist mot en in-memory eier med én lås (selve atomisiteten kommer fra låsen; currentness-sjekken er kun tripwire). En produksjonseier må levere samme egenskap fra sin egen store (f.eks. én transaksjon med radlås på delegasjonsraden): **det er ikke levert og ikke bevist her.**
- In-memory = ingen persistens; en omstart mister ledgeren.
- **Ledger-skriving** krever en engangs-kapabilitet som kun executoren tar (én executor per store). Det er en API-grense i prosessen, ikke isolasjon. `verify_provenance` beviser at ledgeren er *konsistent* (hash-kjede, tillatte kanter, at en løst tilstand er utledet av klassifikasjonen, at NO_COMMIT ikke følger en ack), ikke at en readback er *ekte*: kandidaten har ingen signaturer.
- **Human approval er en gjenbrukbar bundet grense, ikke et engangstoken.** «Ett-bruks» gjelder per idempotency-identitet, og den velges av kalleren: samme approval kan gi flere admissions med nye nøkler. Om en approval skal kunne forbrukes én gang er en owner-beslutning (ikke tatt her).
- `declare_in_flight_lost` nekter kun mens *denne* executoren fortsatt er inne i kallet. Erklærer en owner et pågående kall fra en annen prosess som tapt, og readback så viser «ingen commit», kan en sen provider-commit gjøre NO_COMMIT feil (samme hull som over).

## Tester

Fra repo-rot. `selftest` er ren stdlib (ingen nettverk, ingen prosess, ingen filskriving). `mutation_check.py` er et utviklerverktøy: det kopierer kandidaten til en throw-away tmp-mappe og kjører suiten i en subprocess; det rører aldri repoet.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/controlled_effect_executor_v01_validation.py selftest
PYTHONDONTWRITEBYTECODE=1 python3 candidates/controlled-effect-executor-v0.1/tests/mutation_check.py
```

| Orakel | Hvor |
|---|---|
| T1 revoke-before-fence → DENIED, 0 kall | `test_admission.py` |
| T2 fence-before-revoke → én admission, maks ett kall | `test_admission.py`, `test_execution.py` |
| T3 eksakt replay → samme identitet, ingen ny provider-kall | `test_admission.py`, `test_execution.py` |
| T4 stale revisjon, T5 aktør/generasjon, T6 scope/mål/versjon, T7 utløpt, T8 forfalsket digest | `test_admission.py` |
| T9 respons tapt etter commit → UNKNOWN_EFFECT → COMMITTED_READBACK uten ny mutasjon | `test_execution.py` |
| T10 proven no commit → NO_COMMIT, ingen retry-policy | `test_execution.py` |
| T11 uavklart readback → UNKNOWN_EFFECT/HOLD, 0 retry | `test_execution.py` |
| T12 ingen offentlig bypass (+ ærlig grense) | `test_boundary.py` |
| T13 uforanderlig basis, T14 distinkt batch | `test_boundary.py`, `test_admission.py` |

I tillegg: racetester (revoke vs. admission, 60 gjentak; deterministisk «revoke mellom lesing og innsetting»; eier uten seksjon: revokering *før* `currentness()`-kallet fanges, en revokering rett etter kallet fanges ikke (check-then-act)), samtidighet (8 tråder → ett kall), forfalskede kvitteringer, Human-approval-binding, provenance-kjede + tampering, tilstandsmaskin-lukking for alle tilstandspar, ledger-kapabilitet, semantisk konsistens og engangs-grant, og kilde-guards (ingen nettverk/prosess/miljø/fil/scheduler/while-løkke/hemmelighetsform). `tests/mutation_check.py` muterer kildekoden og krever at testene feiler for hver mutant (51 mutanter, alle drept; én kjent ekvivalent mutant er bevisst utelatt og dokumentert i filen).

## Kilde som er konsultert / gjenbrukt

Komponert (konvensjon, ikke import): `tooling/owner_state/owner_state_persistence.py` (canonical JSON + SHA-256, `ref = PREFIX + fingerprint[:24]`, `request_fingerprint`, CAS/idempotens-semantikk), `mcp/control_owner_effect_receipt.py` (fingerprint uten ref/fingerprint-felter). Disse er PostgreSQL-/eierspesifikke og passer ikke rent som lokal delegasjons-fence, så de importeres ikke; en minimal `AdmissionStore` ligger bak en protokoll i stedet. Se `RETURN.md` for fullstendig liste.

## Eksplisitt ikke

Se `component.yaml → explicitly_not` og `ARCHITECTURE.md §7`.
