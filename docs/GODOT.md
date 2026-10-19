# Godot verification workbench

### Run the workbench

Install **Python 3.11+** and a **Godot 4 editor binary**. The current engine validation used **Godot 4.7.1 on Windows**; other versions and platforms need their own engine run.

```bash
git clone https://github.com/lEWFkRAD/hermes-game-studio.git
cd hermes-game-studio

python skills/game-dev-verify/scripts/studio.py doctor --godot /path/to/godot
python skills/game-dev-verify/scripts/studio.py check --godot /path/to/godot --output artifacts
```

On Windows, replace `python` with `py -3.14` if appropriate and quote the executable path:

```powershell
py -3.14 -B skills/game-dev-verify/scripts/studio.py check --godot "C:/Tools/Godot/Godot_console.exe" --output artifacts
```

If `godot` is on PATH, omit `--godot`. You can also set `GODOT_BIN`.

Each check creates a fresh `artifacts/<run-id>/` with a project copy, separate logs, and `receipt.json`. Exit codes: **0** passed, **1** checks failed, **2** a prerequisite or output-location check blocked the run. Existing runs are preserved. The runner executes only the bundled fixture; it does not verify arbitrary game directories.

### Play Signal Garden

Open `../skills/game-dev-verify/assets/signal-garden/project.godot` in Godot and press **F6** on `main.tscn`, or run the project with **F5**.

| Input | Action |
| :--- | :--- |
| Arrow keys | Move one tile |
| Gold cells | Collect all three |
| Red tiles | Lose the round |
| Green exit | Win after collecting every cell |
| R | Restart with a clean state |

The header illustration represents the fixture; it is not a captured gameplay frame. The game intentionally uses simple procedural shapes so the workflow can be reproduced without an asset pipeline.

### Use the skill

Copy the complete [`../skills/game-dev-verify`](../skills/game-dev-verify) folder into the intended agent's skill library. For Hermes, use your active profile's `skills/` directory. For Codex, use `$CODEX_HOME/skills/` or `~/.codex/skills/`. Keep `scripts/`, `assets/`, `references/`, and `LICENSE` together. Inspect any existing same-named skill before replacing it.

Then ask the agent:

> Use game-dev-verify to check the bundled Signal Garden fixture. Show the receipt and distinguish automated checks from remaining playtest work.

This prototype needs no new MCP server, model provider, or background service. Native Hermes skill loading was verified locally; a complete model-driven game-building session remains unverified.

### What a pass means

| Gate | Current evidence |
| :--- | :--- |
| Engine import | Passed locally on Godot 4.7.1 / Windows |
| Gameplay logic | 12 assertions: fresh state, boundaries, collection, loss, win, and restart |
| Main scene boot | Passed headlessly |
| Verifier behavior | 7 Python tests; CI runs these on Windows and Linux |
| Rendered appearance / physical input | Separate manual acceptance; not certified by this suite |
| Frame times / native export / other engine platforms | Not yet qualified |

The CI badge covers the Python verifier tests, **not Godot execution**. Read the [validation notes](VALIDATION.md) for exact scope. Runtime receipts contain local paths and environment details; review them before sharing.
