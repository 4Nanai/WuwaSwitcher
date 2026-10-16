"""Official-CDN regional synchronization; reuses verified primary-client bytes."""
from __future__ import annotations
import contextlib, gzip, hashlib, io, json, mmap, os, pathlib, re, shutil, struct, time, urllib.parse, urllib.request, uuid, zlib
import wuwa_switcher as ws

INDEX_URLS={
 'GLOBAL':'https://prod-alicdn-gamestarter.kurogame.com/launcher/game/G153/50004_obOHXFrFanqsaIEOmuKroCcbZkQRBC7c/index.json',
 'CN':'https://prod-cn-alicdn-gamestarter.kurogame.com/launcher/game/G152/10003_Y8xXrXk65DqFHEDgApn3cpK5lfczpFx5/index.json'}
ALLOWED_DOMAINS=('aki-game.com','aki-game.net','kurogame.com','kurogame.xyz')

def official_url(base,path=''):
    u=urllib.parse.urljoin(base.rstrip('/')+'/',path) if path else base
    p=urllib.parse.urlparse(u)
    if p.scheme!='https' or not any(p.hostname==d or (p.hostname or '').endswith('.'+d) for d in ALLOWED_DOMAINS):
        raise RuntimeError('下载清单提供了非官方 HTTPS 地址')
    return urllib.parse.urlunparse(p._replace(path=urllib.parse.quote(p.path,safe='/%')))

def md5_bytes(b):return hashlib.md5(b).hexdigest()
def normalize_md5(value):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-fA-F]{1,32}',value):raise RuntimeError('MD5 清单格式错误')
    return value.lower().zfill(32)
def md5_file(p):
    with pathlib.Path(p).open('rb') as f:return hashlib.file_digest(f,'md5').hexdigest()
def matches_file(p,entry):
    return pathlib.Path(p).is_file() and pathlib.Path(p).stat().st_size==entry['size'] and md5_file(p)==normalize_md5(entry['md5'])

def read_json_url(url):
    with urllib.request.urlopen(urllib.request.Request(official_url(url),headers={'Accept-Encoding':'identity'}),timeout=40) as r:
        b=r.read(16*1024*1024+1)
    if len(b)>16*1024*1024:raise RuntimeError('官方清单超出大小限制')
    raw=b
    if b[:2]==b'\x1f\x8b':
        with gzip.GzipFile(fileobj=io.BytesIO(b)) as f:b=f.read(32*1024*1024+1)
    if len(b)>32*1024*1024:raise RuntimeError('解压后的清单超出大小限制')
    return json.loads(b.decode('utf-8-sig')),b,raw

def fetch_channel(region):
    index,_,_=read_json_url(INDEX_URLS[region]);d=index['default'];config=d['config']
    version=str(config['version'])
    if not re.fullmatch(r'\d+\.\d+\.\d+',version):raise RuntimeError('官方版本号格式变化')
    cdns=[x['url'] for x in sorted(d['cdnList'],key=lambda c:c.get('P',0),reverse=True)]
    errors=[]
    for cdn in cdns:
        try:
            entries,b,raw=read_json_url(official_url(cdn,config['indexFile']))
            expected=normalize_md5(config['indexFileMd5'])
            if expected not in (md5_bytes(b),md5_bytes(raw)):raise RuntimeError('官方文件清单 MD5 不符')
            rows=entries['resource']
            if not isinstance(rows,list) or not 1<=len(rows)<=30000:raise RuntimeError('官方文件清单数量异常')
            seen=set()
            for r in rows:
                # Validate paths before any file access or downloads.
                ws.safe_child(pathlib.Path.cwd(),r['dest'])
                if r['dest'] in seen or not isinstance(r['size'],int) or not 0<=r['size']<=256*1024**3:raise RuntimeError('官方资源条目异常')
                seen.add(r['dest']);normalize_md5(r['md5'])
                chunks=r.get('chunkInfos') or []
                end=-1
                for c in chunks:
                    if c['start']!=end+1 or c['end']<c['start'] or c['end']>=r['size']:raise RuntimeError('官方分块范围异常')
                    end=c['end'];normalize_md5(c['md5'])
                if chunks and end+1!=r['size']:raise RuntimeError('官方分块未覆盖完整资源')
            return {'region':region,'version':version,'cdn':cdn,'cdns':cdns,'base_url':config.get('baseUrl',d['resourcesBasePath']),
                    'entries':rows,'index':index,'index_md5':expected,'fetched_at':time.strftime('%Y-%m-%d %H:%M:%S')}
        except Exception as e:errors.append(str(e))
    raise RuntimeError('获取官方文件清单失败：'+'; '.join(errors))

