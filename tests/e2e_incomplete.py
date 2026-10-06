"""
e2e_incomplete.py — 「更新受阻 → 放弃 → 下次补齐」端到端测试
============================================================
真实文件 + 真实网络（好文件从 CDN 下、坏文件用死链让它必然失败）。

流程：
    ① 准备一个"缺文件"的更新包：一个真文件（Modrinth）+ 一个死链文件
    ② 玩家端：定位 → 拖包 → 比对 → 确认更新
    ③ 下载：真文件成功、死链文件失败 → 进入「更新受阻」
       （此时**不应用**，实例保持原样；其余下载不受影响）
    ④ 放弃补齐 → 立刻完成更新 + 在整合包里写「更新未完成」凭证
    ⑤ 模拟"下次定位这个整合包"：探测凭证 → 补齐模式 → 投入文件
       → 跑补丁计划 → 凭证被清理

断言的关键结论：
    · 受阻期间**不会**带着缺失应用（实例 mods 里没有那个文件）
    · 放弃后凭证存在，且 txt 里写清了文件、落点、下载源
    · 补齐后文件就位、凭证消失
    · 凭证文件永远不参与更新比对
"""

import hashlib
import json
import shutil
import sys
import tempfile
import time
import unittest.mock
import urllib.request
import zipfile
from collections import deque
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

import stubs

stubs.install()

from app.core import database as db
from app.core import eapack as eapack_mod
from app.core import pending as pending_logic
from app.core.mrpack import merge_files, parse_mrpack

_MODRINTH_PROJECT = "terrablender"
_FALLBACK_FILE = {
    "filename": "TerraBlender-fabric-1.20.1-3.0.1.11.jar",
    "size": 322417,
    "sha1": "2965a82b66b9f6dac5ac3ccdfbe51b3bc9f5b8f9",
    "url": ("https://cdn.modrinth.com/data/kkmrDlKT/versions/kmob8AJ4/"
            "TerraBlender-fabric-1.20.1-3.0.1.11.jar"),
}
# 死链：连接必然被拒（本机 1 端口没人听）
_DEAD_URL = "http://127.0.0.1:1/never-here.jar"

_ok: list[str] = []
_bad: list[str] = []


def check(cond: bool, label: str, extra: str = ""):
    if cond:
        _ok.append(label)
        print(f"  [PASS] {label}")
    else:
        _bad.append(label)
        print(f"  [FAIL] {label} {extra}")


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fetch_real_mod() -> dict:
    url = f"https://api.modrinth.com/v2/project/{_MODRINTH_PROJECT}/version"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        f = min(data[0].get("files") or [],
                key=lambda x: x.get("size", 1 << 60))
        return {"filename": f["filename"], "size": int(f["size"]),
                "sha1": f["hashes"]["sha1"], "url": f["url"]}
    except Exception as e:  # noqa: BLE001
        print(f"  （在线取版本失败，使用内置兜底条目：{e}）")
        return dict(_FALLBACK_FILE)


class DeferredAfter:
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


def main() -> int:
    real = fetch_real_mod()
    print(f"真文件：{real['filename']}（{real['size']} B）")
    tmp_root = tempfile.mkdtemp(prefix="easier-inc-")
    tmp = Path(tmp_root)
    restore = stubs.isolate_user_dirs(tmp / "userdirs")
    try:
        return _run(tmp, real)
    finally:
        restore()
        shutil.rmtree(tmp_root, ignore_errors=True)


