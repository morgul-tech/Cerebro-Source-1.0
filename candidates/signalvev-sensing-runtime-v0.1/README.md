# signalvev-sensing-runtime-v0.1 — SIGNALVEV_SENSING_RUNTIME_V01

**Status:** isolert implementasjonskandidat, offline. **authority: NONE.** Source-tilstedeværelse gir ingen runtime-aktivering. Ikke deployet, ingen live NATS, ingen credentials,
ingen modellkall, ingen skriving til Drive/kanonisk tilstand, ingen produksjonsklarhetspåstand, ingen feltkanari.

Bestilling: «CEREBRO · SIGNALVEV SENSING RUNTIME v0.1 · BOUNDED CODE SPECIALIST TASK» (2026-10-02). Claude = `CLAUDE_EXTERNAL_SPECIALIST`;
resultatet er et bidrag, ikke Cerebro-sannhet. Merge/deploy/adopsjon er separate Cerebro-/Human-beslutninger.

- **Opprinnelig Claude-basis:** `origin/main` aacd164 (Signalvev reference v0.1–v0.18); opprinnelig lokal kandidatgren var aldri pushet.
- **Source-opptaksbasis:** `bf5a7b83fd0115c771f9995682699b40bc18dbc0` (PR31 inkludert); opptak som isolert kandidat er en egen PR og gir ingen live myndighet.
- **Arkitektur:** [docs/ARKITEKTUR.md](docs/ARKITEKTUR.md)

## Endrede filer
`candidates/signalvev-sensing-runtime-v0.1/**` (component.yaml, README, docs/ARKITEKTUR.md, src/signalvev_sensing/*.py, tests/*.py) og
`tooling/validator/signalvev_sensing_runtime_v01_validation.py` (tynn selftest-runner). Source-opptaket oppdaterer også kun kandidat-antallet i `tooling/validator/test_component_inventory.py` fra 18 til 19; stabilisert rot-tall forblir 19.

## Kjør
```bash
PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_sensing_runtime_v01_validation.py selftest
PYTHONDONTWRITEBYTECODE=1 python3 candidates/signalvev-sensing-runtime-v0.1/tests/mutation_check.py
```
Ren stdlib, Python ≥3.11. Pakken trenger repoet (importerer v0.1/v0.9/v0.15/v0.16 fra `tooling/validator/`); `SIGNALVEV_VALIDATOR_DIR` kan peke annet sted.

## Kilder
| Kilde | Brukt til |
|---|---|
| `signalvev_reference_v01_validation.py` | envelope (uendret), subject registry, ReceiptTrail (sender-side), stadievokabular |
| `…v09_validation.py` | `FirstBrokenEdgeRecorder` for «hva skjedde med event X» (kun når begge sider er synlige) |
| `…v15_validation.py` | typed HOLD for ukjent versjon, ingen implisitt oppgradering (envelope og D0) |
| `…v16_validation.py` | EVENT ≠ STATE_TRUTH: sannhet kun fra eier-reread med riktig `source_ref` |
| Subject Registry v0.1 | `cerebro.v1.state.delta` (inline) og `cerebro.v1.artifact.pointer` (peker), idempotency-scopes |

## Tester
125 stdlib-`unittest` etter X1s interne reparasjoner: de 13 påkrevde falsifiers (F01–F13 + restart-/crash-varianter), skjema-/identitets-/dedupe-herding, resolver-dommer,
lukking/retur, evidensgrense, cursor-/store-restart (avkuttet hale; korrupt komplett linje feiler lukket), CoreNatsAdapter-mapping over injisert klient,
kant-rekonstruksjon, fiendtlige rammer/typer, samtidighet (duplikat under pågående reread, ute-av-orden-fullføring), highwater-forgiftning,
samt statiske (AST) og dynamiske (socket/subprocess blokkert) guards. Mutasjonssjekk: 74 mutanter, alle drept (ett bevisst ekvivalent
mutant-par for lagdelt forsvar er ikke talt med). To uavhengige adversarial review-runder (runde 1: 3 HIGH/4 MEDIUM/4 LOW; runde 2: 1 HIGH/2 MEDIUM/2 LOW) er innarbeidet med egne regresjonstester; runde-2-rettelsene er ikke gjenstand for en tredje uavhengig runde. Se RETURN.

## Kompatibilitet
Ingen eksisterende kandidat/validator endret; selftest v0.1–v0.18 kjørt uendret grønn. Ingen nye avhengigheter.

## Åpne avvik / merknader
- **Overlapp (flagget til X4/X1):** min tidligere `SIGNALVEV_FAST_TRANSPORT_SHADOW_V0_1` (ikke på `main`) overlapper delvis (receiver relevans→dedupe→reread). Brukt kun som `AMBIENT_CONTEXT_USED`; ingenting importert eller vendoret. Bør konsolideres av eier hvis begge skal leve.
- Kandidat-eget vokabular som ikke finnes i Source-referansen: D0 (`sensing.d0/v0.1`, ramme `sensing.frame/v0.1`), `change_class` MECHANICAL/SEMANTIC, ResolverResult-formen, ClosureRecord og utvalgsregelen for «materiell retur». Dette er forslag for X4/X1 å akseptere eller endre.
- Mottaker-siden bruker ikke en `ReceiptTrail`-instans (den kan ikke ærlig emittere PRODUCED/TRANSPORT_ACCEPTED); stadier valideres mot v0.1-vokabularet og stopper ved READ.
- **X1 intern reparasjon:** avsenderen krever filbasert send-ledger før den kan publisere. En komplett JSONL-linje med feil checksum avvises også når den står sist; bare en ufullstendig hale uten linjeskift kan kuttes. Task1 tillater lokal evidens/cursor for gjenoppretting, men ikke et nytt sannhetslager. Koblingen til eierens faktiske persistens og retur er fortsatt ubundet.

## Eksplisitt ikke
Se `component.yaml → explicitly_not`.
