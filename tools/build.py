"""Build the Windows release from source; no local client or resource bundle needed."""
import hashlib
import pathlib
import shutil
import subprocess
import sys
import zipfile

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from app_version import VERSION


def main():
    if sys.platform!='win32':raise SystemExit('Build on Windows x64 with Python 3.12')
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed',
                    '--name','WuwaSwitcher','--hidden-import','wuwa_update','--hidden-import','wuwa_setup',
                    '--distpath',str(ROOT/'dist'),'--workpath',str(ROOT/'build/pyinstaller'),
                    '--specpath',str(ROOT/'build'),str(ROOT/'src/wuwa_switcher.py')],check=True,cwd=ROOT)
    package=ROOT/'build'/'package';package.mkdir(parents=True,exist_ok=True)
    shutil.copy2(ROOT/'dist/WuwaSwitcher.exe',package/'WuwaSwitcher.exe')
    for name in ['README.md','CHANGELOG.md']:shutil.copy2(ROOT/name,package/name)
    shutil.copytree(ROOT/'licenses',package/'licenses',dirs_exist_ok=True)
    archive=ROOT/'dist'/f'WuwaSwitcher-v{VERSION}-windows-x64.zip'
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(package.rglob('*')):
            if path.is_file():z.write(path,path.relative_to(package))
    checksum=ROOT/'dist/SHA256SUMS.txt'
    checksum.write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
                              for p in [archive,ROOT/'dist/WuwaSwitcher.exe']),encoding='utf-8',newline='\n')
    print(archive)


if __name__=='__main__':main()
