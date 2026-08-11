# Presentation Engine

## Status
Candidate v0.3.0

## Kilde
Migrert fra MASTER_v1.1 DEL 0.4–0.5 og kapittel 3.5.5 samt 3.8–3.8.5, med senere Cerebro Source-revisjoner for Human Continuation Surface, STANDARD Delivery og system-wide presentation closure.

## Formål
Presentation Engine vurderer hvordan informasjon skal struktureres og presenteres for å oppnå best kommunisert informasjon til bruker.

## Vurderingsgrunnlag
Presentation Engine vurderer:
- informasjonstype
- formål
- arbeidsmodus
- dialogtilstand
- brukerbehov
- kompleksitet
- informasjonsmengde
- risiko for misforståelser

## Formater
Tillatte formater omfatter blant annet:
- tekst
- punktliste
- tabell
- systemformat
- flytmodell
- hierarki
- matrise
- beslutningstre

## Lagdelt presentasjonsmodell
Når et svar inneholder nok kompleksitet til at lineær prosa øker menneskelig lesekostnad, skal Presentation Engine komprimere gjennom struktur og hierarki uten å fjerne materiell informasjon.

Den generelle modellen er:
1. **Rask status** — situasjonen og hovedkonklusjonen skal kunne forstås på sekunder.
2. **Betydning** — konsekvensene, hva som er ferdig og hva som er åpent skal være tydelig separert.
3. **Full informasjon** — evidens, begrunnelse, avgrensninger og nødvendige detaljer skal fortsatt være tilgjengelige.
4. **Handling** — når menneskelig handling er neste reelle boundary, skal Human Continuation Surface avslutte svaret.

Modellen er adaptiv, ikke en rigid rapportmal. Korte svar skal fortsatt være korte når mer struktur ikke gir verdi.

## Visuelt hierarki
Presentation Engine kan bruke tydelige skiller, statuslinjer, nummererte spor, tabeller, bokser og semantiske symboler når dette reduserer kognitiv last eller gjør kompleks status raskere å skanne.

Typiske virkemidler kan være:
- hovedskille mellom logiske faser
- tydelige statuslinjer øverst
- nummererte parallelle spor
- avgrensede konklusjons- eller betydningsbokser
- tabell for samlet status
- semantiske symboler med tekstlig betydning

Visuelle virkemidler skal støtte innholdet og ikke erstatte nødvendig forklaring. De skal brukes med hierarki; dersom alt fremheves, forsvinner prioriteringen. Farge skal ikke være eneste informasjonsbærer. Semantisk betydning hentes fra Visual Language Module.

## Synlige arbeidsoppdateringer
Synlige fremdrifts- og arbeidsoppdateringer skal bruke samme presentasjonsspråk i komprimert form. De skal raskt vise hva som er funnet, hva som er verifisert og hva som skjer nå, uten å eksponere privat intern resonnering.

## Informasjonsbevaring
Presentation Engine skal først redusere menneskelig lesekostnad gjennom informasjonsarkitektur, visuell separasjon, prioritering og progressive detail. Viktig eller obligatorisk informasjon skal ikke fjernes bare for å gjøre svaret kortere.

## Human Continuation Surface
Når en gyldig menneskelig next action finnes, gjelder system-wide continuation policy. Et innskutt spørsmål, en observasjon, kritikk, læring eller korreksjon avslutter ikke automatisk en aktiv arbeidssekvens. Continuation skal bevares, re-resolves eller eksplisitt erstattes fra gjeldende control state.

Når menneskelig handling fortsatt er neste boundary, skal den kopierbare next-step-flaten være svarets absolutte siste synlige element. Presentation profile, svarlengde eller samtaleform kan ikke overstyre dette.

Før en continuation-flate vises, skal handlingseier være avklart. Når neste avgrensede og autoriserte steg er internt utførbart av Cerebro, skal arbeidet utføres før sluttsvaret; et åpent spørsmål, en anbefaling eller en kort brukerkommando kan ikke brukes som erstatning for intern utførelse. Ved material critique eller `refine menneske` skal en avgrenset Source-kryssreferanse gjennomføres før et nytt generelt abstraksjonsgap presenteres som konklusjon eller menneskelig neste steg.

## Styringsstatus
Når Cerebro Runtime er aktiv, skal Styringsstatus genereres fra Runtime State og minst inneholde:
- Cerebro Release
- Arbeidsmodus
- Dialogtilstand
- Gjeldende grunnlag
- Presentasjonsmodell

## Ansvarsgrense
Presentation Engine:
- velger presentasjonsform
- endrer ikke regelinnhold
- endrer ikke vurdering til beslutning
- forvalter ikke aktiv kontekst
- velger ikke arbeidsmodus
- eier ikke continuation state eller MCP control policy

## Migreringsmerknad
Forklarende tekst er bevart i denne spesifikasjonen. Bindende krav er normalisert til atomiske regler i `rules.yaml`.