class Reconstructor:
    def __init__(self,channel,log=print):
        self.channel=channel;self.log=log
        self.stats={'downloaded_bytes':0,'reused_bytes':0,'shifted_reused_bytes':0,'requests':0}
    def range_get(self,entry,start,end):
        if start<0 or end<start or end>=entry['size']:raise RuntimeError('请求范围超出官方文件')
        expected=end-start+1;errors=[]
        for cdn in self.channel['cdns']:
            try:
                url=official_url(cdn,self.channel['base_url'].strip('/')+'/'+entry['dest'])
                req=urllib.request.Request(url,headers={'Range':f'bytes={start}-{end}','Accept-Encoding':'identity'})
                with urllib.request.urlopen(req,timeout=60) as r:
                    if r.status==206:
                        if r.headers.get('Content-Range')!=f'bytes {start}-{end}/{entry["size"]}':raise RuntimeError('CDN 返回的分块范围不符')
                    elif r.status!=200 or start!=0 or end+1!=entry['size']:raise RuntimeError('CDN 未支持指定范围下载')
                    b=r.read(expected+1)
                if len(b)!=expected:raise RuntimeError('下载分块长度不符')
                self.stats['downloaded_bytes']+=len(b);self.stats['requests']+=1
                return b
            except Exception as e:errors.append(str(e))
        raise RuntimeError('范围下载失败：'+entry['dest']+'; '+'; '.join(errors))
    def pak_tail(self,entry):
        if not entry['dest'].endswith('.pak') or entry['size']<512:return None
        b=self.range_get(entry,entry['size']-512,entry['size']-1)
        pos=b.rfind(b'\xe1\x12\x6f\x5a')
        if pos<17 or pos+44>len(b):return None
        version,offset,size=struct.unpack_from('<iqq',b,pos+4)
        if not 3<=version<=13 or offset<0 or size<0 or offset+size>entry['size'] or entry['size']-offset>64*1024**2:return None
        return offset,self.range_get(entry,offset,entry['size']-1)
    def reconstruct(self,entry,source,destination=None,extra_sources=(),sink=None):
        source=pathlib.Path(source);destination=pathlib.Path(destination) if destination is not None else None
        if destination is not None:
            destination.parent.mkdir(parents=True,exist_ok=True)
            if matches_file(destination,entry):self.log('续传复用：'+entry['dest']);return
        sources=[]
        for p in [source]+[pathlib.Path(x) for x in extra_sources]:
            if p.is_file() and p not in sources:sources.append(p)
        chunks=(entry.get('chunkInfos') or [{'start':0,'end':entry['size']-1,'md5':entry['md5']}]) if entry['size'] else []
        tail=None;tail_attempted=False;whole=hashlib.md5();strong=hashlib.sha256()
        temp=destination.with_name(destination.name+'.download') if destination is not None else None
        with (temp.open('wb') if temp is not None else contextlib.nullcontext(None)) as out:
            for chunk in chunks:
                start,end=chunk['start'],chunk['end'];length=end-start+1;expected=normalize_md5(chunk['md5'])
                found=None;shifted=False;overlaid=0
                for p in sources:
                    with p.open('rb') as f:f.seek(start);candidate=f.read(length)
                    if len(candidate)==length and md5_bytes(candidate)==expected:found=candidate;break
                if found is None and sources and entry['dest'].endswith('.pak'):
                    if not tail_attempted:
                        tail=self.pak_tail(entry);tail_attempted=True
                    if tail and end>=tail[0]:
                        for p in sources:
                            with p.open('rb') as f:f.seek(start);candidate=f.read(length)
                            if len(candidate)!=length:continue
                            v=bytearray(candidate);lo=max(start,tail[0]);v[lo-start:]=tail[1][lo-tail[0]:end-tail[0]+1]
                            if md5_bytes(v)==expected:found=v;overlaid=end-lo+1;break
                # A small remote anchor can locate a shifted, otherwise identical
                # block in the primary file. The official whole-block MD5 proves it.
                if found is None and sources and entry['dest'].endswith('.pak') and length>=1024**2:
                    probe_start=start+min(65536,length-65536);probe=self.range_get(entry,probe_start,probe_start+65535)
                    anchor=probe[4096:4160]
                    if len(set(anchor))>=8:
                        for p in sources:
                            with p.open('rb') as f:
                                if p.stat().st_size==0:continue
                                with mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as mm:
                                    lo=max(0,probe_start-128*1024**2);hi=min(len(mm),probe_start+128*1024**2)
                                    match=mm.find(anchor,lo,hi)
                                    if match<0:continue
                                    offset=match-(probe_start-start)-4096
                                    if offset<0 or offset+length>len(mm):continue
                                    candidate=bytearray(mm[offset:offset+length])
                            if tail and end>=tail[0]:
                                t=max(start,tail[0]);candidate[t-start:]=tail[1][t-tail[0]:end-tail[0]+1]
                            if md5_bytes(candidate)==expected:
                                found=candidate;shifted=True
                                if tail and end>=tail[0]:overlaid=end-max(start,tail[0])+1
                                break
                if found is None:
                    self.log(f'下载必要差异：{entry["dest"]} @ {start//1024**2} MiB')
                    found=self.range_get(entry,start,end)
                    if md5_bytes(found)!=expected:raise RuntimeError('官方分块 MD5 校验失败：'+entry['dest'])
                else:
                    self.stats['reused_bytes']+=length-overlaid
                    if shifted:self.stats['shifted_reused_bytes']+=length
                if sink is not None:sink(start,found)
                if out is not None:
                    require_space(destination.parent,len(found));out.write(found)
                whole.update(found);strong.update(found)
            if out is not None:out.flush();os.fsync(out.fileno())
        if (temp is not None and temp.stat().st_size!=entry['size']) or whole.hexdigest()!=normalize_md5(entry['md5']):raise RuntimeError('重建整文件 MD5 校验失败：'+entry['dest'])
        if temp is not None:os.replace(temp,destination)
        return {'md5':whole.hexdigest(),'sha256':strong.hexdigest(),'bytes':entry['size']}

