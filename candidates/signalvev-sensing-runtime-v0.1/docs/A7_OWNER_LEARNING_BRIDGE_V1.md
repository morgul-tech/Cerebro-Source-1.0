# A7-01 · owner receipt → durable learning bridge v1

**Status:** isolert Source-kandidat. \`authority: NONE\`. Ingen live provider, database, credentials, deploy eller canary.

## Scope

Denne slicen implementerer bare A7-consumer-siden fra P22 Buildday:

1. constructor-bound trusted owner commit receipt-reader,
2. verifisert receipt → eksisterende \`OwnerEvent\`,
3. lokal applicability/relevance,
4. Context-eid, deduplisert learning append + fresh readback,
5. varig pending encounter-outbox som overlever restart,
6. eksplisitt senere encounter-dispatch med idempotency key.

X4/NAL eier producer-siden av den delte owner-committed event-grensen. Denne kandidaten endrer ingen producer-fil.

## Authority boundary

\`COMMITTED_READBACK\` er bare et felt i eksisterende \`OwnerEvent\`-formen. Det gir ingen autentisering. A7-bridgen aksepterer eventet bare via \`TrustedOwnerCommitReader.read_verified(...)\`, krever eksakt owner, verified + readback_verified, receipt fingerprint/provider revision og bygger commit-feltet selv fra den verifiserte kvitteringen.

Et caller-supplied event som inneholder sitt eget \`commit\`-objekt avvises.

## Learning identity

Learning key bindes til:

- owner,
- event_id,
- owner revision_after,
- classifier revision.

Semantic payload binder referent, expected hash, outcome, origin, evidence refs og Way Home.

- samme event+basis+semantic payload → samme learning record, ingen andreffekt,
- samme event+basis+endret semantic payload → \`A7_LEARNING_CONFLICT\`,
- equivalent owner reread/receipt kan returnere den eksisterende learning-recorden uten å lage en ny.

Receipt/provider-evidensen lagres på første commit, men er ikke en del av semantic payload-fingerprint. Dermed gjør ikke en ny equivalent provider receipt én faktisk hendelse til flere læringer.

## Durability / outbox

\`SqliteContextLearningSink\` er **kun en lokal disposable proof-adapter**. Den modellerer Context-owned contract med to tabeller i én lokal SQLite-transaksjon:

- \`learning_record\`,
- \`pending_encounter\`.

Learning + pending encounter-intent committes atomisk. En restart etter learning commit men før senere encounter beholder pending-obligasjonen. \`dispatch_pending()\` er eksplisitt og oppretter ingen watcher eller global polling. Pending slettes først etter consumer ACK.

Dette beviser ikke produksjons-Context-DB, server custody eller live currentness.

## Frozen falsifiers

- unverified receipt → DENY, zero learning,
- wrong owner → DENY, zero learning,
- raw/self-attested \`COMMITTED_READBACK\` → DENY,
- same event+basis twice → one learning / one pending,
- changed semantic payload under same event+basis → conflict,
- irrelevant event → NOT_APPLICABLE, zero learning/pending,
- crash/restart after learning → pending preserved,
- failed later encounter → pending preserved,
- positive/negative/unknown and HUMAN origin survive durable readback/encounter.

## Validation

Existing candidate validator discovers \`tests/test_a7_learning_bridge.py\` automatically because it runs all \`test_*.py\` under the signalvev sensing candidate.

A4/C919 must verify the exact pinned branch head independently. Branch presence or local candidate PASS is not Source publication, deployment or ryggmargsfesting.
