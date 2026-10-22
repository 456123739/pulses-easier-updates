"""
mirrors.py — 下载镜像源（Modrinth 优先走镜像）
================================================
官方 CDN（`cdn.modrinth.com`）在国内经常比镜像慢，所以下载时把镜像地址
排在前面：镜像通了就直接用，镜像挂了就退回官方地址，对上层完全透明。

设计要点：

* **只在有镜像配置时生效**：`download.mirror_first = 0` 或
  `download.mirror_hosts` 为空 → 原样返回，行为与旧版一致。
* **镜像地址是"派生"的**，不改任务本身：`ordered(urls)` 只返回一个新列表。
* **进程内健康度**：某个镜像连续失败 3 次就整轮跳过，避免每个文件都先
  等一次超时。成功一次立刻清零。
* 同一个镜像会生成两种路径（`<host><path>` 与 `<host>/modrinth<path>`），
  让不同实现的反向代理都能命中；哪种能用由健康度自动筛掉。

配置（设置 → 下载）：

    mirror_first  1/0      默认 1
    mirror_hosts  https://mod.mcimirror.top;https://...   分号分隔
"""

import threading
import urllib.parse

# 默认镜像：MCIM（国内常用的 Modrinth/CurseForge 反代）
DEFAULT_HOSTS = "https://mod.mcimirror.top"

# 只给这些官方主机派生镜像
MIRRORED_HOSTS = ("cdn.modrinth.com", "cdn-raw.modrinth.com")

_FAIL_LIMIT = 3

_lock = threading.Lock()
_fail_count: dict[str, int] = {}     # host → 连续失败次数
_dead: set[str] = set()


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
def _settings() -> tuple[bool, list[str]]:
    try:
        from . import database as db
        opts = db.get_download_options()
    except Exception:  # noqa: BLE001
        opts = {}
    try:
        enabled = bool(int(opts.get("mirror_first", 1)))
    except (TypeError, ValueError):
        enabled = True
    raw = str(opts.get("mirror_hosts", DEFAULT_HOSTS) or "")
    hosts: list[str] = []
    for part in raw.replace("；", ";").replace(",", ";").split(";"):
        h = part.strip().rstrip("/")
        if h and "://" in h and h not in hosts:
            hosts.append(h)
    return enabled, hosts


def enabled() -> bool:
    on, hosts = _settings()
    return on and bool(hosts)


# ----------------------------------------------------------------------
# 派生
# ----------------------------------------------------------------------
def _split(url: str) -> tuple[str, str]:
    try:
        p = urllib.parse.urlsplit(url)
        return p.netloc.lower(), p.path or "/"
    except Exception:  # noqa: BLE001
        return "", ""


def candidates(url: str) -> list[str]:
    """给一个官方地址返回它的镜像候选（按配置顺序，已去掉不可用的）。"""
    on, hosts = _settings()
    if not on or not hosts:
        return []
    host, path = _split(url)
    if host not in MIRRORED_HOSTS:
        return []
    out: list[str] = []
    with _lock:
        dead = set(_dead)
    for base in hosts:
        if base in dead:
            continue
        for cand in (base + path, base + "/modrinth" + path):
            if cand not in out:
                out.append(cand)
    return out


def ordered(urls: list[str]) -> list[str]:
    """镜像在前、官方在后；无镜像配置时原样返回（同一个对象）。"""
    if not urls:
        return urls
    on, hosts = _settings()
    if not on or not hosts:
        return urls
    head: list[str] = []
    tail: list[str] = []
    for u in urls:
        cands = candidates(u)
        if cands:
            head.extend(cands)
            tail.append(u)
        else:
            tail.append(u)
    if not head:
        return urls
    seen: set[str] = set()
    out: list[str] = []
    for u in head + tail:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ----------------------------------------------------------------------
# 健康度（进程内）
# ----------------------------------------------------------------------
def _base_of(url: str) -> str:
    try:
        p = urllib.parse.urlsplit(url)
    except Exception:  # noqa: BLE001
        return ""
    return f"{p.scheme}://{p.netloc}".rstrip("/")


def note_failure(url: str) -> None:
    base = _base_of(url)
    if not base:
        return
    on, hosts = _settings()
    if base not in hosts:
        return
    with _lock:
        n = _fail_count.get(base, 0) + 1
        _fail_count[base] = n
        if n >= _FAIL_LIMIT:
            _dead.add(base)


def note_success(url: str) -> None:
    base = _base_of(url)
    if not base:
        return
    with _lock:
        _fail_count.pop(base, None)
        _dead.discard(base)


def is_mirror(url: str) -> bool:
    base = _base_of(url)
    on, hosts = _settings()
    return bool(base) and base in hosts


def reset() -> None:
    """测试用：清空健康度。"""
    with _lock:
        _fail_count.clear()
        _dead.clear()


def dead_hosts() -> set[str]:
    with _lock:
        return set(_dead)
