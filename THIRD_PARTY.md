# External tools

The MIT license covers this launcher and its setup/packaging code. It does not relicense external software.

Setup downloads these tools locally; they are excluded from the share ZIP:

- [Arcturus AV1 3DGS Pipeline](https://arcturus.vision/apps#create-gaussian-model), revision `bde1c9812`. Original files and README are preserved in `.runtime/upstream`. A working copy has five PyCOLMAP 3.12.6 database/path compatibility substitutions. Consult Arcturus for upstream licensing and use terms.
- [COLMAP 3.12.6](https://github.com/colmap/colmap/releases/tag/3.12.6), Windows CUDA archive. COLMAP uses the BSD 3-Clause license; its bundled dependencies retain their notices.
- Python packages listed in `requirements.txt`, installed from PyPI with their own license metadata.

[LichtFeld Studio](https://github.com/MrNeRF/LichtFeld-Studio) is obtained separately. The tested `5a92bff` source identifies its license as GPL-3.0-or-later. Keep the complete upstream distribution and its notices.

Pinned archive SHA-256 values are in `setup.py`. The COLMAP hash matches its published GitHub release asset digest; the Arcturus hash was measured from the downloaded archive. Setup fails if an archive changes.

This project is not affiliated with Arcturus, Valve, LichtFeld or Resonite.
