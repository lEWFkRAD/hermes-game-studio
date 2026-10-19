# Mac generation queue

Recovered controller and agent tools for SDXL references, native TRELLIS 512 meshes, and bounded instrumental music. One heavy process group runs at a time. Requests persist in SQLite; interruptions become terminal rather than replaying automatically. Admission checks include idle time, memory pressure, thermal state, voice leases and unmanaged model processes.

## Explicit setup

The controller runs on macOS and uses the standard Python library. The optional MCP entry point additionally needs the Python `mcp` package. Install your chosen backend and licensed weights separately. The native mesh adapter targets the recorded [trellis.cpp commit](https://github.com/pwilkin/trellis.cpp/tree/883346efb49bd3b46875ea996cb9f195b954d525); this repo does not supply its server binary.

1. Create a private runtime directory; set `MAC_AUX_ROOT` to its absolute path.
2. Copy `controller.example.json` there as `controller.json`. Set a private random API key of at least 32 characters. Keep the bind address on loopback.
3. For each intended workload, configure its exact backend command, port, health endpoint, working directory/environment and memory reserve. Image jobs also require `comfy_root` and the checkpoint filename. The example command is a placeholder, **not** an installation recipe.
4. Qualify the backend manually before setting its `qualified` field to `true`. Leave other workloads disabled. The controller owns backend startup/shutdown; do not point it at a separately managed production service.
5. Start explicitly with `python mac_controller.py`. No launch agent or cron is installed.
6. On the agent machine, copy `client.example.json` to a private path and set `MAC_AUX_CONFIG`. Set the matching key and origin. Use loopback through an operator-established SSH tunnel, or a properly configured HTTPS proxy. The client refuses redirects and plaintext remote origins.
7. Add `python /absolute/path/mac_aux_mcp.py` as an explicit stdio MCP entry in your agent, with `MAC_AUX_CONFIG` in its environment. Dependencies belong in that interpreter’s environment.

## Agent tools

| Tool | Contract |
| :--- | :--- |
| `mac_comfy_image` | One SDXL image; 256–768 dimensions in 64-pixel increments; at most 30 steps. Optional inline PNG or completed image job reference. Saves exact Comfy workflow. |
| `mac_image_to_3d` | One native 512-resolution mesh from a PNG or completed image job reference. |
| `mac_music` | Bounded instrumental music adapter for the existing ACE-Step API contract. |
| `mac_auxiliary_status`, `mac_job_result` | Read existing availability and durable state. |
| `mac_fetch_artifact` | Fetch completed PNG/WAV/GLB plus workflow where available. |
| `mac_background_mode` | Explicit operator busy/auto gate; resource admission still applies. |

Use a stable request ID. After an uncertain connection, check the existing job; do not create a fresh ID just to retry. The queue retains jobs for a bounded period, so copy completed artifacts and receipts into the project promptly. The GLB summary is a structural check, not a full glTF conformance validator.

The recovered controller retains generic text/vision and legacy voice route code from its original context. The example config and published MCP expose only the game asset tools; unrelated service adapters have not been qualified here. No fleet routing or host discovery is included.

Run offline tests from the repository root with `python -B scripts/check.py`. Tests mock process control and telemetry; they do not establish that a Mac is available or that any backend is currently running.
