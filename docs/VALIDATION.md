# Validation scope

The initial local run on 2026-10-19 used Godot `4.7.1.stable.official.a13da4feb`, Python 3.14 and Windows. Engine import, 12 gameplay assertions, and a five-frame headless scene boot passed. Seven verifier tests passed, including real subprocess timeout handling. The fixture source hashes were unchanged by the runner.

The 12 gameplay assertions cover fresh state, out-of-bounds movement, diagonal rejection, collection, repeat collection, hazard loss, movement lock after loss, reset, incomplete exit, reachable win, movement lock after win, and restart after win. These call the actual gameplay functions; they do not simulate physical keyboard input or judge rendering.

The GitHub workflow runs Python verifier tests only, on Windows and Linux with Python 3.11 and 3.14. A green workflow does not establish Godot compatibility on either operating system. Pin and run the Godot editor independently for an engine result.

## Receipt contract

`receipt.json` records schema version, run ID and timestamp, environment, engine binary hash, fixture input hashes, runner hash, source-unchanged flag, stage commands/exit codes/durations/output, log hashes, and explicit unverified claims. The final status passes only when all three stages pass and source inputs remain unchanged.

Engine stages stop at the first failure. Each stage has a 45-second subprocess timeout; the version probe has 10 seconds. This runner is scoped to the included fixture, which starts no child services. It is not a process sandbox or a general untrusted-project executor.

All captured evidence stays in a fresh run directory. Receipts and logs contain local paths and environment identifiers and are ignored by Git. Review and sanitize them before public sharing.

Physical input, visual quality, performance, native export, other Godot versions/platforms, and an end-to-end agent session remain independent acceptance tasks. A file hash proves identity, not quality.
