"""
engines/__init__.py — 下载引擎注册表
------------------------------------------------
两条引擎并存，各有取舍，互不依赖（只共享任务/结果数据结构）：

  ridex  RideX 锐驰引擎（默认）
         多槽位池 + 文件内分片 + 分片直写 + 磁盘类型感知。
         见 app/core/downloader.py。

  aster  Aster 星驰引擎（自适应分片）
         策略移植自 Plain Craft Launcher 2 的 NetTask：
         单线程起步、按"最大碎片"自适应增开线程、下载源逐个降级、
         全源失败后整文件单线程重试。见 engines/aster.py。

两者对外接口完全一致：

    download_files(tasks, target_dir, threads=None, progress=None,
                   log=None, byte_progress=None, should_abort=None,
                   on_file_started=None, on_file_done=None,
                   on_file_failed=None) -> list[DownloadResult]

引擎名存 `download.engine`，玩家端在「设置 → 下载引擎」里切换。
"""

from importlib import import_module

# 显示名（设置界面用）
ENGINE_LABELS = {
    "ridex": "RideX 锐驰（默认）",
    "aster": "Aster 星驰（自适应分片）",
}

ENGINE_ORDER = ["ridex", "aster"]
DEFAULT_ENGINE = "ridex"


def normalize(name: str | None) -> str:
    """把任意输入收敛成一个已知引擎名；未知一律回默认。"""
    n = str(name or "").strip().lower()
    return n if n in ENGINE_ORDER else DEFAULT_ENGINE


def load(name: str | None = None):
    """按名字取引擎模块（惰性导入，避免循环依赖）。"""
    n = normalize(name)
    if n == "aster":
        return import_module("app.core.engines.aster")
    return import_module("app.core.downloader")


def label(name: str | None) -> str:
    return ENGINE_LABELS.get(normalize(name), ENGINE_LABELS[DEFAULT_ENGINE])


def banner(name: str | None = None) -> str:
    """一行引擎标识（日志 / 关于页用）。"""
    n = normalize(name)
    if n == "aster":
        mod = load(n)
        return (f"Aster 星驰（自适应分片，移植 PCL2 NetTask 策略）"
                f"（engine build {getattr(mod, 'BUILD', '1')}）")
    from app.core import downloader as ridex
    return ridex.engine_banner()
