# Contributing

Small contributions with a reproducible result are welcome. Open a focused issue before a larger engine integration so the approach can be discussed.

## Develop

Python 3.11+ is sufficient for the verifier tests; no pip dependencies are needed:

```bash
python -B -m unittest discover -s tests -v
```

For changes to the Godot fixture or engine invocation, run the workbench too:

```bash
python -B skills/game-dev-verify/scripts/studio.py check --godot /path/to/godot --output artifacts
```

Use the engine's real behavior in regression checks. Preserve prior attempts. A test should demonstrate a failure mode, not simply match implementation wording. Do not change the gameplay success marker without updating both the runner and tests.

## Pull requests

Describe the problem, resulting behavior, commands run, engine/platform versions, and any acceptance still missing. For behavior changes, include the smallest useful reproduction. For visual changes, supply original captures and identify what was actually reviewed. Disclose material AI assistance.

Keep local receipts, caches, credentials, private infrastructure notes, commercial game files, and unreviewed third-party assets out of commits. Logs and receipts may contain local paths. Use synthetic or original assets, with source, authorship, license and modifications recorded in provenance.json.

This is a standalone community prototype. It does not imply permission to change another project's services, native game session, agent routing, or publishing settings. Contributions are provided under this repository's MIT license.
