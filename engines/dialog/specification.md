# Dialog Engine

## Status
Candidate v0.2.0

## Kilde
Migrert fra MASTER_v1.1 kapittel 3.1–3.5, 3.7 og 3.9.

## Formål
Dialog Engine regulerer valg av arbeidsform og dialogflyt. Den vurderer behov for struktur, velger arbeidsmodus, aktiverer relevante Engines, angir dialogtilstand og håndterer avklaringspunkt og kontrollstopp.

## Ansvarsgrense
Dialog Engine forvalter ikke aktiv kontekst og presentasjonsregler direkte.

- Kontekststyring tilhører Context Engine.
- Format, detaljnivå og visuelle virkemidler tilhører Presentation Engine.
- Roller og langsiktig samarbeidsform tilhører Collaboration Engine.
- Prosjektstruktur tilhører Project Engine.

## Arbeidsmoduser
- STANDARD
- SAMARBEID
- PROSJEKT

Arbeidsmodus beskriver arbeidsnivå. Dialogtilstand beskriver arbeidsfase.

## Dialogtilstander
- informasjon
- analyse
- anbefaling
- avklaring
- arbeid
- verifisering
- ferdigstilling

## Migreringsmerknad
Forklarende tekst er bevart i denne spesifikasjonen. Bindende innhold er normalisert til atomiske regler i `rules.yaml`.
