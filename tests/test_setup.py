from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_share
import setup


class SetupTests(unittest.TestCase):
    def test_archive_rejects_paths_outside_install_folder(self):
        for name in ('../outside.txt', '/absolute.txt', 'C:/outside.txt', '..\\outside.txt'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                with zipfile.ZipFile(root / 'unsafe.zip', 'w') as bundle:
                    bundle.writestr(name, 'unsafe')
                with self.assertRaises(ValueError):
                    setup.safe_extract(root / 'unsafe.zip', root / 'install')
                self.assertFalse((root / 'outside.txt').exists())

    def test_checksum_mismatch_is_never_installed(self):
        import io
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(setup.urllib.request, 'urlopen', return_value=io.BytesIO(b'not the archive')):
            with self.assertRaisesRegex(RuntimeError, 'checksum'):
                setup.download('arcturus.zip', Path(temp))
            self.assertFalse((Path(temp) / 'arcturus.zip').exists())

    def test_patch_rejects_changed_upstream_source(self):
        with self.assertRaisesRegex(RuntimeError, 'Unexpected Arcturus source'):
            setup.patch_pipeline('unrelated source')

    @unittest.skipUnless((setup.RUNTIME / 'upstream/av1-3dgs-pipeline-bde1c9812/av1_colmap.py').is_file(), 'Run Setup.cmd')
    def test_installed_adapter_matches_pinned_patch(self):
        original = (setup.RUNTIME / 'upstream/av1-3dgs-pipeline-bde1c9812/av1_colmap.py').read_text(encoding='utf-8')
        installed = (setup.PIPELINE / 'av1_colmap.py').read_text(encoding='utf-8')
        self.assertEqual(installed, setup.patch_pipeline(original))

    def test_release_has_only_public_files_and_imports_after_relocation(self):
        with tempfile.TemporaryDirectory(prefix='frame splat test ') as temp:
            root = Path(temp)
            archive = build_share.build_release(root)
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                self.assertEqual(set(names), {'frame-splat-to-reso/' + name for name in build_share.PUBLIC_FILES})
                self.assertFalse(any('/.local/' in name or '/.runtime/' in name or name.endswith('.mp4') for name in names))
                bundle.extractall(root / 'relocated')
            moved = root / 'relocated/frame-splat-to-reso'
            command = 'from pathlib import Path; import config,flow,app; assert config.ROOT==Path.cwd(); assert config.OUTPUTS==Path.cwd()/"outputs"; assert config.preferences()=={}'
            subprocess.run([sys.executable, '-c', command], cwd=moved, check=True)


if __name__ == '__main__':
    unittest.main()
