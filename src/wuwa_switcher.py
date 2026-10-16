"""Wuthering Waves regional resource switcher. No key extraction or executable patching."""
from __future__ import annotations
import csv, ctypes, hashlib, io, json, os, pathlib, re, shutil, struct, subprocess, sys, threading, time, zlib
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk, messagebox
import pyxdelta
from app_version import VERSION

REGIONS = ('GLOBAL', 'CN')
MAGIC = b'WWDELTA1'
APP_DIR = pathlib.Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
DEFAULT_DATA = pathlib.Path(os.environ.get('WUWA_SWITCHER_DATA', APP_DIR / 'WuwaSwitcher.data'))
EMBEDDED_MANIFEST = pathlib.Path(getattr(sys,'_MEIPASS',pathlib.Path(__file__).resolve().parent))/'manifest.json'

def sha(path):
    with pathlib.Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def stamp(path):
    s=pathlib.Path(path).stat()
    return [s.st_size,s.st_mtime_ns]

def atomic_json(path, value):
    path = pathlib.Path(path)
    tmp = path.with_suffix(path.suffix + '.new')
    with tmp.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def safe_child(root, rel):
    if not rel or '\\' in rel or ':' in rel or pathlib.PurePosixPath(rel).is_absolute() or '..' in pathlib.PurePosixPath(rel).parts:
        raise RuntimeError('差异清单包含不安全的路径')
    p = pathlib.Path(root) / rel
    if not p.resolve().is_relative_to(pathlib.Path(root).resolve()):
        raise RuntimeError('路径超出资源目录')
    return p

def patch_records(path):
    with pathlib.Path(path).open('rb') as f:
        if f.read(8) != MAGIC: raise RuntimeError('差异文件格式错误')
        end = 0
        while True:
            header = f.read(16)
            if not header: break
            if len(header) != 16: raise RuntimeError('差异文件截断')
            offset, length, packed = struct.unpack('<QII', header)
            if length > 65536 or packed > 131072 or offset < end: raise RuntimeError('差异文件记录错误')
            payload = f.read(packed)
            decoder = zlib.decompressobj()
            b = decoder.decompress(payload, length + 1)
            if len(b) != length or not decoder.eof or decoder.unused_data: raise RuntimeError('差异数据损坏')
            end = offset + length
            yield offset, b

def apply_delta(path, patch, size):
    with pathlib.Path(path).open('r+b') as f:
        for off, data in patch_records(patch):
            if off + len(data) > size: raise RuntimeError('差异超出目标文件')
            f.seek(off); f.write(data)
        f.truncate(size); f.flush(); os.fsync(f.fileno())

def projected_hash(path, patch, size):
    """Hash the reconstructed target stream without modifying or copying the source."""
    h = hashlib.sha256(); pos = 0
    with pathlib.Path(path).open('rb') as f:
        for offset, data in patch_records(patch):
            if offset + len(data) > size: raise RuntimeError('差异超出文件')
            while pos < offset:
                b = f.read(min(4*1024*1024, offset-pos))
                if not b: raise RuntimeError('目标缺少正文数据')
                h.update(b); pos += len(b)
            h.update(data); pos = offset + len(data); f.seek(pos)
        while pos < size:
            b = f.read(min(4*1024*1024, size-pos))
            if not b: raise RuntimeError('目标缺少正文数据')
            h.update(b); pos += len(b)
    return h.hexdigest()

def decode_vcdiff(source, patch, target):
    # Native decoder runs in a separate process to keep the GUI responsive.
    command=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(pathlib.Path(__file__).resolve())]
    subprocess.run(command+['--decode',str(source),str(patch),str(target)],check=True,capture_output=True,creationflags=0x08000000 if os.name=='nt' else 0)
    with pathlib.Path(target).open('r+b') as f:os.fsync(f.fileno())

def make_junction(path, target):
    # Literal PowerShell paths; no cmd shell interpolation.
    quote = lambda p: "'" + str(pathlib.Path(p).absolute()).replace("'", "''") + "'"
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
        "New-Item -ItemType Junction -Path " + quote(path) + " -Target " + quote(target) + " -ErrorAction Stop | Out-Null"],
        check=True, capture_output=True, creationflags=0x08000000)
    if pathlib.Path(path).resolve() != pathlib.Path(target).resolve(): raise RuntimeError('目录联接目标不符')

def is_junction(path):
    try:
        s = pathlib.Path(path).lstat()
        return bool(getattr(s, 'st_file_attributes', 0) & 0x400)
    except FileNotFoundError: return False

