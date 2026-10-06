#!/usr/bin/env python3
"""
realworld_test.py — Easier 下载器真实网络实战测试。

流程（尽量跑项目自己的代码，不另写一套逻辑）：
  1. 从 Modrinth 官方 API 取 10 个不同的 mod（大小铺开，最大的接近 20MB）
  2. 从 CurseForge（经 mod.mcimirror.top 镜像取元数据，文件仍走官方 CDN）
     取 10 个不同的 mod
  3. 组装成 Easier 的清单结构（path / downloads / sha1 / file_size）
  4. **用项目自己的 differ 做分类**（空客户端实例 → 全部落进 added）
  5. **用项目自己的 player_view._collect_download_tasks_from_diff 生成
     DownloadTask**（这就是 Easier 点「开始更新」时真正走的那一步）
  6. 三种下载方式各跑一遍、逐文件校验 sha1：
       A. 裸 urllib 顺序单线程（一个下完再下一个）
       B. 原版 downloader（改动前，出厂默认参数）
       C. RideX 锐驰引擎（本次交付，出厂默认参数 + 磁盘类型策略）
  7. 打印参数对照表 + 实测结果

用法：
    python3 tests/realworld_test.py            # 完整跑
    python3 tests/realworld_test.py --dry-run  # 只取元数据、不下载
    python3 tests/realworld_test.py --keep     # 保留下载目录（默认跑完删掉）
"""

import argparse
import hashlib
import importlib.util
import json
import shutil
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

UA = "PulsesEasier/0.2.0 (realworld-test)"
MR_API = "https://api.modrinth.com/v2"
CF_API = "https://mod.mcimirror.top/curseforge/v1"
SIZE_CAP = 20 * 1024 * 1024
WORK = _ROOT / "_realworld"
META_CACHE = _HERE / "logs" / "realworld_meta.json"


# ----------------------------------------------------------------------
# HTTP 小工具
# ----------------------------------------------------------------------
def http_json(url, timeout=40):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def http_json_retry(url, tries=3, timeout=15):
    """真实网络会偶尔碰到坏路由/瞬时 5xx，这里重试几次。"""
    last = None
    for i in range(tries):
        try:
            return http_json(url, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.3 * (i + 1))
    raise last if last else RuntimeError("unreachable")


def parallel_map(fn, items, workers=8):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def pick_spread(pool, want):
    """按大小分位挑 want 个，保证小/中/大都有。"""
    pool = sorted(pool, key=lambda x: x["file_size"])
    if len(pool) <= want:
        return pool
    idx = [round(i * (len(pool) - 1) / (want - 1)) for i in range(want)]
    seen, out = set(), []
    for i in idx:
        if i not in seen:
            seen.add(i)
            out.append(pool[i])
    # 分位可能撞车，用剩下的补满
    for item in pool:
        if len(out) >= want:
            break
        if item not in out:
            out.append(item)
    return out[:want]


def fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0


# ----------------------------------------------------------------------
# 1) Modrinth
# ----------------------------------------------------------------------
MR_SLUGS = [
    "fabric-api", "sodium", "lithium", "iris", "jei", "rei", "create",
    "distanthorizons", "alexscaves", "iceandfire", "twilightforest",
    "immersiveengineering", "epicfight", "botania", "thermal-expansion",
    "mekanism", "appliedenergistics2", "powah", "modernfix", "ferritecore",
    "cloth-config", "architectury-api", "geckolib", "patchouli",
    "terrablender", "citadel", "yungsapi", "cristellib", "supermartijn642corelib",
    "moonlight", "kotlinforforge", "farmers-delight", "waystones",
    "journeymap", "xaeros-minimap", "jade", "wthit", "emi", "roughly-enough-items",
]


