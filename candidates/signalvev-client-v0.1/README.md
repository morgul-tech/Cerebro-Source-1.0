# signalvev-client-v0.1 — SIGNALVEV_CLIENT_V01

**Status:** isolert implementasjonskandidat. **authority: NONE.** Source-tilstedeværelse gir ingen runtime-aktivering. Ikke deployet, ingen kontakt med live Cerebro-node eller EDGE-01, ingen ekte credentials, ingen produksjonsklarhetspåstand.

Bestilling: `EXT-CLAUDE-SIGNALVEV-CLIENT-V01` rev 1.0 (kroppens SHA-256 er oppgitt i `RETURN.md`). Claude = `CLAUDE_EXTERNAL_SPECIALIST`; resultatet er et produsentbidrag, ikke Cerebro-sannhet. Integrasjon, Source-publisering og feltkanari har hver sin egen gate og gis ikke av denne leveransen.

## Hva dette er

Den eksisterende Signalvev D0-koden (`candidates/signalvev-sensing-runtime-v0.1`) gjort **installerbar** på Windows/Linux, med en **ekte privat Core NATS-tilkobling** (nats-py) og en minimal forgrunns-CLI: `check-config`, `health`, `send`, `listen`.

- **send** → eksisterende `SensingSender` + durabel `SendLedger` (UNKNOWN_SEND ⇒ ingen replay) → eksisterende `CoreNatsAdapter` → ekte nats-py-tilkobling.
- **listen** → ekte abonnement på de to registrerte literal-subjektene → avgrenset kø → eksisterende `SensingReceiver` (dedupe, TTL, stale/superseded, ett eier-reread, typede utfall) → eksisterende `OwnerResolver`-port.
- Klienten legger kun til installasjons-/transportsømmen. Ingen sender-, receiver-, envelope-, cursor- eller eiersannhetslogikk er kopiert eller endret.

## Kilde og base

| | |
|---|---|
| Repo / gren | `morgul-tech/Cerebro-Source-1.0` / `main` |
| `SOURCE_BASE_AT_PREPARATION` (oppgitt) | `59aa4e72025c98edc6ec5dbbe88bb610d0ea6232` |
| Faktisk base brukt | `59aa4e72025c98edc6ec5dbbe88bb610d0ea6232` (HEAD = oppgitt base; `main` var ikke flyttet) |
| Kandidatgren (lokal, aldri pushet) | `claude/signalvev-client-v0.1` |

## Gjenbruk-kart

| Del | Status |
|---|---|
| `candidates/signalvev-sensing-runtime-v0.1/src/signalvev_sensing/*` (sender, receiver, d0, cursor, evidence, store, transport-sømmen m.m.) | **GJENBRUKT UENDRET** (byte-identisk; ingen diff i denne kandidaten; pakkes inn i wheel med SHA-256-pinning) |
| v0.1 envelope/registry/ReceiptTrail, v0.9, v0.15, v0.16 (`tooling/validator/…`, subject-registry + schema JSON) | **GJENBRUKT UENDRET**, pakket som `_reference_resources/` i samme repo-layout slik at kjernens bro (`SIGNALVEV_VALIDATOR_DIR`) og v0.1 finner dem |
| `CoreNatsAdapter` | **GJENBRUKT**: den injiserte klienten er nå `NatsPyConnection` |
| `signalvev_client/*` | **NY**: config, nats_binding, dispatch, session, cli, lock, synthetic_owner, closure_log, _bootstrap |
| `tooling/validator/signalvev_client_v01_validation.py` | **NY** tynn selftest-runner |
| `tooling/validator/test_component_inventory.py` | **MEKANISK ENDRING**: kandidat-antall 19→20 (og 20→21 i hjelpetesten), akkurat som ved forrige kandidatopptak; ingen dekning fjernet |

Ingenting er flyttet/refaktorert i den eksisterende kjernen. `_reference.py` er **ikke** endret: installert modus peker kjernens eksisterende `SIGNALVEV_VALIDATOR_DIR`-mekanisme mot de medfølgende referansefilene, etter at hver pinnet fil er verifisert (`signalvev_client/_bootstrap.py`, eneste modul som berører `os.environ`).

## Avhengigheter