def require_space(path,amount):
    if shutil.disk_usage(path).free<amount+512*1024**2:
        raise RuntimeError('磁盘空间不足：需容纳本次新增区域缓存、差异档案和程序组件，并保留 512 MiB 余量；无需预留整套国服资源')


class StreamingDeltaBuilder:
    """Verified target chunks become patches or a runtime cache, never a full mirror."""
    def __init__(self,relative,source,entry,data,profiles,force_full=False):
        self.relative=relative;self.source=pathlib.Path(source);self.entry=entry;self.data=data
        self.before=ws.stamp(self.source);self.fa=self.source.open('rb')
        self.sizes={'GLOBAL':self.before[0],'CN':entry['size']}
        self.hashes={r:hashlib.sha256() for r in ws.REGIONS}
        key=hashlib.sha256(relative.encode()).hexdigest()
        self.names={r:key+'.'+r+'.delta' for r in ws.REGIONS}
        self.files={r:(data/n).open('wb') for r,n in self.names.items()}
        for f in self.files.values():f.write(ws.MAGIC)
        self.cache=ws.safe_child(profiles/'CN',relative)
        self.temp=self.cache.with_name(self.cache.name+'.streaming')
        self.out=None;self.pending=bytearray();self.offset=0;self.received=0;self.different=0;self.space_budget=0
        if force_full:
            try:self.promote()
            except Exception:self.close();raise

    def guard_write(self,amount):
        self.space_budget-=amount
        if self.space_budget<0:
            require_space(self.data,max(amount,16*1024**2));self.space_budget=16*1024**2

    def promote(self):
        if self.out is not None:return
        self.cache.parent.mkdir(parents=True,exist_ok=True)
        require_space(self.cache.parent,self.entry['size'])
        for f in self.files.values():f.flush()
        self.out=self.temp.open('w+b')
        # Recover the already verified prefix from the source plus emitted CN
        # fragments. No second network pass and no full temporary installation.
        with self.source.open('rb') as f:
            left=min(self.offset,self.entry['size'])
            while left:
                size=min(left,4*1024**2);part=f.read(size)
                self.out.write(part+b'\0'*(size-len(part)));left-=size
        for offset,raw in ws.patch_records(self.data/self.names['CN']):
            self.out.seek(offset);self.out.write(raw)
        self.out.truncate(min(self.offset,self.entry['size']));self.out.seek(0)
        check=hashlib.sha256()
        while part:=self.out.read(4*1024**2):check.update(part)
        if check.hexdigest()!=self.hashes['CN'].hexdigest():raise RuntimeError('流式缓存前缀校验失败')
        self.out.seek(min(self.offset,self.entry['size']))

    def block(self,y):
        x=self.fa.read(65536)
        self.hashes['GLOBAL'].update(x)
        if x!=y:
            self.different+=max(len(x),len(y))
            if self.out is None and max(self.sizes.values())>16*1024**2 and self.different>max(self.sizes.values())*.35:
                self.promote()
            if self.out is None:
                for region,v in [('GLOBAL',x),('CN',y)]:
                    if v:
                        compressed=zlib.compress(v,1);self.guard_write(len(compressed)+16)
                        self.files[region].write(struct.pack('<QII',self.offset,len(v),len(compressed)))
                        self.files[region].write(compressed)
        if self.out is not None and y:
            self.guard_write(len(y));self.out.write(y)
        self.hashes['CN'].update(y);self.offset+=max(len(x),len(y))

    def feed(self,start,value):
        if start!=self.received:raise RuntimeError('资源流偏移不连续')
        self.received+=len(value)
        if self.received>self.entry['size']:raise RuntimeError('资源流超出清单大小')
        view=memoryview(value);pos=0
        if self.pending:
            take=min(65536-len(self.pending),len(view));self.pending.extend(view[:take]);pos=take
            if len(self.pending)==65536:self.block(self.pending);self.pending=bytearray()
        while pos+65536<=len(view):self.block(view[pos:pos+65536]);pos+=65536
        self.pending.extend(view[pos:])

    def finish(self,result):
        if self.received!=self.entry['size']:raise RuntimeError('资源流不完整')
        if self.pending:self.block(self.pending);self.pending=bytearray()
        while self.offset<self.sizes['GLOBAL']:self.block(b'')
        if self.hashes['CN'].hexdigest()!=result['sha256']:raise RuntimeError('流式目标 SHA-256 不符')
        if self.before!=ws.stamp(self.source):raise RuntimeError('同步期间主资源被修改')
        for f in self.files.values():f.flush();os.fsync(f.fileno());f.close()
        self.fa.close()
        row={'relative':self.relative,'sizes':self.sizes,'sha256':{r:h.hexdigest() for r,h in self.hashes.items()},
             'strategy':'full' if self.out is not None else 'delta' if self.different else 'identical'}
        if self.out is not None:
            self.out.truncate(self.entry['size']);self.out.flush();os.fsync(self.out.fileno());self.out.close()
            if ws.sha(self.temp)!=result['sha256']:raise RuntimeError('流式区域缓存校验失败')
            os.replace(self.temp,self.cache)
        if row['strategy']=='delta':
            row.update(patches=self.names,patch_sizes={r:(self.data/n).stat().st_size for r,n in self.names.items()},
                       patch_sha256={r:ws.sha(self.data/n) for r,n in self.names.items()})
        else:
            for n in self.names.values():(self.data/n).unlink()
        return row

    def close(self):
        self.fa.close()
        for f in self.files.values():f.close()
        if self.out is not None:self.out.close()
        if self.temp.exists():self.temp.unlink()


