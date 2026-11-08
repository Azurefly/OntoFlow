"""Browser state regression for reopening workspaces at their persisted stage."""
from pathlib import Path
import shutil
import subprocess
import pytest


def test_frontend_stage_restore():
    node=shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable; frontend VM regression is run separately when installed')
    script=Path(__file__).with_name('stage_restore_v124.cjs')
    r=subprocess.run([node,str(script)],capture_output=True,text=True,timeout=12,check=False)
    assert r.returncode==0, r.stdout+'\n'+r.stderr
