# Blender preparation and review

These scripts open disposable Blender sessions and preserve the input asset. They do not control a running Blender window. Use Blender’s bundled Python for scripts importing `bpy`; use normal Python 3.11+ for `studio_jobs.py` and the MCP entry point.

## Prepare a mesh for editing

```bash
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python pipelines/blender/prepare-editable.py -- --input /path/candidate.glb --out-dir /path/new-editable --height 1.5 --up-axis +Y --front-axis +Z
```

Creates an editable `.blend`, normalized GLB and verification evidence. It preserves original materials, explicitly normalizes orientation/scale and checks the export by reimport. This is preparation, not automatic retopology, rigging or proof of animation readiness.

## Inspect actual geometry and appearance

```bash
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python pipelines/blender/geometry-topology.py -- --input /path/candidate.glb --out-dir /path/new-topology
blender --background --factory-startup --disable-autoexec --python-exit-code 1 --python pipelines/blender/review-asset.py -- --input /path/candidate.glb --out-dir /path/new-review --up-axis +Y --front-axis +Z --size 768 --samples 32 --threads 4
```

Topology analysis reports components, degenerate/duplicate geometry, bounds and surface measures. Exact-position welding is only in analysis arrays; it does not repair the asset. Review renders original materials and clay views, including a game-like camera. Those are Blender review images, not engine screenshots.

## Persistent review jobs and MCP

Install the Python `mcp` package in your chosen agent-tool environment. Set `STUDIO_ART_ROOT` to the asset directory and `STUDIO_BLENDER` to the Blender executable. Register `python /absolute/path/studio_mcp.py` as an explicit stdio MCP entry.

Tools: `review_blend`, `review_job_status`, `review_preview`, `resume_blender_review`. The input must be a `.blend` beneath the configured art root. Reviews run in an owned background process with auto-execution disabled. Source hashes, logs, UV/material/texture audit and preview are recorded in `review-jobs/`. A dead session is detected; only explicit resume starts a replacement, and changed sources require a new submission.

`studio_jobs.StudioJobs(art_root, blender).submit(source, audit_only=True)` also exposes the review runner directly from Python. The command-line `studio_jobs.py --run` interface is an internal worker for existing job directories.

Offline tests cover recovery and ownership decisions. Fresh asset-quality acceptance must include the actual texture, silhouette, deformation and intended engine camera; automated receipt success is only one gate.

For negative axis values, use equals syntax (for example `--front-axis=-Y`). Fresh synthetic Blender 5.2.1 preparation/topology/render results are recorded in [validation](../../docs/VALIDATION.md).
