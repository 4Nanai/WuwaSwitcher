"""First-run bundle generation changes no original game files."""
import pathlib
import sys

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
from test_switcher import fixture
import wuwa_setup as setup
import wuwa_switcher as ws

backend,original=fixture('local-setup')
destination=backend.data.parent/'generated'
setup.create_bundle(backend.roots['GLOBAL'],backend.roots['CN'],destination,lambda _:None)
generated=ws.Backend(destination,lambda _:None)
for region,files in original.items():
    for relative,content in files.items():assert (backend.roots[region]/'Client/Content'/relative).read_bytes()==content
generated.install();generated.enable_fast_cache();generated.switch('CN')
for relative,content in original['CN'].items():assert (generated.active/relative).read_bytes()==content
generated.restore_independent()
try:setup.create_bundle(backend.roots['GLOBAL'],backend.roots['CN'],destination,lambda _:None)
except RuntimeError as exc:assert '已存在' in str(exc)
else:raise AssertionError('Existing bundle was overwritten')
print('Local bundle generation, install, switch, restore and overwrite refusal PASS')

# Reuse an installed vault on first run without generating a second bundle.
from unittest.mock import patch, MagicMock
existing,_=fixture('existing-install')
existing.install()
config_dir=existing.data.parent/'app';config_dir.mkdir()
with patch.object(ws,'APP_DIR',config_dir),patch.object(ws,'DEFAULT_DATA',config_dir/'missing'),patch.object(setup.tk,'Tk',return_value=MagicMock()),patch.object(setup.filedialog,'askdirectory',return_value=str(existing.roots['GLOBAL'])) as ask:
    selected=setup.ensure_bundle()
    assert selected.resolve()==(existing.vault/'data').resolve()
    assert ws.Backend(selected,lambda _:None).state['phase']=='installed'
    assert ask.call_count==1
existing.restore_independent()
print('Existing installation first-run discovery PASS')