def stream_resource(planner,relative,source,entry,primary_entry,data,profiles,receipts,extra_sources=(),force_full=False):
    receipts.mkdir(parents=True,exist_ok=True)
    receipt=receipts/(hashlib.sha256(relative.encode()).hexdigest()+'.json')
    identity={'source':ws.stamp(source),'primary_md5':primary_entry['md5'],'entry':entry,'force_full':force_full}
    if receipt.exists():
        try:
            saved=json.loads(receipt.read_text(encoding='utf-8'));row=saved['row']
            valid=saved['identity']==identity and row['relative']==relative
            if row['strategy']=='delta':
                valid=valid and all(ws.sha(ws.safe_child(data,row['patches'][r]))==row['patch_sha256'][r] for r in ws.REGIONS)
            elif row['strategy']=='full':valid=valid and ws.sha(ws.safe_child(profiles/'CN',relative))==row['sha256']['CN']
            elif row['strategy']!='identical':valid=False
            if valid:planner.log('续传复用已校验差异：'+relative);return row
        except (OSError,ValueError,KeyError):pass
    if entry['size']==primary_entry['size'] and normalize_md5(entry['md5'])==normalize_md5(primary_entry['md5']):
        digest=ws.sha(source)
        row={'relative':relative,'sizes':{r:entry['size'] for r in ws.REGIONS},'sha256':{r:digest for r in ws.REGIONS},'strategy':'identical'}
    else:
        builder=StreamingDeltaBuilder(relative,source,entry,data,profiles,force_full)
        try:
            result=planner.reconstruct(entry,source,extra_sources=extra_sources,sink=builder.feed)
            row=builder.finish(result)
        finally:builder.close()
    ws.atomic_json(receipt,{'identity':identity,'row':row})
    return row


