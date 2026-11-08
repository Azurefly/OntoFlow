from pathlib import Path
import subprocess
import shutil
import pytest


def test_cross_page_and_conflict_ui():
    node=shutil.which('node')
    if not node:pytest.skip('Node not installed; frontend VM test requires Node')
    result=subprocess.run([node,str(Path(__file__).with_name('review_all_ui_v139.cjs'))],
                          text=True,capture_output=True,timeout=15)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
