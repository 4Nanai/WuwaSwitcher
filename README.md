# WuwaSwitcher

[![Windows build and release](https://github.com/4Nanai/WuwaSwitcher/actions/workflows/release.yml/badge.svg)](https://github.com/4Nanai/WuwaSwitcher/actions/workflows/release.yml)

Windows 上的鸣潮国服 / Steam 国际服资源切换工具。共用一套主资源，保留区域差异和快速缓存，通过原始资源还原与文件重命名切换。

## 下载与首次使用

从 [Releases](https://github.com/4Nanai/WuwaSwitcher/releases/latest) 下载 `WuwaSwitcher-v版本-windows-x64.zip`，解压到可写目录，运行 `WuwaSwitcher.exe`。支持 Windows 10/11 x64。

- 已使用旧版本共用资源：把新版 EXE 放在原工具目录；若要求选择路径，选择 Steam 游戏根目录，新版会读取其中 `.wuwa-switcher` 的现有档案与缓存，无需重装。
- 第一次使用：需要两套同版本、同资源档位的独立客户端。按提示选择两个游戏根目录（包含 `Client`），工具只读取它们并生成本机档案。完成后点击“安装共用资源”，再点击“启用快速缓存”。
- 确认两区都能正常启动后，可点击“释放重复资源”；原国服备份的删除由此按钮执行。

仓库和发布包只包含工具，不包含游戏正文、资源差异包、账号或缓存。第一次生成档案仍需读取两套客户端资源，耗时取决于磁盘速度。初始档案生成失败时，原游戏文件保持原样；检查输出目录旁的 `.building` 目录后，可改用新的输出位置重试。

## 日常使用与更新

关闭游戏后，点击“国服：切换并启动”或“国际服 / Steam：切换并启动”。统一由工具选择对应资源后启动。

之后更新只需：

1. 点击“准备更新 Steam”。
2. 在 Steam 完成国际服主客户端更新。
3. 回工具点击“同步国服更新”。

同步读取官方两区清单，优先复用国际服本地分块，仅从官方 CDN 获取无法验证复用的国服数据及程序组件。逐块与整文件 MD5 校验通过后，生成双向差异及 SHA-256 记录。Steam 专属程序保持原样。

同步采用流式处理，不会临时写出整套国服 Content。需要容纳新版区域缓存、差异档案和 SDK；提交成功前会保留旧缓存。下载量与空间取决于版本差异，无法复用的块会从官方重新下载。

游戏内热更新、语音、账号和设置仍分别管理。自动同步覆盖官方启动器清单中的主资源与国服程序组件，尚未实现完整的 `Saved/Resources` 热更新协议。两区主版本不一致或目录协议发生变化时会停止同步。未来未发布版本的兼容性无法提前保证。

## 从源码运行与构建

使用 Windows x64 和 Python 3.12：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
python src/wuwa_switcher.py
```

也可以明确指定本机档案生成路径（目标目录必须尚不存在）：

```powershell
python src/wuwa_switcher.py --create-bundle "D:\Games\WuwaGlobal" "G:\Games\WuwaCN" "D:\Tools\WuwaSwitcher.data"
```

把生成的 `WuwaSwitcher.data` 放在 EXE 旁边即可读取；或使用环境变量 `WUWA_SWITCHER_DATA` 指定其位置。GUI 首次配置会把所选档案路径记录在 EXE 旁的 `WuwaSwitcher.json`，旧安装的状态保存在 Steam 游戏目录的 `.wuwa-switcher`。

```powershell
python tests/test_setup.py
python tests/test_switcher.py
python tests/test_updater.py
python tests/test_streaming.py
python tools/build.py
python tools/smoke_exe.py
```

产物位于 `dist/`，包含 EXE、ZIP 与 `SHA256SUMS.txt`。测试使用合成文件和模拟 CDN，不读取真实游戏安装，也不需要访问游戏服务器。

## 发布新版本

主分支 push、Pull Request 及手动运行 Actions 会执行测试、构建和 EXE 自检，并保存构建产物。推送版本标签会在这些检查通过后自动创建 GitHub Release，上传 EXE、ZIP 和校验和。

1. 修改 `src/app_version.py` 中的版本号，并更新 `CHANGELOG.md`。
2. 提交并推送到 `main`。
3. 推送与版本号完全相同的 `v` 前缀标签：

```powershell
git tag v2.1.1
git push origin v2.1.1
```

标签必须与源码版本一致。无需添加个人 Token：发布任务使用 GitHub 自动提供的 `GITHUB_TOKEN`，仅该任务拥有 `contents: write`。首次上传时先创建草稿，附件上传成功后再发布；失败可重新运行任务。含 `-` 的版本标签会标为预发布。

工作流采用 GitHub 的 [标签触发机制](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)，并使用 [GitHub CLI](https://cli.github.com/manual/gh_release_create) 管理 Release。

## 依赖与实现范围

Python 标准库、Tkinter、`pyxdelta`，打包使用 PyInstaller。第三方许可证见 `licenses/`。

工具不提取解密密钥、不修改游戏可执行程序。切换档案在本机由用户自己的客户端生成；工具与库洛、Steam 无隶属关系。
