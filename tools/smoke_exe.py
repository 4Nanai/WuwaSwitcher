"""Exercise the packaged backend against synthetic files, without a game install."""
import json
import os
import pathlib
import subprocess
import sys

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from test_switcher import fixture

backend,_=fixture('packaged-smoke')
report=ROOT/'.tmp/packaged-self-check.json'
env={**os.environ,'WUWA_SWITCHER_DATA':str(backend.data)}
subprocess.run([str(ROOT/'dist/WuwaSwitcher.exe'),'--self-check',str(report)],env=env,check=True,timeout=90)
result=json.loads(report.read_text(encoding='utf-8'))
assert result['result']=='PASS' and result['files']==4,result
print('Packaged EXE synthetic bundle self-check PASS')

# Ensure the native decoder and its dependencies were bundled, too.
import pyxdelta
source=backend.roots['GLOBAL']/'Client/Content/Paks/delta.pak'
target=backend.roots['CN']/'Client/Content/Paks/delta.pak'
patch=ROOT/'.tmp/packaged-native.vcdiff'
decoded=ROOT/'.tmp/packaged-native.bin'
assert pyxdelta.run(str(source),str(target),str(patch))
subprocess.run([str(ROOT/'dist/WuwaSwitcher.exe'),'--decode',str(source),str(patch),str(decoded)],check=True,timeout=90)
assert decoded.read_bytes()==target.read_bytes()
print('Packaged EXE native decoder PASS')
