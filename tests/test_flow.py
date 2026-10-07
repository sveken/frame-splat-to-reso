import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import config
import flow


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.source = self.base / 'recording with spaces.mp4'
        self.source.write_bytes(b'test source')
        self.tools = {name: str(self.base / f'{name} with spaces.exe')
                      for name in ('python', 'colmap', 'lichtfeld', 'pipeline')}
        self.tools_patch = patch.object(flow, 'tool_paths', return_value=self.tools.copy())
        self.tools_patch.start()
        self.addCleanup(self.tools_patch.stop)
        self.state_patch = patch.object(flow, 'STATE', self.base / 'private')
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.disk_patch = patch.object(flow.shutil, 'disk_usage', return_value=type('Disk', (), {'free': 100 * 2**30})())
        self.disk_patch.start()
        self.addCleanup(self.disk_patch.stop)

    def test_new_runs_preserve_existing_data_and_snapshot_tools(self):
        first = flow.make_job(self.source, self.base / 'out', 'Preview')
        (first / 'scene.ply').write_bytes(b'keep previous output')
        second = flow.make_job(self.source, self.base / 'out', 'Preview')
        self.assertNotEqual(first, second)
        self.assertEqual(self.source.read_bytes(), b'test source')
        self.assertEqual((first / 'scene.ply').read_bytes(), b'keep previous output')
        saved = flow.read_json(second / 'settings.json')
        self.assertEqual(saved['tools'], self.tools)
        self.assertEqual(flow.read_json(flow.STATE / 'last_job.json')['path'], str(second))

    def test_reuse_rejects_another_recording(self):
        dataset = self.base / 'previous/dataset'
        (dataset / 'sparse/0').mkdir(parents=True)
        (dataset / 'sparse/0/cameras.bin').touch()
        flow.write_json(dataset / 'alignment.json', {})
        flow.write_json(dataset.parent / 'source.json', {'sha256': 'different'})
        with self.assertRaisesRegex(ValueError, 'different recording'):
            flow.make_job(self.source, self.base / 'out', 'Preview', prepared=dataset)

    def test_prepared_scan_keeps_original_location(self):
        dataset = self.base / 'previous/dataset'
        (dataset / 'sparse/0').mkdir(parents=True)
        (dataset / 'sparse/0/cameras.bin').touch()
        flow.write_json(dataset / 'alignment.json', {})
        flow.write_json(dataset.parent / 'source.json', {'sha256': flow.sha256(self.source)})
        job = flow.make_job(self.source, self.base / 'out', 'Detailed', prepared=dataset.parent)
        self.assertEqual(flow.read_json(job / 'settings.json')['prepared_dataset'], str(dataset))
        self.assertNotEqual(job, dataset.parent)

    def test_missing_tool_fails_before_creating_output(self):
        self.tools_patch.stop()
        with patch('config.lichtfeld_path', return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, 'LichtFeld'):
                flow.make_job(self.source, self.base / 'out', 'Preview')
        self.assertFalse((self.base / 'out').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows worker')
    def test_parser_failure_stops_before_reconstruction_and_cleans_lock(self):
        job = flow.make_job(self.source, self.base / 'out', 'Preview')
        worker = flow.Worker(job)
        with patch.object(flow, 'gpu_info', return_value={'available': True}), \
             patch.object(flow, 'validate_training_command', side_effect=RuntimeError('unsupported trainer')), \
             patch.object(worker, 'run_command') as run:
            self.assertEqual(worker.execute(), 1)
        run.assert_not_called()
        self.assertEqual(flow.read_json(job / 'status.json')['status'], 'failed')
        self.assertFalse((flow.STATE / 'running.lock').exists())
        self.assertFalse((job / 'scene.ply').exists())


class ConfigTests(unittest.TestCase):
    def test_preferences_are_local_and_preserve_other_choices(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(config, 'STATE', Path(temp)):
            config.save_preferences(lichtfeld='chosen.exe', output='chosen output')
            config.save_preferences(preset='Room')
            self.assertEqual(config.preferences()['output'], 'chosen output')
            self.assertEqual(config.lichtfeld_path(), Path('chosen.exe'))

    def test_selected_tool_is_used_as_one_argument(self):
        command = flow.training_command('dataset', 'out', flow.PRESETS['Detailed'], 'log', 'C:/tools with spaces/trainer.exe')
        self.assertEqual(command[0], 'C:/tools with spaces/trainer.exe')
        self.assertEqual(command[command.index('--use_cpu_cache') + 1], '0')
        self.assertEqual(command[command.index('--use_fs_cache') + 1], '1')


class RuntimeTests(unittest.TestCase):
    @unittest.skipUnless(config.lichtfeld_path() and config.lichtfeld_path().is_file(), 'Choose LichtFeld or set LICHTFELD_PATH')
    def test_real_parser_accepts_all_presets(self):
        for preset, settings in flow.PRESETS.items():
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                command = flow.training_command(base / 'unused data', base / 'unused out', settings, base / 'unused.log')
                flow.validate_training_command(command)

    @unittest.skipUnless((config.PIPELINE / 'av1_train.py').is_file(), 'Run Setup.cmd for pipeline validation')
    def test_validator_rejects_truncated_ply(self):
        sys.path.insert(0, str(config.PIPELINE))
        from av1_train import validate_ply
        fields = ['x', 'y', 'z', 'f_dc_0', 'f_dc_1', 'f_dc_2', 'opacity',
                  'scale_0', 'scale_1', 'scale_2', 'rot_0', 'rot_1', 'rot_2', 'rot_3']
        header = 'ply\nformat binary_little_endian 1.0\nelement vertex 1\n'
        header += ''.join(f'property float {field}\n' for field in fields) + 'end_header\n'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'splat.ply'
            path.write_bytes(header.encode() + struct.pack('<14f', *([0] * 14)))
            self.assertEqual(validate_ply(path), 1)
            path.write_bytes(path.read_bytes()[:-1])
            with self.assertRaises(ValueError):
                validate_ply(path)


if __name__ == '__main__':
    unittest.main()