- **nats-py `>=2.16.0,<3`** (`REAL_CORE_NATS_BINDING`). Valgt fordi oppdraget peker på nats-py som vanlig klientbinding, og ingen annen egnet NATS-avhengighet fantes i Source. Testet versjon: **2.16.0** (upstream-tag `v2.16.0`, commit `3547a63b2658db4596928668d75fab7c673f6fe6`). Se «Kjente grenser» for hvordan den ble installert her.
- **nkeys `>=0.2.1`** kun hvis `nats.credentials_file` brukes: `pip install "signalvev-client[credentials]"`. Uten nkeys feiler klienten tidlig med typet `NKEYS_NOT_INSTALLED` (ingen stille degradering).
- Python **≥ 3.11** (stdlib `tomllib`).

## Installasjon

Wheel: `signalvev_client-0.1.0-py3-none-any.whl` (følger med i ZIP-en; hash i `SHA256SUMS` og `RETURN.md`).

**Linux**
```bash
python3 -m venv /opt/signalvev/venv
/opt/signalvev/venv/bin/pip install signalvev_client-0.1.0-py3-none-any.whl      # + "[credentials]" ved credentials_file
/opt/signalvev/venv/bin/signalvev-client --version
```

**Windows (PowerShell)**
```powershell
py -3.11 -m venv C:\signalvev\venv
C:\signalvev\venv\Scripts\pip install signalvev_client-0.1.0-py3-none-any.whl
C:\signalvev\venv\Scripts\signalvev-client.exe --version
```

Støttet importsti er `signalvev_client` (den binder de pinnede referansene før kjernen importeres). `import signalvev_sensing` alene i en installert miljø er ikke en støttet inngang.

## Konfigurasjon (ikke-hemmelig eksempel)

`examples/node-listen.example.toml` og `examples/node-send.example.toml`. Kjerne:

```toml
[node]
id = "node-a"
[nats]
server = "nats://127.0.0.1:4222"     # privat Core NATS; ikke-loopback krever tls:// / tls_*-filer eller allow_plaintext = true
flush_timeout_seconds = 2
# credentials_file = "/etc/signalvev/node-a.creds"          # kun filsti; aldri inline
[evidence]
dir = "state-a"                       # lokal evidens (cursor/recorder/closures/sender-ledger), ikke eiersannhet
[[listen.interest]]
owner_ref = "owner:example-synthetic"
referent_type = "doc"
[resolver]
kind = "synthetic_fixture"            # KUN utvikling; produksjonsporten er kind = "factory"
fixture_file = "owner.synthetic.json"
```

Config feiler lukket: manglende/ukjente nøkler, inline hemmeligheter (`password`, `token`, `jwt`, `user`, `nkey…`, `user:pw@` i URL), plaintext mot ikke-loopback uten eksplisitt opt-in, utenfor grenser, manglende filer. Hemmeligheter logges aldri; `health`/`check-config` viser kun «configured/absent».

## Kommandoer (forgrunn; ingen daemon/tjeneste)

```bash
signalvev-client check-config --config node-a.toml                 # offline, ingen nettverk
signalvev-client health       --config node-a.toml [--connect]     # --connect = én avgrenset server-roundtrip (PING/PONG)
signalvev-client send         --config node-b.toml --event event.example.json
signalvev-client listen       --config node-a.toml [--duration 60] [--max-frames 1]
```

Windows: samme kommandoer med `C:\signalvev\venv\Scripts\signalvev-client.exe` og `--config C:\signalvev\node-a.toml`. `listen` stoppes med Ctrl+C / SIGTERM / `--duration` / `--max-frames`.

Utdata er JSON-linjer. **Exit-koder:** 0 ok · 2 bruk/config/event-feil (ingen nettverk brukt) · 3 `NOT_SENT` · 4 `UNKNOWN_SEND` (eller send pågår) · 5 connect/health feilet (inkl. at serveren ikke svarer på abonnement-roundtrip ved `listen`) · 6 listen avsluttet fordi tilkoblingen gikk tapt.

## Semantikk som er bevart (og grensene som er hevdet)

