import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import config
import flow
import mesh
from tests import test_flow


def write_mesh(folder):
    from PIL import Image
    folder.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (4, 4), (70, 120, 180)).save(folder / 'scene.png')
    (folder / 'scene.mtl').write_text('newmtl photo\nmap_Kd scene.png\n')
    obj = folder / 'scene.obj'
    obj.write_text('mtllib scene.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n'
                   'vt 0 0\nvt 1 0\nvt 0 1\nusemtl photo\nf 1/1 2/2 3/3\n')
    return obj


class MeshValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mesh test ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.obj = write_mesh(self.root / 'mesh')

    def test_textured_bundle_survives_relocation(self):
        before = mesh.validate_obj(self.obj)
        moved = self.root / 'moved folder'
        shutil.copytree(self.obj.parent, moved)
        after = mesh.validate_obj(moved / self.obj.name)
        self.assertEqual(before['files'], after['files'])
        self.assertEqual(after['triangles'], 1)
        self.assertEqual(after['bounds'], {'min': [0, 0, 0], 'max': [1, 1, 0]})

    def test_rejects_broken_geometry_and_uvs(self):
        original = self.obj.read_text()
        for old, new in [('v 0 0 0', 'v nan 0 0'), ('3/3', '4/3'),
                         ('3/3', '3/8'), ('3/3', '3'), ('3/3', '2/3')]:
            with self.subTest(new=new):
                self.obj.write_text(original.replace(old, new))
                with self.assertRaises(ValueError):
                    mesh.validate_obj(self.obj)

    def test_rejects_missing_texture_and_path_escape(self):
        mtl = self.obj.with_suffix('.mtl')
        for reference in ('missing.png', '../outside.png', 'C:/outside.png'):
            with self.subTest(reference=reference):
                mtl.write_text(f'newmtl photo\nmap_Kd {reference}\n')
                with self.assertRaises(ValueError):
                    mesh.validate_obj(self.obj)

    def test_rejects_corrupt_texture_and_unknown_material(self):
        (self.obj.parent / 'scene.png').write_bytes(b'bad image')
        with self.assertRaises(OSError):
            mesh.validate_obj(self.obj)
        write_mesh(self.obj.parent)
        self.obj.write_text(self.obj.read_text().replace('usemtl photo', 'usemtl absent'))
        with self.assertRaisesRegex(ValueError, 'material'):
            mesh.validate_obj(self.obj)