def idle_guard():
    if os.name != 'nt': return
    proc = subprocess.run(['tasklist.exe', '/FO', 'CSV', '/NH'], capture_output=True, text=True, errors='replace', creationflags=0x08000000, check=True)
    names = {r[0].lower() for r in csv.reader(io.StringIO(proc.stdout)) if r}
    blocked = names & {'client-win64-shipping.exe', 'wuthering waves.exe', 'launcher.exe', 'krlauncher.exe', 'krinstall.exe'}
    if blocked: raise RuntimeError('请先关闭游戏和库洛启动器：' + '、'.join(sorted(blocked)))

def cn_launch_arguments(game_root):
    """Use the resource tier selected by the installed official launcher."""
    game_root=pathlib.Path(game_root)
    config=game_root.parent/'kr_game_cache'/'Extend_command_cache.json'
    if config.exists():
        try:
            value=json.loads(config.read_text(encoding='utf-8-sig'))
            if value.get('commandSwitch')==1:
                for entry in value.get('commandList',[]):
                    command=entry.get('cmd','')
                    if entry.get('default')==1 and isinstance(command,str) and re.fullmatch(r'-krqlv=(hd|sd|uhd)',command):return [command]
        except (ValueError,OSError):pass
    hd=game_root/'Client'/'Content'/'HD'
    if hd.is_dir() and any(hd.glob('*.pak')):return ['-krqlv=hd']
    raise RuntimeError('未找到官方启动器的资源档位配置，请先通过国服官方启动器运行一次游戏')

def recent_game_crash(game_root, started):
    crash_root=pathlib.Path(game_root)/'Client'/'Saved'/'Crashes'
    if not crash_root.exists():return None
    for p in crash_root.glob('*/CrashContext.runtime-xml'):
        try:
            if p.stat().st_mtime<started:continue
            root=ET.fromstring(p.read_text(encoding='utf-8'))
            error=root.find('.//ErrorMessage')
            if error is not None and error.text:return error.text.strip()
        except (OSError,ET.ParseError):continue
    return None

