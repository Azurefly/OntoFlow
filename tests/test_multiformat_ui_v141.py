from pathlib import Path
import subprocess
import shutil
import pytest

def test_real_frontend_formats_and_partial_upload_script():
    node=shutil.which('node')
    if not node:pytest.skip('Node.js not installed; requires independent frontend validation')
    result=subprocess.run([node,str(Path(__file__).with_name('multiformat_ui_v141.cjs'))],capture_output=True,text=True,timeout=12)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
