"""Install the pinned Windows runtime into this folder. No administrator needed."""
import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

from config import ROOT, RUNTIME, PIPELINE, PYTHON

DOWNLOADS = {
    'arcturus.zip': (
        'https://arcturus.vision/downloads/av1-3dgs-pipeline/av1-3dgs-pipeline-bde1c9812.zip',
        '4440bae3c7eae013286eed4fec0b4c3bae7bc56069e98edc27b040fd9b84fbe4'),
    'colmap.zip': (
        'https://github.com/colmap/colmap/releases/download/3.12.6/colmap-x64-windows-cuda.zip',
        'bf9e01ba942df89d3dc561626e5a89300b0b09371afcdc33e17a60e9511cf081'),
}
PATCHES = {
    'pycolmap.Database.open(database_path)': 'pycolmap.Database(str(database_path))',
    'pycolmap.Reconstruction(path)': 'pycolmap.Reconstruction(str(path))',
    'pycolmap.Reconstruction(selected_path)': 'pycolmap.Reconstruction(str(selected_path))',
    'reconstruction.write(aligned_path)': 'reconstruction.write(str(aligned_path))',
    'pycolmap.Reconstruction(aligned)': 'pycolmap.Reconstruction(str(aligned))',
}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def download(name, cache):
    url, expected = DOWNLOADS[name]
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / name
    if dest.is_file() and sha256(dest) == expected:
        print(f'Using verified {name}', flush=True)
        return dest
    print(f'Downloading {name}...', flush=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'frame-splat-to-reso-setup'})
    partial = dest.with_suffix('.partial')
    with urllib.request.urlopen(request, timeout=90) as response, partial.open('wb') as output:
        shutil.copyfileobj(response, output)
    if sha256(partial) != expected:
        raise RuntimeError(f'{name} checksum did not match. Setup stopped; no files were extracted.')
    partial.replace(dest)
    return dest


def safe_extract(archive, folder):
    folder = Path(folder).resolve()
    with zipfile.ZipFile(archive) as bundle:
        for entry in bundle.infolist():
            name = entry.filename
            parts = PurePosixPath(name).parts
            if ('\\' in name or ':' in name or name.startswith('/') or '..' in parts
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError(f'Unsafe ZIP entry: {name}')
            if not (folder / name).resolve().is_relative_to(folder):
                raise ValueError(f'ZIP entry escapes its folder: {name}')
        bundle.extractall(folder)


def patch_pipeline(source):
    for old, new in PATCHES.items():
        if source.count(old) != 1:
            raise RuntimeError(f'Unexpected Arcturus source; could not apply compatibility fix: {old}')
        source = source.replace(old, new)
    return source


def prepare_tools(cache):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    arcturus = download('arcturus.zip', cache)
    colmap = download('colmap.zip', cache)
    # Unpack only after checksum verification. Original upstream files stay intact.
    with tempfile.TemporaryDirectory(prefix='setup-', dir=RUNTIME) as temporary:
        extracted = Path(temporary)
        safe_extract(arcturus, extracted)
        original = extracted / 'av1-3dgs-pipeline-bde1c9812'
        vendor = RUNTIME / 'upstream/av1-3dgs-pipeline-bde1c9812'
        shutil.copytree(original, vendor, dirs_exist_ok=True)
        PIPELINE.mkdir(exist_ok=True)
        for item in original.iterdir():
            if item.is_file():
                shutil.copy2(item, PIPELINE / item.name)
        patched = patch_pipeline((original / 'av1_colmap.py').read_text(encoding='utf-8'))
        (PIPELINE / 'av1_colmap.py').write_text(patched, encoding='utf-8')
    safe_extract(colmap, RUNTIME / 'colmap')
    if not (RUNTIME / 'colmap/bin/colmap.exe').is_file():
        raise RuntimeError('COLMAP archive did not contain the expected Windows executable.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=ROOT / '.downloads')
    args = parser.parse_args()
    if os.name != 'nt' or struct.calcsize('P') != 8 or sys.version_info[:2] != (3, 12):
        raise RuntimeError('Setup needs 64-bit Python 3.12 on Windows. See README.md.')
    import tkinter  # Fail early if Python was installed without Tcl/Tk.
    if not PYTHON.is_file():
        print('Creating the local Python environment...', flush=True)
        subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
    print('Installing Python packages...', flush=True)
    subprocess.run([str(PYTHON), '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', str(ROOT / 'requirements.txt')], check=True)
    prepare_tools(args.cache.resolve())
    subprocess.run([str(PYTHON), '-c', 'import av, cv2, numpy, pycolmap, tkinter'], check=True)
    print('\nSetup complete. Open Start.cmd and choose your LichtFeld-Studio.exe.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'\nSetup failed: {error}', file=sys.stderr)
        raise SystemExit(1)