def _pick_best_modrinth(slug, log=print):
    try:
        versions = http_json_retry(f"{MR_API}/project/{slug}/version")
    except Exception as e:  # noqa: BLE001
        log(f"    ! Modrinth {slug} 取版本失败：{type(e).__name__}")
        return None
    best = None
    for v in versions:
        if v.get("version_type") not in ("release", "beta"):
            continue
        for f in v.get("files", []):
            if not f.get("primary"):
                continue
            size = int(f.get("size") or 0)
            if size <= 0 or size > SIZE_CAP:
                continue
            if best is None or size > best["file_size"]:
                best = {
                    "source": "Modrinth",
                    "name": slug,
                    "title": v.get("name", ""),
                    "filename": f["filename"],
                    "urls": [f["url"]],
                    "file_size": size,
                    "sha1": (f.get("hashes") or {}).get("sha1", ""),
                    "sha512": (f.get("hashes") or {}).get("sha512", ""),
                    "game_versions": v.get("game_versions", []),
                    "version": v.get("version_number", ""),
                }
    return best


def fetch_modrinth(want=10, log=print):
    log(f"  Modrinth：并发查询 {len(MR_SLUGS)} 个项目 ...")
    pool = [p for p in parallel_map(_pick_best_modrinth, MR_SLUGS)
            if p]
    log(f"  Modrinth 候选 {len(pool)} 个，挑 {want} 个")
    return pick_spread(pool, want)


# ----------------------------------------------------------------------
# 2) CurseForge（元数据走镜像，文件走官方 CDN）
# ----------------------------------------------------------------------
def _pick_best_curseforge(mod, log=print):
    mid = mod.get("id")
    if not mid:
        return None
    try:
        files = http_json_retry(f"{CF_API}/mods/{mid}/files?pageSize=50")
    except Exception:  # noqa: BLE001
        return None
    best = None
    for f in files.get("data") or []:
        if f.get("releaseType") != 1 or not f.get("downloadUrl"):
            continue
        size = int(f.get("fileLength") or 0)
        if size <= 0 or size > SIZE_CAP:
            continue
        gv = f.get("gameVersions") or []
        if not any(str(g).startswith(("1.21", "1.20")) for g in gv):
            continue
        sha1 = ""
        for h in f.get("hashes") or []:
            if h.get("algo") == 1:
                sha1 = h.get("value", "")
        if not sha1:
            continue
        if best is None or size > best["file_size"]:
            best = {
                "source": "CurseForge",
                "name": str(mod.get("slug") or mid),
                "title": mod.get("name", ""),
                "filename": f.get("fileName", ""),
                "urls": [f["downloadUrl"]],
                "file_size": size,
                "sha1": sha1,
                "sha512": "",
                "game_versions": gv,
                "version": str(f.get("displayName", "")),
            }
    return best


def fetch_curseforge(want=10, log=print):
    try:
        search = http_json_retry(
            f"{CF_API}/mods/search?gameId=432&classId=6"
            f"&pageSize=50&sortField=2&sortOrder=desc")
    except Exception as e:  # noqa: BLE001
        log(f"  CurseForge 搜索失败：{e}")
        return []
    mods = search.get("data") or []
    log(f"  CurseForge：并发查询 {len(mods)} 个项目的文件列表 ...")
    pool = [p for p in parallel_map(_pick_best_curseforge, mods) if p]
    log(f"  CurseForge 候选 {len(pool)} 个，挑 {want} 个")
    return pick_spread(pool, want)


