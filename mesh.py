"""Textured mesh branch: COLMAP dense stereo followed by OpenMVS surface/textures."""
from pathlib import Path, PureWindowsPath
import hashlib
import math
import subprocess

NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
MESH_PRESETS = {
    'Preview': dict(max_image_size=768, neighbours=6, depth_iterations=3,
                    target_faces=100000, texture_size=2048),
    'Detailed': dict(max_image_size=1600, neighbours=12, depth_iterations=5,
                     target_faces=300000, texture_size=4096),
    'Room': dict(max_image_size=1600, neighbours=12, depth_iterations=5,
                 target_faces=600000, texture_size=4096),
}


def commands(dataset, job, settings, tools):
    """Return (description, fractional progress, argv) for a fresh mesh workspace."""
    dataset, job = Path(dataset).resolve(), Path(job).resolve()
    work, export = job / 'mesh-work', job / 'mesh'
    dense = work / 'dense'
    m = settings['mesh']
    colmap = tools['colmap']
    common = ['--working-folder', work, '--max-threads', '4']
    stages = [
        ('Preparing mesh images', 0, [colmap, 'image_undistorter',
         '--image_path', dataset / 'images', '--input_path', dataset / 'sparse/0',
         '--output_path', dense, '--output_type', 'COLMAP',
         '--max_image_size', m['max_image_size'],
         '--num_patch_match_src_images', m['neighbours']]),
        ('Estimating surface depth', .08, [colmap, 'patch_match_stereo',
         '--workspace_path', dense, '--workspace_format', 'COLMAP',
         '--PatchMatchStereo.max_image_size', m['max_image_size'],
         '--PatchMatchStereo.num_iterations', m['depth_iterations'],
         '--PatchMatchStereo.geom_consistency', '1', '--PatchMatchStereo.cache_size', '4',
         '--PatchMatchStereo.gpu_index', '0']),
        ('Combining surface depth', .65, [colmap, 'stereo_fusion',
         '--workspace_path', dense, '--workspace_format', 'COLMAP',
         '--input_type', 'geometric', '--output_path', dense / 'fused.ply',
         '--StereoFusion.num_threads', '4', '--StereoFusion.use_cache', '1',
         '--StereoFusion.cache_size', '4']),
        ('Preparing textured mesh', .74, [tools['mvs_interface'], *common,
         '--input-file', dense, '--image-folder', dense / 'images',
         '--output-file', work / 'scene.mvs']),
        ('Building and simplifying mesh', .79, [tools['mvs_reconstruct'], *common,
         '--input-file', work / 'scene.mvs', '--pointcloud-file', work / 'scene.ply',
         '--output-file', work / 'surface.ply', '--target-face-num', m['target_faces'],
         '--close-holes', '0', '--export-type', 'ply']),
        ('Applying photo textures', .9, [tools['mvs_texture'], *common,
         '--input-file', work / 'scene.mvs', '--mesh-file', work / 'surface.ply',
         '--output-file', export / 'scene.obj', '--export-type', 'obj',
         '--resolution-level', '0', '--max-texture-size', m['texture_size'],
         '--close-holes', '0']),
    ]
    return [(stage, fraction, list(map(str, command))) for stage, fraction, command in stages]


