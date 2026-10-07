"""Paths and private settings for this copy of the app."""
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parent
STATE = ROOT / '.local'
RUNTIME = ROOT / '.runtime'
PIPELINE = RUNTIME / 'pipeline'
PYTHON = ROOT / '.venv/Scripts/python.exe'
COLMAP = RUNTIME / 'colmap/bin/colmap.exe'
OPENMVS = RUNTIME / 'openmvs/vc17/x64/Release'
OUTPUTS = ROOT / 'outputs'


def preferences():
    try:
        value = json.loads((STATE / 'preferences.json').read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_preferences(**values):
    STATE.mkdir(parents=True, exist_ok=True)
    data = preferences()
    data.update(values)
    temp = STATE / 'preferences.pending.json'
    temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    temp.replace(STATE / 'preferences.json')


def lichtfeld_path():
    saved = preferences().get('lichtfeld') or os.environ.get('LICHTFELD_PATH')
    if saved:
        return Path(saved).expanduser()
    found = shutil.which('LichtFeld-Studio.exe')
    if found:
        return Path(found)
    return None


def tool_paths(lichtfeld=None, output_type='Splat'):
    paths = dict(python=PYTHON, colmap=COLMAP, pipeline=PIPELINE / 'av1_colmap.py')
    if output_type in ('Splat', 'Both'):
        selected = Path(lichtfeld).expanduser() if lichtfeld else lichtfeld_path()
        if not selected or not selected.is_file():
            raise FileNotFoundError('Choose LichtFeld-Studio.exe using Browse next to LichtFeld Studio.')
        paths['lichtfeld'] = selected.resolve()
    if output_type in ('Mesh', 'Both'):
        paths.update(mvs_interface=OPENMVS / 'InterfaceCOLMAP.exe',
                     mvs_reconstruct=OPENMVS / 'ReconstructMesh.exe',
                     mvs_texture=OPENMVS / 'TextureMesh.exe')
    for name, path in paths.items():
        if not path.is_file():
            if name.startswith('mvs_'):
                raise FileNotFoundError('Mesh tools are missing. Run Setup.cmd once to add them, then reopen the app.')
            raise FileNotFoundError(f'{name} is missing. Run Setup.cmd, then reopen the app.')
    return {name: str(path) for name, path in paths.items()}


def runtime_python():
    return str(PYTHON if PYTHON.is_file() else Path(sys.executable))