class Backend:
    def __init__(self, data=DEFAULT_DATA, log=print):
        self.data = pathlib.Path(data).absolute(); self.log = log
        source_manifest=self.data/'manifest.json'
        if not source_manifest.exists():source_manifest=EMBEDDED_MANIFEST
        self.manifest = json.loads(source_manifest.read_text(encoding='utf-8'))
        if self.manifest.get('schema') != 1: raise RuntimeError('不支持的清单版本')
        self.roots = {k:pathlib.Path(v).absolute() for k,v in self.manifest['roots'].items()}
        if set(self.roots) != set(REGIONS): raise RuntimeError('区域清单不完整')
        if self.roots['GLOBAL'] == self.roots['CN']: raise RuntimeError('两个客户端路径必须不同')
        self.active = self.roots['GLOBAL']/'Client'/'Content'
        self.cn = self.roots['CN']/'Client'/'Content'
        self.vault = self.roots['GLOBAL']/'.wuwa-switcher'
        self.state_path = self.vault/'state.json'; self.journal_path = self.vault/'journal.json'
        self.backup = self.roots['CN']/'Client'/'Content.WuwaSwitcher-original'
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else None
        if self.state and self.state.get('phase')!='uninstalled' and (self.vault/'data'/'manifest.json').exists():
            self.manifest=json.loads((self.vault/'data'/'manifest.json').read_text(encoding='utf-8'))
        if not (self.data/'manifest.json').exists() and self.state:self.data=self.vault/'data'
        rows = self.manifest['files']; seen = set()
        for r in rows:
            safe_child(self.active, r['relative'])
            if r['relative'] in seen or r['strategy'] not in ('delta', 'vcdiff', 'full', 'identical'): raise RuntimeError('资源清单重复或格式错误')
            seen.add(r['relative'])
            for region in REGIONS:
                if r['sizes'][region] < 0 or len(r['sha256'][region]) != 64: raise RuntimeError('资源校验信息错误')
                if r['strategy'] in ('delta','vcdiff'): safe_child(self.data, r['patches'][region])
        self.rows = rows
        self.verified_patch_stamps={}

    def save_state(self): atomic_json(self.state_path, self.state)

    def patch_path(self, row, region):
        return safe_child(self.vault/'data' if self.state else self.data, row['patches'][region])

    def profile_path(self, row, region): return safe_child(self.vault/'profiles'/region, row['relative'])

    def check_patches(self, data=None):
        data = data or (self.vault/'data' if self.state else self.data)
        for r in self.rows:
            if r['strategy'] in ('delta','vcdiff'):
                for region in REGIONS:
                    p = safe_child(data, r['patches'][region])
                    key=str(p);current_stamp=stamp(p)
                    if self.verified_patch_stamps.get(key)!=current_stamp:
                        if sha(p) != r['patch_sha256'][region]: raise RuntimeError('差异档案损坏：' + p.name)
                        self.verified_patch_stamps[key]=current_stamp

    def check_tree(self, path, region):
        actual = {p.relative_to(path).as_posix() for p in path.rglob('*') if p.is_file()}
        if actual != {r['relative'] for r in self.rows}: raise RuntimeError('资源列表已改变，请恢复独立客户端并重新生成差异档案')
        for n,r in enumerate(self.rows,1):
            p = safe_child(path,r['relative'])
            if p.stat().st_size != r['sizes'][region] or sha(p) != r['sha256'][region]: raise RuntimeError('版本或资源不匹配：'+r['relative'])
            if n % 20 == 0: self.log(f'完整校验 {region}：{n}/{len(self.rows)}')

    def snapshots(self):
        return {r['relative']:stamp(safe_child(self.active,r['relative'])) for r in self.rows}

    def cache_snapshots(self):
        result={}
        for r in self.rows:
            if r['strategy'] not in ('vcdiff','full'):continue
            for region in REGIONS:
                p=safe_child(self.active,r['relative']) if region==self.state['active'] else self.profile_path(r,region)
                result[region+':'+r['relative']]=stamp(p)
        return result

    def enable_fast_cache(self):
        idle_guard();self.verify_current();self.check_patches()
        if self.state.get('fast_cache') and self.cache_snapshots()==self.state.get('cached_snapshots'):
            self.log('快速缓存已经启用，无需重复生成。');return
        other='CN' if self.state['active']=='GLOBAL' else 'GLOBAL'
        total=sum(r['sizes'][other] for r in self.rows if r['strategy']=='vcdiff')
        self.log(f'生成快速缓存，另占约 {total/2**30:.2f} GiB；此步骤只执行一次。')
        if shutil.disk_usage(self.vault).free<total+1024**3:raise RuntimeError('快速缓存空间不足')
        for r in self.rows:
            if r['strategy'] not in ('vcdiff','full'):continue
            dst=self.profile_path(r,other);dst.parent.mkdir(parents=True,exist_ok=True)
            if dst.exists() and dst.stat().st_size==r['sizes'][other] and sha(dst)==r['sha256'][other]:continue
            if r['strategy']=='full':raise RuntimeError('缺少原完整区域包：'+r['relative'])
            self.log('建立缓存：'+r['relative']);temp=dst.with_name(dst.name+'.new')
            decode_vcdiff(safe_child(self.active,r['relative']),self.patch_path(r,other),temp)
            if sha(temp)!=r['sha256'][other]:raise RuntimeError('缓存原包校验失败')
            os.replace(temp,dst)
        self.state['fast_cache']=True;self.state['cached_snapshots']=self.cache_snapshots();self.save_state()
        self.log('快速缓存已启用。以后切换这些大包仅做文件重命名。')

    def switch_cached(self,row,source,target,recovering):
        current=safe_child(self.active,row['relative']);old=self.profile_path(row,source);new=self.profile_path(row,target)
        old.parent.mkdir(parents=True,exist_ok=True)
        if new.exists():
            expected=self.state.get('cached_snapshots',{}).get(target+':'+row['relative'])
            if stamp(new)!=expected:
                if sha(new)!=row['sha256'][target]:raise RuntimeError('区域缓存被修改：'+row['relative'])
            if current.exists():
                if old.exists():raise RuntimeError('缓存位置冲突：'+row['relative'])
                os.rename(current,old)
            if not old.exists():raise RuntimeError('缺少源区域缓存')
            os.rename(new,current)
        elif not recovering or not current.exists() or sha(current)!=row['sha256'][target]:
            raise RuntimeError('区域缓存缺失：'+row['relative'])

    def verify_current(self, full=False):
        if not self.state or self.state.get('phase') != 'installed': raise RuntimeError('请先安装共用资源')
        if not is_junction(self.cn) or self.cn.resolve() != self.active.resolve(): raise RuntimeError('国服资源目录联接已改变')
        actual = {p.relative_to(self.active).as_posix() for p in self.active.rglob('*') if p.is_file()}
        if actual != {r['relative'] for r in self.rows}: raise RuntimeError('主资源清单已改变，停止使用旧差异。请点击同步国服更新')
        if full:
            self.check_tree(self.active,self.state['active']); return
        if self.snapshots() != self.state['snapshots']: raise RuntimeError('检测到资源被更新或修改，停止使用旧差异。请点击同步国服更新验证官方版本')

    def install(self):
        idle_guard()
        retired=self.state and self.state.get('phase')=='uninstalled'
        if self.state and not retired: raise RuntimeError('已经安装，或存在未完成的恢复操作')
        if (self.vault.exists() and not retired) or self.backup.exists(): raise RuntimeError('发现以前的工作目录，停止以免覆盖备份')
        if is_junction(self.active) or is_junction(self.cn): raise RuntimeError('资源目录已经是联接，请先恢复原始目录')
        self.log('安装前完整校验两个客户端（约 160 GiB 读取）…')
        self.check_patches(self.data); self.check_tree(self.active,'GLOBAL'); self.check_tree(self.cn,'CN')
        needed = sum(r['sizes']['CN'] for r in self.rows if r['strategy']=='full') + sum(sum(r.get('patch_sizes',{}).values()) for r in self.rows) + max((max(r['sizes'].values()) for r in self.rows if r['strategy']=='vcdiff'),default=0) + 1024**3
        if shutil.disk_usage(self.active).free < needed: raise RuntimeError('国际服所在磁盘空间不足')
        idle_guard()
        if retired:
            if self.vault.resolve().parent!=self.roots['GLOBAL'].resolve():raise RuntimeError('工作目录位置异常')
            archive=self.vault.with_name('.wuwa-switcher-retired-'+time.strftime('%Y%m%d-%H%M%S'))
            data_relative=self.data.resolve().relative_to(self.vault.resolve()) if self.data.resolve().is_relative_to(self.vault.resolve()) else None
            os.rename(self.vault,archive)
            if data_relative is not None:self.data=archive/data_relative
            self.state=None
        self.vault.mkdir()
        self.state = {'phase':'installing','active':'GLOBAL','backup_present':True}
        self.save_state()
        try:
            shutil.copytree(self.data, self.vault/'data')
            for r in self.rows:
                if r['strategy']=='full':
                    dst=self.profile_path(r,'CN');dst.parent.mkdir(parents=True,exist_ok=True)
                    self.log('保留区域独有完整包：'+r['relative']);shutil.copy2(safe_child(self.cn,r['relative']),dst)
                    if sha(dst)!=r['sha256']['CN']: raise RuntimeError('区域资源复制校验失败')
            os.rename(self.cn,self.backup)
            make_junction(self.cn,self.active)
            self.state.update(phase='installed',snapshots=self.snapshots()); self.save_state()
        except Exception:
            if is_junction(self.cn) and self.cn.resolve()==self.active.resolve(): os.rmdir(self.cn)
            if self.backup.exists() and not self.cn.exists(): os.rename(self.backup,self.cn)
            self.state['phase']='install_failed';self.save_state();raise
        self.log('共用资源已安装。原国服资源完整保留，尚未释放空间。')

    def verify_affected(self, row, region):
        p=safe_child(self.active,row['relative'])
        if p.stat().st_size != row['sizes'][region]: raise RuntimeError('差异应用后的文件大小错误')
        with p.open('rb') as f:
            for off,b in patch_records(self.patch_path(row,region)):
                f.seek(off)
                if f.read(len(b))!=b: raise RuntimeError('差异写入校验失败：'+row['relative'])

    def switch(self, region, recovering=False):
        if region not in REGIONS: raise ValueError(region)
        requested=region
        idle_guard(); self.check_patches()
        if self.journal_path.exists():
            journal=json.loads(self.journal_path.read_text(encoding='utf-8'))
            region=journal['target'];recovering=True
        else:
            self.verify_current()
            if self.state['active']==region: self.log('当前已是 '+region);return
            journal={'source':self.state['active'],'target':region,'completed':[]}
            atomic_json(self.journal_path,journal)
        source=journal['source']
        for n,r in enumerate(self.rows,1):
            rel=r['relative'];current=safe_child(self.active,rel)
            if rel in journal['completed']: continue
            if self.state.get('fast_cache') and r['strategy'] in ('vcdiff','full'):
                self.switch_cached(r,source,region,recovering)
            elif r['strategy']=='delta':
                apply_delta(current,self.patch_path(r,region),r['sizes'][region]);self.verify_affected(r,region)
            elif r['strategy']=='vcdiff':
                temp=self.vault/'reconstruction.tmp'
                if recovering and current.stat().st_size==r['sizes'][region] and sha(current)==r['sha256'][region]:
                    if temp.exists():temp.unlink()
                else:
                    self.log('正在重建并校验区域包：'+rel)
                    if temp.exists():temp.unlink()
                    if shutil.disk_usage(self.vault).free<r['sizes'][region]+128*1024**2:raise RuntimeError('切换需要约 7 GiB 临时空间')
                    decode_vcdiff(current,self.patch_path(r,region),temp)
                    if temp.stat().st_size!=r['sizes'][region] or sha(temp)!=r['sha256'][region]:raise RuntimeError('重建的区域包校验失败，原文件保留')
                    os.replace(temp,current)
            elif r['strategy']=='full':
                old=self.profile_path(r,source);new=self.profile_path(r,region)
                old.parent.mkdir(parents=True,exist_ok=True)
                # Recovery is idempotent even if power was lost between the two renames.
                if new.exists():
                    if sha(new)!=r['sha256'][region]: raise RuntimeError('区域完整包校验失败')
                    if current.exists():
                        if old.exists(): raise RuntimeError('区域文件位置冲突，停止恢复')
                        os.rename(current,old)
                    if not old.exists(): raise RuntimeError('缺少源区域备份')
                    os.rename(new,current)
                elif not current.exists() or sha(current)!=r['sha256'][region]: raise RuntimeError('区域完整包缺失')
            journal['completed'].append(rel);atomic_json(self.journal_path,journal)
            if n%25==0:self.log(f'切换 {region}：{n}/{len(self.rows)}')
        self.state.update(active=region,snapshots=self.snapshots())
        if self.state.get('fast_cache'):self.state['cached_snapshots']=self.cache_snapshots()
        self.save_state();self.journal_path.unlink()
        if recovering:self.check_tree(self.active,region)
        self.log('已切换至 '+('国服' if region=='CN' else '国际服 / Steam'))
        if requested!=region:self.switch(requested)

    def release_backup(self):
        idle_guard();self.verify_current(full=True);self.check_patches()
        if not self.backup.exists():self.log('原资源备份已释放');return
        self.log('逐个验证可还原为原国服包后，释放重复文件…')
        for n,r in enumerate(self.rows,1):
            original=safe_child(self.backup,r['relative'])
            if not original.exists():continue
            if sha(original)!=r['sha256']['CN']:raise RuntimeError('原备份已改变：'+r['relative'])
            current=safe_child(self.active,r['relative'])
            if r['strategy']=='full':
                candidate=current if self.state['active']=='CN' else self.profile_path(r,'CN')
                result=sha(candidate)
            elif r['strategy']=='vcdiff' and self.state.get('fast_cache') and self.state['active']!='CN':
                result=sha(self.profile_path(r,'CN'))
            elif r['strategy']=='vcdiff' and self.state['active']!='CN':
                temp=self.vault/'verification.tmp'
                decode_vcdiff(current,self.patch_path(r,'CN'),temp);result=sha(temp);temp.unlink()
            elif r['strategy']=='identical' or self.state['active']=='CN': result=sha(current)
            else:result=projected_hash(current,self.patch_path(r,'CN'),r['sizes']['CN'])
            if result!=r['sha256']['CN']:raise RuntimeError('无法还原原国服包，拒绝释放：'+r['relative'])
            original.unlink()
            if n%20==0:self.log(f'释放空间：{n}/{len(self.rows)}')
        # Remove only now-empty directories within this exact known backup root.
        for p in sorted(self.backup.rglob('*'), key=lambda p:len(p.parts),reverse=True):
            if p.is_dir() and not is_junction(p):p.rmdir()
        self.backup.rmdir();self.state['backup_present']=False;self.save_state()
        self.log('重复资源已释放。恢复独立客户端时会从共用正文和差异档案重建。')

    def restore_independent(self):
        idle_guard()
        if not self.state: self.log('当前是独立客户端');return
        if self.journal_path.exists():self.switch('GLOBAL',recovering=True)
        phase=self.state.get('phase')
        if phase=='uninstalled':self.log('当前是独立客户端');return
        if phase=='restoring_detach':
            self.finish_detach();return
        if phase in ('installing','install_failed'):
            if is_junction(self.cn) and self.cn.resolve()==self.active.resolve():os.rmdir(self.cn)
            if self.backup.exists() and not self.cn.exists():os.rename(self.backup,self.cn)
            if not self.cn.is_dir():raise RuntimeError('安装恢复时没有找到原国服资源')
            self.state['phase']='uninstalled';self.save_state();self.log('原目录已恢复，请重新分析后使用新档案');return
        if phase!='restoring':
            self.verify_current();self.check_patches()
            # Validate both profile sets before beginning a large reconstruction.
            for r in self.rows:
                if r['strategy']=='full':
                    other='CN' if self.state['active']=='GLOBAL' else 'GLOBAL'
                    if sha(self.profile_path(r,other))!=r['sha256'][other]:raise RuntimeError('另一区域完整包损坏')
            self.state['phase']='restoring';self.save_state()
        self.log('正在重建国服独立资源；请预留约 79 GiB 空间…')
        self.backup.mkdir(exist_ok=True)
        missing=sum(r['sizes']['CN'] for r in self.rows if not safe_child(self.backup,r['relative']).exists())
        if shutil.disk_usage(self.backup).free<missing+1024**3:raise RuntimeError('国服磁盘空间不足，恢复操作可稍后重试')
        for n,r in enumerate(self.rows,1):
            dst=safe_child(self.backup,r['relative']);current=safe_child(self.active,r['relative'])
            if dst.exists() and dst.stat().st_size==r['sizes']['CN'] and sha(dst)==r['sha256']['CN']:continue
            dst.parent.mkdir(parents=True,exist_ok=True)
            cached=r['strategy']=='full' or r['strategy']=='vcdiff' and self.state.get('fast_cache')
            source=self.profile_path(r,'CN') if cached and self.state['active']!='CN' else current
            if r['strategy']=='vcdiff' and not cached and self.state['active']!='CN':decode_vcdiff(source,self.patch_path(r,'CN'),dst)
            else:
                shutil.copy2(source,dst)
                if r['strategy']=='delta' and self.state['active']!='CN':apply_delta(dst,self.patch_path(r,'CN'),r['sizes']['CN'])
            if sha(dst)!=r['sha256']['CN']:raise RuntimeError('国服重建校验失败：'+r['relative'])
            if n%20==0:self.log(f'重建国服：{n}/{len(self.rows)}')
        # Bring the global shared tree back to its original region. If files were
        # updated outside the tool, preserve all paths and stop instead of guessing.
        self.state['phase']='installed';self.state['snapshots']=self.snapshots();self.save_state()
        if self.state['active']!='GLOBAL':self.switch('GLOBAL')
        self.check_tree(self.active,'GLOBAL')
        self.state['phase']='restoring_detach';self.save_state();self.finish_detach()

    def finish_detach(self):
        if is_junction(self.cn):
            if self.cn.resolve()!=self.active.resolve():raise RuntimeError('恢复时国服目录联接不符')
            os.rmdir(self.cn)
        if not self.cn.exists() and self.backup.exists():os.rename(self.backup,self.cn)
        if not self.cn.is_dir() or is_junction(self.cn):raise RuntimeError('无法完成国服目录恢复')
        self.check_tree(self.cn,'CN')
        self.state.update(phase='uninstalled',backup_present=False);self.save_state()
        self.log('两个客户端已恢复原始独立资源，可分别官方更新。旧差异档案不适用于新版。')

    def launch(self, region):
        if self.state and self.state.get('phase')=='updating':
            if region!='GLOBAL':raise RuntimeError('国服等待新版差异同步；请先完成 Steam 更新并点击同步国服更新')
            idle_guard()
        elif self.state and self.state.get('phase')=='uninstalled':idle_guard()
        else:self.switch(region);self.verify_current()
        if region=='GLOBAL':
            appid=(self.roots[region]/'Client'/'Binaries'/'Win64'/'steam_appid.txt').read_text().strip()
            if not appid.isdigit():raise RuntimeError('Steam App ID 格式错误')
            os.startfile('steam://rungameid/'+appid)
        else:
            exe=self.roots[region]/'Wuthering Waves.exe'
            arguments=cn_launch_arguments(self.roots[region])
            self.log('使用官方启动器配置：'+' '.join(arguments))
            started=time.time()
            subprocess.Popen([str(exe)]+arguments,cwd=str(self.roots[region]),creationflags=0x08000000)
            def observe_startup():
                try:
                    for _ in range(20):
                        time.sleep(0.5)
                        error=recent_game_crash(self.roots[region],started)
                        if error:self.log('国服启动崩溃：'+error);return
                except (OSError,RuntimeError):pass
            threading.Thread(target=observe_startup,daemon=True).start()
        self.log('已启动原版客户端。')

