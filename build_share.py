"""Build a source release without private settings, scans or dependencies."""
from datetime import datetime
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
PUBLIC_FILES = (
    'README.md', 'LICENSE', 'THIRD_PARTY.md', 'DEVELOPING.md', 'Guide.html',
    '.gitignore', '.gitattributes', 'requirements.txt', 'app.py', 'app.ico',
    'config.py', 'flow.py', 'setup.py', 'build_share.py', 'Setup.cmd', 'Start.cmd',
    'Build share zip.cmd', 'tests/test_flow.py', 'tests/test_setup.py',
    'docs/interface.jpg',
)


def build_release(destination=None):
    destination = Path(destination) if destination else ROOT / 'dist'
    destination.mkdir(parents=True, exist_ok=True)
    name = 'frame-splat-to-reso-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.zip'
    output = destination / name
    missing = [name for name in PUBLIC_FILES if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(f'Release files missing: {missing}')
    with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in PUBLIC_FILES:
            bundle.write(ROOT / name, 'frame-splat-to-reso/' + name)
    return output


if __name__ == '__main__':
    print(build_release())
