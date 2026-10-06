"""
e2e_flow.py — 玩家端完整流程端到端测试（真实文件 + 真实网络下载）
================================================================
不依赖图形界面（用 tests/stubs.py 的桩替代 Tk），但**其余全部是真的**：

  真实整合包目录  →  真实开发者端导出 .eapack（export_eapack）
  →  玩家端拖入 → 比对（differ）→ 真实从 CDN 下载（downloader）
  → 应用（updater.execute_plan）→ 校验（verify_after_update）

断言清单：
  A. 更新包声明的 mod 被真实下载、sha1 校验通过、字节数与 CDN 一致
  B. 「新版已移除」的本地 mod 被**永久删除**（完全匹配 = 含删除），
     且不产生任何回收站/备份目录
  C. config/ 里玩家本地文件保留；包内同名文件被覆盖（替换重名）
  D. 玩家根目录散文件（options.txt）保留
  E. 包自身的元数据（modrinth.index.json / ea_*.json / changelog.md）
     **没有**被写进整合包根目录
  F. resume.json 被清除、phase 回到 idle、按钮回到「确认更新」
  G. 工作目录在 <db>/cache/temp 内，合并副本在应用后被丢弃
  H. 缓存目录里的陈旧遗留文件**不会**被并入应用源

运行：  python3 tests/e2e_flow.py
"""

import json
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections import deque
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs                                       # noqa: E402

stubs.install()

from app.core import database as db                # noqa: E402
from app.core import eapack as eapack_mod          # noqa: E402
from app.core.mrpack import (                      # noqa: E402
    merge_files,
    parse_mrpack,
)
from app.utils.files import sha256_of_file         # noqa: E402

# 真实 Modrinth 文件（运行时会在线刷新；失败则用这里钉住的值）
_MODRINTH_PROJECT = "terrablender"
_FALLBACK_FILE = {
    "filename": "TerraBlender-fabric-1.20.1-3.0.1.11.jar",
    "size": 322417,
    "sha1": "2965a82b66b9f6dac5ac3ccdfbe51b3bc9f5b8f9",
    "url": ("https://cdn.modrinth.com/data/kkmrDlKT/versions/kmob8AJ4/"
            "TerraBlender-fabric-1.20.1-3.0.1.11.jar"),
}

_ok: list[str] = []
_bad: list[str] = []
_todo: list[str] = []


def check(cond: bool, label: str, extra: str = ""):
    if cond:
        _ok.append(label)
        print(f"  [PASS] {label}")
    else:
        _bad.append(label)
        print(f"  [FAIL] {label} {extra}")


def known_pending(cond: bool, label: str):
    """已知待修项：通过就报 PASS，未通过记为 TODO（不计失败）。"""
    if cond:
        _ok.append(label)
        print(f"  [PASS] {label}（原待修项已修复）")
    else:
        _todo.append(label)
        print(f"  [TODO] {label}（已知待修，第二批处理）")


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fetch_real_mod() -> dict:
    """从 Modrinth API 取一个真实文件条目（含 url / sha1 / size）。"""
    url = f"https://api.modrinth.com/v2/project/{_MODRINTH_PROJECT}/version"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        files = data[0].get("files") or []
        # 取最小的那个，测试快
        f = min(files, key=lambda x: x.get("size", 1 << 60))
        return {
            "filename": f["filename"],
            "size": int(f["size"]),
            "sha1": f["hashes"]["sha1"],
            "url": f["url"],
        }
    except Exception as e:  # noqa: BLE001
        print(f"  （在线取版本失败，使用内置兜底条目：{e}）")
        return dict(_FALLBACK_FILE)


class DeferredAfter:
    """把 after() 的回调排队，由主线程 drain —— 更接近真实 Tk 的语义。"""

    def __init__(self):
        self.q: deque = deque()

    def __call__(self, _ms, fn=None, *args, **kwargs):
        if callable(fn):
            self.q.append((fn, args, kwargs))
        return "after#0"

    def drain(self) -> int:
        n = 0
        while self.q:
            fn, args, kwargs = self.q.popleft()
            n += 1
            try:
                fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                print(f"  [警告] after 回调异常：{type(e).__name__}: {e}")
        return n