class App:
    def __init__(self,data=DEFAULT_DATA):
        self.root=tk.Tk();self.root.title('鸣潮 · 国服 / 国际服切换 v'+VERSION);self.root.geometry('900x660');self.root.minsize(820,600)
        self.root.configure(bg='#111827');self.busy=False
        style=ttk.Style();style.theme_use('clam');style.configure('TButton',font=('Microsoft YaHei UI',11),padding=10)
        self.title=tk.Label(self.root,text='鸣潮资源共用与区域切换',font=('Microsoft YaHei UI',20,'bold'),bg='#111827',fg='#f9fafb');self.title.pack(pady=(22,8))
        self.status=tk.Label(self.root,text='正在读取本地档案…',font=('Microsoft YaHei UI',11),bg='#111827',fg='#93c5fd',wraplength=730);self.status.pack(padx=20,pady=6)
        row=tk.Frame(self.root,bg='#111827');row.pack(pady=10)
        self.buttons=[]
        for label,fn in [('国服：切换并启动',lambda:self.backend.launch('CN')),('国际服 / Steam：切换并启动',lambda:self.backend.launch('GLOBAL'))]:
            b=ttk.Button(row,text=label,command=lambda f=fn:self.run(f));b.pack(side='left',padx=8);self.buttons.append(b)
        ops=tk.Frame(self.root,bg='#111827');ops.pack(pady=4)
        for label,fn in [('安装共用资源',self.install),('释放重复资源',self.release),('恢复独立客户端',self.restore),('完整校验',lambda:self.run(lambda:self.backend.verify_current(full=True)))]:
            b=ttk.Button(ops,text=label,command=fn);b.pack(side='left',padx=4);self.buttons.append(b)
        update_ops=tk.Frame(self.root,bg='#111827');update_ops.pack(pady=5)
        for label,fn in [('启用快速缓存',lambda:self.backend.enable_fast_cache()),('检查官方版本',self.check_updates),('准备更新 Steam',self.prepare_update),('同步国服更新',self.sync_update),('取消更新模式',self.cancel_update)]:
            b=ttk.Button(update_ops,text=label,command=lambda f=fn:self.run(f));b.pack(side='left',padx=4);self.buttons.append(b)
        tk.Label(self.root,text='首次生成大包缓存后，日常切换改用重命名。两区账号、语音和热更新分别保留。\n更新流程：准备更新 Steam → Steam 更新主客户端 → 同步国服更新。',font=('Microsoft YaHei UI',10),bg='#111827',fg='#d1d5db',justify='left').pack(pady=10)
        self.text=tk.Text(self.root,bg='#0b1220',fg='#e5e7eb',font=('Microsoft YaHei UI',10),relief='flat',wrap='word',height=12);self.text.pack(fill='both',expand=True,padx=20,pady=(0,20))
        self.backend=Backend(data,log=self.log);self.refresh()
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        if self.backend.journal_path.exists():self.log('发现未完成切换，请点击任一区域按钮，工具会先恢复已记录的目标区域。')
    def log(self,s):
        log_path=pathlib.Path(sys.executable if getattr(sys,'frozen',False) else __file__).resolve().parent/'WuwaSwitcher.log'
        try:
            with log_path.open('a',encoding='utf-8') as f:f.write(time.strftime('%Y-%m-%d %H:%M:%S')+'  '+s+'\n')
        except OSError:pass
        def add():self.text.insert('end',time.strftime('%H:%M:%S')+'  '+s+'\n');self.text.see('end')
        self.root.after(0,add)
    def refresh(self):
        s=self.backend.state
        saved=self.backend.manifest['summary']['estimated_saved_bytes']/2**30
        if s and s.get('fast_cache'):saved-=sum(r['sizes']['CN'] for r in self.backend.rows if r['strategy']=='vcdiff')/2**30
        version=self.backend.manifest.get('base_version','待官方校验')
        msg=f'基础档案净节省 {saved:.2f} GiB · 主资源版本 {version}'
        msg+=' · '+('尚未安装' if not s else '当前 '+s['active']+' / '+s['phase'])
        self.status.config(text=msg)
        if s and s.get('fast_cache'):self.status.config(text=msg+' · 快速缓存已启用')
    def check_updates(self):
        import wuwa_update
        for region in REGIONS:
            channel=wuwa_update.fetch_channel(region)
            self.log(region+' 官方主版本：'+channel['version'])
    def prepare_update(self):
        import wuwa_update
        wuwa_update.prepare_update(self.backend)
    def sync_update(self):
        import wuwa_update
        wuwa_update.sync_updated_primary(self.backend)
    def cancel_update(self):
        import wuwa_update
        wuwa_update.cancel_update(self.backend)
    def run(self,fn):
        if self.busy:return
        self.busy=True
        for b in self.buttons:b.config(state='disabled')
        def worker():
            try:fn()
            except Exception as e:
                self.log('停止：'+str(e));self.root.after(0,lambda msg=str(e):messagebox.showerror('操作未完成',msg))
            finally:self.root.after(0,self.finished)
        threading.Thread(target=worker,daemon=False).start()
    def finished(self):
        self.busy=False
        for b in self.buttons:b.config(state='normal')
        self.refresh()
    def install(self):
        if messagebox.askyesno('安装共用资源','工具会完整校验两个客户端，并把国服 Content 改为指向国际服资源的目录联接。\n原国服 Content 会重命名保留备份；尚不删除任何游戏资源。\n\n继续安装？'):self.run(self.backend.install)
    def release(self):
        if messagebox.askyesno('释放重复资源','此操作会逐个验证还原能力，然后删除原国服的重复 Content 备份。\n切换档案及区域独有资源会保留。恢复独立客户端需要约 79 GiB 空间。\n\n确认释放？'):self.run(self.backend.release_backup)
    def restore(self):
        if messagebox.askyesno('恢复独立客户端','将重建国服独立 Content 并恢复国际服原始资源。可能需要数分钟和约 79 GiB 空间。\n恢复后可分别官方更新；旧切换档案不能继续用于新版。\n\n继续恢复？'):self.run(self.backend.restore_independent)
    def close(self):
        if self.busy:messagebox.showinfo('正在处理','请等待操作完成后再关闭工具。');return
        self.root.destroy()

