# AI Disclosure
All the scripts here where created with GPT Astra.

Arcturus do not provide a workflow for windows and their camera for the frame. Hence the creation of this tool to provide an easy to use GUI that allows you to turn a Spatial Video captured by the color passthrough addon for the frame into a splat that is useable in resonite. 


The app extracts stereo images and reconstructs camera positions with COLMAP. Choose **Splat**, **Mesh** or **Both**: splats train in LichtFeld; meshes use COLMAP dense stereo and OpenMVS to generate simplified surfaces with photo textures. Processing stays on your PC. Recordings are never overwritten.

![Frame Splat to Resonite interface with recording, output folder, LichtFeld Studio and quality controls](docs/interface.png)

## Set up once

1. Use **64-bit Windows and an NVIDIA GPU** with a working CUDA driver. 8 GB VRAM and 32 GB RAM are recommended. Start with Preview.
2. Install [Python 3.12, 64-bit](https://www.python.org/downloads/release/python-31210/). Keep **Tcl/Tk** and the **Python launcher** enabled.
3. For **Splat / Both**, download and extract [LichtFeld Studio for Windows](https://github.com/MrNeRF/LichtFeld-Studio). Keep its whole folder together. Mesh-only conversion does not need LichtFeld.
4. Extract this project to a writable folder. Double-click **Setup.cmd**. It downloads the pinned reconstruction tools and Python packages.
5. Double-click **Start.cmd**. Browse to your `LichtFeld-Studio.exe` once if using splats.

Updating an existing install: rerun **Setup.cmd** to add the mesh tools. Advanced shortcut: `.venv\Scripts\python.exe setup.py --mesh-only` installs only the pinned OpenMVS runtime.

Tested with LichtFeld build **5a92bff**. Other versions may change its command-line options. The app checks those options before reconstruction. This is a community Windows adapter, not an official Arcturus release.

## Convert

1. Sync your recording with **Arcturus Vision Sync**. Use the original MP4, not a side-by-side video export.
2. Choose the recording, output folder, output type and quality. Click **Convert**.
3. When finished, click **Inspect / crop in LichtFeld**.
4. Right-click the model → **Add Crop Box**. Right-click the box → **Fit to Scene (Trimmed)**. Adjust it around what you want to keep, then **Apply**.
5. **File → Export → PLY**. Save a new file.
6. Import that file into Resonite as **Gaussian Splat**. Test without SPZ compression first.

For **Mesh**, use **Open mesh files** after completion. Import `mesh/scene.obj` into Resonite as a regular **3D model**. Keep its `.mtl` and all texture images alongside the OBJ, including when copying it to another PC. Try **Unlit / PBR Emissive** material to preserve the captured lighting. Check scale, orientation, missing surfaces and performance. This creates a visual mesh; it does not automatically create clean collision geometry or join separate recordings.

**Both** produces `scene.ply` and `mesh/scene.obj` from the same reconstruction. If the mesh stage fails, the completed splat remains available. Each run writes `result.json` with validated output files and checksums. Structural validation does not certify visual quality.

| Quality | Use | Stereo pairs | Steps | Splat limit |
|---|---|---:|---:|---:|
| Preview | First test; half resolution | 100 | 7,000 | 500,000 |
| Detailed | Objects; full resolution | 150 | 30,000 | 1.5 million |
| Room | More viewpoints; full resolution | 300 | 30,000 | 1.5 million |

Mesh settings use the same stereo-pair counts, with these additional limits:

| Quality | Maximum image dimension | Target triangles | Maximum size per texture |
|---|---:|---:|---:|
| Preview | 768 px | 100,000 | 2,048 px |
| Detailed | 1,600 px | 300,000 | 4,096 px |
| Room | 1,600 px | 600,000 | 4,096 px |

Triangle counts are simplification targets, not guarantees; several texture images may be generated. Preview reduces both surface and texture detail. Mesh generation estimates dense depth on the NVIDIA GPU, then builds and textures the surface on the CPU. Progress is approximate and dense reconstruction can take a long time. A mesh is reconstructed from the source images, not converted from the trained splat.

**Mesh processing** offers three choices:

- **Faster (up to 800 px)** is the default. New scans calculate depth at up to 800 px. Fusion uses four CPU threads and lowers resolution further if needed to fit available memory. Recovery keeps existing depth maps. This avoids the severe repeated file loading caused by COLMAP's small disk-cache mode on large rooms.
- **Balanced (up to 1,024 px)** calculates new depth at a middle resolution and allows more RAM than Faster for fusion, while reserving at least 2 GiB for other activity. It reduces fusion resolution further if needed to fit. It uses four threads without disk caching. This is a detail/memory choice, not a fixed 30-minute timer.
- **Full detail** preserves the preset's surface resolution. It uses four threads when the maps fit in RAM; otherwise it uses a cache sized to currently available memory. COLMAP's cached fusion is single-threaded and large rooms can still take hours. Closing other memory-heavy applications before fusion gives it more room. No fixed completion time is guaranteed.

The chosen fusion resolution, processing mode and memory estimate are recorded in `fusion-plan.json`. These choices affect new mesh depth calculations and fusion, not splat training or the photo texture limit. Faster recovery reuses previously calculated depth; its runtime is not a prediction for a fresh room scan.

If a run stops or fails **after depth generation has finished**, recover it without repeating training or depth calculation:

```powershell
.venv\Scripts\python.exe flow.py --job "D:\path\to\existing-run" --resume-mesh
```

Wait for the previous worker to stop first. Recovery checks every depth/normal map and verifies the completed splat checksum, then restarts fusion, meshing and texturing in the same run folder. If the previous run reached texturing and its surface files remain, recovery retries just texturing. An explicit `--mesh-fusion` choice rebuilds from fusion instead. Partial fusion itself has no resumable checkpoint. Earlier failures still need a new run with **Reuse prepared scan**.

For CLI runs or recovery, `--mesh-fusion auto` selects Faster, `--mesh-fusion balanced` selects Balanced, and `--mesh-fusion full` selects Full detail. Omitting it during recovery keeps the job's saved choice (older jobs default to Faster).

If the Windows texture tool crashes with an access violation, the worker retries once with global/local seam blending disabled. Texture resolution is unchanged, but patch boundaries may be more visible. Successful fallback outputs record this limitation in `result.json` and `mesh/IMPORT.txt`.

## Remember

- Walk slowly around a still subject. Capture overlapping views at different heights.
- Arcturus recommend 30fps for recordings intended for splatting.
- Crop unwanted background. Higher quality cannot recover unseen surfaces.
- A full 1.5-million-splat PLY is about **372 MB**. Cropping reduces it.
- Keep at least **12 GB free** for Splat or **25 GB** for Mesh / Both; longer scans may need more. Keep the PC awake.
- **Reuse prepared scan** skips image extraction and camera reconstruction. It can produce either output from an existing scan, but keeps the existing images even if you change quality. It starts fresh splat training / mesh generation; it does not resume partially calculated depth or training.
- A house needs separate, overlapping sections. The app does not stitch recordings.


License: [MIT](LICENSE). External tools keep their own licenses; see [THIRD_PARTY.md](THIRD_PARTY.md).