def _run(tmp: Path, real: dict) -> int:
    dbdir = tmp / "db"
    db.create_database(dbdir)
    db.set_db_path(dbdir)

    # 缺的那个文件：本地造一份，记录它的哈希（模拟"玩家能自己下到"）
    bad_payload = bytes(range(256)) * 40          # 10240 B
    bad_sha1 = hashlib.sha1(bad_payload).hexdigest()
    bad_name = "missing-mod-1.0.jar"

    inst = tmp / "versions" / "TestPack"
    _write(inst / "mods" / "keep.jar", "KEEP")
    _write(inst / "options.txt", "fov:90\n")

    src = tmp / "pack_src"
    src.mkdir(parents=True)
    index = {
        "formatVersion": 1, "game": "minecraft", "versionId": "3.0.0",
        "name": "TestPack",
        "files": [
            {"path": "mods/" + real["filename"],
             "hashes": {"sha1": real["sha1"]},
             "downloads": [real["url"]], "fileSize": real["size"]},
            {"path": "mods/" + bad_name,
             "hashes": {"sha1": bad_sha1},
             "downloads": [_DEAD_URL], "fileSize": len(bad_payload)},
        ],
        "dependencies": {"minecraft": "1.20.1"},
    }
    (src / "modrinth.index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    _write(src / "overrides" / "config" / "a.toml", "k = 1\n")

    src_zip = tmp / "TestPack-3.0.0.zip"
    with zipfile.ZipFile(src_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in src.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(src).as_posix())
    pack, err = parse_mrpack(src_zip)
    assert not err and pack is not None, err
    eapack = tmp / "TestPack-3.0.0.eapack"
    assert eapack_mod.export_eapack(
        source_dir=src, source_zip=src_zip, out_path=eapack, pack=pack,
        merged_files=merge_files(pack),
        strategies={"mods": "完全匹配", "config": "替换重名"},
        changelog_md="# 3.0.0\n") is not None
    print(f"更新包：{eapack.name}（{eapack.stat().st_size} B），"
          f"其中 {bad_name} 用死链 {_DEAD_URL}")

    # ---------------- 玩家端 ----------------
    import main as main_mod
    from app.ui.main_window import MainWindow
    AppBase, _dnd = main_mod._pick_app_base()
    app = type("W", (MainWindow, AppBase), {})()
    pv = app.player_view
    pump = DeferredAfter()
    pv.after = pump
    logs: list[tuple[str, str]] = []
    pv.log.log = lambda level, msg: logs.append((level, str(msg)))

    print("\n── 1) 定位 + 拖包 + 比对 ──")
    app.app_state.set_modpack(inst)
    pv._on_update_pack_dropped([eapack])
    pv._start_scan()
    _wait(lambda: pv.diff is not None, pump, 120, "比对超时")
    pump.drain()
    tasks = {t.rel_path for t in pv._download_tasks}
    check(f"mods/{real['filename']}" in tasks and f"mods/{bad_name}" in tasks,
          "两个文件都生成了下载任务", str(sorted(tasks)))

    print("\n── 2) 下载：一个成功、一个必然失败 ──")
    pv._on_main_click()                     # CONFIRM → 下载
    _wait(lambda: pv._phase != "download", pump, 300, "下载超时")
    pump.drain()
    check(pv._phase == "manual", "进入「更新受阻」态", pv._phase)
    check(pv.pending_banner is not None, "右栏亮出受阻提示条")
    check(pv.pending_window is not None, "补入子窗口已打开")
    check(pv.pending_window.pending_count() == 1,
          "只有 1 个文件需要补入",
          str(pv.pending_window.pending_count()))
    item = pv.pending_window._items.get(f"mods/{bad_name}", {})
    check(item.get("rel") == f"mods/{bad_name}", "补入页对应的是那个死链文件")
    check(bool(item.get("urls")), "页面带着下载链接")
    check(item.get("sha1") == bad_sha1, "页面带着正确的 SHA1")
    check(pv.pending_window.current_rel() == f"mods/{bad_name}",
          "当前页就是要补的那个文件")

    print("\n── 3) 受阻期间：绝不带缺失应用 ──")
    check(not (inst / "mods" / bad_name).exists(),
          "缺的文件没有被写进整合包")
    check((pv._cache_root / "mods" / real["filename"]).is_file(),
          "能下到的文件已经下好（在缓存区，等待后续应用）")
    check((pv._cache_root / "resume.json").is_file(),
          "续传记录已落盘（关闭后可继续）")
    check(app.app_state.is_locked, "受阻期间锁定，不能换包")

    print("\n── 4) 放弃补齐 → 完成更新 + 写凭证 ──")
    pv._do_give_up(True)
    _wait(lambda: pv._phase == "idle", pump, 300, "应用超时")
    pump.drain()
    check((inst / "mods" / real["filename"]).is_file(),
          "放弃后正常文件已应用")
    check(not (inst / "mods" / bad_name).exists(),
          "缺的文件确实缺失")
    check(pending_logic.marker_path(inst).is_file(),
          "写入了给程序看的凭证 .pulses_easier/pending_update.json")
    check(pending_logic.notice_path(inst).is_file(),
          f"写入了给人看的 {pending_logic.NOTICE_FILENAME}")
    marker = pending_logic.read_marker(inst)
    check(bool(marker), "凭证可读")
    missing = (marker or {}).get("missing") or []
    check([m.get("rel") for m in missing] == [f"mods/{bad_name}"],
          "凭证里记的就是缺的那个文件", str(missing))
    check((missing[0].get("urls") or []) == [_DEAD_URL],
          "凭证里带着下载链接")
    check(not (pv._cache_root / "resume.json").is_file(),
          "放弃后清掉了续传记录（改由凭证负责）")
    check(pv.pending_banner is None and pv.pending_window is None,
          "受阻界面已收起")
    check(not pv._give_up, "放弃标记已复位")
    text = pending_logic.notice_path(inst).read_text("utf-8")
    check(f"mods/{bad_name}" in text and "应放到" in text,
          "txt 里写清了文件名与落点")
    check("Modrinth" in text or "127.0.0.1" in text or "http" in text,
          "txt 里带着下载源")
    check("!!!更新未完成-请阅读!!!.txt" in
          [p.name for p in inst.iterdir()],
          "说明文件排在整合包根目录（! 开头会排最前）")

    print("\n── 5) 凭证不参与更新比对 ──")
    from app.core.differ import diff_packs_parallel
    d = diff_packs_parallel(inst, src / "overrides", whitelist=["mods"],
                            index_hashes={})
    rels = {c.rel_path.as_posix()
            for c in d.added + d.modified + d.deleted}
    check(pending_logic.NOTICE_FILENAME not in rels,
          "说明 txt 不会出现在变更列表里")
    check(pending_logic.MARKER_DIR not in rels,
          "凭证目录不会出现在变更列表里")

    print("\n── 6) 下次定位：探测到凭证并补齐 ──")
    res = pending_logic.check_marker(inst)
    check(res["has"] and not res["resolved"], "探测到未完成凭证")
    check(len(res["outstanding"]) == 1, "凭证里有 1 个待补项")

    asked = []
    import app.ui.player_view as pv_mod

    def _auto_yes(_parent, _title, _text, **kw):
        asked.append(kw.get("confirm_text", ""))
        cb = kw.get("on_result")
        if callable(cb):
            cb(True)

    # 模拟"关掉软件再打开、重新定位这个整合包"
    pv._reset_pack_state()
    with unittest.mock.patch.object(pv_mod, "confirm",
                                    side_effect=_auto_yes):
        app.app_state.set_modpack(inst)      # 定位 → after(250) 会去核对凭证
        _wait(lambda: bool(asked), pump, 30, "凭证询问超时")
    pump.drain()
    check(asked and asked[0] == "现在补齐", "弹出了「上次更新未完成」询问",
          str(asked))
    check(pv._pending_mode == "complete", "进入补齐模式", pv._pending_mode)
    check(pv.pending_window is not None, "补齐窗口已打开")
    check(pv.pending_window.pending_count() == 1, "补齐页 = 1")

    # 玩家把文件下好，拖进本页的拖入框
    supplied = tmp / bad_name
    supplied.write_bytes(bad_payload)
    ok = pv._on_file_dropped(f"mods/{bad_name}", supplied)
    check(ok, "拖入的文件校验通过（SHA1 与凭证一致）")
    pv._on_all_failures_resolved()          # 全部补齐 → 跑补丁计划
    _wait(lambda: pv._phase == "idle", pump, 120, "补丁计划超时")
    pump.drain()
    check((inst / "mods" / bad_name).is_file(), "缺失文件已补进整合包")
    if (inst / "mods" / bad_name).is_file():
        check((inst / "mods" / bad_name).read_bytes() == bad_payload,
              "补齐的文件内容正确")
    check(not pending_logic.marker_path(inst).is_file(),
          "凭证 json 已清理")
    check(not pending_logic.notice_path(inst).is_file(),
          "说明 txt 已清理")
    check(not pending_logic.marker_dir(inst).exists(),
          "凭证目录也一并删掉")
    check(pv._pending_mode == "wait" and pv.pending_window is None,
          "补齐模式收尾干净")

    print("\n── 7) 最终目录 ──")
    for p in sorted(inst.rglob("*")):
        if p.is_file():
            print(f"    {p.relative_to(inst).as_posix()}")

    print("\n" + "=" * 62)
    print(f"通过 {len(_ok)} 项，失败 {len(_bad)} 项")
    if _bad:
        for b in _bad:
            print(f"  ✗ {b}")
        return 1
    print("受阻 / 放弃 / 补齐 端到端测试：全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
