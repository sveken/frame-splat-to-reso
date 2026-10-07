# Development

Run `Setup.cmd`, then `.venv\Scripts\python.exe -m unittest discover -s tests -v`.

Set `LICHTFELD_PATH` to a Windows executable to include the real trainer argument-parser tests. They use `--help` and do not train.

## Files

- `app.py`: Tk interface.
- `config.py`: paths and ignored local preferences.
- `flow.py`: worker, reconstruction, training and PLY checks.
- `mesh.py`: mesh presets, COLMAP/OpenMVS commands, tool preflight and textured OBJ validation.
- `setup.py`: pinned downloads, safe ZIP extraction and the five upstream compatibility fixes.
- `build_share.py`: explicit release file list. Add new public files here when needed.
- `.local`: private preferences, latest-run pointer and worker lock.
- `.runtime`, `.venv`, `.downloads`: local dependencies and downloads; never distribute these folders as part of the launcher.

Each job snapshots executable paths and settings. A worker continues after the interface closes. A changed preset or retry creates a new run. Prepared-scan reuse keeps the existing images/camera solution; it is not a training-checkpoint resume.

`output_type` is `Splat` (also the default for old jobs), `Mesh`, or `Both`. Mesh-only jobs do not look up or validate LichtFeld. Mesh settings are snapshotted separately from splat settings. Both runs train the splat first, publish its verified artifact, then build the mesh; a subsequent failure retains the completed output in `result.json`. The `artifacts` map contains `splat` and/or `mesh`; top-level primary artifact fields remain for older readers.

Mesh work runs in `mesh-work`: undistortion/resizing, CUDA PatchMatch, geometric fusion (including `.vis` visibility), OpenMVS COLMAP import, surface reconstruction/decimation, then photo texturing. Finished OBJ/MTL/images live in `mesh`. The prepared dataset is only read. OpenMVS 2.4.0 Windows writes diagnostics to log files and returns 1 for help without an input; preflight requires its banner, help section and all used options, without passing a real input. Logs are retained and copied into the main log after each OpenMVS stage. Four CPU threads and 4 GB COLMAP caches limit memory pressure; these are not guarantees of peak memory usage.

OBJ validation checks finite coordinates, triangle and UV references, material/texture references confined to the export folder, decodable texture images, and per-file SHA-256. It does not certify surface accuracy, appearance, watertightness or suitability as a collider. Export paths are relative so the complete mesh folder can be moved.

The worker checks the selected LichtFeld command before reconstruction. Tested build: `5a92bff`, numeric `0`/`1` cache flags, MCMC, SH degree 3 and tiled training. Other builds need end-to-end verification, even when their argument parser accepts these options.

PLY verification covers required attributes, binary payload size, finite values and SHA-256. It does not evaluate appearance. The original Windows workflow completed training and a separately cropped result was confirmed in Resonite; generalizing the launcher does not establish compatibility with every GPU, driver or LichtFeld release.

## CLI

```powershell
.venv\Scripts\python.exe flow.py --video "recording.mp4" --output-root "outputs" --preset Preview --lichtfeld "path\LichtFeld-Studio.exe"
.venv\Scripts\python.exe flow.py --video "recording.mp4" --output-root "outputs" --preset Preview --output-type Mesh --prepared "previous-run"
```

Use `--prepared "previous-run\dataset"` to skip completed reconstruction. `--reuse-extraction "previous-run"` is an advanced recovery option that verifies the recording hash and frame selection, copies completed extraction and reruns reconstruction.

Run `Build share zip.cmd` to create a release. Its allowlist excludes recordings, results, executables, local paths and Git metadata. Do not zip the whole working folder.
