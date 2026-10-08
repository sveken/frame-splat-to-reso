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


def write_depths(dense):
    (dense / 'images').mkdir(parents=True, exist_ok=True)
    (dense / 'images/view.jpg').write_bytes(b'image placeholder')
    for folder, channels in (('depth_maps', 1), ('normal_maps', 3)):
        path = dense / 'stereo' / folder / 'view.jpg.geometric.bin'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f'256&256&{channels}&'.encode() + bytes(256 * 256 * channels * 4))
    (dense / 'stereo/fusion.cfg').write_text('view.jpg\n')


class FusionMemoryTests(unittest.TestCase):
    def test_depth_resolution_matches_processing_mode_but_texture_inputs_do_not_change(self):
        tools = {name: name for name in ('colmap', 'mvs_interface', 'mvs_reconstruct', 'mvs_texture')}
        for mode, expected in (('auto', 800), ('balanced', 1024), ('full', 1600)):
            settings = {'mesh': dict(mesh.MESH_PRESETS['Room'], fusion_mode=mode)}
            stages = mesh.commands('dataset', 'job', settings, tools)
            depth = stages[1][2]
            images = stages[0][2]
            texture = stages[-1][2]
            self.assertEqual(depth[depth.index('--PatchMatchStereo.max_image_size') + 1], str(expected))
            self.assertEqual(images[images.index('--max_image_size') + 1], '1600')
            self.assertEqual(texture[texture.index('--max-texture-size') + 1], '4096')
        self.assertEqual(mesh.depth_image_size(mesh.MESH_PRESETS['Preview'], 'balanced'), 768)

    def test_large_room_fits_budget_without_disk_cache(self):
        with patch.object(mesh, 'depth_workspace_shapes', return_value=[(1600, 1600)] * 580):
            plan = mesh.fusion_plan('unused', available_bytes=14 * 2**30)
        self.assertLess(plan['max_image_size'], 1600)
        self.assertLessEqual(plan['estimated_bytes'], plan['budget_bytes'])
        self.assertFalse(plan['use_cache'])
        command = mesh.planned_fusion_command(['colmap', 'stereo_fusion', '--StereoFusion.use_cache', '1'], plan)
        self.assertEqual(command[command.index('--StereoFusion.use_cache') + 1], '0')

    def test_small_scan_preserves_full_resolution(self):
        with patch.object(mesh, 'depth_workspace_shapes', return_value=[(768, 768)] * 200):
            plan = mesh.fusion_plan('unused', available_bytes=14 * 2**30)
        self.assertEqual(plan['max_image_size'], 768)

    def test_balanced_gives_room_more_detail_within_memory_budget(self):
        with patch.object(mesh, 'depth_workspace_shapes', return_value=[(1600, 1600)] * 580):
            fast = mesh.fusion_plan('unused', available_bytes=14 * 2**30)
            balanced = mesh.fusion_plan('unused', available_bytes=14 * 2**30, mode='balanced')
        self.assertGreater(balanced['max_image_size'], fast['max_image_size'])
        self.assertLessEqual(balanced['max_image_size'], 1024)
        self.assertFalse(balanced['use_cache'])
        self.assertLessEqual(balanced['estimated_bytes'], balanced['budget_bytes'])
        self.assertGreaterEqual(balanced['available_bytes'] - balanced['budget_bytes'], 2 * 2**30)

    def test_balanced_caps_at_1024_and_does_not_upscale_preview(self):
        for original, expected in ((1600, 1024), (768, 768)):
            with patch.object(mesh, 'depth_workspace_shapes', return_value=[(original, original)] * 200):
                plan = mesh.fusion_plan('unused', available_bytes=32 * 2**30, mode='balanced')
            self.assertEqual(plan['max_image_size'], expected)

    def test_full_detail_preserves_resolution_with_memory_sized_cache(self):
        with patch.object(mesh, 'depth_workspace_shapes', return_value=[(1600, 1600)] * 580):
            plan = mesh.fusion_plan('unused', available_bytes=14 * 2**30, mode='full')
        self.assertEqual(plan['max_image_size'], 1600)
        self.assertTrue(plan['use_cache'])
        self.assertEqual(plan['threads'], 1)
        self.assertGreater(plan['cache_size_gib'], 4)
        self.assertLessEqual(plan['estimated_bytes'], plan['budget_bytes'])
        command = mesh.planned_fusion_command(['colmap', 'stereo_fusion'], plan)
        self.assertEqual(command[command.index('--StereoFusion.use_cache') + 1], '1')
        self.assertEqual(command[command.index('--StereoFusion.max_image_size') + 1], '1600')

    def test_full_detail_uses_threads_when_all_maps_fit(self):
        with patch.object(mesh, 'depth_workspace_shapes', return_value=[(1600, 1600)] * 580):
            plan = mesh.fusion_plan('unused', available_bytes=64 * 2**30, mode='full')
        self.assertEqual(plan['max_image_size'], 1600)
        self.assertFalse(plan['use_cache'])
        self.assertEqual(plan['threads'], 4)

    def test_insufficient_memory_stops_instead_of_thrashing(self):
        with patch.object(mesh, 'depth_workspace_shapes', return_value=[(1600, 1600)] * 580):
            with self.assertRaisesRegex(RuntimeError, 'Too little free RAM'):
                mesh.fusion_plan('unused', available_bytes=2 * 2**30)

    def test_rejects_incomplete_maps_and_escaped_image_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            dense = Path(temp)
            write_depths(dense)
            self.assertEqual(mesh.depth_workspace_shapes(dense), [(256, 256)])
            (dense / 'stereo/normal_maps/view.jpg.geometric.bin').write_bytes(b'256&256&3&')
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                mesh.depth_workspace_shapes(dense)
            (dense / 'stereo/fusion.cfg').write_text('../../../outside.jpg\n')
            with self.assertRaises(ValueError):
                mesh.depth_workspace_shapes(dense)


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
        job = flow.make_job(self.source, self.base / 'out', 'Room', output_type='Mesh', fusion_mode='full')
        settings = flow.read_json(job / 'settings.json')
        self.assertEqual(settings['output_type'], 'Mesh')
        self.assertEqual(settings['mesh']['target_faces'], 600000)
        self.assertEqual(settings['mesh']['fusion_mode'], 'full')

    def test_unknown_fusion_mode_rejected_before_job_created(self):
        with self.assertRaisesRegex(ValueError, 'fusion mode'):
            flow.make_job(self.source, self.base / 'out', 'Room', output_type='Mesh', fusion_mode='invalid')
        self.assertFalse((self.base / 'out').exists())

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
            if stage == 'Estimating surface depth':
                write_depths(job / 'mesh-work/dense')
            if stage == 'Combining surface depth':
                dense = job / 'mesh-work/dense'
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
    def test_resume_skips_training_and_depth_and_preserves_splat(self):
        job = flow.make_job(self.source, self.base / 'out', 'Room', output_type='Both')
        write_depths(job / 'mesh-work/dense')
        (job / 'scene.ply').write_bytes(b'previous validated splat')
        splat = dict(kind='splat', path=str(job / 'scene.ply'), validated=True,
                     sha256=flow.sha256(job / 'scene.ply'))
        flow.write_json(job / 'result.json', dict(artifacts={'splat': splat}))
        flow.write_json(job / 'status.json', dict(status='cancelled', started='original time'))
        flow.write_json(job / 'commands.json', dict(commands=[['colmap', 'stereo_fusion']]))
        (job / 'STOP').touch()
        worker = flow.Worker(job, resume_mesh=True)
        seen = []
        def run(command, stage, **kwargs):
            seen.append(stage)
            if stage == 'Combining surface depth':
                (job / 'mesh-work/dense/fused.ply').touch()
                (job / 'mesh-work/dense/fused.ply.vis').touch()
            if stage == 'Applying photo textures':
                write_mesh(job / 'mesh')
        with patch.object(mesh, 'preflight', return_value=[]), \
             patch.object(worker, 'run_command', side_effect=run), \
             patch.object(worker, 'build_splat', side_effect=AssertionError('Splat restarted')):
            self.assertEqual(worker.execute(), 0)
        self.assertEqual(seen, ['Combining surface depth', 'Preparing textured mesh',
                                'Building and simplifying mesh', 'Applying photo textures'])
        self.assertEqual(flow.sha256(job / 'scene.ply'), splat['sha256'])
        self.assertEqual(set(flow.read_json(job / 'result.json')['artifacts']), {'splat', 'mesh'})
        self.assertEqual(flow.read_json(job / 'status.json')['status'], 'complete')
        self.assertFalse((job / 'STOP').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows worker')
    def test_resume_refuses_active_run_without_changing_status(self):
        job = flow.make_job(self.source, self.base / 'out', 'Room', output_type='Mesh')
        before = dict(status='running', stage='Combining surface depth', pid=999999)
        flow.write_json(job / 'status.json', before)
        worker = flow.Worker(job, resume_mesh=True)
        with patch.object(worker, 'run_command') as run:
            self.assertEqual(worker.execute(), 1)
        run.assert_not_called()
        self.assertEqual(flow.read_json(job / 'status.json'), before)

    @unittest.skipUnless(os.name == 'nt', 'Windows worker')
    def test_resume_refuses_changed_splat_and_keeps_checkpoint(self):
        job = flow.make_job(self.source, self.base / 'out', 'Room', output_type='Both')
        write_depths(job / 'mesh-work/dense')
        flow.write_json(job / 'mesh-depth-complete.json', {'completed': True})
        flow.write_json(job / 'status.json', {'status': 'cancelled'})
        (job / 'scene.ply').write_bytes(b'changed splat')
        flow.write_json(job / 'result.json', {'artifacts': {'splat': dict(
            path=str(job / 'scene.ply'), validated=True, sha256='old checksum')}})
        (job / 'STOP').touch()
        worker = flow.Worker(job, resume_mesh=True)
        with patch.object(worker, 'run_command') as run:
            self.assertEqual(worker.execute(), 1)
        run.assert_not_called()
        self.assertTrue((job / 'STOP').exists())
        self.assertEqual((job / 'scene.ply').read_bytes(), b'changed splat')
        self.assertEqual(flow.read_json(job / 'status.json'), {'status': 'cancelled'})
        self.assertFalse((flow.STATE / 'running.lock').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows worker')
    def test_new_stop_during_resume_preflight_is_honoured(self):
        job = flow.make_job(self.source, self.base / 'out', 'Room', output_type='Mesh')
        write_depths(job / 'mesh-work/dense')
        flow.write_json(job / 'mesh-depth-complete.json', {'completed': True})
        flow.write_json(job / 'status.json', {'status': 'cancelled'})
        worker = flow.Worker(job, resume_mesh=True)
        with patch.object(mesh, 'preflight', side_effect=lambda *_: (job / 'STOP').touch()), \
             patch.object(worker, 'run_command') as run:
            self.assertEqual(worker.execute(), 0)
        run.assert_not_called()
        self.assertEqual(flow.read_json(job / 'status.json')['status'], 'cancelled')

    @unittest.skipUnless(os.name == 'nt', 'Windows worker')
    def test_texture_crash_resume_skips_fusion_and_keeps_texture_resolution(self):
        job = flow.make_job(self.source, self.base / 'out', 'Room', output_type='Mesh')
        write_depths(job / 'mesh-work/dense')
        (job / 'mesh-work/scene.mvs').write_bytes(b'camera checkpoint')
        (job / 'mesh-work/surface.ply').write_bytes(b'surface checkpoint')
        flow.write_json(job / 'mesh-depth-complete.json', {'completed': True})
        flow.write_json(job / 'commands.json', {'commands': [['TextureMesh.exe']]})
        flow.write_json(job / 'status.json', dict(status='failed',
            error='Applying photo textures failed (exit 3221225477).'))
        worker = flow.Worker(job, resume_mesh=True)
        seen = []
        def run(command, stage, **kwargs):
            seen.append((command, stage))
            write_mesh(job / 'mesh')
        with patch.object(mesh, 'preflight', return_value=[]), \
             patch.object(worker, 'run_command', side_effect=run):
            self.assertEqual(worker.execute(), 0)
        self.assertEqual(len(seen), 1)
        command, stage = seen[0]
        self.assertEqual(stage, 'Applying photo textures')
        self.assertEqual(command[command.index('--global-seam-leveling') + 1], '0')
        self.assertEqual(command[command.index('--local-seam-leveling') + 1], '0')
        self.assertEqual(command[command.index('--max-texture-size') + 1], '4096')
        self.assertTrue(flow.read_json(job / 'result.json')['artifacts']['mesh']['warnings'])
        self.assertFalse((job / 'fusion-plan.json').exists())

    def test_texture_native_crash_retries_only_once(self):
        job = flow.make_job(self.source, self.base / 'out', 'Preview', output_type='Mesh')
        worker = flow.Worker(job)
        stages = mesh.commands(job / 'dataset', job, worker.settings, self.tools)[-1:]
        try:
            with patch.object(worker, 'run_command', side_effect=flow.CommandFailed('Applying photo textures', 3221225477)) as run:
                with self.assertRaises(flow.CommandFailed):
                    worker.build_mesh(stages)
            self.assertEqual(run.call_count, 2)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('--global-seam-leveling') + 1], '0')
        finally:
            worker.log.close()

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
