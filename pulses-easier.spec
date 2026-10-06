# -*- mode: python ; coding: utf-8 -*-
"""
pulses-easier.spec — PyInstaller 打包脚本（Windows / Linux 通用）
------------------------------------------------
用法（在项目根目录）：

    pip install -r requirements.txt pyinstaller
    pyinstaller --clean --noconfirm pulses-easier.spec

产物：dist/Pulses Easier/（免安装目录版）
最终发给玩家的 zip 由 build_windows.bat 生成。

要点（改这个文件之前先看）：
  1. **onedir 而不是 onefile**：onefile 每次启动都要把上百 MB 解压到临时目录，
     tkinterdnd2 的 tkdnd 动态库加载也更脆弱；目录版启动快、杀软误报少。
  2. **assets 放在 exe 同级**（contents_directory="."）：程序里用
     `Path(__file__).parents[2] / "assets"` 定位 logo，冻结后这个根就是
     exe 所在目录，所以 assets 必须平铺在 exe 旁边，不能塞进 _internal。
  3. customtkinter / tkinterdnd2 的**数据文件**必须一起打包，否则运行时报
     "找不到主题 json / tkdnd 库"，表现为启动即崩。
  4. pywebview 走"能用就打包、不能就跳过"：它依赖 .NET(WebView2)，装不上时
     程序会自动降级用系统浏览器打开更新日志，不影响主流程。
"""
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH)
NAME = "Pulses Easier"

# ---- 数据文件 ----
datas = [(str(ROOT / "assets"), "assets")]
for pkg in ("customtkinter", "tkinterdnd2"):
    try:
        datas += collect_data_files(pkg)
    except Exception as e:                       # noqa: BLE001
        print(f"[warn] 收集 {pkg} 资源失败：{e}")

# ---- 隐藏导入（静态分析看不到的）----
hiddenimports = [
    "PIL._tkinter_finder",        # Pillow 的 tkinter 适配
    "multimark", "markdown", "tkhtmlview",
    "darkdetect",
]
try:
    import webview                                # noqa: F401
    hiddenimports += ["webview", "webview.platforms.edgechromium",
                      "clr_loader"]
except Exception:                                 # noqa: BLE001
    print("[info] 未安装 pywebview，更新日志预览将降级为系统浏览器")

# pkg_resources 的外挂件是运行时动态加载的，静态分析看不到；
# 少一个就是启动即崩（实测：先缺 jaraco，再缺 platformdirs）。
try:
    import pkg_resources                          # noqa: F401
    hiddenimports += ["pkg_resources", "jaraco", "jaraco.text",
                      "platformdirs"]
except Exception:                                 # noqa: BLE001
    pass

# 只排除确实用不到的第三方重货。
# 注意：**不要排除 setuptools / pkg_resources** —— PyInstaller 的
# pyi_rth_pkgres 运行时钩子依赖它，排掉会让程序启动即报
# "The 'jaraco' package is required"（实测踩过）。
excludes = ["matplotlib", "numpy", "scipy", "pytest", "pydoc_data"]

a = Analysis(
    ["main.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # UPX 容易被杀软误报，关掉
    console=False,              # 无控制台窗口；崩溃日志写到 logs/
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
    contents_directory=".",     # assets 平铺在 exe 旁边（见文件头第 2 条）
)
