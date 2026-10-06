"""
database.py — 数据库路径与配置管理
------------------------------------------------
数据库结构：
  <db_root>/
  ├── config.json
  ├── projects/<project_id>/
  └── cache/mod_sides.json

程序级配置（记录数据库路径）：
  ~/.pulses_easier/config.json
"""

import json
import os
import uuid
from pathlib import Path

DB_FORMAT_VERSION = 1

_APP_DIR = Path.home() / ".pulses_easier"
_APP_CFG = _APP_DIR / "config.json"


def _atomic_write_text(path: Path, text: str) -> bool:
    """tmp + os.replace：避免断电/崩溃留下半截 JSON（读侧会静默回默认值）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        return False

DEFAULT_WHITELIST = ["mods", "resourcepacks", "shaderpacks"]
DEFAULT_SCAN_LIST = DEFAULT_WHITELIST
DEFAULT_BLACKLIST: list[str] = []

DEFAULT_EXPORT_OPTIONS = {
    "include_hashes": True,
    "tamper_proof": True,
    "precompute_overrides": True,
    "min_size": False,
    "compress": True,
}

# ----------------------------------------------------------------------
# 下载器配置
# ----------------------------------------------------------------------
DEFAULT_DOWNLOAD_OPTIONS = {
    # 两级槽位池
    "multi_slots": 16,
    "single_slots": 4,
    # 文件内分片
    "part_threads": 4,
    "max_connections": 128,
    # 超时与看门狗
    "connect_timeout": 10,
    "read_idle_timeout": 20,
    "per_url_timeout": 90,
    "stall_timeout": 10,
    # 低速判定
    "min_speed_bps": 10240,
    "speed_window": 5.0,
    # 重试
    "retry_same_url": 1,
    "retry_backoff_ms": 500,
    # 单线程模式放宽参数
    "single_url_timeout": 180,
    "single_stall_timeout": 30,
    "single_min_speed_bps": 1024,
    # 兼容旧配置
    "file_threads": 16,
    "downgrade_after": 2,
    # ---- 下载引擎增量改进（默认值 = 修复/保持既有行为，1/0 表示开关） ----
    "read_poll_interval": 1.0,      # socket 读超时轮询粒度（秒）
    "http_status_check": 1,         # 非 2xx 视为失败，不落盘
    "range_validate": 1,            # 分片必须 206 且 Content-Range 一致
    "part_meta_enabled": 1,         # .part.meta 记录可信区间（安全续传）
    "part_meta_trust_size": 0,      # 无 meta 时也信任 .part 大小（旧行为，危险）
    "part_retry": 1,                # 单分片失败重试次数
    "shared_ssl_context": 1,        # 进程内共享 SSLContext
    "hash_while_streaming": 1,      # 单连接边下边算哈希
    "fallocate": 1,                 # 分片优先 os.posix_fallocate 预分配
    "keepalive_retry": 1,           # 陈旧 keep-alive 连接透明重试次数
    "release_response_timeout": 2.0,  # 排空响应体的最长等待（秒）
    "backoff_max_ms": 8000,         # 指数退避上限（毫秒）
    "backoff_jitter": 0.3,          # 退避抖动比例
    "retryable_status_retry": 1,    # 408/429/5xx 的额外重试次数
    "cleanup_residual_parts": 1,    # 下载结束清理残留 .part*
    "user_agent": "PulsesEasier/1.0",
    # ---- 按磁盘类型限制并发（只能调低，不能调高） ----
    "disk_aware_slots": 1,          # 1=按目标盘类型限制并发
    "disk_type_override": "",       # ""=自动探测；可填 ssd/hdd/network/removable/unknown
    "hdd_max_files": 4,             # 机械盘：同时下载的文件数上限
    "hdd_part_threads": 1,          # 机械盘：单文件分片线程上限（1=不分片）
    "ssd_max_files": 0,             # 固态：0=不额外限制
    "ssd_part_threads": 0,
    "network_max_files": 6,         # 网络盘 / 网盘挂载
    "network_part_threads": 1,
    "removable_max_files": 2,       # U 盘 / 移动硬盘 / 光驱
    "removable_part_threads": 1,
    "disk_probe_fallback": 1,       # Windows 下 IOCTL 失败时用 PowerShell 兜底
    "disk_probe_timeout": 2.0,      # 兜底探测超时（秒）
    # ---- 分片策略：按大小分级（静态，不做动态拆分） ----
    "multi_part_min_bytes": 4 * 1024 * 1024,   # 小于此值永不分片
    "target_part_size": 8 * 1024 * 1024,       # 目标片大小
    "max_part_count": 8,                       # 初始分片数上限
    "large_file_multi_first_bytes": 0,         # >0 时超大文件先试分片
}

DEFAULT_COMPARE_OPTIONS = {
    "hash_threads": 16,
}

DEFAULT_UI_OPTIONS = {
    "log_max_lines": 500,
    "progress_throttle_ms": 200,
    "boot_min_ms": 2000,       # 启动页最短显示时间
    "boot_max_ms": 3000,       # 启动页最长显示时间
    # ---- 应用阶段"搬运"节流（主要针对 Windows 的机械盘/低速盘）----
    "apply_chunk_kb": 4096,           # 跨盘复制时的分块大小（KB）
    "apply_chunk_pause_ms": 0,        # 每块之间暂停（毫秒）
    "apply_batch_mb": 96,             # 每搬多少 MB 让出一次
    "apply_batch_pause_ms": 12,       # 每批之间暂停（毫秒）
    "apply_space_check": 1,           # 应用前检查目标磁盘空间
    "apply_deep_verify": 1,           # 应用后对能拿到摘要的文件做内容级校验
    "apply_clean_cache": 1,           # 更新成功后释放本次下载缓存
}

# ----------------------------------------------------------------------
# 配置项描述（首选项 UI 显示）
# ----------------------------------------------------------------------
DOWNLOAD_OPTION_META = [
    ("multi_slots", "并行下载槽位",
     "同时并行下载几个文件。每个槽位跑一个文件（大文件会自动分片）。",
     ("如果你家宽带很快、想更快下完，可以调到 6 或 8；"
      "如果网络经常卡、或想省内存，保持 3~4。")),
    ("single_slots", "单线程重试槽位",
     "多线程槽位失败的文件，会移到单线程槽位重试。这里设置同时重试几个。",
     ("默认 2 个；如果失败文件很多、想更快补完，调到 3；"
      "如果网络很慢，保持 1 也可以。")),
    ("part_threads", "单文件分片线程数",
     "对大于 4 MB 的文件，拆成几段同时下载。",
     ("默认 4；网络不稳定时调到 2 更稳，"
      "高速网络下可以调到 6。")),
    ("max_connections", "全局连接上限",
     "所有下载任务加起来最多同时开多少条连接，防止打满网络。",
     "如果同时开游戏、看视频，调到 32；独占网络时可以调到 96。"),
    ("connect_timeout", "连接超时（秒）",
     "连不上服务器时最多等多久，超过就放弃这条链接。",
     ("如果下载源服务器在国外、经常连不上，可以调到 15；"
      "国内源保持 10。")),
    ("read_idle_timeout", "读空闲超时（秒）",
     "连接建好后，多久收不到新数据就判定为卡死。",
     ("网络波动大时可以调到 30，避免误杀；"
      "网络稳定时保持 20。")),
    ("per_url_timeout", "单链接总时限（秒）",
     "一条下载链接最多下多久，超时就换下一个链接。",
     ("如果某些源特别慢但能下完，调到 180；"
      "想快速失败换源，调到 60。")),
    ("stall_timeout", "停滞超时（秒）",
     "文件下载中若连续这么多秒没有字节增长，判定为卡死。",
     ("如果下载源偶尔会停一会儿再继续，调到 20 更宽容；"
      "想快速识别卡死，调到 5。")),
    ("min_speed_bps", "最低速度（字节/秒）",
     "下载速度长期低于这个值就判定为卡死，会换源或重试。",
     ("默认 10240（10 KB/s）；如果你在用很慢的网络，"
      "调到 2048（2 KB/s）。")),
    ("speed_window", "速度统计窗口（秒）",
     "用最近多少秒的速度判断是否卡死。",
     "默认 5 秒；想更灵敏调到 3，想更稳定调到 8。"),
    ("retry_same_url", "同一链接重试次数",
     "一个链接失败后，立即重试几次再换下一个。",
     "默认 1 次；如果某些源偶尔抽风，调到 2。"),
    ("retry_backoff_ms", "换源等待（毫秒）",
     "切换到下一个下载源之前先等一会儿，避免连续打同一个 CDN。",
     "默认 500 毫秒；如果经常被限流，调到 1500。"),
    ("single_url_timeout", "单线程模式时限（秒）",
     "移到单线程槽位重试后，一条链接最多下多久。",
     "默认 180 秒，比多线程宽松；想更快失败调到 120。"),
    ("single_stall_timeout", "单线程模式停滞超时（秒）",
     "单线程重试时，多久没数据算卡死。",
     "默认 30 秒；网络稳定时可调到 15。"),
    ("single_min_speed_bps", "单线程模式最低速度（字节/秒）",
     "单线程重试的最低速度阈值，低于就换源。",
     "默认 1024（1 KB/s）；在很慢的网络下调到 512。"),
    ("read_poll_interval", "读超时轮询粒度（秒）",
     ("多久检查一次「是不是卡住了」。数值越小越灵敏，但唤醒更频繁；"
      "它同时决定单次阻塞读的最长时间。"),
     ("默认 1.0 秒；老机器或想省电可调到 2.0；"
      "局域网高速下载可调到 0.5。")),
    ("part_retry", "分片失败重试次数",
     "单个分片下载失败后，同一条链接最多再试几次。",
     "默认 1 次；源不稳定时调到 2~3，稳定时设 0。"),
    ("http_status_check", "校验 HTTP 状态码",
     ("开启后 404/403 等错误页面不会被当成文件写入磁盘，"
      "而是直接判为失败并换源。"),
     "默认开启（1）；除非要兼容非常规的私有源，否则不建议关。"),
    ("part_meta_enabled", "安全续传（记录已下区间）",
     ("用 <文件>.part.meta 记录「哪些字节确实下好了」，"
      "只按连续前缀续传，避免把预分配的零填充文件当成下载完成。"),
     "默认开启（1）；关闭后会退回按文件大小判断的旧口径。"),
    ("cleanup_residual_parts", "收尾清理残留分片",
     "一次下载结束后，是否清掉缓存目录里遗留的 .part / .part.meta。",
     "默认开启（1）；如果希望保留分片供下次续传，设为 0。"),
    ("disk_aware_slots", "按磁盘类型自动限制并发",
     ("自动探测缓存目录所在磁盘是固态/机械/网络盘，并按类型给"
      "「同时下载的文件数」和「单文件分片线程数」设上限。"
      "机械盘是寻道瓶颈，多开文件和多段随机写都会明显变慢。"),
     ("默认开启（1）；只会在你配置的基础上**调低**，不会调高。"
      "固态一般不会触发限制。")),
    ("disk_type_override", "强制指定磁盘类型",
     ("留空 = 自动探测。如果探测不准（例如虚拟盘、RAID、网盘挂载），"
      "可以在这里直接指定。"),
     "可选值：ssd / hdd / network / removable / unknown；留空表示自动。"),
    ("hdd_max_files", "机械盘并发文件数上限",
     "目标盘是机械盘时，同时下载几个文件。",
     ("默认 4；机械盘上 2~4 比较合适，超过 4 磁头会来回寻道，"
      "总体反而更慢。")),
    ("hdd_part_threads", "机械盘单文件分片线程上限",
     ("机械盘上对同一个文件做多段随机写会让磁头反复移动，"
      "1 表示干脆不分片、顺序下载。"),
     "默认 1（不分片）；如果是 SSD 缓存加速过的混合盘，可以试 2。"),
    ("multi_part_min_bytes", "小于该大小不分片（字节）",
     ("小文件单连接最划算：多开连接要先付一次连接建立/TLS 握手"
      "（约 0.7 秒，与文件大小无关），片太小反而更慢。"),
     ("默认 4194304（4 MiB）；想更保守可调到 8388608。")),
    ("target_part_size", "目标分片大小（字节）",
     ("用来算初始分片数：分片数 = 文件大小 / 这个值，再夹在 2~上限之间。"),
     ("默认 8388608（8 MiB）；大盘鸡/高速链路可调到 4194304 让大文件多开几路。")),
    ("max_part_count", "初始分片数上限",
     "单个文件最多切成几片。",
     "默认 8；机械盘或弱网调到 4，高速链路可以到 16。"),
    ("large_file_multi_first_bytes", "超大文件优先分片（字节，0=关闭）",
     ("大于该值时先试分片、失败再退回单连接。"
      "默认 0 = 沿用「单连接优先」：本机实测单文件多连接只有 1.02~1.33× 收益，"
      "而单连接更省请求、失败面更小。"),
     "默认 0（关闭）；想给超大文件提速可以试 16777216（16 MiB）。"),
]

COMPARE_OPTION_META = [
    ("hash_threads", "哈希计算并发线程数",
     "比对文件时，同时计算多少个文件的哈希。",
     "默认 16；如果你的 CPU 较弱，调到 8 降低占用。"),
]

UI_OPTION_META = [
    ("apply_chunk_kb", "更新时单块大小（KB）",
     ("把新文件搬进整合包时，一次读写多少数据。"
      "只在「跨盘搬运」时才有意义：同一块硬盘上是瞬间移动，不产生读写。"),
     "默认 4096（4 MiB）。改成 512 会更温和但更慢。"),
    ("apply_chunk_pause_ms", "更新时每块暂停（毫秒）",
     "每搬完一块休息多久，用来避免更新时把硬盘占满、其它程序卡顿。",
     "默认 0（不停）。机械盘上更新特别卡可以设成 5~20。"),
    ("apply_batch_mb", "更新时每批大小（MB）",
     "累计搬够多少数据休息一次。",
     "默认 96；机械盘可以调到 32，让出更频繁。"),
    ("apply_batch_pause_ms", "更新时每批暂停（毫秒）",
     "每批之间休息多久。默认值对总耗时的影响不到 0.1%。",
     "默认 12；想完全不让步就设 0。"),
    ("apply_space_check", "更新前检查磁盘空间",
     "开始应用前先估算需要多少空间，放不下就直接拒绝（绝不会写一半）。",
     "默认 1（开启）。网络盘/虚拟盘估算不准时可以设 0 关闭。"),
    ("apply_deep_verify", "更新后做内容级校验",
     ("对更新包里带 SHA 摘要的文件（不太大的那些）重新算一遍哈希，"
      "确认写进去的内容真的对。预算内只读 256MB，几秒钟的事。"),
     "默认 1（开启）。想要最快完成更新可以设 0。"),
    ("apply_clean_cache", "更新完成后释放下载缓存",
     ("更新彻底成功后，把本次下载下来的文件缓存删掉（腾空间）。"
      "有失败项时会保留，方便重试。"),
     "默认 1（开启）。想留着以后手动重试可以设 0。"),
    ("log_max_lines", "日志最多保留行数",
     "日志区最多保留多少行，超出会丢弃最早的。",
     "默认 500；如果喜欢看历史日志，调到 2000。"),
    ("progress_throttle_ms", "进度刷新节流（毫秒）",
     "进度条更新的最小间隔，越大越省 CPU。",
     "默认 200；如果界面卡顿，调到 400。"),
    ("boot_min_ms", "启动页最短显示时间（毫秒）",
     "启动页至少显示多久，避免一闪而过。",
     "默认 1500（1.5 秒）；如果想更快进主界面，调到 800。"),
    ("boot_max_ms", "启动页最长显示时间（毫秒）",
     "启动页最多显示多久，即使后台任务没跑完也会关闭。",
     "默认 3000（3 秒）；如果启动任务很多，调到 4000。"),
]

# 首选项中隐藏的兼容字段
_HIDDEN_DOWNLOAD_KEYS = {"file_threads", "downgrade_after"}


# ----------------------------------------------------------------------
# 程序级配置
# ----------------------------------------------------------------------
def load_app_config() -> dict:
    try:
        if _APP_CFG.is_file():
            return json.loads(_APP_CFG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001, S110
        pass
    return {}


def save_app_config(cfg: dict):
    _atomic_write_text(
        _APP_CFG, json.dumps(cfg, ensure_ascii=False, indent=2))


def get_db_path() -> Path | None:
    cfg = load_app_config()
    p = cfg.get("db_path")
    if not p:
        return None
    path = Path(p)
    return path if path.is_dir() else None


def set_db_path(db_root: Path):
    cfg = load_app_config()
    cfg["db_path"] = str(db_root)
    save_app_config(cfg)


# ----------------------------------------------------------------------
# 数据库操作
# ----------------------------------------------------------------------
def create_database(db_root: Path) -> bool:
    try:
        db_root = Path(db_root)
        (db_root / "projects").mkdir(parents=True, exist_ok=True)
        (db_root / "cache").mkdir(parents=True, exist_ok=True)

        cfg = db_root / "config.json"
        if not cfg.is_file():
            cfg.write_text(json.dumps({
                "format_version": DB_FORMAT_VERSION,
                "whitelist": list(DEFAULT_WHITELIST),
                "blacklist": list(DEFAULT_BLACKLIST),
                "export_options": dict(DEFAULT_EXPORT_OPTIONS),
                "download": dict(DEFAULT_DOWNLOAD_OPTIONS),
                "compare": dict(DEFAULT_COMPARE_OPTIONS),
                "ui": dict(DEFAULT_UI_OPTIONS),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def is_valid_database(db_root: Path) -> bool:
    try:
        db_root = Path(db_root)
        if not (db_root / "config.json").is_file():
            return False
        return (db_root / "projects").is_dir()
    except Exception:  # noqa: BLE001
        return False


def load_db_config() -> dict:
    db = get_db_path()
    if not db:
        return {}
    try:
        cfg = json.loads((db / "config.json").read_text(encoding="utf-8"))
        cfg.setdefault("format_version", DB_FORMAT_VERSION)
        if "whitelist" not in cfg and "scan_list" in cfg:
            cfg["whitelist"] = cfg["scan_list"]
        cfg.setdefault("whitelist", list(DEFAULT_WHITELIST))
        cfg.setdefault("blacklist", list(DEFAULT_BLACKLIST))
        cfg.setdefault("export_options", dict(DEFAULT_EXPORT_OPTIONS))
        cfg.setdefault("download", dict(DEFAULT_DOWNLOAD_OPTIONS))
        cfg.setdefault("compare", dict(DEFAULT_COMPARE_OPTIONS))
        cfg.setdefault("ui", dict(DEFAULT_UI_OPTIONS))
        return cfg
    except Exception:  # noqa: BLE001
        return {}


def save_db_config(cfg: dict):
    db = get_db_path()
    if not db:
        return
    _atomic_write_text(
        db / "config.json",
        json.dumps(cfg, ensure_ascii=False, indent=2))


# ----------------------------------------------------------------------
# 白名单
# ----------------------------------------------------------------------
def get_whitelist() -> list[str]:
    cfg = load_db_config()
    return list(cfg.get("whitelist", DEFAULT_WHITELIST))


def set_whitelist(items: list[str]):
    cfg = load_db_config()
    clean: list[str] = []
    seen: set[str] = set()
    for it in items:
        s = str(it).strip().replace("\\", "/").strip("/")
        if not s or s in seen:
            continue
        seen.add(s)
        clean.append(s)
    cfg["whitelist"] = clean
    save_db_config(cfg)


def get_scan_list() -> list[str]:
    return get_whitelist()


def set_scan_list(items: list[str]):
    set_whitelist(items)


# ----------------------------------------------------------------------
# 黑名单
# ----------------------------------------------------------------------
def get_blacklist() -> list[str]:
    cfg = load_db_config()
    return list(cfg.get("blacklist", DEFAULT_BLACKLIST))


def set_blacklist(items: list[str]):
    cfg = load_db_config()
    cfg["blacklist"] = list(items)
    save_db_config(cfg)


# ----------------------------------------------------------------------
# 导出选项
# ----------------------------------------------------------------------
def get_export_options() -> dict:
    cfg = load_db_config()
    opts = dict(DEFAULT_EXPORT_OPTIONS)
    opts.update(cfg.get("export_options", {}))
    opts["compress"] = True
    return opts


def set_export_options(opts: dict):
    cfg = load_db_config()
    cur = dict(DEFAULT_EXPORT_OPTIONS)
    cur.update(cfg.get("export_options", {}))
    cur.update(opts)
    cur["compress"] = True
    cfg["export_options"] = cur
    save_db_config(cfg)


# ----------------------------------------------------------------------
# 下载器 / 比对 / 界面配置
# ----------------------------------------------------------------------
def _get_section(section: str, defaults: dict) -> dict:
    cfg = load_db_config()
    opts = dict(defaults)
    opts.update(cfg.get(section, {}))
    return opts


def _set_section(section: str, defaults: dict, opts: dict):
    cfg = load_db_config()
    cur = dict(defaults)
    cur.update(cfg.get(section, {}))
    cur.update(opts)
    cfg[section] = cur
    save_db_config(cfg)


# 旧版本默认值 → 新版本默认值（仅在用户未修改过该项时升级）
_DOWNLOAD_MIGRATE = {
    "multi_slots": [(4, 16), (8, 16)],
    "single_slots": [(2, 4), (3, 4)],
    "part_threads": [(6, 4)],
    "max_connections": [(64, 128)],
    "file_threads": [(4, 16), (8, 16)],
}


def get_download_options() -> dict:
    opts = _get_section("download", DEFAULT_DOWNLOAD_OPTIONS)
    # 迁移：仅当某项等于某个旧默认值时，升级为新默认值
    changed = False
    cfg = load_db_config()
    section = cfg.get("download", {})
    for key, pairs in _DOWNLOAD_MIGRATE.items():
        if key not in section:
            continue
        cur = section.get(key)
        for old_val, new_val in pairs:
            if cur == old_val:
                section[key] = new_val
                opts[key] = new_val
                changed = True
                break
    if changed:
        cfg["download"] = section
        save_db_config(cfg)
    return opts


def set_download_options(opts: dict):
    _set_section("download", DEFAULT_DOWNLOAD_OPTIONS, opts)


def get_compare_options() -> dict:
    return _get_section("compare", DEFAULT_COMPARE_OPTIONS)


def set_compare_options(opts: dict):
    _set_section("compare", DEFAULT_COMPARE_OPTIONS, opts)


def get_ui_options() -> dict:
    return _get_section("ui", DEFAULT_UI_OPTIONS)


def set_ui_options(opts: dict):
    _set_section("ui", DEFAULT_UI_OPTIONS, opts)


# ----------------------------------------------------------------------
# 项目
# ----------------------------------------------------------------------
def new_project_id() -> str:
    return uuid.uuid4().hex[:12]


def project_dir(project_id: str) -> Path | None:
    db = get_db_path()
    if not db:
        return None
    return db / "projects" / project_id


def save_project(project_id: str, data: dict) -> bool:
    d = project_dir(project_id)
    if not d:
        return False
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "project.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def load_project(project_id: str) -> dict | None:
    d = project_dir(project_id)
    if not d:
        return None
    try:
        return json.loads((d / "project.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------
# 策略配置导入/导出
# ----------------------------------------------------------------------
def build_profile(strategies: dict, export_options: dict,
                  blacklist: list[str] | None = None,
                  whitelist: list[str] | None = None) -> dict:
    return {
        "format_version": DB_FORMAT_VERSION,
        "strategies": dict(strategies or {}),
        "export_options": dict(export_options or {}),
        "blacklist": list(blacklist or []),
        "whitelist": list(whitelist or get_whitelist()),
    }


def export_profile(path: Path, profile: dict) -> bool:
    try:
        Path(path).write_text(
            json.dumps(profile, ensure_ascii=False, indent=2),
            encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def import_profile(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None