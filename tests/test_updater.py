import pathlib,sys,hashlib,json,tempfile,os,time
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
import wuwa_switcher as ws,wuwa_update as u
from test_switcher import fixture

def md5(b):return hashlib.md5(b).hexdigest()
def make_channel(region,files):
    rows=[]
    for dest,b in files.items():
        row={'dest':dest,'size':len(b),'md5':md5(b).lstrip('0')}
        if len(b)>1024**2:
            row['chunkInfos']=[{'start':o,'end':min(len(b),o+1024**2)-1,'md5':md5(b[o:o+1024**2]).lstrip('0')} for o in range(0,len(b),1024**2)]
        rows.append(row)
    return {'region':region,'version':'9.9.0','entries':rows,'index_md5':'a'*32,'cdn':'https://example.aki-game.com/','cdns':['https://example.aki-game.com/'],'base_url':'files'}

def network_update(name,crash=False,automatic=False):
    b,old=fixture(name);b.install();b.enable_fast_cache()
    sdk='Client/Binaries/Win64/Client-Win64-Shipping.exe'
    primary_sdk=b.roots['GLOBAL']/sdk;secondary_sdk=b.roots['CN']/sdk
    for p,data in [(primary_sdk,b'Steam-specific executable'),(secondary_sdk,b'old CN executable')]:p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    account=b.roots['CN']/'Client/Binaries/Win64/username_kurodata';account.write_bytes(b'account untouched')
    if automatic:b.switch('CN')
    else:u.prepare_update(b)
    content_global={
        'HD/equal.pak':b'new shared data'*1000,
        'Paks/delta.pak':b'new shared resource'*10000+b'GLOBAL new index',
        'Paks/full.pak':b'A'*(17*1024**2),
        'HD/shrink.pak':b'new data'*13000+b'GLOBAL tail'}
    content_cn={**content_global,'Paks/delta.pak':b'new shared resource'*10000+b'CN     new index',
                 'Paks/full.pak':b'B'*(17*1024**2),'HD/shrink.pak':b'new data'*13000+b'CN tail'}
    files={'GLOBAL':{'Client/Content/'+k:v for k,v in content_global.items()},'CN':{'Client/Content/'+k:v for k,v in content_cn.items()}}
    files['GLOBAL'][sdk]=b'official non-Steam executable';files['CN'][sdk]=b'new official CN executable'
    for rel,data in content_global.items():(b.active/rel).write_bytes(data)
    channels={r:make_channel(r,x) for r,x in files.items()}
    old_fetch=u.fetch_channel;old_range=u.Reconstructor.range_get;old_replace=os.replace;old_reconstruct=u.Reconstructor.reconstruct
    def checked_reconstruct(self,entry,source,destination=None,**kwargs):
        if entry['dest'].startswith('Client/Content/'):assert destination is None
        return old_reconstruct(self,entry,source,destination,**kwargs)
    u.Reconstructor.reconstruct=checked_reconstruct
    u.fetch_channel=lambda region:channels[region]
    def local_range(self,entry,start,end):
        data=files[self.channel['region']][entry['dest']][start:end+1]
        self.stats['downloaded_bytes']+=len(data);self.stats['requests']+=1;return data
    u.Reconstructor.range_get=local_range
    def fail_replace(src,dst):
        if pathlib.Path(dst)==secondary_sdk:raise RuntimeError('simulated crash while swapping CN SDK')
        return old_replace(src,dst)
    if crash:os.replace=fail_replace
    try:
        if crash:
            try:u.sync_updated_primary(b)
            except RuntimeError as e:assert 'simulated crash' in str(e)
            else:raise AssertionError('Expected commit interruption')
            assert (b.vault/'update-journal.json').exists()
            assert primary_sdk.read_bytes()==b'Steam-specific executable'
            os.replace=old_replace;u.finish_commit(b)
        else:u.sync_updated_primary(b)
        assert b.state['phase']=='installed' and b.state['base_version']=='9.9.0'
        assert primary_sdk.read_bytes()==b'Steam-specific executable'
        assert secondary_sdk.read_bytes()==b'new official CN executable'
        assert account.read_bytes()==b'account untouched'
        assert b.state['archived_original_backups'] and pathlib.Path(b.state['archived_original_backups'][0]).is_dir()
        b.switch('CN')
        for rel,data in content_cn.items():assert (b.active/rel).read_bytes()==data,rel
        b.switch('GLOBAL')
        for rel,data in content_global.items():assert (b.active/rel).read_bytes()==data,rel
        assert not (b.vault/'update-journal.json').exists()
        assert not list(b.vault.glob('profiles.previous-*'))
        b.restore_independent()
    finally:u.fetch_channel=old_fetch;u.Reconstructor.range_get=old_range;u.Reconstructor.reconstruct=old_reconstruct;os.replace=old_replace

def corrupt_download():
    root=pathlib.Path(tempfile.mkdtemp(prefix='corruption-',dir=ROOT/'.tmp'))
    source=root/'source';source.write_bytes(b'local unrelated bytes');target=root/'target'
    original=b'official target data';entry={'dest':'Client/Content/Paks/test.sig','size':len(original),'md5':md5(original)}
    planner=u.Reconstructor(make_channel('CN',{}),log=lambda s:None)
    planner.range_get=lambda *args:b'corrupt payload!!!!!'[:len(original)]
    try:planner.reconstruct(entry,source,target)
    except RuntimeError:pass
    else:raise AssertionError('corrupt payload accepted')
    assert not target.exists() and source.read_bytes()==b'local unrelated bytes'

def cached_switch_crash():
    b,c=fixture('cache_rename_crash');b.install();b.enable_fast_cache()
    original=os.rename
    def crash(src,dst):
        original(src,dst)
        if pathlib.Path(src)==b.active/'Paks/full.pak':raise RuntimeError('simulated interrupted fast rename')
    os.rename=crash
    try:
        try:b.switch('CN')
        except RuntimeError:pass
    finally:os.rename=original
    b=ws.Backend(b.data,log=lambda s:None);b.switch('GLOBAL')
    for rel,data in c['GLOBAL'].items():assert (b.active/rel).read_bytes()==data
    b.restore_independent()

if __name__=='__main__':
    assert u.official_url('https://example.aki-game.com/','a/Wuthering Waves.exe')=='https://example.aki-game.com/a/Wuthering%20Waves.exe'
    print('Official URL spaces encoded correctly PASS',flush=True)
    network_update('major_version_update');print('Single-primary major update and SDK preservation PASS',flush=True)
    network_update('commit_crash',True);print('Cross-volume-style SDK commit crash recovery PASS',flush=True)
    network_update('automatic_Steam_update',automatic=True);print('Automatic Steam update while CN selected avoids old delta writes PASS',flush=True)
    corrupt_download();print('Corrupt official range rejected without changing originals PASS',flush=True)
    cached_switch_crash();print('Interrupted fast-cache rename recovery PASS',flush=True)
