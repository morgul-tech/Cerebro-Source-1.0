# Runtime Host

Runtime Host supersedes Loader as the tooling component that prepares a
validated execution environment and hosts Runtime 0.1.

Bootstrap State describes pre-runtime boot/alignment/control-transfer evidence.
Runtime State is created only for the active Runtime 0.1 invocation.

Runtime Host is derived implementation tooling. It never owns Cerebro Source
authority and never mutates Source from Runtime execution.