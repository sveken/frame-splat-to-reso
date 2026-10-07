"""Windows worker for Arcturus recordings and Resonite Gaussian PLY output."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime

from config import ROOT, STATE, PIPELINE, PYTHON, OUTPUTS, tool_paths, lichtfeld_path
import mesh
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
OUTPUT_TYPES = ('Splat', 'Mesh', 'Both')
PRESETS = {
    'Preview': dict(frames=100, iterations=7000, cap=500000, resize=2, tile=2),
    'Detailed': dict(frames=150, iterations=30000, cap=1500000, resize=1, tile=4),
    'Room': dict(frames=300, iterations=30000, cap=1500000, resize=1, tile=4),
}


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.pending.json')
    temp.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    # A GUI may briefly have the destination open on Windows.
    for attempt in range(20):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.05)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def process_alive(pid):
    if not pid:
        return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return False
    code = ctypes.c_ulong()
    try:
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def gpu_info():
    """Query CUDA directly, avoiding the privileged nvidia-smi dependency."""
    try:
        cuda = ctypes.WinDLL('nvcuda.dll')
        if cuda.cuInit(0) != 0:
            raise RuntimeError('CUDA could not initialize')
        count = ctypes.c_int()
        if cuda.cuDeviceGetCount(ctypes.byref(count)) != 0 or count.value < 1:
            raise RuntimeError('No CUDA device is enabled')
        name = ctypes.create_string_buffer(256)
        device = ctypes.c_int()
        cuda.cuDeviceGet(ctypes.byref(device), 0)
        cuda.cuDeviceGetName(name, 256, device)
        memory = ctypes.c_size_t()
        cuda.cuDeviceTotalMem_v2(ctypes.byref(memory), device)
        return dict(available=True, name=name.value.decode(), memory_gb=round(memory.value / 2**30, 1))
    except (OSError, RuntimeError) as error:
        return dict(available=False, error=str(error), name='NVIDIA GPU unavailable')


def training_command(dataset, output, settings, log_file, lichtfeld=None):
    # This LichtFeld build parses boolean options as numeric 0/1, not true/false.
    return list(map(str, [
        lichtfeld or lichtfeld_path(), '--data-path', dataset, '--output-path', output,
        '--headless', '--iter', settings['iterations'],
        '--max-cap', settings['cap'], '--resize_factor', settings['resize'],
        '--max-width', '2464', '--tile-mode', settings['tile'],
        '--strategy', 'mcmc', '--sh-degree', '3',
        '--use_cpu_cache', '0', '--use_fs_cache', '1',
        '--log-file', log_file,
    ]))


def validate_training_command(command):
    """Exercise the actual trainer's argument parser without starting training."""
    result = subprocess.run(
        [*command, '--help'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding='utf-8', errors='replace', timeout=30, creationflags=NO_WINDOW,
        cwd=Path(command[0]).resolve().parent,
    )
    if result.returncode:
        detail = next((line.strip() for line in result.stdout.splitlines()
                       if line.strip()), f'exit {result.returncode}')
        raise RuntimeError(f'LichtFeld rejected the training settings: {detail}')
    return result.stdout


def make_job(recording, output_root, preset, prepared=None, extracted=None, lichtfeld=None, output_type='Splat'):
    recording = Path(recording).resolve()
    if not recording.is_file() or recording.suffix.lower() != '.mp4':
        raise ValueError('Choose a synced Arcturus MP4 recording.')
    if preset not in PRESETS:
        raise ValueError('Unknown quality preset.')
    if output_type not in OUTPUT_TYPES:
        raise ValueError('Unknown output type.')
    paths = tool_paths(lichtfeld, output_type=output_type)
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    required_gb = 12 if output_type == 'Splat' else 25
    if shutil.disk_usage(output_root).free < required_gb * 2**30:
        raise ValueError(f'At least {required_gb} GB free space is required for a new run.')
    dataset = None
    if prepared and extracted:
        raise ValueError('Choose only one reuse mode.')
    if prepared:
        dataset = Path(prepared).resolve()
        previous_dataset = read_json(dataset / 'settings.json').get('prepared_dataset')
        if previous_dataset:
            dataset = Path(previous_dataset).resolve()
        if (dataset / 'dataset/sparse/0/cameras.bin').is_file():
            dataset = dataset / 'dataset'
        if not (dataset / 'sparse/0/cameras.bin').is_file() or not (dataset / 'alignment.json').is_file():
            raise ValueError('Select a previous run with a completed scan, or its aligned dataset folder.')
        provenance = read_json(dataset.parent / 'source.json')
        if provenance.get('sha256') and provenance['sha256'] != sha256(recording):
            raise ValueError('This prepared scan belongs to a different recording. Select its original MP4 or clear reuse.')
    if extracted:
        extracted = Path(extracted).resolve()
        previous = read_json(extracted / 'settings.json')
        source_info = read_json(extracted / 'source.json')
        if previous.get('frames') != PRESETS[preset]['frames'] or source_info.get('sha256') != sha256(recording):
            raise ValueError('Extracted image selection or source differs. Create a fresh scan.')
        if not (extracted / 'dataset/main.aiscene').is_file():
            raise ValueError('No completed extraction checkpoint found.')
    name = re.sub(r'[^\w.-]+', '_', recording.stem)[:70]
    stamp = datetime.now().astimezone().strftime('%Y-%m-%d_%H-%M-%S')
    base = f'{stamp}_{name}_{preset.lower()}'
    if output_type != 'Splat':
        base += '_' + output_type.lower()
    for index in range(1000):
        job = output_root / (base if index == 0 else f'{base}_{index}')
        try:
            job.mkdir(exist_ok=False)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError('Could not allocate a new output folder.')
    settings = dict(recording=str(recording), preset=preset, **PRESETS[preset],
                    prepared_dataset=str(dataset) if dataset else None,
                    extracted_run=str(extracted) if extracted else None,
                    output_type=output_type, mesh=dict(mesh.MESH_PRESETS[preset]),
                    created=datetime.now().astimezone().isoformat(), adapter_version=4,
                    tools=paths)
    write_json(job / 'settings.json', settings)
    write_json(job / 'status.json', dict(status='queued', stage='Waiting to start', progress=0))
    write_json(STATE / 'last_job.json', dict(path=str(job)))
    return job


class Cancelled(Exception):
    pass


class Worker:
    def __init__(self, job):
        self.job = Path(job).resolve()
        self.settings = read_json(self.job / 'settings.json')
        self.output_type = self.settings.get('output_type', 'Splat')
        self.tools = self.settings.get('tools') or tool_paths(output_type=self.output_type)
        self.training_range = (42, 68 if self.output_type == 'Both' else 96)
        self.artifacts = {}
        self.state = dict(status='running', stage='Checking tools', progress=0,
                          pid=os.getpid(), started=datetime.now().astimezone().isoformat())
        self.log = (self.job / 'conversion.log').open('a', encoding='utf-8', buffering=1)

    def update(self, **fields):
        self.state.update(fields)
        write_json(self.job / 'status.json', self.state)

    def emit(self, line):
        self.log.write(line.rstrip() + '\n')
        print(line.rstrip(), flush=True)

    def check_cancel(self):
        if (self.job / 'STOP').exists():
            raise Cancelled('Stopped. Existing files and checkpoints were retained.')

    def run_command(self, command, stage, cwd=None, progress_range=None):
        self.check_cancel()
        self.update(stage=stage)
        command = list(map(str, command))
        self.emit('+ ' + subprocess.list2cmdline(command))
        history = read_json(self.job / 'commands.json').get('commands', [])
        history.append(command)
        write_json(self.job / 'commands.json', dict(commands=history))
        environment = os.environ.copy()
        environment.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1', QT_QPA_PLATFORM='offscreen')
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding='utf-8', errors='replace', env=environment,
                                cwd=cwd or ROOT, creationflags=NO_WINDOW)
        self.update(child_pid=proc.pid)
        lines = queue.Queue()
        def read_lines():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)
        threading.Thread(target=read_lines, daemon=True).start()
        last_view = depth_pass = 0
        try:
            while True:
                self.check_cancel()
                try:
                    line = lines.get(timeout=.5)
                except queue.Empty:
                    continue
                if line is None:
                    break
                self.emit(line)
                clean = re.sub(r'\x1b\[[0-9;]*m', '', line).strip()
                self.state['last_message'] = clean[-500:]
                extract = re.search(r'Extraction progress: decoded (\d+)%', clean)
                training = re.search(r'(\d+)\s*/\s*' + str(self.settings['iterations']), clean) if stage == 'Training Gaussian splats' else None
                mesh_view = re.search(r'(?:Processing view|Undistorting image|Fusing image)\s*\[?(\d+)\s*/\s*(\d+)', clean)
                if progress_range and mesh_view:
                    current, total = map(int, mesh_view.groups())
                    if current < last_view:
                        depth_pass += 1
                    last_view = current
                    fraction = (current - 1) / max(total, 1)
                    if stage == 'Estimating surface depth':
                        fraction = (min(depth_pass, 1) + fraction) / 2
                    start, end = progress_range
                    self.update(progress=start + (end - start) * fraction)
                elif extract:
                    self.update(stage='Extracting calibrated stereo images', progress=int(extract[1]) * .2)
                elif 'feature_extractor' in clean or 'Processed file' in clean:
                    self.update(stage='Finding image features', progress=22)
                elif 'Matching block' in clean or 'exhaustive_matcher' in clean:
                    self.update(stage='Matching overlapping views', progress=28)
                elif 'Registering image' in clean or 'Global bundle adjustment' in clean:
                    self.update(stage='Reconstructing camera positions', progress=35)
                elif training:
                    start, end = self.training_range
                    self.update(stage='Training Gaussian splats', progress=start + (end - start) * int(training[1]) / self.settings['iterations'])
            code = proc.wait()
            if code:
                raise RuntimeError(f'{stage} failed (exit {code}). See conversion.log; files have been retained.')
        except BaseException:
            if proc.poll() is None:
                subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                               capture_output=True, creationflags=NO_WINDOW, timeout=15)
                proc.wait(timeout=20)
            raise
        finally:
            proc.stdout.close()
            self.update(child_pid=None)

    def save_artifact(self, kind, artifact):
        self.artifacts[kind] = artifact
        # Retain the legacy primary artifact fields for readers of old splat jobs.
        result = dict(next(iter(self.artifacts.values())), artifacts=dict(self.artifacts))
        write_json(self.job / 'result.json', result)
        self.update(result=result)

    def build_mesh(self, stages):
        work, export = self.job / 'mesh-work', self.job / 'mesh'
        work.mkdir(exist_ok=False)
        export.mkdir(exist_ok=False)
        start = 69 if self.output_type == 'Both' else 42
        for index, (stage, fraction, command) in enumerate(stages):
            self.check_cancel()
            self.update(stage=stage, progress=start + (96 - start) * fraction)
            previous_logs = set(work.glob('*.log'))
            try:
                next_fraction = stages[index + 1][1] if index + 1 < len(stages) else 1
                self.run_command(command, stage, cwd=work, progress_range=(
                    start + (96 - start) * fraction, start + (96 - start) * next_fraction))
            finally:
                # The Windows OpenMVS binaries write diagnostics to files, not stdout.
                for log in sorted(set(work.glob('*.log')) - previous_logs):
                    self.emit(log.read_text(encoding='utf-8', errors='replace'))
            if stage == 'Combining surface depth':
                fused = work / 'dense/fused.ply'
                if not fused.is_file() or not fused.with_suffix('.ply.vis').is_file():
                    raise RuntimeError('Dense fusion did not produce points and visibility data.')
        self.check_cancel()
        self.update(stage='Verifying textured mesh', progress=97)
        artifact = mesh.validate_obj(export / 'scene.obj')
        (export / 'IMPORT.txt').write_text(
            'Import scene.obj into Resonite as a regular 3D model.\n'
            'Keep the OBJ, MTL and texture images together in this folder.\n'
            'Try Unlit / PBR Emissive material for the captured photo lighting.\n'
            'Inspect scale, orientation, missing surfaces and performance.\n'
            'This is a visual mesh, not an automatically validated collision mesh.\n', encoding='utf-8')
        self.save_artifact('mesh', artifact)
        self.emit(f'TEXTURED MESH EXPORTED: {artifact["path"]} ({artifact["triangles"]:,} triangles)')

    def execute(self):
        STATE.mkdir(parents=True, exist_ok=True)
        lock = STATE / 'running.lock'
        acquired = False
        # Keep only the system awake during work; the display may still turn off.
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
        try:
            previous = read_json(lock)
            if process_alive(previous.get('pid')):
                raise RuntimeError('A conversion is already running. Open its run folder to check progress.')
            if lock.exists():
                lock.unlink()
            with lock.open('x', encoding='utf-8') as stream:
                json.dump(dict(pid=os.getpid(), path=str(self.job)), stream)
            acquired = True
            self.update()
            self.check_cancel()
            gpu = gpu_info()
            self.emit('GPU: ' + json.dumps(gpu))
            if not gpu['available']:
                raise RuntimeError('No NVIDIA CUDA GPU is available. Enable your NVIDIA GPU and check its driver, then try again.')
            self.update(gpu=gpu)
            dataset = Path(self.settings['prepared_dataset']) if self.settings['prepared_dataset'] else self.job / 'dataset'
            training = self.job / 'training'
            checks = []
            if self.output_type in ('Splat', 'Both'):
                command = training_command(dataset, training, self.settings, self.job / 'training.log', self.tools['lichtfeld'])
                self.update(stage='Checking LichtFeld settings')
                validate_training_command(command)
                checks.append(dict(command=[*command, '--help'], passed=True))
            if self.output_type in ('Mesh', 'Both'):
                self.update(stage='Checking mesh tools')
                mesh_stages = mesh.commands(dataset, self.job, self.settings, self.tools)
                checks.extend(mesh.preflight(mesh_stages, self.job / 'mesh-preflight'))
            write_json(self.job / 'preflight.json', dict(
                checks=checks, passed=True,
                checked=datetime.now().astimezone().isoformat()))
            self.emit('Output tool checks passed before reconstruction.')
            self.check_cancel()
            self.update(stage='Checking source recording')
            source = Path(self.settings['recording'])
            source_hash = sha256(source)
            write_json(self.job / 'source.json', dict(path=str(source), sha256=source_hash, bytes=source.stat().st_size))
            if not self.settings['prepared_dataset']:
                reuse_args = []
                if self.settings.get('extracted_run'):
                    old_dataset = Path(self.settings['extracted_run']) / 'dataset'
                    self.update(stage='Copying completed extraction into the new run')
                    dataset.mkdir(exist_ok=False)
                    shutil.copytree(old_dataset / 'images', dataset / 'images')
                    shutil.copy2(old_dataset / 'main.aiscene', dataset / 'main.aiscene')
                    reuse_args = ['--resume']
                self.run_command([self.tools['python'], '-u', self.tools['pipeline'], source, dataset,
                                  '--frames', self.settings['frames'], '--colmap', self.tools['colmap'],
                                  '--colmap-gpu', '--colmap-threads', '4', '--no-guided-matching',
                                  '--hardware-decode', 'auto', *reuse_args], 'Preparing calibrated stereo reconstruction')
            self.update(stage='Checking reconstruction', progress=40)
            import pycolmap
            model = pycolmap.Reconstruction(str(dataset / 'sparse/0'))
            expected = len(list((dataset / 'images').rglob('*.jpg')))
            registered, points = model.num_reg_images(), model.num_points3D()
            alignment = read_json(dataset / 'alignment.json')
            report = dict(registered_images=registered, extracted_images=expected, sparse_points=points,
                          registration_fraction=registered / max(expected, 1), alignment=alignment)
            write_json(self.job / 'reconstruction.json', report)
            self.emit(f'Reconstruction: {registered}/{expected} views, {points:,} points; alignment RMS {alignment.get("rmsError", float("nan")):.4f} m')
            if registered < max(6, expected * .5) or points < 100:
                raise RuntimeError('Reconstruction coverage is too low to train a trustworthy model. See reconstruction.json; try slower capture with more overlap.')
            self.update(reconstruction=report)
            if self.output_type in ('Splat', 'Both'):
                self.build_splat(training, command)
            if self.output_type in ('Mesh', 'Both'):
                self.build_mesh(mesh_stages)
            self.check_cancel()
            self.update(status='complete', stage='Conversion complete — inspect before import', progress=100,
                        finished=datetime.now().astimezone().isoformat())
        except Cancelled as error:
            self.emit(str(error))
            self.update(status='cancelled', stage='Stopped; files retained', error=str(error))
        except BaseException as error:
            import traceback
            self.emit(traceback.format_exc())
            self.update(status='failed', stage='Needs attention', error=str(error))
            return 1
        finally:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            if acquired and read_json(lock).get('pid') == os.getpid():
                lock.unlink(missing_ok=True)
            self.log.close()
        return 0

    def build_splat(self, training, command):
        training.mkdir(exist_ok=False)
        self.run_command(command, 'Training Gaussian splats')
        self.update(stage='Verifying Resonite PLY', progress=self.training_range[1])
        sys.path.insert(0, str(PIPELINE))
        from av1_train import validate_ply
        candidates = list(training.rglob(f'splat_{self.settings["iterations"]}.ply'))
        if len(candidates) != 1:
            raise RuntimeError('The trainer did not produce exactly one final PLY. Check training.log.')
        count = validate_ply(candidates[0])
        import numpy as np
        with candidates[0].open('rb') as stream:
            while stream.readline().strip() != b'end_header':
                pass
            data_offset = stream.tell()
        floats = np.memmap(candidates[0], dtype='<f4', mode='r', offset=data_offset)
        for start in range(0, len(floats), 1_000_000):
            if not np.isfinite(floats[start:start + 1_000_000]).all():
                raise RuntimeError('The final PLY contains non-finite values.')
        del floats
        final = self.job / 'scene.ply'
        with final.open('xb') as dest, candidates[0].open('rb') as source_stream:
            shutil.copyfileobj(source_stream, dest, length=4 * 1024 * 1024)
        artifact = dict(kind='splat', path=str(final), gaussians=count, bytes=final.stat().st_size,
                        sha256=sha256(final), validated=True)
        self.save_artifact('splat', artifact)
        self.emit(f'PLY EXPORTED — inspect several angles before import: {final} ({count:,} Gaussians)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', type=Path)
    parser.add_argument('--video', type=Path)
    parser.add_argument('--output-root', type=Path, default=OUTPUTS)
    parser.add_argument('--preset', choices=PRESETS, default='Detailed')
    parser.add_argument('--output-type', choices=OUTPUT_TYPES, default='Splat')
    parser.add_argument('--prepared', type=Path)
    parser.add_argument('--reuse-extraction', type=Path)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--lichtfeld', type=Path, help='Path to LichtFeld-Studio.exe')
    args = parser.parse_args()
    if args.check:
        print(json.dumps(gpu_info(), indent=2))
        return 0
    if not args.job and not args.video:
        parser.error('Choose --video or an existing queued --job.')
    job = args.job or make_job(args.video, args.output_root, args.preset, args.prepared, args.reuse_extraction, args.lichtfeld, args.output_type)
    if args.job and read_json(job / 'status.json').get('status') != 'queued':
        raise RuntimeError('Existing jobs cannot be restarted. Create a new run; reuse a completed dataset if desired.')
    return Worker(job).execute()


if __name__ == '__main__':
    raise SystemExit(main())
