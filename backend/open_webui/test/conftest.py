import os
import shutil
import sys
import tempfile
from pathlib import Path

# Make `open_webui` importable when pytest runs from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# open_webui reads these at import time: env.py exits without a secret key, and
# importing open_webui.config deletes every file in STATIC_DIR (it resyncs the
# frontend's static assets), which would wipe the tracked open_webui/static.
# Always use throwaway data and static directories.
_tmp = Path(tempfile.mkdtemp(prefix='open-webui-test-'))
(_tmp / 'data').mkdir()
(_tmp / 'static').mkdir()  # main mounts it as StaticFiles, which requires it to exist
os.environ.setdefault('WEBUI_SECRET_KEY', 'test-secret-key')
os.environ['DATA_DIR'] = str(_tmp / 'data')
os.environ['STATIC_DIR'] = str(_tmp / 'static')


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_tmp, ignore_errors=True)
