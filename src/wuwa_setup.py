"""Local configuration and initial bundle generation; no game data is distributed."""
import json
import os
import pathlib
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import wuwa_switcher as ws


def create_bundle(global_root, cn_root, destination, log=print):
    from wuwa_update import compare_pair, require_space
    ws.idle_guard()
    roots={'GLOBAL':pathlib.Path(global_root).resolve(),'CN':pathlib.Path(cn_root).resolve()}
    if any(a.is_relative_to(b) for a,b in [(roots['GLOBAL'],roots['CN']),(roots['CN'],roots['GLOBAL'])]):
        raise RuntimeError('两个客户端目录必须分开，且不能相互包含')
    content={r:p/'Client'/'Content' for r,p in roots.items()}
    for p in content.values():
        if not p.is_dir() or ws.is_junction(p):raise RuntimeError('初始生成需要两套完整、独立的 Client/Content 目录')
    files={r:{p.relative_to(c).as_posix() for p in c.rglob('*') if p.is_file()} for r,c in content.items()}
    if not files['GLOBAL'] or files['GLOBAL']!=files['CN']:
        raise RuntimeError('两区资源文件列表不同，请先把两套客户端更新到相同版本和资源档位')
    destination=pathlib.Path(destination).absolute()
    for c in content.values():
        if destination.resolve().is_relative_to(c.resolve()):raise RuntimeError('档案输出不能位于游戏 Content 内')
    destination.parent.mkdir(parents=True,exist_ok=True)
    if destination.exists():raise RuntimeError('档案输出目录已存在；请选择新目录，避免覆盖原档案')
    staging=destination.with_name(destination.name+'.building')
    if staging.exists():raise RuntimeError('发现上次未完成的 .building 目录，请保留检查后换一个输出目录')
    staging.mkdir();rows=[]
    # All writes stay in our new staging directory. The original clients remain
    # unchanged, even when comparison fails or the process is interrupted.
    for n,rel in enumerate(sorted(files['GLOBAL']),1):
        require_space(staging,16*1024**2)
        log(f'生成本机档案 {n}/{len(files["GLOBAL"])}：{rel}')
        row=compare_pair(rel,ws.safe_child(content['GLOBAL'],rel),ws.safe_child(content['CN'],rel),staging)
        rows.append(row)
    summary={'bytes_global':sum(r['sizes']['GLOBAL'] for r in rows),
             'bytes_cn':sum(r['sizes']['CN'] for r in rows),
             'full_cn_bytes':sum(r['sizes']['CN'] for r in rows if r['strategy']=='full'),
             'both_delta_bytes':sum(sum(r.get('patch_sizes',{}).values()) for r in rows),
             'shared_files':sum(r['strategy']!='full' for r in rows)}
    summary['estimated_saved_bytes']=summary['bytes_cn']-summary['full_cn_bytes']-summary['both_delta_bytes']
    ws.atomic_json(staging/'manifest.json',{'schema':1,'roots':{r:str(p) for r,p in roots.items()},'files':rows,'summary':summary})
    os.rename(staging,destination)
    log('本机档案生成完成；现在可点击安装共用资源。')
    return destination


def ensure_bundle():
    if (ws.DEFAULT_DATA/'manifest.json').is_file():return ws.DEFAULT_DATA
    config=ws.APP_DIR/'WuwaSwitcher.json'
    if config.is_file():
        data=pathlib.Path(json.loads(config.read_text(encoding='utf-8'))['data'])
        if (data/'manifest.json').is_file():return data
    root=tk.Tk();root.withdraw()
    global_root=filedialog.askdirectory(parent=root,title='选择 Steam 国际服根目录（包含 Client）')
    if not global_root:root.destroy();return None
    # Existing installations keep their own current manifest and caches. Reuse
    # those directly rather than comparing a junction with itself.
    existing=pathlib.Path(global_root)/'.wuwa-switcher'/'data'
    state=existing.parent/'state.json'
    if (existing/'manifest.json').is_file() and state.is_file() and json.loads(state.read_text(encoding='utf-8')).get('phase')!='uninstalled':
        ws.atomic_json(config,{'data':str(existing.resolve())});root.destroy();return existing
    cn_root=filedialog.askdirectory(parent=root,title='选择国服游戏根目录（包含 Client）')
    if not cn_root:root.destroy();return None
    if not messagebox.askyesno('生成本机档案','首次使用需要两套同版本、同资源档位的完整客户端。\n接下来只读取资源并生成本机差异档案，可能需要数分钟。\n\n继续？',parent=root):
        root.destroy();return None
    destination=ws.APP_DIR/'WuwaSwitcher.data'
    root.deiconify();root.title('生成鸣潮本机切换档案');root.geometry('760x420')
    text=tk.Text(root,wrap='word');text.pack(fill='both',expand=True)
    events=queue.Queue();result=[]
    root.protocol('WM_DELETE_WINDOW',lambda:messagebox.showinfo('正在生成','请等待资源比较完成。',parent=root))
    def work():
        try:
            result.append(create_bundle(global_root,cn_root,destination,lambda s:events.put(('log',s))))
            ws.atomic_json(config,{'data':str(destination.resolve())})
            events.put(('done',None))
        except Exception as exc:events.put(('error',str(exc)))
    def poll():
        while not events.empty():
            kind,value=events.get()
            if kind=='log':text.insert('end',value+'\n');text.see('end')
            elif kind=='done':root.destroy();return
            else:messagebox.showerror('生成未完成',value,parent=root);result.clear();root.destroy();return
        root.after(100,poll)
    threading.Thread(target=work,daemon=False).start();root.after(100,poll);root.mainloop()
    return result[0] if result else None
