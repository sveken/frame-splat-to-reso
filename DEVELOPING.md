# Development

Run `Setup.cmd`, then `.venv\Scripts\python.exe -m unittest discover -s tests -v`.

Set `LICHTFELD_PATH` to a Windows executable to include the real trainer argument-parser tests. They use `--help` and do not train.

## Files

- `app.py`: Tk interface.
- `config.py`: paths and ignored local preferences.
- `flow.py`: worker, reconstruction, training and PLY checks.
- `setup.py`: pinned downloads, safe ZIP extraction and the five upstream compatibility fixes.
- `build_share.py`: explicit release file list. Add new public files here when needed.
- `.local`: private preferences, latest-run pointer and worker lock.
- `.runtime`, `.venv`, `.downloads`: local dependencies and downloads; never distribute these folders as part of the launcher.

Each job snapshots executable paths and settings. A worker continues after the interface closes. A changed preset or retry creates a new run. Prepared-scan reuse keeps the existing images/camera solution; it is not a training-checkpoint resume.

The worker checks the selected LichtFeld command before reconstruction. Tested build: `5a92bff`, numeric `0`/`1` cache flags, MCMC, SH degree 3 and tiled training. Other builds need end-to-end verification, even when their argument parser accepts these options.

PLY verification covers required attributes, binary payload size, finite values and SHA-256. It does not evaluate appearance. The original Windows workflow completed training and a separately cropped result was confirmed in Resonite; generalizing the launcher does not establish compatibility with every GPU, driver or LichtFeld release.

## CLI

```powershell
.venv\Scripts\python.exe flow.py --video "recording.mp4" --output-root "outputs" --preset Preview --lichtfeld "path\LichtFeld-Studio.exe"
```

Use `--prepared "previous-run\dataset"` to skip completed reconstruction. `--reuse-extraction "previous-run"` is an advanced recovery option that verifies the recording hash and frame selection, copies completed extraction and reruns reconstruction.

Run `Build share zip.cmd` to create a release. Its allowlist excludes recordings, results, executables, local paths and Git metadata. Do not zip the whole working folder.
