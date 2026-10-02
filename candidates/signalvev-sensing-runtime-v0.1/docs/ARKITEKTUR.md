# SIGNALVEV_SENSING_RUNTIME_V01 — arkitektur (authority NONE)

Kandidat, offline. Ingen live NATS, ingen credentials, ingen modellkall, ingen skriving til Drive/kanonisk tilstand.

## Kjeden (nordstjernen) og hvor hver ledd bor

```
eier-endring ─▶ owner_event.accept_owner_event      (kun COMMITTED_READBACK; identitet, revisjonsbasis, proveniens, Way Home)
            ─▶ d0.build_frame                        (lavest tilstrekkelige D0: inline ≤8 små flate felt ELLER pekere+hash; ≤1024 B)
            ─▶ sender.SensingSender + SendLedger     (write-ahead intent; UNKNOWN_SEND ⇒ NO_REPLAY; v0.1 ReceiptTrail PRODUCED→TRANSPORT_ACCEPTED)
            ─▶ transport.Transport                   (FakeTransport | CoreNatsAdapter over injisert klient; ACCEPTED≠DELIVERED≠READ)
mottaker:   ─▶ d0.decode_frame / validate_d0 / validate_envelope_for_d0   (v0.15 versjonsgate → v0.1 envelope → binding D0↔envelope)
            ─▶ applicability.InterestTable           (lokal, billig; NOT_APPLICABLE stopper her: ingen state, ingen reread, ingen wake)
            ─▶ TTL                                   (EXPIRED stopper; ingenting lagres)
            ─▶ cursor.DedupeCursor                   (event_id + registry-idempotency-scope + eier-bekreftet owner_seq-highwater ⇒ DUPLICATE / CONFLICT_HOLD / STALE_SUPERSEDED)
            ─▶ resolver.OwnerResolver                (NØYAKTIG ett kall for APPLIES; inline ⇒ REVISION_CHECK, peker ⇒ POINTER_GROUND)
            ─▶ receiver._judge                       (v0.16 truth-guard for inline; ACK_READ/STALE/CONFLICT/HOLD_*)
            ─▶ activation.decide_activation          (DETERMINISTIC_DISPOSITION_COMPLETE | JUDGMENT_REQUIRED — aldri et valgt svar)
            ─▶ return_sink.ClosureRecord ─▶ ReturnSink (valgt materiell retur; Way Home; work_consumed=False, effect=NONE_CLAIMED)
evidens:       cursor / FlightRecorder / SendLedger     (JSONL, kun id/hash/kode; nekter fri tekst; ikke kanonisk)
```

## Rekkefølgeregel
Billigst først, og alt før resolveren er lokalt. Et signal lagres i cursoren først når det er verifisert (skjema/identitet),
relevant og levende (TTL). Ugyldige/utløpte/irrelevante rammer kan dermed ikke forgifte dedupe for den ekte hendelsen.
Merk: «verifisert» betyr her skjema/identitet/TTL/relevans — IKKE at avsenderen er autentisert (se KNOWN_UNKNOWNS i RETURN).

## Samtidighet og feilsikkerhet
- Låsen (RLock) dekker cursor/recorder/sink og gjør *admit* (dedupe-sjekk + claim) atomisk. Den holdes IKKE under eier-kallet: NOT_APPLICABLE og DUPLICATE
  svares mens en eier-reread pågår. Resolveren eier sin egen deadline (ingen timeout håndheves her).
- `on_frame` kaster aldri: fiendtlige rammer (feil typer, lone surrogates, dyp nesting, NaN/Infinity, duplikate JSON-nøkler, enorme heltall) blir typet
  HOLD_SCHEMA/HOLD_IDENTITY uten reread og uten lagret state. Et ubrukelig resolver-svar etter claim blir HOLD_UNREADABLE (aldri en hengende PENDING).
- Highwater flyttes KUN av en ACK_READ der eier-resolveren også har bekreftet `owner_seq` (`ResolverResult.owner_seq == event.owner_seq`), og går aldri ned (også ved ute-av-orden-fullføring). Et uverifisert signal, eller en forfalsket seq på en ekte revisjon, kan dermed ikke avgjøre hva som er stale. Rapporterer eieren en annen seq ved samme revisjon ⇒ CONFLICT_HOLD (OWNER_SEQ_DIFFERS_AT_SAME_REVISION); rapporterer den ingen seq ⇒ den lokale stale-snarveien er inert og eieren dømmer hver gang. Et eier-avvist claim (CONFLICT_HOLD/HOLD_IDENTITY) slipper idempotency-scopet, så det ikke holder den ekte hendelsen som gissel.
- Sender: check+intent er én kort atomisk seksjon (låsen holdes IKKE under publish, så sends av ulike events overlapper og handlere kan re-entre `send`; samtidig send av samme event gir IN_FLIGHT); en `publish` som kaster er UNKNOWN_SEND (med mindre den beviser `NotSentError`); INTENDED uten utfall regnes som UNKNOWN_SEND.
- Crash mellom claim og finalize: ved restart blir hendelsen `HOLD_UNREADABLE/RECOVERED_PENDING_OUTCOME_UNKNOWN`; første redelivery melder holdet til eier én gang (lukking til sink), deretter DUPLICATE. Aldri ny resolver-kjøring.
- I/O-feil: claim som ikke kan skrives varig ⇒ HOLD_UNREADABLE/CURSOR_UNWRITABLE_NO_WORK_DONE uten resolver-kall (fail closed). Feilet FINAL/SURFACED/CONFLICT-skriving: minnet er autoritativt i prosessen, feilen telles (`IngressResult.evidence_error`), og etter restart blir manglende FINAL den typede RECOVERED-holden. Feilende recorder endrer aldri et utfall (teller `RECORDER_WRITE_FAILED`).
- Evidens-flommer: conflict-bevis er begrenset (4 per event, 10 000 totalt); recorder-budsjettet gjenoppbygges fra filen (begrenser filen på tvers av restarter; INGRESS_REJECT 1 000, øvrige 10 000).
- Evidens-flommer (forts.): DUPLICATE og NOT_APPLICABLE er kun tellere; INGRESS_REJECT m.fl. degraderer til teller etter 10 000 linjer per type. HOLD_SCHEMA går aldri til sink (kan forfalskes).

