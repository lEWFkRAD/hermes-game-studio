---
name: game-dev-verify
description: Verify a Godot game slice with engine import, gameplay regressions, scene boot and evidence receipts; plan separate visual, asset and playtest acceptance when preparing an agent game-development workflow.
---

# Verify a game slice

Use this for Godot verification or an agent game studio handoff. Preserve the user's chosen engine and existing project; the bundled Godot sample is an optional clean fixture, not a reason to migrate their game.

For a new workflow, read [references/workflow.md](references/workflow.md). The self-contained fixture is under assets/signal-garden and uses only authored procedural geometry. From this skill directory:

```text
python scripts/studio.py doctor --godot /absolute/path/to/godot
python scripts/studio.py check --godot /absolute/path/to/godot --output /path/to/evidence
```

`check` only exercises the bundled fixture. It does not certify another project. To apply the pattern to a real game, inspect its own build/tests, use an isolated candidate, and add behavior-specific checks there. Keep failed attempts and identify source revision, engine version, commands, logs and output hashes. Existing project permissions and process ownership remain authoritative.

Read the returned receipt before claiming success. A zero exit code is insufficient when Godot emits script/import errors; the runner checks logs and explicit gameplay markers. Headless import, gameplay logic and scene boot are distinct gates. Physical input, rendered appearance, frame times, exports and subjective playability remain unverified until separately observed. Never describe an unmeasured frame rate as GPU headroom.

For assets, distinguish file integrity from acceptance: provenance/license -> engine import -> game-camera material review -> deformation/animation checks -> in-scene validation. A valid GLB is not proof of usable topology, rig quality or performance. Read workflow.md for the minimum handoff record.

Use deterministic tools first. Any model-assisted review supports measured evidence; it cannot turn missing evidence into a pass. This skill does not start services, schedule background jobs, publish work or change model routing.
