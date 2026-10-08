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

Mesh work runs in `mesh-work`: undistortion/resizing, CUDA PatchMatch, geometric fusion (including `.vis` visibility), OpenMVS COLMAP import, surface reconstruction/decimation, then photo texturing. Finished OBJ/MTL/images live in `mesh`. The prepared dataset is only read. OpenMVS 2.4.0 Windows writes diagnostics to log files and returns 1 for help without an input; preflight requires its banner, help section and all used options, without passing a real input. Logs are retained and copied into the main log after each OpenMVS stage. PatchMatch uses a 4 GB cache. Fusion uses four threads and **no disk cache**: COLMAP 3.12.6 forces cached fusion onto one thread, and small caches can repeatedly reload maps for large rooms.

Before fusion, validate all geometric depth/normal map dimensions and lengths. Faster caps fusion at 800 px and budgets 70% of currently available physical RAM, estimating 24 bytes per fusion pixel plus 1 GiB overhead. Reduce only fusion's maximum image dimension until that estimate fits; retain original maps and texture inputs. `fusion-plan.json` records the decision. This is a conservative estimate, not a hard process memory limit. `--job PATH --resume-mesh` accepts stopped/failed runs after depth completion, checks existing maps and any completed splat checksum, then restarts from fusion. Older runs qualify if their command history reached stereo_fusion; new runs also write `mesh-depth-complete.json`. Recovery preserves result artifacts and records prior status in `mesh-resumes.json`.

The above is `mesh.fusion_mode=auto` (Faster). `full` explicitly preserves resolution. If its in-memory estimate exceeds the budget, it uses cached fusion with one thread, reserving the full-resolution visited masks plus 2 GiB overhead and giving the remaining budget to the cache, rounded down to 0.25 GiB. This can still be much slower; the UI describes the tradeoff. Both modes retain texture settings. `--mesh-fusion` can override the saved choice during recovery without altering source data; the actual selection is recorded in the fusion plan.

`balanced` aims for a maximum of 1024 px with no disk cache. It budgets the smaller of 90% of available RAM or available RAM minus 2 GiB, estimating the 20 bytes per pixel for RGB/depth/normals/visited masks plus 1 GiB overhead. This allows more surface detail than Auto's more conservative estimate/reserve when memory is tight. Resolution is still reduced until the estimate fits; no mode promises a particular runtime.

New PatchMatch work also caps depth at 800/1024/preset pixels for auto/balanced/full. Undistorted photo inputs retain the preset's image limit for texturing. Recovery uses existing depth maps without recalculating them. If command history reached TextureMesh and its scene/surface files still exist, recovery skips directly to textures unless `--mesh-fusion` explicitly requests a rebuild. A texture process access violation (`0xc0000005`) receives one retry with global/local seam leveling disabled; the fallback and visible-seam caveat are recorded with the artifact. Cancellation and other exit codes do not trigger this retry.

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