# ----------------------------------------------------------------------
# 3) Easier 自己的分类 + 任务生成
# ----------------------------------------------------------------------
def build_tasks_with_easier(items, work: Path, log=print):
    """
    走项目自己的代码：
        differ.diff_packs_parallel  →  DiffResult（分类）
        PlayerView._collect_download_tasks_from_diff → list[DownloadTask]
    """
    import stubs
    stubs.install()
    from app.core import differ
    from app.core.downloader import DownloadTask
    from app.ui.player_view import PlayerView

    merged = []
    index_hashes = {}
    for it in items:
        rel = f"mods/{it['filename']}"
        it["rel_path"] = rel
        merged.append({
            "path": rel,
            "downloads": list(it["urls"]),
            "sha1": it["sha1"],
            "sha256": "",
            "sha512": it.get("sha512", ""),
            "file_size": it["file_size"],
        })
        index_hashes[rel] = {"sha1": it["sha1"]}

    old_root = work / "instance_old"      # 玩家当前实例（空）
    new_root = work / "instance_new"      # 新版整合包解包目录（空）
    old_root.mkdir(parents=True, exist_ok=True)
    new_root.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    diff = differ.diff_packs_parallel(old_root, new_root,
                                      index_hashes=index_hashes,
                                      threads=8)
    dt = time.perf_counter() - t0
    log(f"  differ 分类：added={len(diff.added)} modified={len(diff.modified)} "
        f"deleted={len(diff.deleted)}  用时 {dt * 1000:.0f}ms")

    targets = work / "downloads"
    targets.mkdir(parents=True, exist_ok=True)
    tasks = PlayerView._collect_download_tasks_from_diff(
        None, diff, merged, targets, checked=None, strategies=None)
    log(f"  player_view 生成下载任务：{len(tasks)} 个"
        f"（应为 {len(items)}）")
    assert all(isinstance(t, DownloadTask) for t in tasks)
    return tasks, merged, targets


