# Pulses Easier 打包 beta 版（PyInstaller）

> 目标产物：一个 zip，解压后里面**有软件本体、有 assets（含 logo）**，
> 双击即用，不需要装 Python。

## 一、环境准备（只做一次）

1. Windows 10/11 + Python **3.11**（安装时勾选自带的 *Add Python to PATH*）。
   > 用 3.11 打包，和开发/测试时的版本一致，避免 tkinter 行为差异。
2. 在项目根目录执行：

```bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller
```

## 二、打包（一条命令）

```bat
build_windows.bat
```

脚本做四件事：装依赖 → 清旧产物 → `pyinstaller pulses-easier.spec` →
组装目录并压成 zip。产物：

```
dist/pulses-easier-0.6.1-beta-win64.zip
```

解压后的目录长这样（**assets 就在 exe 旁边**，这就是要的效果）：

```
pulses-easier-0.6.1-beta-win64/
├── Pulses Easier.exe          ← 双击运行（已带图标）
├── assets/                    ← logo 与图标
│   ├── logo.png               （侧边栏）
│   ├── Pulses0-new.png        （启动页）
│   ├── app.ico                （exe 图标；想换图标就覆盖这个文件）
│   └── app_icon.png           （app.ico 的 256px 预览）
├── logs/                      ← 崩溃/启动日志（自动写入）
├── README.md / CHANGELOG.md
├── base_library.zip
├── python311.dll / _tkinter.pyd / ...（运行时）
├── customtkinter/  tkinterdnd2/  webview/ ...（第三方资源）
└── app/  （本程序的模块）
```

想手动跑（不用脚本）：

```bat
pyinstaller --clean --noconfirm pulses-easier.spec
```

Linux/macOS 上打包（用于自测，产物是 Linux 可执行文件，不能发给 Windows 玩家）：

```bash
pyinstaller --clean --noconfirm pulses-easier.spec && ./dist/Pulses\ Easier/Pulses\ Easier
```

## 三、spec 里几个不能乱改的点

1. **用 onedir，不用 onefile**。onefile 每次启动都要把上百 MB 解压到临时目录，
   启动慢、杀软误报多，tkinterdnd2 的 tkdnd 动态库加载也更脆。
2. **`contents_directory="."`**：让 `assets/` 平铺在 exe 同级。
   程序里用 `Path(__file__).resolve().parents[2] / "assets"` 找 logo，
   冻结后这个根就是 exe 所在目录 —— 把 assets 塞进 `_internal/` 会直接启动失败。
3. **customtkinter / tkinterdnd2 的数据文件必须收集**（spec 里
   `collect_data_files`）。少了就是"启动就崩"，报错一般是
   `FileNotFoundError: ...customtkinter/assets/themes/...` 或
   `找不到 tkdnd 库`。
4. **`console=False`**：玩家看到的是干净窗口；程序会把 stdout/stderr 写进
   `logs/pulses-easier.log`（`main.py::_setup_frozen_logging`）。
   自己排查问题时可以临时改成 `console=True` 重新打包，就能看到实时输出。
5. **`upx=False`**：UPX 压缩极易被杀软误判成病毒，beta 阶段不要开。
6. **pywebview 可选**：它依赖 .NET / WebView2；打包机装不上时会跳过，
   程序里会自动降级用系统浏览器打开更新日志。要完整功能就确保
   `pip install pywebview` 成功（spec 会自动加隐藏导入）。

## 四、发版前自测清单（5 分钟）

| # | 操作 | 期望 |
|---|---|---|
| 1 | 解压 zip → 双击 `Pulses Easier.exe` | 启动页 → 主界面，**没有黑控制台窗口** |
| 2 | 首次运行 | 弹出"选择数据库文件夹"向导（或直接进主界面） |
| 3 | 拖入一个 `.eapack` 更新包 | 拖拽可用（说明 tkdnd 打包正确） |
| 4 | 走一遍"定位整合包 → 比对 → 更新" | 与源码运行表现一致（应用/替换阶段尤其要看） |
| 5 | 看 `logs/pulses-easier.log` | 有 `启动 Pulses Easier v0.6.1` 一行 |
| 6 | 关掉软件再开 | 数据库/最近记录还在 |
| 7 | 点开发者端的"更新日志预览" | 弹出独立预览窗口；失败会降级到浏览器 |

出问题先看 `logs/pulses-easier.log`；要更细的现场就用 `console=True` 重打一次。

## 五、换成你自己的图标 / 换版本号

* **图标**：现在是拿 `assets/Pulses0-new.png`（横幅）自动生成的方形近似图标
  —— 深色底 + 白色 logo，能用但不算精致。**建议你给一张 256×256 以上的方形
  logo**，覆盖 `assets/app.ico` 后重新打包即可（spec 自动使用）。
* **版本号**：改 `app/core/__init__.py` 的 `__version__` 与
  `build_windows.bat` 顶部的 `VERSION=`，两处保持一致。
