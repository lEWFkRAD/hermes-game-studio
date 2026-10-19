# Game studio workflow

## One playable slice

Write a short brief with the player action, win/loss conditions, target engine/platform, camera and performance budget. Implement a small loop first; defer asset expansion until it works. Pick a concrete acceptance task such as move -> collect -> exit, collision -> lose, restart -> clean state.

Use one owner per mutable project or native session. Other contributors can prepare isolated candidates; they do not share an editor, save directory or GPU lease implicitly. Recheck current state when taking over and preserve saves/accepted outputs. Do not silently restart an existing game or claim an active session.

## Gates

| Gate | Evidence | Does not establish |
|---|---|---|
| Source | revision or file hashes and exact dependencies | runtime behavior |
| Import | engine version, command, exit, error-free log | gameplay or art quality |
| Behavior | real gameplay functions exercised; explicit assertions | physical input or fun |
| Scene boot | actual main scene instantiates without errors | complete playthrough |
| Art | original full-resolution game-camera frames and reviewer notes | motion/deformation |
| Motion | exported rig/clips tested at representative poses | frame-time budget |
| Playtest | input sequence, expected/actual outcome, save/reset checks | other platforms |
| Performance | hardware, renderer, resolution, warmup, p50/p95 frame times | unmeasured GPU headroom |
| Export | exact exported package hashes and clean-host boot | deployment acceptance |

A failing gate remains visible. Fix and create a new run; preserve the earlier evidence. Do not replace a failing log with a passing summary. Stop bounded automation on timeouts; inspect uncertain child state before retries.

## Asset record

For every imported asset record relative path, SHA256, source URL or authored-source path, author, license/permission, modifications, and references. Generated assets additionally need model/tool revision and input provenance where available. Record units, pivot, triangle/material/texture budgets, named clips and tested poses. Do not bundle commercial game data from private experiments into public examples. For the bundled fixture there are no external assets; its provenance file explicitly records that fact.

## Handoff

Provide the brief, candidate/source identity, commands and versions, asset ledger, receipts and original captures, observed limits, next failing gate and one next task. Distinguish prepared / tested / visually reviewed / accepted / exported / deployed. An optional MCP should expose a real unmet integration need; start with existing terminal tools and a small deterministic CLI.

## Contribution sequence

Read current upstream instructions and search code plus open/merged issues and PRs. Prefer improving an active effort. Keep the initial contribution narrow and reproduce it in a clean directory. Prepare a concrete patch/evidence packet before requesting any necessary publication approval. Record AI assistance and attribution according to the target repository. Do not infer an API or architecture from a social post.