def compare_pair(relative,a,b,data_dir):
    """Emit original regional 64 KiB fragments, or retain a full cached variant."""
    rel=pathlib.Path(relative);sizes={'GLOBAL':a.stat().st_size,'CN':b.stat().st_size}
    initial=[ws.stamp(p) for p in (a,b)];hashes={r:hashlib.sha256() for r in ws.REGIONS}
    patch_names={r:hashlib.sha256(rel.as_posix().encode()).hexdigest()+'.'+r+'.delta' for r in ws.REGIONS}
    files={r:(data_dir/name).open('wb') for r,name in patch_names.items()}
    for f in files.values():f.write(ws.MAGIC)
    offset=0;different=0;full=False
    try:
        with a.open('rb') as fa,b.open('rb') as fb:
            while True:
                x=fa.read(4*1024**2);y=fb.read(4*1024**2)
                if not x and not y:break
                hashes['GLOBAL'].update(x);hashes['CN'].update(y)
                if x!=y:
                    for pos in range(0,max(len(x),len(y)),65536):
                        xx=x[pos:pos+65536];yy=y[pos:pos+65536]
                        if xx==yy:continue
                        different+=max(len(xx),len(yy))
                        if different>max(sizes.values())*.35 and max(sizes.values())>16*1024**2:full=True
                        if not full:
                            for region,v in [('GLOBAL',xx),('CN',yy)]:
                                if v:
                                    compressed=zlib.compress(v,1);files[region].write(struct.pack('<QII',offset+pos,len(v),len(compressed)));files[region].write(compressed)
                offset+=4*1024**2
    finally:
        for f in files.values():f.close()
    if initial!=[ws.stamp(p) for p in (a,b)]:raise RuntimeError('比较过程中资源被更新：'+relative)
    row={'relative':relative,'sizes':sizes,'sha256':{r:h.hexdigest() for r,h in hashes.items()},'strategy':'full' if full else 'delta' if different else 'identical'}
    if full or not different:
        for name in patch_names.values():(data_dir/name).unlink()
    else:
        row['patches']=patch_names;row['patch_sizes']={r:(data_dir/n).stat().st_size for r,n in patch_names.items()}
        row['patch_sha256']={r:ws.sha(data_dir/n) for r,n in patch_names.items()}
    return row

def prepare_update(backend):
    ws.idle_guard()
    if backend.state and backend.state.get('phase')=='updating':backend.log('已处于更新模式；完成 Steam 更新后点击同步国服更新。');return
    backend.switch('GLOBAL');backend.verify_current()
    backend.state['phase']='updating';backend.state['update_primary']='GLOBAL';backend.save_state()
    backend.log('已进入 Steam 更新模式。现在使用 Steam 更新国际服主客户端；完成后点击“同步国服更新”。')

def cancel_update(backend):
    ws.idle_guard()
    if (backend.vault/'update-journal.json').exists():raise RuntimeError('存在待恢复的同步提交，请点击同步国服更新完成恢复')
    if not backend.state or backend.state.get('phase')!='updating':backend.log('当前没有待取消的更新模式。');return
    if backend.snapshots()!=backend.state.get('snapshots'):raise RuntimeError('主资源已更新，须完成国服差异同步，不能恢复旧档案')
    backend.state['phase']='installed';backend.save_state();backend.log('主资源尚未改变，已退出更新模式。')

