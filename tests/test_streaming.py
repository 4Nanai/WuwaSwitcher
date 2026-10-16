import pathlib,sys,tempfile,hashlib,json,shutil
from unittest.mock import patch
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
import wuwa_update as u,wuwa_switcher as ws
from test_updater import make_channel
root=pathlib.Path(tempfile.mkdtemp(prefix='stream-',dir=ROOT/'.tmp'))

def case(name,a,b,forced=False):
    folder=root/name;folder.mkdir();data=folder/'data';data.mkdir();profiles=folder/'profiles';profiles.mkdir()
    source=folder/'source';source.write_bytes(a)
    channel=make_channel('CN',{'Client/Content/test.bin':b});entry=channel['entries'][0]
    # Non-64KiB-aligned official boundaries exercise reblocking.
    entry['chunkInfos']=[{'start':i,'end':min(len(b),i+100003)-1,'md5':hashlib.md5(b[i:i+100003]).hexdigest()} for i in range(0,len(b),100003)]
    primary={'size':len(a),'md5':hashlib.md5(a).hexdigest()}
    planner=u.Reconstructor(channel,lambda _:None);requests=[]
    def remote(e,s,t):requests.append((s,t));return b[s:t+1]
    planner.range_get=remote
    row=u.stream_resource(planner,'test.bin',source,entry,primary,data,profiles,folder/'receipts',force_full=forced)
    assert row['sha256']=={'GLOBAL':hashlib.sha256(a).hexdigest(),'CN':hashlib.sha256(b).hexdigest()}
    assert source.read_bytes()==a
    if row['strategy']=='delta':
        copy=folder/'copy';copy.write_bytes(a);ws.apply_delta(copy,data/row['patches']['CN'],len(b));assert copy.read_bytes()==b
        ws.apply_delta(copy,data/row['patches']['GLOBAL'],len(a));assert copy.read_bytes()==a
    elif row['strategy']=='full':assert (profiles/'CN/test.bin').read_bytes()==b
    else:assert a==b
    count=len(requests)
    assert u.stream_resource(planner,'test.bin',source,entry,primary,data,profiles,folder/'receipts',force_full=forced)==row
    assert len(requests)==count
    if row['strategy']=='delta':
        (data/row['patches']['CN']).write_bytes(b'bad')
        u.stream_resource(planner,'test.bin',source,entry,primary,data,profiles,folder/'receipts',force_full=forced)
        assert ws.sha(data/row['patches']['CN'])==row['patch_sha256']['CN']
    assert not list(folder.rglob('*.streaming'))
    return row,folder,planner,entry,primary,source

for name,a,b in [('empty',b'',b''),('grow',b'a'*70001,b'a'*70001+b'grow'*40000),('shrink',b'a'*200001,b'a'*70001),('to_empty',b'x'*70001,b''),('from_empty',b'',b'q'*80001),('sparse',b'a'*400001,b'a'*200001+b'Z'+b'a'*200000)]:
    case(name,a,b);print(name,'PASS',flush=True)
case('forced',b'a'*200001,b'a'*100001+b'b'*100000,True)
# An unanticipated full-cache candidate promotes after emitted prefix fragments.
row,folder,planner,entry,primary,source=case('adaptive',b'a'*(17*1024**2),b'b'*(17*1024**2))
assert row['strategy']=='full'

print('Adaptive prefix reconstruction, receipts and corrupt receipt regeneration PASS',flush=True)
# Each chunk is valid but the final file MD5 is intentionally wrong.
folder=root/'badwhole';folder.mkdir();data=folder/'data';data.mkdir();profiles=folder/'profiles';profiles.mkdir()
source=folder/'source';source.write_bytes(b'aaa');target=b'bbb';entry={'dest':'test.bin','size':3,'md5':hashlib.md5(b'xxx').hexdigest(),'chunkInfos':[{'start':0,'end':2,'md5':hashlib.md5(target).hexdigest()}]}
planner=u.Reconstructor(make_channel('CN',{}),lambda _:None);planner.range_get=lambda *args:target
try:u.stream_resource(planner,'test.bin',source,entry,{'md5':hashlib.md5(b'aaa').hexdigest(),'size':3},data,profiles,folder/'receipts',force_full=True)
except RuntimeError as e:assert 'MD5' in str(e)
else:raise AssertionError('whole-file corruption accepted')
assert not list((folder/'receipts').glob('*.json')) and not (profiles/'CN/test.bin').exists() and not list(folder.rglob('*.streaming'))
print('Whole-file failure never publishes cache or receipt PASS',flush=True)
with patch.object(u.shutil,'disk_usage',return_value=shutil._ntuple_diskusage(10**9,10**9-1024,1024)):
    try:u.require_space(root,1)
    except RuntimeError:pass
    else:raise AssertionError('low space allowed')
print('Incremental free-space guard PASS',flush=True)
# Interrupt a file after one valid chunk: no receipt, no published full cache.
folder=root/'interrupted';folder.mkdir();data=folder/'data';data.mkdir();profiles=folder/'profiles';profiles.mkdir()
source=folder/'source';source.write_bytes(b'a'*200006);target=b'b'*200006
entry={'dest':'test.bin','size':len(target),'md5':hashlib.md5(target).hexdigest(),
       'chunkInfos':[{'start':i,'end':i+100002,'md5':hashlib.md5(target[i:i+100003]).hexdigest()} for i in (0,100003)]}
planner=u.Reconstructor(make_channel('CN',{}),lambda _:None)
def interrupted(e,s,t):
    if s:raise RuntimeError('interrupted')
    return target[s:t+1]
planner.range_get=interrupted
try:u.stream_resource(planner,'test.bin',source,entry,{'md5':hashlib.md5(source.read_bytes()).hexdigest(),'size':source.stat().st_size},data,profiles,folder/'receipts',force_full=True)
except RuntimeError as e:assert str(e)=='interrupted'
else:raise AssertionError('interruption ignored')
assert not list((folder/'receipts').glob('*.json')) and not list(folder.rglob('*.streaming'))
assert source.read_bytes()==b'a'*200006
planner.range_get=lambda e,s,t:target[s:t+1]
u.stream_resource(planner,'test.bin',source,entry,{'md5':hashlib.md5(source.read_bytes()).hexdigest(),'size':source.stat().st_size},data,profiles,folder/'receipts',force_full=True)
assert (profiles/'CN/test.bin').read_bytes()==target
print('Interrupted stream remains unpublished and retries safely PASS',flush=True)
(ROOT/'.tmp/streaming-tests.json').write_text(json.dumps({'result':'PASS','cases':12,'root':str(root)}),encoding='utf-8')

