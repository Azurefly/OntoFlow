from pathlib import Path
import shutil,subprocess
import pytest

def test_chinese_labels_and_code_fields_use_actual_frontend():
    node=shutil.which('node')
    if not node:pytest.skip('Frontend VM requires Node.js')
    r=subprocess.run([node,str(Path(__file__).with_name('naming_ui_v1310.cjs'))],
        text=True,capture_output=True,timeout=12)
    assert r.returncode==0,r.stdout+'\n'+r.stderr
