# Native Metal asset pipeline

Recovered Apple Silicon TRELLIS.2 quality-job wrapper, binary dump inspector and replay/export extension. This path separates expensive inference from mesh export so saved intermediates can be reviewed and replayed without generating again.

## Recorded profile

- Native source: [`pwilkin/trellis.cpp` at `883346ef…`](https://github.com/pwilkin/trellis.cpp/tree/883346efb49bd3b46875ea996cb9f195b954d525).
- Q8 model revision: [`ilintar/trellis2-gguf` at `a57397bd…`](https://huggingface.co/ilintar/trellis2-gguf/tree/a57397bd3d351599d9729fc144b3f87c3f87d65b).
- Geometry 1024, PBR 1024, 12 steps, 4 threads, required GPU, 1800-second guard deadline.
- Ten model files, native CLI and five ggml libraries have recorded hashes in `quality-job/profile.json`.

Weights and binaries are not bundled. A local rebuild can have different binary hashes. Do not simply remove the checks: establish and review a new profile with evidence, then update the source/profile hash bindings and regression fixtures. `reference-pins.json` is a sanitized projection of original receipts; their source hashes are in `docs/RECOVERY.json`.

## Prepare and inspect locally

Python 3.11+; preparation requires no model or network. All target paths are explicit absolute POSIX paths. Replace every example path and the input digest:

```bash
python pipelines/native-metal/quality-job/quality-job.py prepare --out-dir artifacts/prop-001 --run-name prop-001 --image reference.png --input-sha256 YOUR_SHA256 --seed 42 --mac-root /absolute/path/runs --python /absolute/path/venv/bin/python --repo /absolute/path/trellis.cpp --cli /absolute/path/trellis.cpp/build/trellis-cli --models /absolute/path/models/q8
python pipelines/native-metal/quality-job/quality-job.py status --run-dir artifacts/prop-001
```

Preparation validates PNG integrity, freezes the input and source snapshot, and produces a hash-bound launch plan. It does not transfer files or start generation. Existing/partial run directories are preserved.

## Target-machine launch

The Mac must have the matching runtime, Python with `psutil`, and an explicitly configured local queue controller from the adjacent `mac-auxiliary` component on port 18080. The guard and manual lease use that controller’s private configuration under `MAC_AUX_ROOT` (default `~/auxiliary`).

Transfer the prepared directory to the exact target run path in its plan. Run `launch` from **its prepared source snapshot**, passing the exact plan SHA-256 returned by prepare. The launcher checks platform, source, inputs, CLI, libraries and models before dispatch. It claims the queue’s BUSY gate and runs an owned guard/watchdog. A launch-intent marker is permanent: uncertainty is report-only, never an automatic retry. Successful generation still awaits dump and art review.

The portable adaptation has offline tests, but no new end-to-end Mac launch qualification. The recorded CLI/library pins may prevent use with another build by design.

## Inspect and replay

```bash
python pipelines/native-metal/inspect-trellis-dump.py /path/cleaned-mesh-pbr.bin --report artifacts/dump-report.json
```

The inspector bounds bytes/time and validates serialized lengths, finite values, triangle indices, coordinates and PBR channels. It preserves the source and requires a new report path. Passing does not judge shape, topology or appearance.

`post-replay-quality.cpp` extends the pinned upstream replay tool with separate geometry resolution and export controls. To build, copy it to a new work directory, use an already-built matching upstream checkout and Ninja, then run:

```bash
python pipelines/native-metal/build-replay-quality.py --work-dir /absolute/path/new-replay-build --repo /absolute/path/trellis.cpp --ninja /absolute/path/ninja
```

This compiles a separate executable and records hashes of protected upstream binaries/libraries before and after. It expects the recorded macOS Ninja recipe; arbitrary compiler/build layouts are unsupported. Invoke the resulting binary with `--help` for export controls. Its current portable wrapper was syntax checked, not rebuilt on macOS during extraction. Preserve `LICENSE.trellis.cpp` with the derivative source.