- `TRANSPORT_ACCEPTED != DELIVERED != ACK_READ != WORK_CONSUMED != EFFECT`. `ACCEPTED` fra `send` betyr kun at serveren flushet publiseringen. `health` beviser ikke mottaker-liveness. Alle `status`/`send`/`health`-svar bærer `claims` (`delivered: NOT_PROVEN`, `peer_receiver_liveness: NOT_PROVEN`, `effect: NONE_CLAIMED`, `authority: NONE`).
- **UNKNOWN_SEND / buffering / reconnect.** `send`-tilkoblingen bruker `allow_reconnect=False` og `pending_size=0`: biblioteket buffrer aldri og har ingen reconnect-vei som kan skrive en tidligere tvetydig publisering på nytt. `publish` avslår (`NotSentError`, bevist ikke skrevet) når tilkoblingen ikke er CONNECTED, og mapper bibliotekets egne før-skriving-avslag (closed/draining/buffer-limit/max-payload/bad-subject) til samme bevis. Alt annet (skrivefeil, timeout, mislykket flush) er `UNKNOWN_SEND`, og den durable ledgeren nekter en ny publisering av samme event — også etter prosess-restart. `listen`-tilkoblingen kan reconnecte (re-subscribe), kan aldri publisere.
- **Utførelsesmodell.** nats-py (asyncio) kjører i en egen event-loop-tråd per tilkobling bak en synkron fasade (`nats_binding.py`, eneste fil som importerer `nats`/`asyncio`/`ssl`). Mottatte rammer går via en avgrenset kø til én arbeidstråd som kaller den eksisterende (blokkerende) receiveren; full kø ⇒ rammen **droppes og telles** (D0 er flyktig og rekonstruerbar; aldri rapportert som levert).
- **Subjekter.** Kun de to registrerte literal-subjektene `cerebro.v1.state.delta` og `cerebro.v1.artifact.pointer`. Ingen wildcard, ingen request/reply, ingen JetStream.
- **C1** (gammelt/forsinket/omordnet hint kan ikke overstyre nyere eiersannhet) og **C2** (flyktig D0 kan gå tapt ved utfall; ingen falsk levering/liveness; ingen blind replay etter UNKNOWN_SEND; ingen utledet worker-START/effekt; ingen JetStream lagt til) har egne tester, også mot ekte nats-py over loopback.
- **Autoritet.** Transportlegitimasjon autentiserer en tilkobling; den gir ingen eier-/PM-myndighet. Klienten binder/starter ingen workers, admitterer/frigir ingen claims, tar ingen eierbeslutninger og lagrer ikke kanonisk PM-sannhet. `ClosureRecord`/evidens har `authority: NONE`, `work_consumed: false`, `effect: NONE_CLAIMED`. Den medfølgende resolveren er en **merket syntetisk fixture**, og `listen` sier det i hver oppstart.

## Tester (kommandoer)

