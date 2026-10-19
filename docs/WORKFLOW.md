# Asset-to-game handoff

Treat each deliverable as a candidate until its own acceptance gates pass. The generation, export, Blender and engine stages may run on different hosts; pass files and receipts between them explicitly.

| Handoff | Required evidence |
| :--- | :--- |
| Brief → reference | Asset ID, purpose, dimensions, silhouette constraints, intended camera, source/rights and prompt/seed. |
| Reference → generation | Exact input hash, backend/profile revision, request/job ID and one explicit attempt. |
| Generation → export | Terminal job receipt, unchanged saved intermediates, dump validation, export parameters and output hashes. |
| Export → Blender | Self-contained textures, known axes/scale, editable master, topology report and actual material/clay views. |
| Blender → character/game | Required retopology, named materials, pivots/collision, rig weights, deformation poses, animation names, LOD budgets and license record. |
| Game → acceptance | Actual engine captures at the intended camera, gameplay/input checks, frame-time budget, export/install/boot result and remaining issues. |

Keep the original generated mesh and an editable master. Make game exports separately. Do not infer animation readiness from a GLB extension, infer texture quality from a mesh count, or treat a Blender render as proof of in-engine appearance.

The two mesh interfaces are distinct: the Mac queue exposes `/jobs` to agents and owns a native `/generate` backend; the durable photo client targets the Camera-to-Blender `/generate3d` application. They are alternatives with separate receipts, not interchangeable endpoints.

Existing project-specific character assembly and rig/LOD work informs this workflow but has not yet been generalized here. Record those steps honestly as manual or project-specific until there is a tested reusable tool. The bundled Godot fixture demonstrates engine verification independently of generated assets.