def validate_primary(backend,channel):
    rows=[r for r in channel['entries'] if r['dest'].startswith('Client/Content/')]
    expected={r['dest'][len('Client/Content/'):] for r in rows}
    actual={p.relative_to(backend.active).as_posix() for p in backend.active.rglob('*') if p.is_file()}
    if expected!=actual:raise RuntimeError('Steam 的资源文件列表与官方最新主版本不同，请先完成 Steam 更新')
    stamps={}
    for n,r in enumerate(rows,1):
        p=ws.safe_child(backend.roots['GLOBAL'],r['dest']);before=ws.stamp(p)
        if not matches_file(p,r) or before!=ws.stamp(p):
            raise RuntimeError('Steam 主资源未匹配官方版本：'+r['dest']+'；先完成 Steam 更新或校验')
        stamps[r['dest']]=before
        if n%20==0:backend.log(f'验证 Steam 主资源：{n}/{len(rows)}')
    return stamps

def sync_updated_primary(backend):
    ws.idle_guard()
    journal=backend.vault/'update-journal.json'
    if journal.exists():finish_commit(backend);return
    if not backend.state or backend.state.get('phase') not in ('updating','installed'):raise RuntimeError('请先安装共用资源')
    if backend.state.get('phase')=='installed':
        if not ws.is_junction(backend.cn) or backend.cn.resolve()!=backend.active.resolve():raise RuntimeError('国服共用资源目录已改变，停止自动同步')
        actual={p.relative_to(backend.active).as_posix() for p in backend.active.rglob('*') if p.is_file()}
        old={r['relative'] for r in backend.rows}
        changed=actual!=old or backend.snapshots()!=backend.state.get('snapshots')
        if changed:
            # Steam may have updated in the background while CN was selected.
            # Never apply the old regional recipe to those newly updated files.
            backend.state.update(phase='updating',active='GLOBAL',update_primary='GLOBAL');backend.save_state()
            backend.log('检测到主资源变化，将先按官方新清单完整验证；不会向新版文件应用旧差异。')
        else:backend.verify_current();prepare_update(backend)
    if backend.state.get('update_primary')!='GLOBAL':raise RuntimeError('目前自动同步以 Steam 国际服为主')
    backend.log('读取两区官方版本和经过 MD5 验证的文件清单…')
    global_channel=fetch_channel('GLOBAL');cn_channel=fetch_channel('CN')
    if global_channel['version']!=cn_channel['version']:raise RuntimeError('两区主版本尚未一致，保留 Steam 主客户端；等版本一致后重试同步')
    version=global_channel['version'];backend.log('官方主版本：'+version)
    ws.atomic_json(backend.vault/'latest-channels.json',{'GLOBAL':global_channel,'CN':cn_channel})
    if backend.manifest.get('base_version')==version and backend.snapshots()==backend.state.get('snapshots'):
        backend.state['phase']='installed';backend.save_state();backend.log('当前主资源已经支持这个官方版本，无需重新同步。');return
    primary_stamps=validate_primary(backend,global_channel)
    gp={r['dest']:r for r in global_channel['entries'] if r['dest'].startswith('Client/Content/')}
    cp={r['dest']:r for r in cn_channel['entries'] if r['dest'].startswith('Client/Content/')}
    if set(gp)!=set(cp):raise RuntimeError('两区资源目录结构发生变化；停止同步，Steam 主客户端保留可用')
    stage=backend.vault/('update-stage-'+version);target=stage/'CN';data=stage/'data';profiles=stage/'profiles'
    for p in (target,data,profiles):p.mkdir(parents=True,exist_ok=True)
    require_space(stage,0)
    planner=Reconstructor(cn_channel,backend.log)
    rows=[];old_rows={r['relative']:r for r in backend.rows}
    backend.log('流式校验并生成双向差异；仅保留运行所需区域缓存，不落盘整套国服正文…')
    for n,dest in enumerate(sorted(cp),1):
        entry=cp[dest];source=ws.safe_child(backend.roots['GLOBAL'],dest)
        rel=dest[len('Client/Content/'):]
        cache=ws.safe_child(backend.vault/'profiles'/'CN',rel)
        row=stream_resource(planner,rel,source,entry,gp[dest],data,profiles,stage/'stream-receipts',
            extra_sources=[cache],force_full=old_rows.get(rel,{}).get('strategy') in ('full','vcdiff'))
        rows.append(row)
        if n%10==0:backend.log(f'流式同步：{n}/{len(cp)}，网络下载 {planner.stats["downloaded_bytes"]/1024**3:.2f} GiB')
    # Update the CN signed executable/SDK from CN official files; never replace
    # Steam's executable with the distinct non-Steam global build.
    sdk_changes=[]
    for entry in cn_channel['entries']:
        if entry['dest'].startswith(('Client/Content/','Client/Saved/')):continue
        current=ws.safe_child(backend.roots['CN'],entry['dest'])
        if matches_file(current,entry):continue
        dst=ws.safe_child(target,entry['dest'])
        planner.reconstruct(entry,ws.safe_child(backend.roots['GLOBAL'],entry['dest']),dst,extra_sources=[current])
        sdk_changes.append(entry)
    summary={'bytes_global':sum(r['sizes']['GLOBAL'] for r in rows),'bytes_cn':sum(r['sizes']['CN'] for r in rows),
             'full_cn_bytes':sum(r['sizes']['CN'] for r in rows if r['strategy']=='full'),
             'both_delta_bytes':sum(sum(r.get('patch_sizes',{}).values()) for r in rows),'shared_files':sum(r['strategy']!='full' for r in rows)}
    summary['estimated_saved_bytes']=summary['bytes_cn']-summary['full_cn_bytes']-summary['both_delta_bytes']
    manifest={'schema':1,'roots':{r:str(p) for r,p in backend.roots.items()},'files':rows,'base_version':version,'summary':summary,
              'built_at':time.strftime('%Y-%m-%d %H:%M:%S'),'official_index_md5':{r:c['index_md5'] for r,c in [('GLOBAL',global_channel),('CN',cn_channel)]}}
    ws.atomic_json(data/'manifest.json',manifest)
    ws.atomic_json(stage/'network-report.json',planner.stats)
    # Check emitted archives and all full cached variants before committing.
    for row in rows:
        if row['strategy']=='delta':
            for r in ws.REGIONS:
                if ws.sha(data/row['patches'][r])!=row['patch_sha256'][r]:raise RuntimeError('新版差异档案损坏')
        elif row['strategy']=='full':
            if ws.sha(ws.safe_child(profiles/'CN',row['relative']))!=row['sha256']['CN']:raise RuntimeError('新版区域缓存损坏')
    ws.idle_guard()
    if primary_stamps!={dest:ws.stamp(ws.safe_child(backend.roots['GLOBAL'],dest)) for dest in primary_stamps}:
        raise RuntimeError('同步期间 Steam 主资源又发生变化，保留临时数据；完成 Steam 更新后重新同步')
    token=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    state=dict(backend.state);state.update(phase='installed',active='GLOBAL',fast_cache=True,base_version=version,
        update_primary='GLOBAL',backup_present=False,snapshots=backend.snapshots() if {r['relative'] for r in rows}=={r['relative'] for r in backend.rows} else {r['relative']:ws.stamp(ws.safe_child(backend.active,r['relative'])) for r in rows})
    state['cached_snapshots']={}
    for row in rows:
        if row['strategy']=='full':
            state['cached_snapshots']['GLOBAL:'+row['relative']]=ws.stamp(ws.safe_child(backend.active,row['relative']))
            state['cached_snapshots']['CN:'+row['relative']]=ws.stamp(ws.safe_child(profiles/'CN',row['relative']))
    operations=[]
    if backend.backup.exists():
        archived=backend.backup.with_name('Content.WuwaSwitcher-original-'+token)
        operations.append({'kind':'rename','source':str(backend.backup),'target':str(archived)})
        state.setdefault('archived_original_backups',[]).append(str(archived))
    operations.append({'kind':'rename','source':str(backend.vault/'data'),'target':str(backend.vault/('data.previous-'+token))})
    operations.append({'kind':'rename','source':str(data),'target':str(backend.vault/'data')})
    if (backend.vault/'profiles').exists():operations.append({'kind':'rename','source':str(backend.vault/'profiles'),'target':str(backend.vault/('profiles.previous-'+token))})
    operations.append({'kind':'rename','source':str(profiles),'target':str(backend.vault/'profiles')})
    for entry in sdk_changes:
        operations.append({'kind':'sdk','source':str(ws.safe_child(target,entry['dest'])),'target':str(ws.safe_child(backend.roots['CN'],entry['dest'])),
          'backup':str(ws.safe_child(backend.roots['CN']/'.wuwa-switcher-sdk-backup'/token,entry['dest'])),'entry':entry})
    # Exact current official manifest prevents the secondary official launcher
    # from treating the updated SDK as the previous installation inventory.
    inventory=stage/'LocalGameResources.json';ws.atomic_json(inventory,{'resource':cn_channel['entries']})
    operations.append({'kind':'sdk','source':str(inventory),'target':str(backend.roots['CN']/'LocalGameResources.json'),
       'backup':str(backend.roots['CN']/'.wuwa-switcher-sdk-backup'/token/'LocalGameResources.json'),
       'entry':{'size':inventory.stat().st_size,'md5':md5_file(inventory),'dest':'LocalGameResources.json'}})
    disposable=[op['target'] for op in operations if op['kind']=='rename' and pathlib.Path(op['target']).parent==backend.vault and '.previous-' in pathlib.Path(op['target']).name]
    ws.atomic_json(journal,{'schema':1,'operations':operations,'done':0,'new_state':state,'stage':str(stage),'network':planner.stats,'disposable_previous_cache':disposable})
    finish_commit(backend)

