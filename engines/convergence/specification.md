# Convergence Engine — Architecture Lock 0.1

## Why it exists

Cerebro already has macro-level project structure and micro-level execution, validation, failure learning and delivery controls. Repeated P002 probe/retry friction exposed a missing middle layer: no single owner organized a material multi-step objective into coherent work families, batched checks, dependency-aware retries and whole-candidate convergence.

The Convergence Engine owns that middle layer. It does not replace MCP, Quality, Project, Context, Change tooling or Temporaris.

## Human model

Think of Project as the map of the journey, MCP as the authority to travel, Quality as the acceptance standard, capabilities as the vehicles, and Temporaris as the safe test ground. Convergence decides which roads belong in the same leg, which checks should happen together, what must be repeated after a change, and when the whole route is actually coherent.

## Normal family loop

1. Form the broadest safe work family.
2. Apply known prevention before execution.
3. Run all relevant non-hard-stop checks.
4. Collect the full knowable defect set.
5. Cluster common causes.
6. Remediate the set.
7. Rerun the entire family.
8. Mark dependent prior PASS states STALE when their basis changed.
9. Resume from the earliest affected family.
10. Run a final cross-family sweep before declaring CONVERGED.

## Foundation rule learned here

A verified foundation should be trusted, but not protected from evidence. If recurring friction persists even though the relevant rules already exist, Cerebro must challenge activation, ownership and structure before merely adding more policy. Existing structure and new structure compete on equal criteria. The correct answer is the smallest sufficient solution at the correct structural level.

## Temporaris and future local execution

Temporaris remains the candidate environment; Convergence is the orchestration owner. When a local runtime is available, deterministic and previously learned retry loops should happen locally: build candidate, run family, collect defects, remediate known families, revalidate affected dependencies, and repeat until converged or genuinely blocked.

This is intended to reduce the user's execution round-trips and waiting. Machine compute may increase because validation is broader, but the human should no longer be the transport mechanism between predictable attempts. Chat or another higher-level reasoning surface should receive compact evidence only when a novel problem or real decision boundary remains.

## Activation boundary

This lock defines the architecture and Source ownership. It does **not** claim that an executable Convergence Engine is already wired into the normal runtime path. Executable binding, deterministic validation, MCP/Quality integration and local Temporaris iteration remain implementation work.