Fra repo-rot:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_sensing_runtime_v01_validation.py selftest   # eksisterende kjerne: 125 tester
PYTHONDONTWRITEBYTECODE=1 python3 candidates/signalvev-sensing-runtime-v0.1/tests/mutation_check.py        # eksisterende: 74 mutanter
PYTHONDONTWRITEBYTECODE=1 python3 tooling/validator/signalvev_reference_vNN_validation.py selftest          # NN = 01..18
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=<mappe med nats-pakken> python3 tooling/validator/signalvev_client_v01_validation.py selftest   # klient: 82 tester (runneren melder 82/82 PASS, 1 skipped; skippen er modus-avhengig: kilde- vs. installert kjøring)
PYTHONPATH=<mappe med nats-pakken> python3 candidates/signalvev-client-v0.1/tests/mutation_check.py        # klient: 15 mutanter
python3 -m unittest tooling.validator.test_component_inventory                                              # inventar
```
Uten nats-py på stien hoppes `test_real_nats_py` over (og runneren sier det); resten kjører.

Klientens tester: `test_config` (bounded/fail-closed), `test_binding` (adapter-mapping mot fake async-klient: tilkoblingsvalg, publish/flush-utfall, rolle-/subjekthåndheving, livssyklus, kjøremodell), `test_session` (send → broker → listen → eksisterende receiver; C1/C2; lås; restart), `test_cli` (exit-koder, JSON), `test_guards` (AST: nettverk kun i bindingfilen, ingen JetStream/request/daemon/scheduler/wildcard/worker-logikk; kjerne uten nats/asyncio; dynamisk: DNS/utgående connect blokkert), `test_bootstrap` (pinning), `test_real_nats_py` (**ekte nats-py over ekte loopback-sockets mot en protokoll-STUB** i `tests/protocol_stub.py` — en testdobbel, **ikke** en broker og **ikke** `DISPOSABLE_NATS_ROUNDTRIP`).

## Bygg, installasjonsbevis og disponibel broker

```bash
python3 candidates/signalvev-client-v0.1/packaging/build_dist.py --out <dist>                     # deterministisk wheel (SOURCE_DATE_EPOCH = commit-tid)
python3 candidates/signalvev-client-v0.1/packaging/verify_install.py --wheelhouse <dir med klient- og nats-py-wheel> --work <mappe UTENFOR repoet> --expect-sha256 <wheel-hash>
python3 candidates/signalvev-client-v0.1/packaging/disposable_roundtrip.py --venv <work>/venv --nats-server <sti til nats-server>
```
`verify_install.py`: ny venv utenfor sjekkouten, `pip install --no-index --no-deps`, `python -I` fra annen cwd; beviser at alt importeres fra venv, at hver pinnet fil er byte-lik repo-kilden, at console-scriptet virker, at en tuklet kopi feiler lukket, og kjører klientsuiten mot den **installerte** koden. `build_dist.py` skriver wheel selv (stdlib; byggevertens setuptools trengs ikke). `disposable_roundtrip.py` starter en loopback-`nats-server`, kjører `listen` (node-a) og `send` (node-b) fra installasjonen, og stopper alt; uten `nats-server` skriver den `UNRUN`. Med protokoll-stubben (`packaging/stub_nats_server.py`, kun dry-run) skriver den bevisst `DRYRUN_WITH_PROTOCOL_STUB_NOT_A_BROKER`, aldri `PASS`.

## Kjente grenser / claim-tak

- `listen` bruker en ikke-daemon arbeidstråd. Henger en (utenfor-scope) eier-resolver midt i en frame, kan `stop()` gi opp etter tidsavbrudd (`worker_stopped_cleanly: false` i `LISTEN_END`), men prosessen kan da bli stående til resolveren returnerer. Observerbart, ikke løst.
- `health` skriveprobe (`evidence_dir.writable`) er en reell midlertidig fil i nærmeste eksisterende katalog, ikke `os.access`.

**Historisk produsentbevis (før uavhengig Windows-test):** `RETURN.md` rapporterte Linux-test, Windows `UNRUN` og ekte broker `UNRUN` i produsentens byggemiljø. Produsenten bygde nats-py 2.16.0 fra upstream-tag lokalt fordi PyPI var blokkert; dette beviser ingen PyPI-oppløsning. Hashen for den opprinnelige oppgavetekstens kropp er fortsatt `UNKNOWN`, ikke verifisert.

**Uavhengig Cyborg-bevis for uendret levert wheel:** `P22-SIGNALVEV-WINDOWS-VERIFY-001` (2026-10-03), kvittering SHA-256 `9ac63a06ded12a9c4d1bdf05562ed93d4dfc74109d8b99991906a4ee1cb30d22`; retur-ZIP SHA-256 `4c9525136e67ffd15cdbdac1a4e0c1c2fa47710358e7d8e69a27ecadaed4f91a`; opprinnelig wheel SHA-256 `685fbc6ceb0768699f5c5e3c271908d930b0107cce2c93e77119cf0dadba49e1`. På Windows 11 / Python 3.13.15 besto installasjon, isolerte importer og 33 pinnede ressurser; installert suite: **81 PASS + 1 forventet skip** av 82 oppdagede tester. Offisiell NATS Server 2.15.0 på loopback ga `ACCEPTED` ved send og `ACK_READ` ved listen mot `SYNTHETIC_FIXTURE_NOT_OWNER_TRUTH`; prosessene ble stoppet. Dette er transportbevis med syntetisk eier, ikke produksjonseier, privat tverrnode/EDGE, worker-/NAL-autoritet eller live effekt.

**Denne Source-kandidaten:** den isolerte lokale utsjekken bygger på GitHub-main `7321e4592eac0940a8b075c1932765a9c4d046d8`; dette er ingen publisering til main. Det historiske Cyborg-beviset gjelder fortsatt bare det opprinnelige uendrede wheelet; ny bygging og installasjon fra denne kandidaten krever egen kvittering. Credentials-fil (nkeys) er ikke prøvd, og produksjonseieradapter, bærer og live-node gjenstår.


## Eksplisitt ikke

Se `component.yaml → explicitly_not`.