def finish_commit(backend):
    ws.idle_guard();path=backend.vault/'update-journal.json';journal=json.loads(path.read_text(encoding='utf-8'))
    allowed=[p.resolve() for p in backend.roots.values()]
    for operation in journal['operations']:
        for key in ('source','target','backup'):
            if key not in operation:continue
            p=pathlib.Path(operation[key]).absolute()
            if not any(p.resolve().is_relative_to(root) for root in allowed):raise RuntimeError('更新日志包含客户端目录之外的路径')
    for i,op in enumerate(journal['operations']):
        if i<journal['done']:continue
        source=pathlib.Path(op['source']);target=pathlib.Path(op['target']);target.parent.mkdir(parents=True,exist_ok=True)
        if op['kind']=='rename':
            if source.exists():
                if target.exists():raise RuntimeError('更新提交目录冲突')
                os.rename(source,target)
            elif not target.exists():raise RuntimeError('更新提交目录缺失')
        elif op['kind']=='sdk':
            entry=op['entry']
            if not matches_file(target,entry):
                temp=target.with_name(target.name+'.WuwaSwitcher-new');shutil.copy2(source,temp)
                if not matches_file(temp,entry):raise RuntimeError('SDK 复制校验失败')
                backup=pathlib.Path(op['backup']);backup.parent.mkdir(parents=True,exist_ok=True)
                if target.exists() and not backup.exists():os.rename(target,backup)
                os.replace(temp,target)
        else:raise RuntimeError('未知的更新提交步骤')
        journal['done']=i+1;ws.atomic_json(path,journal)
    ws.atomic_json(backend.state_path,journal['new_state'])
    # Remove only disposable content copies inside our validated staging root.
    stage=pathlib.Path(journal['stage']);content=stage/'CN'/'Client'/'Content'
    if not content.resolve().is_relative_to(backend.vault.resolve()):raise RuntimeError('临时资源目录超出工作目录')
    if content.exists():
        for p in content.rglob('*'):
            if p.is_file():p.unlink()
        for p in sorted(content.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            if p.is_dir() and not ws.is_junction(p):p.rmdir()
        content.rmdir()
    for value in journal.get('disposable_previous_cache',[]):
        previous=pathlib.Path(value)
        if previous.parent.resolve()!=backend.vault.resolve() or not previous.name.startswith(('data.previous-','profiles.previous-')):
            raise RuntimeError('旧缓存清理路径异常')
        if previous.exists():
            for p in previous.rglob('*'):
                if ws.is_junction(p) or not p.resolve().is_relative_to(previous.resolve()):raise RuntimeError('旧缓存包含异常联接，保留以待检查')
                if p.is_file():p.unlink()
            for p in sorted(previous.rglob('*'),key=lambda p:len(p.parts),reverse=True):
                if p.is_dir():p.rmdir()
            previous.rmdir()
    path.unlink()
    fresh=ws.Backend(backend.data,backend.log);backend.__dict__.update(fresh.__dict__)
    backend.log('国服已同步到官方同一主版本；Steam 专属程序保持原样。网络共下载 '+f'{journal["network"]["downloaded_bytes"]/1024**3:.2f} GiB 的必要差异。')