def preflight(stages, folder):
    """Check installed CLI capabilities without passing input files or starting work.

    OpenMVS 2.4 Windows writes help to a log and exits 1 when no input is given.
    Require the version banner, option list, and each used flag in that case.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    checked = []
    for index, (_, _, command) in enumerate(stages):
        probe = folder / str(index)
        probe.mkdir(exist_ok=True)
        is_colmap = Path(command[0]).stem.lower() == 'colmap'
        prefix = command[:2] if is_colmap else command[:1]
        help_command = [*prefix, '--help']
        result = subprocess.run(help_command, cwd=probe, capture_output=True,
                                encoding='utf-8', errors='replace', timeout=30,
                                creationflags=NO_WINDOW)
        output = result.stdout + result.stderr
        for log in probe.glob('*.log'):
            output += log.read_text(encoding='utf-8', errors='replace')
        (probe / 'help.txt').write_text(output, encoding='utf-8')
        flags = [arg for arg in command[len(prefix):] if arg.startswith('--')]
        valid_exit = result.returncode == 0 if is_colmap else (
            result.returncode in (0, 1) and 'OpenMVS' in output and 'Available options:' in output)
        missing = [flag for flag in flags if flag not in output]
        if not valid_exit or missing:
            raise RuntimeError(f'Mesh tool check failed for {Path(command[0]).name}: '
                               f'exit {result.returncode}, missing options {missing}. '
                               'Run Setup.cmd to install the supported tools.')
        checked.append(dict(command=help_command, passed=True))
    return checked


def _asset(root, parent, name):
    """Only portable bundle-relative references are accepted."""
    if not name or PureWindowsPath(name).is_absolute() or ':' in name:
        raise ValueError(f'Mesh contains a non-portable asset reference: {name}')
    path = (parent / name.replace('\\', '/')).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f'Mesh asset is missing or outside its folder: {name}')
    return path


def validate_obj(path):
    """Validate actual triangles, finite coordinates, UVs, materials and textures."""
    path = Path(path).resolve()
    root = path.parent
    vertices = uvs = normals = triangles = 0
    maxima = [0, 0, 0]
    minimum = [math.inf] * 3
    maximum = [-math.inf] * 3
    materials, used, libraries = {}, set(), set()
    active_material = None
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            fields = line.split()
            if not fields or fields[0].startswith('#'):
                continue
            kind, values = fields[0], fields[1:]
            if kind in ('v', 'vt', 'vn'):
                width = 2 if kind == 'vt' else 3
                if len(values) < width or not all(math.isfinite(float(x)) for x in values):
                    raise ValueError(f'Invalid mesh coordinates on line {number}.')
                if kind == 'v':
                    vertices += 1
                    for axis in range(3):
                        minimum[axis] = min(minimum[axis], float(values[axis]))
                        maximum[axis] = max(maximum[axis], float(values[axis]))
                elif kind == 'vt':
                    uvs += 1
                else:
                    normals += 1
            elif kind == 'mtllib':
                libraries.add(_asset(root, root, ' '.join(values)))
            elif kind == 'usemtl':
                active_material = ' '.join(values)
            elif kind == 'f':
                if len(values) != 3 or not active_material:
                    raise ValueError(f'Expected a textured triangle on line {number}.')
                used.add(active_material)
                indices = []
                for token in values:
                    refs = token.split('/')
                    if len(refs) not in (2, 3) or not refs[1]:
                        raise ValueError(f'Triangle lacks texture coordinates on line {number}.')
                    for axis, ref in enumerate(refs):
                        if not ref:
                            continue
                        value = int(ref)
                        count = (vertices, uvs, normals)[axis]
                        if value == 0 or (value < 0 and -value > count):
                            raise ValueError(f'Invalid mesh index on line {number}.')
                        maxima[axis] = max(maxima[axis], value)
                        if axis == 0:
                            indices.append(value if value > 0 else count + value + 1)
                if len(set(indices)) != 3:
                    raise ValueError(f'Degenerate triangle on line {number}.')
                triangles += 1
    if not triangles or not vertices or not uvs or any(
            needed > count for needed, count in zip(maxima, (vertices, uvs, normals))):
        raise ValueError('Mesh is empty or has out-of-range geometry/UV indices.')
    textures = set()
    from PIL import Image
    for library in libraries:
        current = None
        for line in library.read_text(encoding='utf-8').splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) != 2:
                continue
            if fields[0] == 'newmtl':
                current = fields[1]
                materials[current] = None
            elif fields[0] == 'map_Kd' and current:
                texture = _asset(root, library.parent, fields[1])
                with Image.open(texture) as image:
                    image.verify()
                materials[current] = texture
                textures.add(texture)
    if not used or any(not materials.get(name) for name in used):
        raise ValueError('Mesh is missing a referenced material or its photo texture.')
    files = []
    for asset in sorted({path, *libraries, *textures}):
        with asset.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        files.append(dict(path=asset.relative_to(root).as_posix(),
                          bytes=asset.stat().st_size, sha256=digest))
    return dict(kind='mesh', path=str(path), vertices=vertices, triangles=triangles,
                textures=len(textures), bounds=dict(min=minimum, max=maximum),
                files=files, bytes=sum(f['bytes'] for f in files), validated=True)
