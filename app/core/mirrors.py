"""
mirrors.py — 下载镜像站表（Modrinth / CurseForge）
====================================================
官方 CDN（`cdn.modrinth.com`）在国内经常偏慢，所以下载时把镜像地址排在前面：
镜像通了就用，挂了就退回官方，对上层完全透明。

**这张表是数据，不是逻辑**：新增/停用一个镜像只改 `MIRROR_SITES`，不用动引擎。
每个条目记录"什么时候核过"（`checked`），方便下次排查。

现状（2026-10 核对）：
  * MCIM（mod.mcimirror.top / api.mcimirror.top）已停服（503 / DNS 不可达），
    条目保留但 `enabled=False`，服务恢复后改一个字段就能用。
  * BMCLAPI 只镜像 Minecraft 原版资源（版本/库/资源文件），不含模组文件，
    所以不在本表里（模组下载用不上）。
  * 公开的通用反代（corsproxy / allorigins / codetabs …）实测无法承载二进制
    大文件（401/520/522/400），不进表。
  * 官方 CDN 永远是最后兜底，不依赖表。

如果自己有加速通道（例如自建的 Cloudflare Worker），两种写法都能直接进表：

    MirrorSite("my-worker", "我的 Worker",
               template="https://my-worker.example.workers.dev/?url={qurl}")
    MirrorSite("my-proxy", "我的反代", base="https://cdn.example.com")

设置里的「镜像地址」(`download.mirror_hosts`) 是**临时追加**的前缀列表，
不用改代码就能试；和表里的条目一起参与排序。

自检：`python scripts/mirror_check.py` —— 对每个镜像实测延迟/速度并排序。
"""

import threading
import urllib.parse
from dataclasses import dataclass

# ----------------------------------------------------------------------
# 表
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class MirrorSite:
    """一个镜像站。base 与 template 二选一。"""

    key: str
    name: str
    base: str = ""                     # 前缀式：base + 官方 path
    template: str = ""                 # 模板式：含 {url} 或 {qurl}
    kinds: tuple[str, ...] = ("modrinth",)
    enabled: bool = True
    note: str = ""
    checked: str = ""                  # 最后一次人工核对日期

    def build(self, official: str, path: str) -> str:
        if self.template:
            q = urllib.parse.quote(official, safe="")
            return (self.template
                    .replace("{qurl}", q)
                    .replace("{url}", official))
        return self.base.rstrip("/") + path


#: 主机 → 内容类型
HOST_KIND = {
    "cdn.modrinth.com": "modrinth",
    "cdn-raw.modrinth.com": "modrinth",
    "edge.forgecdn.net": "curseforge",
    "mediafilez.forgecdn.net": "curseforge",
}

MIRROR_SITES: list[MirrorSite] = [
    # ---- 已停服，保留记录（恢复后把 enabled 改回 True 即可）----
    MirrorSite(
        key="mcim", name="MCIM（Modrinth 反代）",
        base="https://mod.mcimirror.top", kinds=("modrinth",),
        enabled=False,
        note="2026-10 起全站 503，DNS 仅返回 IPv6；服务恢复后可重新启用",
        checked="2026-10-22"),
    MirrorSite(
        key="mcim-api", name="MCIM（API 反代）",
        base="https://api.mcimirror.top", kinds=("modrinth",),
        enabled=False,
        note="DNS 不可达（已随主站下线）", checked="2026-10-22"),
    MirrorSite(
        key="bmclapi", name="BMCLAPI",
        base="https://bmclapi2.bangbang93.com", kinds=("minecraft",),
        enabled=False,
        note="只镜像 MC 原版资源（版本/库/资源），没有模组文件；本工具用不上",
        checked="2026-10-22"),
]

# 失败几次后本次运行不再用这个镜像
_FAIL_LIMIT = 3

_lock = threading.Lock()
_fail_count: dict[str, int] = {}
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
    raw = str(opts.get("mirror_hosts", "") or "")
    hosts: list[str] = []
    for part in raw.replace("；", ";").replace(",", ";").split(";"):
        h = part.strip().rstrip("/")
        if h and "://" in h and h not in hosts:
            hosts.append(h)
    return enabled, hosts


def _active() -> list[MirrorSite]:
    """表里启用的 + 设置里临时追加的（后者排前面，方便覆盖）。"""
    out: list[MirrorSite] = []
    _on, hosts = _settings()
    for h in hosts:
        out.append(MirrorSite(key="user:" + h, name=h, base=h,
                              checked="(设置)"))
    out.extend(s for s in MIRROR_SITES if s.enabled)
    return out


def enabled() -> bool:
    on, _hosts = _settings()
    return on and bool(_active())


def sites(include_disabled: bool = False) -> list[MirrorSite]:
    """当前生效的镜像表（自检脚本用）。"""
    if include_disabled:
        return list(MIRROR_SITES)
    return _active()


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
    """给一个官方地址返回它的镜像候选（按表顺序，已去掉不可用的）。"""
    on, _hosts = _settings()
    if not on:
        return []
    host, path = _split(url)
    kind = HOST_KIND.get(host)
    if not kind:
        return []
    out: list[str] = []
    with _lock:
        dead = set(_dead)
    for site in _active():
        if kind not in site.kinds:
            continue
        base = _base_of_site(site)
        if base and base in dead:
            continue
        cand = site.build(url, path)
        if cand and cand not in out:
            out.append(cand)
    return out


def ordered(urls: list[str]) -> list[str]:
    """镜像在前、官方在后；没有可用镜像时原样返回（同一个对象）。"""
    if not urls:
        return urls
    on, _hosts = _settings()
    if not on:
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


def _base_of_site(site: MirrorSite) -> str:
    if site.base:
        return site.base.rstrip("/")
    if site.template:
        return _base_of(site.template.replace("{qurl}", "")
                        .replace("{url}", ""))
    return ""


def _known_bases() -> set[str]:
    return {b for b in (_base_of_site(s) for s in _active()) if b}


def note_failure(url: str) -> None:
    base = _base_of(url)
    if not base or base not in _known_bases():
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
    return _base_of(url) in _known_bases()


def reset() -> None:
    """测试用：清空健康度。"""
    with _lock:
        _fail_count.clear()
        _dead.clear()


def dead_hosts() -> set[str]:
    with _lock:
        return set(_dead)
