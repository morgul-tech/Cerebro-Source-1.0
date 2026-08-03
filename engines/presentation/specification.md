# Presentation Engine

## Status
Candidate v0.2.0

## Kilde
Migrert fra MASTER_v1.1 DEL 0.4–0.5 og kapittel 3.5.5 samt 3.8–3.8.5.

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

## Styringsstatus
Når Cerebro Runtime er aktiv, skal Styringsstatus genereres fra Runtime State og minst inneholde:
- Cerebro Release
- Arbeidsmodus
- Dialogtilstand
- Gjeldende grunnlag
- Presentasjonsmodell

## Visuelle virkemidler
Visuelle virkemidler kan brukes når de gir bedre forståelse eller oversikt. Deres semantiske betydning hentes fra Visual Language Module.

Visuelle virkemidler skal støtte innholdet og ikke erstatte nødvendig forklaring. Farge skal ikke være eneste informasjonsbærer.

## Ansvarsgrense
Presentation Engine:
- velger presentasjonsform
- endrer ikke regelinnhold
- endrer ikke vurdering til beslutning
- forvalter ikke aktiv kontekst
- velger ikke arbeidsmodus

## Migreringsmerknad
Forklarende tekst er bevart i denne spesifikasjonen. Bindende krav er normalisert til atomiske regler i `rules.yaml`.