# ----------------------------------------------------------------------
# 4) 三种下载方式
# ----------------------------------------------------------------------
def verify_all(tasks, items, root: Path):
    by_rel = {it["rel_path"]: it for it in items}
    ok, bad, total = 0, [], 0
    for t in tasks:
        it = by_rel.get(t.rel_path)
        p = root / t.rel_path
        total += 1
        if not p.is_file():
            bad.append((t.rel_path, "缺失"))
            continue
        if it and it["file_size"] and p.stat().st_size != it["file_size"]:
            bad.append((t.rel_path,
                        f"大小 {p.stat().st_size}≠{it['file_size']}"))
            continue
        if it and it["sha1"]:
            h = hashlib.sha1()
            with open(p, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            if h.hexdigest() != it["sha1"]:
                bad.append((t.rel_path, "sha1 不符"))
                continue
        ok += 1
    return ok, total, bad


def arm_naive(tasks, root: Path, budget=900.0, log=print):
    """
    A：裸 urllib，一个下完再下一个（每次新建连接、最后统一算哈希）。

    budget：总时间预算。顺序下载在这种链路上可能很久，超预算就停下并
    如实报告完成了几个 —— 这本身就是结论的一部分。
    """
    root.mkdir(parents=True, exist_ok=True)
    conns = 0
    done = 0
    bytes_done = 0
    timed_out = False
    t0 = time.perf_counter()
    for t in tasks:
        if time.perf_counter() - t0 > budget:
            timed_out = True
            break
        dst = root / t.rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            req = urllib.request.Request(t.urls[0], headers={
                "User-Agent": UA, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=120) as r:
                conns += 1
                with open(dst, "wb") as f:
                    while chunk := r.read(1 << 18):
                        f.write(chunk)
            with open(dst, "rb") as f:      # 单线程：下完再算一遍哈希
                h = hashlib.sha1()
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            done += 1
            bytes_done += t.file_size
        except Exception as e:  # noqa: BLE001
            log(f"    ! A 失败 {t.rel_path}：{type(e).__name__}: {e}")
    return {"elapsed": time.perf_counter() - t0, "conns": conns,
            "requests": conns, "max_conc": 1, "completed": done,
            "bytes_done": bytes_done, "timed_out": timed_out,
            "budget": budget}


def calibrate(url, timeout=60):
    """
    单连接下载一个固定小文件，作为「此刻这条网络的基准速度」。
    用来把网络本身的时间漂移从结果里分离出来。
    """
    t0 = time.perf_counter()
    got = 0
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept-Encoding": "identity",
            "Range": "bytes=0-262143"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            got = len(r.read())
    except Exception:  # noqa: BLE001, S110
        pass
    dt = max(time.perf_counter() - t0, 1e-6)
    return {"seconds": dt, "bytes": got,
            "kbps": got / dt / 1024 if got else 0.0}


def load_downloader(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def arm_engine(impl, tasks, root: Path, opts, log=print):
    root.mkdir(parents=True, exist_ok=True)
    impl._OPT_CACHE = dict(opts)
    logs = []
    t0 = time.perf_counter()
    results = impl.download_files(tasks, target_dir=root, threads=None,
                                  log=lambda lv, m: logs.append((lv, m)))
    dt = time.perf_counter() - t0
    okn = sum(1 for r in results if r.ok)
    return {"elapsed": dt, "ok_results": okn,
            "failed": [(r.task.rel_path, r.error) for r in results
                       if not r.ok],
            "disk_log": [m for lv, m in logs if "磁盘" in m],
            "logs": logs}


# ----------------------------------------------------------------------
def run_arm(label, impl, opts, tasks, items, work, tag, budget, log=print):
    root = work / f"dl_{tag}"
    shutil.rmtree(root, ignore_errors=True)
    log(f"\n  >> {label} ...")
    if impl is None:
        stat = arm_naive(tasks, root, budget=budget)
    else:
        stat = arm_engine(impl, tasks, root, opts)
    okn, total, bad = verify_all(tasks, items, root)
    stat["verify_ok"] = okn
    stat["verify_total"] = total
    stat["bad"] = bad
    want = sum(i["file_size"] for i in items
               if i["rel_path"] in {t.rel_path for t in tasks})
    stat["want_bytes"] = want
    stat["speed"] = stat.get("bytes_done", want) / max(stat["elapsed"], 1e-9)
    log(f"     耗时 {stat['elapsed']:.2f}s  "
        f"吞吐 {stat['speed'] / 1024 / 1024:.2f} MB/s  "
        f"校验 {okn}/{total}  连接 {stat.get('conns', '-')}"
        + (f"  完成 {stat.get('completed')}/{total}"
           if "completed" in stat else ""))
    if stat.get("timed_out"):
        log(f"     ! 顺序臂在 {budget:.0f}s 预算内没跑完"
            f"（完成 {stat.get('completed')}/{total}，"
            f"{fmt_size(stat.get('bytes_done', 0))}）")
    for rp, err in (stat.get("failed") or [])[:3]:
        log(f"     ! 失败 {rp}: {err[:110]}")
    for rp, why in bad[:3]:
        log(f"     ! 校验 {rp}: {why}")
    for m in (stat.get("disk_log") or [])[:1]:
        log(f"     · {m}")
    return stat


PARAM_TABLE = [
    ("并发下载的文件数", "1", "multi_slots = 12（=12 路并行）"),
    ("单文件内分片", "无", "part_threads = 4（>4MB 且单连接失败后启用）"),
    ("连接复用", "无，每个文件新建连接", "线程本地 keep-alive 缓存"),
    ("SSL 上下文", "每次新建 default context", "进程内共享 1 个 context"),
    ("连接/读超时", "urlopen(timeout=120) 一把抓",
     "connect 10s / 读空闲 20s / 单链接 90s / 停滞 10s"),
    ("停滞判定", "无（只能等内核超时）",
     "速度窗口 5s，低于 10KB/s 判停滞；多线程槽位另有 3s/20KB/s 快速筛除"),
    ("失败重试", "无", "每 URL 2 次 + 5xx/429 额外 1 次，指数退避 500ms→8s + 抖动"),
    ("多源换源", "无（只用第 1 个 URL）",
     "task.urls 逐个尝试；Range 不被支持时整份作废换源"),
    ("HTTP 状态码", "不检查（错误页会落盘）", "非 2xx 判失败，不落盘"),
    ("Range 校验", "无", "分片必须 206 且 Content-Range 一致"),
    ("哈希校验", "全部下完后逐文件 SHA1", "单连接边下边算；续传时整文件校验"),
    ("续传 / 断点", "无", ".part + .part.meta 记录可信区间，按连续前缀 Range 续传"),
    ("进度上报", "无", "字节级节流上报（100ms）+ 文件级进度，单调不回退"),
    ("中止响应", "只能等当前文件结束", "0.5s 轮询中止标志，立即断开连接"),
    ("降级策略", "无", "慢源立即移出多线程槽位 → 单线程重试队列"),
    ("磁盘策略", "无", "按 SSD/HDD/网络盘限制并发文件数与分片线程"),
]


# ----------------------------------------------------------------------
# 4.5) 三个下载站 + 镜像：逐源对比 与 换源实战
#      MCIM（mod.mcimirror.top）对文件和 API 都是"转发器"：
#        Modrinth  : mod.mcimirror.top/data/{project}/versions/{ver}/{file}
#                    → 302 → cdn.modrinth.com/...
#        CurseForge: mod.mcimirror.top/files/{a}/{b}/{file}
#                    → 302 → mediafilez.forgecdn.net/...
#      （file_cdn=False 时的默认行为，见 mcim-api 的 app/routes/file_cdn）
# ----------------------------------------------------------------------
MIRROR_HOST = "mod.mcimirror.top"


def mirror_url_of(official: str) -> str | None:
    """把官方文件 URL 映射到 MCIM 的文件路由；不认识的域名返回 None。"""
    for host in ("https://cdn.modrinth.com/", "https://edge.forgecdn.net/"):
        if official.startswith(host):
            return official.replace(host, f"https://{MIRROR_HOST}/", 1)
    return None


def dead_url_of(official: str) -> str:
    """故意把域名写坏，用来验证"换源"真的发生了。"""
    for host in ("cdn.modrinth.com", "edge.forgecdn.net"):
        if host in official:
            return official.replace(host, f"{host.replace('.', '-')}.invalid")
    return official.replace("://", "://dead-")


def _one_download(url: str, it: dict, root: Path, log=print):
    """用 RideX 锐驰引擎单文件下载一个源，返回 (ok, 秒数, 备注)。"""
    from app.core import downloader as dl
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    dl.clear_disk_cache()
    dl._OPT_CACHE = dict(dl._DEFAULTS)
    task = dl.DownloadTask(f"mods/{it['filename']}", [url],
                           sha1=it["sha1"], sha256="",
                           file_size=it["file_size"])
    t0 = time.perf_counter()
    try:
        dl.download_files([task], target_dir=root, threads=1)
    except Exception as e:  # noqa: BLE001
        return False, time.perf_counter() - t0, f"异常 {type(e).__name__}"
    dt = time.perf_counter() - t0
    p = root / task.rel_path
    if not p.is_file():
        return False, dt, "文件未落盘"
    if hashlib.sha1(p.read_bytes()).hexdigest() != it["sha1"]:
        return False, dt, "sha1 不符"
    return True, dt, ""


def source_matrix(items, work, per_source=3, log=print):
    """同一批文件分别从 官方 / 镜像 下载，逐源计时并逐字节校验。"""
    picks = ([i for i in items if i["source"] == "Modrinth"][:per_source]
             + [i for i in items if i["source"] == "CurseForge"][:per_source])
    log(f"  取 {len(picks)} 个文件（Modrinth {per_source} + "
        f"CurseForge {per_source}），每个都分别从官方与镜像各下一次")
    table = []
    for it in picks:
        official = it["urls"][0]
        mirror = mirror_url_of(official)
        cells = []
        for tag, url in (("官方", official), ("镜像", mirror)):
            if not url:
                cells.append((tag, False, 0.0, "无镜像映射"))
                continue
            root = work / "src" / f"{it['sha1'][:8]}_{tag}"
            ok, dt, note = _one_download(url, it, root, log)
            cells.append((tag, ok, dt, note))
            log(f"    {it['filename'][:44]:44s} {tag}: "
                f"{'OK ' if ok else 'FAIL'} {dt:6.2f}s "
                f"{it['file_size'] / max(dt, 1e-9) / 1024:8.1f} KB/s {note}")
        table.append((it, cells))
    return table


def mirror_fallback_test(items, work, per_source=2, log=print):
    """
    换源实战：把官方链接**写坏**，只在候选里留镜像。
    先证明"坏链接单独必然失败"，再证明"坏链接 + 镜像 = 成功"。
    """
    picks = ([i for i in items if i["source"] == "Modrinth"][:per_source]
             + [i for i in items if i["source"] == "CurseForge"][:per_source])
    out = []
    for it in picks:
        official = it["urls"][0]
        mirror = mirror_url_of(official)
        bad = dead_url_of(official)
        if not mirror:
            continue
        only_bad = _one_download(bad, it, work / "fb" / f"{it['sha1'][:8]}_bad")
        both = work / "fb" / f"{it['sha1'][:8]}_both"
        shutil.rmtree(both, ignore_errors=True)
        both.mkdir(parents=True, exist_ok=True)
        from app.core import downloader as dl
        dl.clear_disk_cache()
        dl._OPT_CACHE = dict(dl._DEFAULTS)
        task = dl.DownloadTask(f"mods/{it['filename']}", [bad, mirror],
                               sha1=it["sha1"], sha256="",
                               file_size=it["file_size"])
        t0 = time.perf_counter()
        dl.download_files([task], target_dir=both, threads=1)
        dt = time.perf_counter() - t0
        p = both / task.rel_path
        ok = p.is_file() and \
            hashlib.sha1(p.read_bytes()).hexdigest() == it["sha1"]
        out.append((it, only_bad[0], ok, dt))
        log(f"    {it['filename'][:44]:44s} 坏链接单独: "
            f"{'竟然成功(?)' if only_bad[0] else '失败(预期)'}   "
            f"坏链接+镜像: {'成功' if ok else '失败'} {dt:5.2f}s")
    return out


def print_param_table():
    print("\n" + "=" * 78)
    print("参数对照：单线程顺序下载  vs  RideX 锐驰引擎")
    print("=" * 78)
    w = max(len(r[0]) for r in PARAM_TABLE)
    print(f"  {'项目'.ljust(w)} | {'单线程顺序':<26} | Easier 新版")
    print(f"  {'-' * w}-+-{'-' * 26}-+{'-' * 40}")
    for name, a, c in PARAM_TABLE:
        print(f"  {name.ljust(w)} | {a:<26} | {c}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--refresh", action="store_true",
                    help="强制重新采集元数据（忽略缓存）")
    ap.add_argument("--arm-a-budget", type=float, default=900.0,
                    help="顺序下载臂的总时间预算（秒）")
    ap.add_argument("--subset", type=int, default=6,
                    help="附加的小文件配对回合规模（0=跳过）")
    ap.add_argument("--only-rotate", action="store_true",
                    help="跳过第一回合，只跑轮转配对回合")
    ap.add_argument("--rotate-rounds", type=int, default=0,
                    help="轮转回合数（每轮把三臂的先后顺序轮换，抵消网络漂移）")
    ap.add_argument("--source-matrix", type=int, default=0,
                    help="每个站各取 N 个文件，分别从官方与镜像下载对比（0=跳过）")
    ap.add_argument("--calibrate", action="store_true",
                    help="每臂开跑前先测一次单连接基准速度，用于归一化")
    args = ap.parse_args()

    print("=" * 78)
    print("Easier 下载器 · 真实网络实战测试")
    print("=" * 78)

    print("\n[1] 采集真实 mod 元数据")
    if META_CACHE.is_file() and not args.refresh:
        items = json.loads(META_CACHE.read_text(encoding="utf-8"))
        print(f"  用缓存 {META_CACHE.relative_to(_ROOT)}"
              f"（--refresh 可强制重新采集）")
        mr = [i for i in items if i["source"] == "Modrinth"]
        cf = [i for i in items if i["source"] == "CurseForge"]
    else:
        mr = fetch_modrinth(10)
        cf = fetch_curseforge(10)
        items = mr + cf
        META_CACHE.parent.mkdir(parents=True, exist_ok=True)
        META_CACHE.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n  合计 {len(items)} 个（Modrinth {len(mr)} / "
          f"CurseForge {len(cf)}），总大小 "
          f"{fmt_size(sum(i['file_size'] for i in items))}")
    for it in items:
        print(f"    {it['source']:10s} {fmt_size(it['file_size']):>9s}  "
              f"{it['filename'][:62]}")

    if args.dry_run:
        print("\n--dry-run：不下载。")
        return 0

    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    print("\n[2] 用 Easier 自己的逻辑做分类 + 生成下载任务")
    tasks, _merged, targets = build_tasks_with_easier(items, WORK)


    print("\n[3] 本次磁盘策略")
    from app.core import downloader as new_dl
    print("  " + new_dl.disk_policy_summary(targets, dict(new_dl._DEFAULTS)))

    orig = load_downloader("downloader_original",
                           str(_HERE / "_baseline" / "downloader_original.py"))
    base_opts = dict(new_dl._DEFAULTS)          # Easier 出厂默认
    orig_opts = dict(orig._DEFAULTS)

    arms = [
        ("A 裸 urllib 顺序", None, None, "A"),
        ("B 原版 downloader（改动前）", orig, orig_opts, "B"),
        ("C RideX 锐驰引擎（本次交付）", new_dl, base_opts, "C"),
    ]

    print(f"\n[4] 第一回合：全部 {len(tasks)} 个真实 mod"
          f"（{fmt_size(sum(i['file_size'] for i in items))}）")
    summary = {}
    if not args.only_rotate:
        for label, impl, opts, tag in arms:
            stat = run_arm(label, impl, opts, tasks, items, WORK, tag,
                           args.arm_a_budget)
            summary.setdefault(label, []).append(stat)
    else:
        print("  （--only-rotate：跳过）")

    _total_bytes = sum(i["file_size"] for i in items)

    if summary:
        print("\n" + "=" * 78)
        print("第一回合结果")
        print("=" * 78)
        for label, _i, _o, _t in arms:
            s = (summary.get(label) or [None])[0]
            if not s:
                continue
            print(f"  {label}")
            print(f"    耗时 {s['elapsed']:.2f}s   平均吞吐 "
                  f"{s['speed'] / 1024 / 1024:.2f} MB/s   "
                  f"校验 {s['verify_ok']}/{s['verify_total']}")
            if s.get("timed_out"):
                print(f"    （顺序臂未跑完：{s.get('completed')}/"
                      f"{s['verify_total']}，"
                      f"{fmt_size(s.get('bytes_done', 0))}）")

    # ---------------- 轮转配对回合（抵消网络漂移） ----------------
    if args.rotate_rounds and args.rotate_rounds > 0:
        subset = args.subset or 6
        small = sorted(items, key=lambda x: x["file_size"])[:subset]
        keep = {i["rel_path"] for i in small}
        sub_tasks = [t for t in tasks if t.rel_path in keep]
        sub_bytes = sum(i["file_size"] for i in small)
        calib_url = min(items, key=lambda x: x["file_size"])["urls"][0]
        print(f"\n[5] 轮转配对回合 ×{args.rotate_rounds}：最小的 "
              f"{len(sub_tasks)} 个文件（{fmt_size(sub_bytes)}）")
        print("    每轮把 A/B/C 的先后顺序轮换，抵消网络随时间漂移；"
              "开跑前测一次单连接基准" if args.calibrate else
              "    每轮把 A/B/C 的先后顺序轮换，抵消网络随时间漂移")
        rot = {}
        for rnd in range(args.rotate_rounds):
            order = arms[rnd % len(arms):] + arms[:rnd % len(arms)]
            print(f"\n  --- 第 {rnd + 1} 轮，顺序："
                  f"{' → '.join(a[0][0] for a in order)} ---")
            for label, impl, opts, tag in order:
                cal = calibrate(calib_url) if args.calibrate else None
                stat = run_arm(label, impl, opts, sub_tasks, small, WORK,
                               f"{tag}r{rnd}", args.arm_a_budget)
                stat["calib"] = cal
                if cal:
                    print(f"     网络基准：{cal['kbps']:.0f} KB/s"
                          f"（{cal['seconds']:.2f}s/256KB）  "
                          f"归一化耗时 "
                          f"{stat['elapsed'] / max(cal['seconds'], 1e-6):.2f}"
                          f"×基准")
                rot.setdefault(label, []).append(stat)

        print(f"\n  轮转回合汇总（{len(sub_tasks)} 个文件，"
              f"{fmt_size(sub_bytes)}）")
        for label, _i, _o, _t in arms:
            ss = rot.get(label) or []
            ts = [s["elapsed"] for s in ss]
            print(f"    {label:32s} 各轮 {[round(t, 2) for t in ts]}  "
                  f"中位 {statistics.median(ts):.2f}s")
            if args.calibrate:
                norm = [s["elapsed"] / max(s["calib"]["seconds"], 1e-6)
                        for s in ss if s.get("calib")]
                if norm:
                    print(f"      {'':30s} 归一化中位 "
                          f"{statistics.median(norm):.2f}×基准"
                          f"  各轮 {[round(n, 2) for n in norm]}")
        ta = statistics.median([s["elapsed"] for s in rot[arms[0][0]]])
        tb = statistics.median([s["elapsed"] for s in rot[arms[1][0]]])
        tc = statistics.median([s["elapsed"] for s in rot[arms[2][0]]])
        print(f"\n    中位对比：A {ta:.2f}s  B {tb:.2f}s  C {tc:.2f}s"
              f"   →  C/A = {ta / max(tc, 1e-9):.2f}×  "
              f"C/B = {tb / max(tc, 1e-9):.2f}×")

    # ---------------- 三个下载站 + 镜像 ----------------
    if args.source_matrix and args.source_matrix > 0:
        print("\n[6] 三个下载站 + 镜像：逐源对比")
        print("    Modrinth 官方 = cdn.modrinth.com")
        print("    Modrinth 镜像 = mod.mcimirror.top/data/...（302 到官方 CDN）")
        print("    CurseForge 官方 = edge.forgecdn.net / mediafilez.forgecdn.net")
        print("    CurseForge 镜像 = mod.mcimirror.top/files/...（302 到官方 CDN）")
        _tbl = source_matrix(items, WORK, args.source_matrix)

        print("\n[7] 换源实战：官方链接写坏 + 镜像兜底")
        fb = mirror_fallback_test(items, WORK, max(1, args.source_matrix - 1))
        bad_ok = sum(1 for _i, only_bad, _ok, _dt in fb if only_bad)
        good_ok = sum(1 for _i, _b, ok, _dt in fb if ok)
        print(f"    → 坏链接单独成功 {bad_ok}/{len(fb)}（应为 0）"
              f"；坏链接+镜像成功 {good_ok}/{len(fb)}（应为全部）")

    print_param_table()

    if summary:
        a = summary["A 裸 urllib 顺序"][0]
        b = summary["B 原版 downloader（改动前）"][0]
        c = summary["C RideX 锐驰引擎（本次交付）"][0]
        print("\n" + "=" * 78)
        print("结论（第一回合：全部 20 个真实 mod）")
        print("=" * 78)
        print(f"  · 三臂校验：A {a['verify_ok']}/{a['verify_total']}  "
              f"B {b['verify_ok']}/{b['verify_total']}  "
              f"C {c['verify_ok']}/{c['verify_total']}")
        print(f"  · 总耗时：A {a['elapsed']:.1f}s  B {b['elapsed']:.1f}s  "
              f"C {c['elapsed']:.1f}s")
        print(f"  · C/A = {a['elapsed'] / max(c['elapsed'], 1e-9):.2f}×   "
              f"C/B = {b['elapsed'] / max(c['elapsed'], 1e-9):.2f}×")

    print("\n" + "=" * 78)
    print("本次实战测试用到的全部模组文件")
    print("=" * 78)
    print(f"  {'#':>2}  {'站点':<11}{'大小':>9}  {'文件名':<58} sha1")
    for i, it in enumerate(items, 1):
        print(f"  {i:>2}  {it['source']:<11}{fmt_size(it['file_size']):>9}  "
              f"{it['filename'][:58]:<58} {it['sha1'][:12]}")
    print(f"  合计 {len(items)} 个，"
          f"{fmt_size(sum(i['file_size'] for i in items))}")

    if not args.keep:
        shutil.rmtree(WORK, ignore_errors=True)
        print(f"\n[8] 已清理测试目录 {WORK}（存在={WORK.exists()}）")
    else:
        print(f"\n[8] 保留测试目录 {WORK}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
