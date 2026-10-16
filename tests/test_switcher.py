import pathlib, sys, tempfile, json, hashlib, os, shutil
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parent))
import wuwa_switcher as ws
from wuwa_update import compare_pair
def scan_pair(rel,a,b,out):return compare_pair(rel.as_posix(),a,b,out)

ws.idle_guard=lambda:None
BASE=pathlib.Path(__file__).resolve().parents[1]/'.tmp'
BASE.mkdir(exist_ok=True)
BASE=pathlib.Path(tempfile.mkdtemp(prefix='run-',dir=BASE))

def fixture(name):
    base=BASE/name
    if base.exists():raise RuntimeError('Use a new test fixture name')
    roots={r:base/r for r in ws.REGIONS};data=base/'data';data.mkdir(parents=True)
    contents={'GLOBAL':{'HD/equal.pak':b'shared'*1000,'Paks/delta.pak':b'A'*200000+b'global-index'+b'X'*70000,'Paks/full.pak':b'A'*200000,'HD/shrink.pak':b'A'*100000+b'old'*4000},
              'CN':{'HD/equal.pak':b'shared'*1000,'Paks/delta.pak':b'A'*200000+b'cn-index----'+b'X'*70000,'Paks/full.pak':b'B'*200000,'HD/shrink.pak':b'A'*100000+b'new'}}
    for r,files in contents.items():
        for rel,b in files.items():
            p=roots[r]/'Client'/'Content'/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
    rows=[]
    for rel in contents['GLOBAL']:
        p=pathlib.Path(rel);row=scan_pair(p,roots['GLOBAL']/'Client'/'Content'/p,roots['CN']/'Client'/'Content'/p,data)
        if rel=='Paks/full.pak':
            for fn in row.get('patches',{}).values():(data/fn).unlink()
            row['strategy']='full';row.pop('patches',None);row.pop('patch_sizes',None)
        elif row['strategy']=='delta':row['patch_sha256']={r:ws.sha(data/row['patches'][r]) for r in ws.REGIONS}
        rows.append(row)
    manifest={'schema':1,'roots':{r:str(p.absolute()) for r,p in roots.items()},'files':rows,'summary':{'estimated_saved_bytes':0}}
    (data/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    b=ws.Backend(data,log=lambda s:None)
    return b,contents

def exact(b,c,r):
    for rel,v in c[r].items():assert (b.active/rel).read_bytes()==v,(r,rel)

def scenario_roundtrip():
    b,c=fixture('roundtrip');b.install();assert ws.is_junction(b.cn);exact(b,c,'GLOBAL')
    b.switch('CN');exact(b,c,'CN');b.switch('GLOBAL');exact(b,c,'GLOBAL')
    b.release_backup();assert not b.backup.exists();b.switch('CN');exact(b,c,'CN')
    b.restore_independent();exact(b,c,'GLOBAL');assert not ws.is_junction(b.cn)
    for rel,v in c['CN'].items():assert (b.cn/rel).read_bytes()==v

def scenario_crash_delta():
    b,c=fixture('crash_delta');b.install();original=ws.apply_delta
    def fail(path,patch,size):
        with pathlib.Path(path).open('r+b') as f:
            off,v=next(ws.patch_records(patch));f.seek(off);f.write(v)
        raise RuntimeError('simulated power loss')
    ws.apply_delta=fail
    try:
        try:b.switch('CN')
        except RuntimeError:pass
        assert b.journal_path.exists()
    finally:ws.apply_delta=original
    b=ws.Backend(b.data,log=lambda s:None);b.switch('GLOBAL');exact(b,c,'GLOBAL');assert not b.journal_path.exists()
    b.restore_independent()

def scenario_crash_rename():
    b,c=fixture('crash_rename');b.install();original=os.rename
    def fail(src,dst):
        original(src,dst)
        if pathlib.Path(src)==b.active/'Paks/full.pak':raise RuntimeError('simulated power loss between moves')
    os.rename=fail
    try:
        try:b.switch('CN')
        except RuntimeError:pass
        assert not (b.active/'Paks/full.pak').exists()
    finally:os.rename=original
    b=ws.Backend(b.data,log=lambda s:None);b.switch('CN');exact(b,c,'CN');b.restore_independent()

def scenario_modified_blocked():
    b,c=fixture('modified_blocked');b.install()
    p=b.active/'HD/equal.pak';p.write_bytes(b'changed')
    try:b.switch('CN')
    except RuntimeError as e:assert '更新或修改' in str(e)
    else:raise AssertionError('modified resources were accepted')
    p.write_bytes(c['GLOBAL']['HD/equal.pak']);b.state['snapshots']=b.snapshots();b.save_state();b.restore_independent()

def scenario_projected_and_tamper():
    b,c=fixture('projected')
    for row in b.rows:
        if row['strategy']=='delta':
            for target in ws.REGIONS:
                source='CN' if target=='GLOBAL' else 'GLOBAL'
                path=b.roots[source]/'Client/Content'/row['relative']
                assert ws.projected_hash(path,b.patch_path(row,target),row['sizes'][target])==row['sha256'][target]
    row=next(r for r in b.rows if r['strategy']=='delta');p=b.patch_path(row,'CN');p.write_bytes(p.read_bytes()+b'tampered')
    try:b.check_patches()
    except RuntimeError:pass
    else:raise AssertionError('corrupt patch accepted')

def scenario_vcdiff():
    import pyxdelta
    b,c=fixture('vcdiff');manifest=b.manifest
    r=next(r for r in b.rows if r['strategy']=='full')
    r['strategy']='vcdiff';r['patches']={};r['patch_sizes']={};r['patch_sha256']={}
    for target in ws.REGIONS:
        source='CN' if target=='GLOBAL' else 'GLOBAL';name='full.'+target+'.vcdiff'
        assert pyxdelta.run(str(b.roots[source]/'Client/Content'/r['relative']),str(b.roots[target]/'Client/Content'/r['relative']),str(b.data/name))
        r['patches'][target]=name;r['patch_sizes'][target]=(b.data/name).stat().st_size;r['patch_sha256'][target]=ws.sha(b.data/name)
    ws.atomic_json(b.data/'manifest.json',manifest)
    b=ws.Backend(b.data,log=lambda s:None);b.install()
    original=ws.decode_vcdiff
    def fail(source,patch,target):
        pathlib.Path(target).write_bytes(b'incomplete');raise RuntimeError('simulated interrupted reconstruction')
    ws.decode_vcdiff=fail
    try:
        try:b.switch('CN')
        except RuntimeError:pass
        assert (b.active/r['relative']).read_bytes()==c['GLOBAL'][r['relative']]
    finally:ws.decode_vcdiff=original
    b.switch('CN');exact(b,c,'CN');b.switch('GLOBAL');exact(b,c,'GLOBAL')
    b.release_backup();b.switch('CN');b.restore_independent();exact(b,c,'GLOBAL')
    for rel,v in c['CN'].items():assert (b.cn/rel).read_bytes()==v

def scenario_restore_detach_crash():
    b,c=fixture('detach_crash');b.install();original=os.rename
    def fail(src,dst):
        original(src,dst)
        if pathlib.Path(src)==b.backup:raise RuntimeError('simulated crash after restored directory renamed')
    os.rename=fail
    try:
        try:b.restore_independent()
        except RuntimeError:pass
    finally:os.rename=original
    assert b.state['phase']=='restoring_detach'
    b=ws.Backend(b.data,log=lambda s:None);b.restore_independent();assert b.state['phase']=='uninstalled'

if __name__=='__main__':
    for scenario in [scenario_roundtrip,scenario_crash_delta,scenario_crash_rename,scenario_modified_blocked,scenario_projected_and_tamper,scenario_vcdiff,scenario_restore_detach_crash]:
        scenario();print(scenario.__name__,'PASS',flush=True)