def main() -> int:
    real = fetch_real_mod()
    print(f"真实模组文件：{real['filename']} "
          f"({real['size']} B, sha1 {real['sha1'][:12]}…)")

    tmp_root = tempfile.mkdtemp(prefix="easier-e2e-")
    tmp = Path(tmp_root)
    restore_userdirs = stubs.isolate_user_dirs(tmp / "userdirs")
    try:
        return _run(tmp, real)
    finally:
        restore_userdirs()
        keep = tmp_root if "--keep" in sys.argv else None
        if keep is None:
            shutil.rmtree(tmp_root, ignore_errors=True)
        else:
            print(f"（保留现场：{keep}）")


def _run(tmp: Path, real: dict) -> int:
    # ---------------- 数据库 ----------------
    dbdir = tmp / "db"
    db.create_database(dbdir)
    db.set_db_path(dbdir)

    # ---------------- 玩家本地整合包 ----------------
    inst = tmp / "versions" / "TestPack"
    _write(inst / "mods" / "old-version.jar", "OLD CONTENT OF THE MOD")
    _write(inst / "mods" / "removed-in-new-version.jar", "LOCAL ONLY MOD")
    _write(inst / "config" / "shipped.toml", "key = \"old\"\n")
    _write(inst / "config" / "player-only.toml", "my_setting = 42\n")
    _write(inst / "options.txt", "fov:90\n")
    _write(inst / "resourcepacks" / "keep.zip", "PK")

    # ---------------- 开发者端源包 ----------------
    src = tmp / "pack_src"
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "2.0.0",
        "name": "TestPack",
        "files": [
            {
                "path": "mods/" + real["filename"],
                "hashes": {"sha1": real["sha1"]},
                "downloads": [real["url"]],
                "fileSize": real["size"],
            }
        ],
        "dependencies": {"minecraft": "1.20.1"},
    }
    src.mkdir(parents=True)
    (src / "modrinth.index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    _write(src / "overrides" / "config" / "shipped.toml",
           "key = \"new-from-pack\"\n")
    _write(src / "overrides" / "mods" / "pack-local.jar", "SHIPPED LOCALLY")

    src_zip = tmp / "TestPack-2.0.0.zip"
    with zipfile.ZipFile(src_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())

    pack, err = parse_mrpack(src_zip)
    assert not err and pack is not None, err
    eapack = tmp / "TestPack-2.0.0.eapack"
    out = eapack_mod.export_eapack(
        source_dir=src, source_zip=src_zip, out_path=eapack,
        pack=pack, merged_files=merge_files(pack),
        strategies={"mods": "完全匹配", "config": "替换重名"},
        changelog_md="# TestPack 2.0.0\n\n- 更新 mod\n- 改了 config\n")
    assert out is not None, "导出更新包失败"
    print(f"导出更新包：{eapack.name} ({eapack.stat().st_size} B)")

    with zipfile.ZipFile(eapack) as zf:
        inside = sorted(zf.namelist())
    print("包内条目：")
    for n in inside:
        print(f"    {n}")

    # ---------------- 玩家端流程 ----------------
    import main as main_mod
    from app.ui.main_window import MainWindow
    AppBase, _dnd = main_mod._pick_app_base()
    WindowClass = type("WindowClass", (MainWindow, AppBase), {})

    app = WindowClass()
    pv = app.player_view
    pump = DeferredAfter()
    pv.after = pump            # 让 after 真正被消费

    logs: list[tuple[str, str]] = []
    pv.log.log = lambda level, msg: logs.append((level, str(msg)))

    leases = list(Path(tempfile.gettempdir()).glob("pulses_play_*"))
    before_temp = {str(p) for p in leases}

    print("\n── 1) 定位整合包 ──")
    app.app_state.set_modpack(inst)
    check(pv.pack_info is not None, "定位成功，pack_info 已建立")
    check(pv.pack_info.mod_count >= 1 if pv.pack_info else False,
          "信息卡能读到模组数")

    print("\n── 2) 拖入更新包 ──")
    pv._on_update_pack_dropped([eapack])
    check(pv.update_zip == eapack, "更新包已载入")
    check(any("完整性校验通过" in m for _l, m in logs),
          "更新包完整性校验通过（并有日志）")
    check(pv.pack_kind.value == "update", "识别为更新包 (update)")
    check(pv._btn_state == "ready", "按钮进入「开始更新」")

    print("\n── 3) 比对 ──")
    pv._start_scan()
    _wait(lambda: pv.diff is not None, pump, 120, "比对超时")
    pump.drain()
    check(pv.diff is not None, "比对完成")
    if pv.diff is not None:
        print(f"    变更：+{len(pv.diff.added)} "
              f"~{len(pv.diff.modified)} -{len(pv.diff.deleted)}")
    strategies = pv.strategy_table.get_strategies()
    checked = pv.strategy_table.get_checked()
    print(f"    策略表：{strategies}")
    check(strategies.get("config") == "替换重名",
          "config 默认策略是「替换重名」（不再整体替换）",
          f"实际 {strategies.get('config')!r}")
    check(strategies.get("mods") == "完全匹配",
          "mods 默认策略是「完全匹配」")
    check(all(checked.values()), "所有条目默认勾选")
    check(pv._btn_state == "confirm", "按钮进入「确认更新」")
    tasks = {t.rel_path for t in pv._download_tasks}
    print(f"    待下载：{sorted(tasks)}")
    check(f"mods/{real['filename']}" in tasks, "更新包声明的 mod 有待下载任务")

    # 往缓存目录塞一个"上一次更新遗留"的同名/无关文件：
    # 应用时只应并入本次任务清单里的文件，遗留文件必须被忽略
    if pv._cache_root is not None:
        stale = pv._cache_root / "mods" / "stale-leftover.jar"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("STALE", encoding="utf-8")
        stale2 = pv._cache_root / "config" / "shipped.toml"
        stale2.parent.mkdir(parents=True, exist_ok=True)
        stale2.write_text('key = "STALE-FROM-CACHE"\n', encoding="utf-8")

    print("\n── 4) 下载（真实网络） ──")
    t0 = time.time()
    pv._start_download_and_apply()
    check(app.app_state.is_locked, "下载阶段已锁定 app_state")
    _wait(lambda: pv._phase != "download", pump, 300, "下载超时")
    dt = time.time() - t0
    pump.drain()
    if pv.download_panel is not None and pv.download_panel.has_failures():
        print("    失败项："
              f"{list(pv.download_panel._failed)}")
    check(pv._phase in ("apply", "idle"), "下载阶段结束", pv._phase)
    _wait(lambda: pv._phase == "idle", pump, 300, "应用超时")
    print(f"    下载+应用耗时 {dt:.1f}s")

    # ---------------- 断言 ----------------
    print("\n── 5) 结果校验 ──")
    target = inst / "mods" / real["filename"]
    check(target.is_file(), "真实模组已下载并落位")
    if target.is_file():
        check(target.stat().st_size == real["size"],
              "文件大小与 CDN 一致",
              f"{target.stat().st_size} != {real['size']}")
        import hashlib
        h = hashlib.sha1(target.read_bytes()).hexdigest()
        check(h == real["sha1"], "sha1 与 index 声明一致")

    check(not (inst / "mods" / "old-version.jar").exists(),
          "旧版本 mod 已被移除（完全匹配）")
    check((inst / "mods" / "pack-local.jar").is_file(),
          "更新包 overrides 自带的 mod 已就位")
    check((inst / "config" / "shipped.toml").read_text("utf-8")
          == 'key = "new-from-pack"\n', "config 里的同名文件被覆盖（替换重名）")
    check((inst / "config" / "player-only.toml").is_file(),
          "config 里玩家本地文件被保留 ★P0-1")
    check((inst / "options.txt").read_text("utf-8") == "fov:90\n",
          "根目录玩家本地文件被保留")
    for reserved in ("modrinth.index.json", "ea_settings.json",
                     "ea_manifest.json", "ea_hashes.json", "changelog.md"):
        check(not (inst / reserved).exists(),
              f"包内元数据 {reserved} 没有落进整合包根目录 ★P0-2")

    cache_root = pv._cache_root
    if cache_root is not None:
        check(not (cache_root / "resume.json").exists(),
              "成功应用后 resume.json 已清除")
        check(not (cache_root / "mods" / real["filename"]).exists(),
              "更新成功后释放了本次下载缓存（腾空间）★批次3")
    check(pv._phase == "idle", "phase 回到 idle")
    check(pv._btn_state == "ready",
          "按钮复位为「开始更新」（不再停在「确认更新」）★T4")
    check(pv.diff is None and pv.plan is None,
          "应用完成后变更列表/计划已复位 ★T4")
    check(not pv._download_tasks, "待下载清单已清空 ★T4")
    if pv._overrides_root is not None:
        check(not (pv._overrides_root / "mods" / "pack-local.jar").exists(),
              "overrides 源被「移动」进来（源已消耗，不再多写一份）★T3")
    check(not app.app_state.is_locked, "app_state 已解锁")

    after_temp = {str(p) for p in
                  Path(tempfile.gettempdir()).glob("pulses_play_*")}
    # 注意：pv._temp_dir 仍在（同一个更新包可以重复确认），退出时才该清理。
    # 但"一次成功更新后残留一整份 merged 副本"属于已知问题（P1-5）。
    new_temp = after_temp - before_temp
    check(not new_temp, "系统临时目录没有新增残留", str(new_temp))

    print("\n── 6) 永久删除 & 无备份 ──")
    tdir = tmp / "versions" / ".pulses_trash"
    check(not tdir.exists(), "没有产生任何回收站目录（零备份设计）")
    leftovers = [p.name for p in inst.rglob("*")
                 if p.name.startswith(".") and "pulses" in p.name]
    check(not leftovers, "整合包内没有临时/备份残留", str(leftovers))
    check(not (inst / "mods" / "stale-leftover.jar").exists(),
          "缓存目录里的陈旧遗留文件没有被并入应用源")
    check(not (inst / "config" / "shipped.toml").read_text(
              "utf-8").startswith('key = "STALE'),
          "陈旧缓存文件没有覆盖包内的正确 config")

    print("\n── 8) 工作目录 ──")
    work_root = dbdir / "cache" / "temp"
    work_items = sorted(p.name for p in work_root.iterdir()) \
        if work_root.is_dir() else []
    print(f"    <db>/cache/temp 内容：{work_items}")
    merged_dirs = [p for p in work_root.rglob("_merged")] \
        if work_root.is_dir() else []
    check(not merged_dirs,
          "不再产生整包 _merged 副本（按需取源，UI 线程不被冻住）★T1")

    print("\n── 7) 最终目录 ──")
    for p in sorted(inst.rglob("*")):
        if p.is_file():
            print(f"    {p.relative_to(inst).as_posix()} "
                  f"({p.stat().st_size} B)")

    print("\n" + "=" * 62)
    print(f"通过 {len(_ok)} 项，失败 {len(_bad)} 项，待修 {len(_todo)} 项")
    if _todo:
        for t in _todo:
            print(f"  ⏳ {t}")
    if _bad:
        for b in _bad:
            print(f"  ✗ {b}")
        return 1
    print("端到端流程测试：全部通过 ✓")
    return 0


def _wait(cond, pump: DeferredAfter, timeout: float, label: str) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        pump.drain()
        if cond():
            return True
        time.sleep(0.05)
    pump.drain()
    if cond():
        return True
    print(f"    [超时] {label}")
    return False


if __name__ == "__main__":
    sys.exit(main())