## Typede utfall
| Disposisjon | Når | Lagres i cursor | Returneres (materiell) |
|---|---|---|---|
| ACK_READ | eier-reread, relation SAME, sha stemmer | ja | ja |
| NOT_APPLICABLE | ingen lokal interesse | nei (kun teller) | nei |
| EXPIRED | TTL utløpt | nei | nei |
| DUPLICATE | samme event_id / idempotency-scope, samme fingerprint | (finnes) | nei |
| STALE_SUPERSEDED | owner_seq under eier-bekreftet highwater, eller eier melder SUPERSEDED | ja | ja |
| CONFLICT_HOLD | samme id/scope/seq men annen fingerprint, eller eier-innhold ≠ event ved samme revisjon | første admisjon urørt | ja (første gang) |
| HOLD_UNREADABLE | resolver utilgjengelig/feil/ukjent relasjon/for stor grounding | ja | ja |
| HOLD_IDENTITY | identitet/Way Home/kilde/referent stemmer ikke | ja (resolver-nivå) / nei (skjema-nivå) | ja |
| HOLD_APPLICABILITY | lokal policy kan ikke avgjøre | nei | ja |
| HOLD_SCHEMA | ukjent versjon, feil form, D0 ikke bundet, signal hevder effekt/autoritet | nei | nei |

ACK_READ ≠ WORK_CONSUMED ≠ EFFEKT: ClosureRecord har work_consumed=False, effect=NONE_CLAIMED, receipt_stage ∈ {DELIVERED, READ}.

## Revisjoner er ugjennomsiktige
Runtime sorterer aldri revisjons-id-er. Eieren dømmer forholdet (SAME / SUPERSEDED / UNKNOWN) ved reread; `owner_seq`
(eier-tildelt, monoton per referent) brukes kun som billig lokal staleness-hint. Eierens reread er alltid dommeren for like/nyere.

## Modell-aktivering (grense)
`decide_activation` er en ren funksjon (importerer kun dataclasses/typing). JUDGMENT_REQUIRED oppstår kun når (a) endringsklassen
er SEMANTIC eller (b) eier-resolveren rapporterer ≥2 kandidater. Kandidater listes sortert (nøytral rekkefølge), og beslutningen har
ikke noe felt der et svar kan legges. `wake_bound` er alltid False. En fremtidig eier binder en ekte wake.

## Evidens-/cursor-grense
Stores nekter fri tekst (kun `[A-Za-z0-9_.:@/#=|+-]`, ≤200 tegn) og lagrer ikke inline-verdier eller grounding. Bare en ufullstendig siste linje uten linjeskift
repareres; en korrupt komplett linje gir `StoreCorrupt` også når den står sist (fail-closed, ikke gjetting). CLAIM uten FINAL ved restart blir
`HOLD_UNREADABLE/RECOVERED_PENDING_OUTCOME_UNKNOWN` (ikke ny resolver-kjøring ⇒ ingen replay). Send-ledger: INTENT uten OUTCOME ⇒ UNKNOWN_SEND.
En `SensingSender` med minnebasert `SendLedger(None)` avvises før publisering; filbasert intent blir flush/fsync før transportkallet. Task1 tillater lokal evidens/cursor for gjenoppretting, men ikke et nytt sannhetslager; kobling til eierens reelle persistens er fortsatt ubundet.

## Gjenbruk (importert, ikke kopiert) — alt via `src/signalvev_sensing/_reference.py`
v0.1: `validate_envelope`, `load_registry`, `ReceiptTrail`, `STAGE_PREDECESSORS` · v0.9: `FirstBrokenEdgeRecorder` ·
v0.15: `evaluate_schema_compatibility` · v0.16: `resolve_state_delta_truth` (kun STATE_DELTA; pekerstien bruker samme kilderegel direkte fordi v0.16 feiler lukket for andre typer).
Semantikk gjenbrukt uten import: v0.7 idempotency-scope (registry: referent+revisjon / artefakt+hash), v0.8 revisjon som lokal monoton (kun som owner_seq-hint), v0.17 (transport tilgjengelig ≠ arbeid tillatt).