def main():
    if len(sys.argv)==2 and sys.argv[1]=='--version':
        print(VERSION);return
    if len(sys.argv)==5 and sys.argv[1]=='--create-bundle':
        from wuwa_setup import create_bundle
        create_bundle(sys.argv[2],sys.argv[3],sys.argv[4]);return
    if len(sys.argv)==3 and sys.argv[1]=='--check-updates':
        try:
            import wuwa_update
            result={r:wuwa_update.fetch_channel(r)['version'] for r in REGIONS}
            atomic_json(pathlib.Path(sys.argv[2]),{'result':'PASS','official_versions':result});sys.exit(0)
        except Exception as e:
            atomic_json(pathlib.Path(sys.argv[2]),{'result':'FAIL','error':str(e)});sys.exit(1)
    if len(sys.argv)==3 and sys.argv[1]=='--self-check':
        try:
            backend=Backend();backend.check_patches(backend.data)
            atomic_json(pathlib.Path(sys.argv[2]),{'result':'PASS','files':len(backend.rows),'strategies':{s:sum(r['strategy']==s for r in backend.rows) for s in ('identical','delta','vcdiff','full')},'fast_cache':bool(backend.state and backend.state.get('fast_cache')),'original_game_files_modified':False})
            sys.exit(0)
        except Exception as e:
            atomic_json(pathlib.Path(sys.argv[2]),{'result':'FAIL','error':str(e)});sys.exit(1)
    if len(sys.argv)==5 and sys.argv[1]=='--decode':
        try:
            ok=pyxdelta.decode(sys.argv[2],sys.argv[3],sys.argv[4]);sys.exit(0 if ok else 2)
        except Exception:sys.exit(3)
    mutex=None
    if os.name=='nt':
        ctypes.windll.kernel32.CreateMutexW.restype=ctypes.c_void_p
        mutex=ctypes.windll.kernel32.CreateMutexW(None,False,'Local\\WuwaRegionSwitcher_v1')
        if ctypes.windll.kernel32.GetLastError()==183:
            messagebox.showerror('工具已打开','请使用已经打开的切换工具。');return
    try:
        from wuwa_setup import ensure_bundle
        data=ensure_bundle()
        if data is None:return
        app=App(data);app.root.mainloop()
    except Exception as e:messagebox.showerror('无法启动',str(e))
    finally:
        if mutex:ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))

if __name__=='__main__':main()
