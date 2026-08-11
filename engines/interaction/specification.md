# Interaction Engine

Interaction Engine replaces the active Dialog Engine and Collaboration Engine
as one interaction-boundary component.

It interprets what the user is currently asking, separates explicit instruction
from preference signals, identifies clarification and decision boundaries, and
maintains role/responsibility clarity.

It does not own final control resolution. Working mode, continuation, control
outcome, engine activation, execution profile, and reorientation are resolved by
MCP from the applicable Control State and Source-defined policy.

## Human-meaning refinement

When the user invokes `refine menneske`, Interaction Engine resolves the human
meaning of the applicable utterance before treating its individual words as exact
machine terms. The operation is semantic interpretation, not automatic rewriting.

The assessment accounts for approximate or imperfect terminology, speech-to-text
effects, ellipsis, incomplete grammar, metaphor, figurative language and meaning
that is distributed across the active conversation. It cautiously reconstructs
the likely underlying point and may surface a broader implication when context
supports it. It must preserve explicit constraints, distinguish supported meaning
from inference, avoid inventing intent and request a narrow clarification only
when unresolved ambiguity could materially change the result.

A human-meaning refinement remains part of the active work sequence. Unless the
user changes the objective, the refined meaning updates the work and continuation
rather than replacing progress with a detached text-editing exercise.

Legacy DIA-* and COL-* rule identifiers are preserved inside the Interaction
rule set for traceability during PATCH-005. Their active owner is Interaction.
