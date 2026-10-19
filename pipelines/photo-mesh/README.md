# Durable photo-to-mesh client

The original `neongrid_photo_mesh.py` filename is retained for provenance. It is a standalone client for the existing Camera-to-Blender durable API, separate from the Mac queue protocol. The backend application is not bundled.

Set these variables explicitly before use:

| Variable | Meaning |
| :--- | :--- |
| `STUDIO_ART_ROOT` | Output root; defaults to `./artifacts`. |
| `PHOTO_MESH_API` | Compatible API origin; defaults to loopback port 8000. |
| `PHOTO_MESH_BACKEND` | Required expected backend identifier returned by `/health` and generation responses. There is no automatically selected compute host. |
| `PHOTO_MESH_RECORDS` | Readable durable job-record directory for exact recovery after a lost response. Defaults to an empty project-local directory. |
| `PHOTO_MESH_WATCH` | Human-facing job viewer origin. |

```bash
python pipelines/photo-mesh/neongrid_photo_mesh.py submit --image /path/reference.png --label "Original prop" --run prop-001 --preset detailed --wait 60
python pipelines/photo-mesh/neongrid_photo_mesh.py collect --run prop-001 --wait 60
```

`submit` saves the input and an exclusive attempt marker, then permits one generation POST. `collect` uses GETs only. A timeout never authorizes a second POST. If the response was lost, recovery needs the matching local backend record (request key, input hash and preset), or an explicitly named existing ID verified against its record. Missing evidence remains unresolved.

The API contract includes `/health`, `/generate3d`, `/api/jobs`, job status/photo/checkpoint routes and final download URLs. A generic TRELLIS server does not implement this protocol. This client has no authentication configuration and is intended for a trusted loopback service or operator-established tunnel.

Offline tests cover busy backends, wrong backend identity, ambiguous submissions, exact reconciliation, truncated artifacts, atomic Windows file replacement and duplicate prevention.