class MeshJobTests(unittest.TestCase):
    def setUp(self):
        test_flow.JobTests.setUp(self)
        self.tools.update({key: str(self.base / f'{key}.exe') for key in
                          ('mvs_interface', 'mvs_reconstruct', 'mvs_texture')})
        self.tools_patch.stop()
        self.tools_patch = patch.object(flow, 'tool_paths', return_value=self.tools.copy())
        self.tools_patch.start()
        self.addCleanup(self.tools_patch.stop)

    def test_mesh_job_snapshots_quality_and_allows_no_lichtfeld(self):
        job = flow.make_job(self.source, self.base / 'out', 'Room', output_type='Mesh')
        settings = flow.read_json(job / 'settings.json')
        self.assertEqual(settings['output_type'], 'Mesh')
        self.assertEqual(settings['mesh']['target_faces'], 600000)

    def test_unknown_output_rejected_before_job_created(self):
        with self.assertRaisesRegex(ValueError, 'output type'):
            flow.make_job(self.source, self.base / 'out', 'Preview', output_type='wrong')
        self.assertFalse((self.base / 'out').exists())

    def test_reuse_a_mesh_run_follows_its_original_prepared_dataset(self):
        dataset = self.base / 'original/dataset'
        (dataset / 'sparse/0').mkdir(parents=True)
        (dataset / 'sparse/0/cameras.bin').touch()
        flow.write_json(dataset / 'alignment.json', {})
        flow.write_json(dataset.parent / 'source.json', {'sha256': flow.sha256(self.source)})
        first = flow.make_job(self.source, self.base / 'out', 'Preview', prepared=dataset, output_type='Mesh')
        second = flow.make_job(self.source, self.base / 'out', 'Room', prepared=first, output_type='Both')
        settings = flow.read_json(second / 'settings.json')
        self.assertEqual(settings['prepared_dataset'], str(dataset))
        self.assertEqual(settings['mesh']['target_faces'], 600000)

    @unittest.skipUnless(os.name == 'nt', 'Windows worker')
    def test_mesh_preflight_failure_stops_before_source_processing(self):
        job = flow.make_job(self.source, self.base / 'out', 'Preview', output_type='Mesh')
        worker = flow.Worker(job)
        with patch.object(flow, 'gpu_info', return_value={'available': True}), \
             patch.object(mesh, 'preflight', side_effect=RuntimeError('incompatible mesh tools')), \
             patch.object(worker, 'run_command') as run:
            self.assertEqual(worker.execute(), 1)
        run.assert_not_called()
        self.assertFalse((job / 'source.json').exists())
        self.assertEqual(flow.read_json(job / 'status.json')['status'], 'failed')

    @unittest.skipUnless(os.name == 'nt', 'Windows worker')
    def test_mesh_worker_reuses_scan_and_never_calls_lichtfeld(self):
        dataset = self.base / 'prior/dataset'
        (dataset / 'sparse/0').mkdir(parents=True)
        (dataset / 'sparse/0/cameras.bin').touch()
        flow.write_json(dataset / 'alignment.json', {})
        job = flow.make_job(self.source, self.base / 'out', 'Preview', prepared=dataset, output_type='Mesh')
        # This intentionally excludes LichtFeld from the worker's snapshot.
        settings = flow.read_json(job / 'settings.json')
        settings['tools'].update(self.tools)
        settings['tools'].pop('lichtfeld')
        flow.write_json(job / 'settings.json', settings)
        worker = flow.Worker(job)
        reconstruction = types.SimpleNamespace(num_reg_images=lambda: 6, num_points3D=lambda: 1000)
        seen = []
        def run(command, stage, **kwargs):
            seen.append(stage)
            if stage == 'Combining surface depth':
                dense = job / 'mesh-work/dense'
                dense.mkdir()
                (dense / 'fused.ply').touch()
                (dense / 'fused.ply.vis').touch()
            if stage == 'Applying photo textures':
                write_mesh(job / 'mesh')
        with patch.object(flow, 'gpu_info', return_value={'available': True}), \
             patch.object(mesh, 'preflight', return_value=[]), \
             patch.dict(sys.modules, pycolmap=types.SimpleNamespace(Reconstruction=lambda _: reconstruction)), \
             patch.object(worker, 'run_command', side_effect=run), \
             patch.object(flow, 'validate_training_command', side_effect=AssertionError('LichtFeld called')):
            self.assertEqual(worker.execute(), 0)
        self.assertEqual(len(seen), 6)
        self.assertFalse((job / 'training').exists())
        result = flow.read_json(job / 'result.json')
        self.assertEqual(set(result['artifacts']), {'mesh'})
        self.assertTrue(result['validated'])
        self.assertFalse((flow.STATE / 'running.lock').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows worker')
    def test_stop_before_mesh_does_not_run_a_tool(self):
        job = flow.make_job(self.source, self.base / 'out', 'Preview', output_type='Mesh')
        (job / 'STOP').touch()
        worker = flow.Worker(job)
        with patch.object(worker, 'run_command') as run:
            worker.execute()
        run.assert_not_called()
        self.assertEqual(flow.read_json(job / 'status.json')['status'], 'cancelled')
        self.assertFalse((flow.STATE / 'running.lock').exists())

    def test_both_retains_completed_splat_if_mesh_fails(self):
        job = flow.make_job(self.source, self.base / 'out', 'Preview', output_type='Both')
        worker = flow.Worker(job)
        try:
            worker.save_artifact('splat', dict(kind='splat', path=str(job / 'scene.ply'), gaussians=10))
            with patch.object(worker, 'run_command', side_effect=RuntimeError('mesh failed')):
                with self.assertRaisesRegex(RuntimeError, 'mesh failed'):
                    worker.build_mesh(mesh.commands(job / 'dataset', job, worker.settings, self.tools))
            self.assertIn('splat', flow.read_json(job / 'result.json')['artifacts'])
        finally:
            worker.log.close()


class MeshConfigTests(unittest.TestCase):
    def test_mesh_tools_do_not_require_lichtfeld(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('python.exe', 'colmap.exe', 'av1_colmap.py', 'InterfaceCOLMAP.exe',
                         'ReconstructMesh.exe', 'TextureMesh.exe'):
                (root / name).touch()
            with patch.multiple(config, PYTHON=root / 'python.exe', COLMAP=root / 'colmap.exe',
                                PIPELINE=root, OPENMVS=root), \
                 patch.object(config, 'lichtfeld_path', side_effect=AssertionError('Unneeded LichtFeld lookup')):
                result = config.tool_paths(output_type='Mesh')
                self.assertNotIn('lichtfeld', result)
                self.assertIn('mvs_texture', result)

    @unittest.skipUnless((config.OPENMVS / 'TextureMesh.exe').is_file() and config.COLMAP.is_file(), 'Install mesh tools')
    def test_real_mesh_tools_accept_required_options(self):
        with tempfile.TemporaryDirectory(prefix='mesh parser ') as temp:
            root = Path(temp)
            tools = config.tool_paths(output_type='Mesh')
            for preset in ('Preview', 'Detailed', 'Room'):
                stages = mesh.commands(root / 'dataset', root / 'job', {'mesh': mesh.MESH_PRESETS[preset]}, tools)
                self.assertEqual(len(mesh.preflight(stages, root / preset)), 6)
            self.assertFalse((root / 'job').exists())


if __name__ == '__main__':
    unittest.main()
