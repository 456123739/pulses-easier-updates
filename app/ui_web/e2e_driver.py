"""
e2e_driver.py — 让"真机端到端"能在 CI 里无人值守跑完
------------------------------------------------
不引入 CDP、不装额外浏览器：用 pywebview 自己的 `evaluate_js` 在真实页面里
点按钮、读文本，走的是和玩家完全一样的路径（真 WebView2、真前端、真桥）。

覆盖范围与 `scripts/dev/e2e_ui.mjs` 一一对应（同一批断言，别只改一边）：
  策略切换/重新扫描、折叠区、更新日志抽屉、首选项「界面」、白名单、数据库、
  更新应用 + 磁盘核对、开发者导出、真下载（进度/槽位/关闭守卫）、中止 + 续传。

两个差异是**故意的**：
  · 文件选择走 `invoke` 而不是点"选择"按钮 —— 真机会弹原生对话框，点不了；
  · 窗口关闭守卫直接问 `controller.can_close()` —— 比伪造一个关闭事件更真。

由环境变量开启：

    PULSES_E2E=1
    PULSES_E2E_CFG=<cfg.json>      # 见 tests/web_e2e_windows.py
    PULSES_E2E_REPORT=<结果 JSON>

    （兼容老写法：只给 PULSES_E2E_PACK / PULSES_E2E_UPDATE 时只跑主流程）

结果写进 report，字段：ok / steps[] / error；失败也会写，方便 CI 直接看。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

CLICK = """
(() => {
  const want = %s;
  const nodes = Array.from(document.querySelectorAll('button'));
  const hit = nodes.find((b) => (b.innerText || '').replace(/\\s+/g, '').includes(want));
  if (!hit) return 'MISS:' + want;
  hit.click();
  return 'OK';
})()
"""

TEXT = "document.body.innerText"

# 行名 / 胶囊 / 折叠标题都在 React 里，按"按钮文案 + 父节点"定位最稳
CLICK_ROW = """
(() => {
  const want = %s;
  const btn = Array.from(document.querySelectorAll('button'))
    .find((b) => (b.innerText || '').replace(/\\s+/g, '').trim() === want);
  if (!btn) return 'MISS';
  btn.click();
  return 'OK';
})()
"""

CLICK_ROW_CHIP = """
(() => {
  const pair = %s;
  const rows = Array.from(document.querySelectorAll('button'))
    .filter((b) => (b.innerText || '').replace(/\\s+/g, '').trim() === pair[0]);
  for (const nameBtn of rows) {
    const row = nameBtn.parentElement;
    if (!row) continue;
    const target = Array.from(row.querySelectorAll('button'))
      .find((b) => (b.innerText || '').trim() === pair[1]);
    if (target) { target.click(); return 'OK'; }
  }
  return 'MISS';
})()
"""

CLICK_STEP = """
(() => {
  const want = %s;
  const btn = Array.from(document.querySelectorAll('button'))
    .find((b) => (b.innerText || '').replace(/\\s+/g, ' ').includes(want) &&
      b.getClientRects().length);
  if (!btn) return 'MISS';
  btn.click();
  return 'OK';
})()
"""

BUTTON_LABELS = """
(() => Array.from(document.querySelectorAll('button'))
  .filter((b) => b.getClientRects().length > 0)
  .map((b) => (b.innerText || '').replace(/\\s+/g, ' ').trim()))()
"""

ROW_COUNT = """
(() => {
  const want = %s;
  return Array.from(document.querySelectorAll('button'))
    .filter((b) => b.getClientRects().length > 0)
    .filter((b) => (b.innerText || '').replace(/\\s+/g, ' ').trim() === want).length;
})()
"""

BAR_PCT = """
(() => {
  const btn = document.querySelector('button[title="点击查看并行槽位"]');
  if (!btn) return null;
  const widths = Array.from(btn.querySelectorAll('span'))
    .map((s) => parseFloat(s.style.width || ''))
    .filter((n) => Number.isFinite(n));
  return widths.length ? Math.max.apply(null, widths) : null;
})()
"""

SLOT_BUSY = """
(() => Array.from(document.querySelectorAll('[data-idle]'))
  .filter((r) => r.getAttribute('data-idle') === 'false')
  .map((r) => (r.innerText || '').replace(/\\s+/g, ' ').trim()))()
"""

OPEN_SLOTS = """
(() => {
  const btn = document.querySelector('button[title="点击查看并行槽位"]');
  if (!btn) return 'MISS';
  btn.click();
  return 'OK';
})()
"""

PRESS_ESC = """
(() => {
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  return 'OK';
})()
"""

DIALOG_TEXT = """
(() => {
  const d = document.querySelector('[role="dialog"]');
  return d ? d.innerText : '';
})()
"""

DIALOGS = "document.querySelectorAll('[role=\"dialog\"]').length"

SET_INPUT = """
(() => {
  const info = %s;
  const el = document.querySelector(info.sel);
  if (!el) return 'MISS';
  const proto = el.tagName === 'TEXTAREA'
    ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
  setter.call(el, info.value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  return 'OK';
})()
"""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def run(window, controller, bus, cfg: dict) -> None:
    """在后台线程里驱动界面；结束后写报告并关窗。"""
    report: dict = {"ok": False, "steps": [], "error": "", "artifacts": {}}

    def js(code: str):
        """带看门狗：页面没加载时 evaluate_js 可能一直不返回，不能拖住报告。"""
        box: dict = {}

        def work():
            try:
                box["value"] = window.evaluate_js(code)
            except Exception as exc:                  # noqa: BLE001
                box["error"] = exc

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(15.0)
        if th.is_alive():
            raise RuntimeError("evaluate_js 超时（页面可能没渲染出来）")
        if "error" in box:
            return None
        return box.get("value")

    def invoke(method: str, params: dict, timeout: float = 120.0):
        """evaluate_js 不会等 Promise：发起后轮询一个全局变量拿结果。"""
        payload = json.dumps(params)
        js(f"""window.__e2e = null;
            window.pywebview.api.invoke({json.dumps(method)}, {payload})
              .then(function (r) {{ window.__e2e = r; }})
              .catch(function (e) {{
                window.__e2e = {{ok: false, error: {{message: String(e)}}}};
              }});
            'started'""")
        end = time.time() + timeout
        while time.time() < end:
            res = js("window.__e2e")
            if res:
                return res
            time.sleep(0.2)
        raise RuntimeError(f"桥调用超时：{method}")

    def step(name: str, ok: bool, detail: str = "") -> None:
        report["steps"].append({"name": name, "ok": bool(ok),
                                "detail": str(detail)[:800]})
        if not ok:
            raise RuntimeError(f"{name} 失败：{detail}"[:800])

    def call(method: str, params: dict, name: str):
        res = invoke(method, params)
        step(name, bool(res and res.get("ok")),
             json.dumps((res or {}).get("error") or "", ensure_ascii=False))
        return (res or {}).get("data")

    def wait_text(substr: str, timeout: float = 120.0) -> str:
        end = time.time() + timeout
        while time.time() < end:
            body = js(TEXT) or ""
            if substr in body:
                return body
            time.sleep(0.3)
        raise RuntimeError(f"等不到界面出现：{substr}")

    def wait_gone(substr: str, timeout: float = 20.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if substr not in (js(TEXT) or ""):
                return True
            time.sleep(0.2)
        return False

    def wait_js(code: str, timeout: float = 20.0, interval: float = 0.15):
        end = time.time() + timeout
        while time.time() < end:
            value = js(code)
            if value:
                return value
            time.sleep(interval)
        return None

    def click(label: str, timeout: float = 20.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            if js(CLICK % json.dumps(label)) == "OK":
                time.sleep(0.1)
                return
            time.sleep(0.25)
        raise RuntimeError(f"点不到按钮：{label}")

    def click_row(name: str) -> None:
        if js(CLICK_ROW % json.dumps(name)) != "OK":
            raise RuntimeError(f"点不到行：{name}")
        time.sleep(0.2)

    def click_row_chip(name: str, chip: str) -> None:
        if js(CLICK_ROW_CHIP % json.dumps([name, chip])) != "OK":
            raise RuntimeError(f"点不到胶囊：{name} / {chip}")
        time.sleep(0.2)

    def click_step(step_text: str) -> None:
        if js(CLICK_STEP % json.dumps(step_text)) != "OK":
            raise RuntimeError(f"点不到 {step_text} 拖入区")
        time.sleep(0.2)

    def row_count(name: str) -> int:
        return int(js(ROW_COUNT % json.dumps(name)) or 0)

    def wait_row(name: str, timeout: float = 20.0) -> None:
        """快照到了不等于 DOM 已经渲染，点之前先等行出现。"""
        if not wait_js(ROW_COUNT % json.dumps(name) + ">= 1", timeout):
            raise RuntimeError(f"等不到行：{name}")

    def labels() -> list:
        return list(js(BUTTON_LABELS) or [])

    def body() -> str:
        return js(TEXT) or ""

    def dialog_text() -> str:
        return str(js(DIALOG_TEXT) or "")

    def press_esc() -> None:
        js(PRESS_ESC)
        time.sleep(0.25)

    def snapshot() -> dict:
        return controller.flow.snapshot()

    def dev_snapshot() -> dict:
        return controller.dev.snapshot()

    # ------------------------------------------------------------------
    def basic_suite(fx: dict) -> None:
        pack = Path(fx["pack"])
        wait_text("定位整合包", 120)
        step("界面渲染", True)

        call("app.pick_pack", {"path": fx["pack"]}, "定位整合包")
        wait_text(pack.name[:6], 30)
        step("整合包信息显示", True)

        call("update.pick", {"path": fx["update"]}, "导入更新包")
        wait_text(Path(fx["update"]).name, 30)
        step("更新包信息（含更新日志）",
             bool(snapshot().get("update", {}).get("changelog")))

        click("开始更新")
        wait_text("变更内容", 180)
        step("比对完成", True)
        counts = snapshot().get("counts") or {}
        step("比对计数（+1 ~2 -1）",
             counts.get("added") == 1 and counts.get("modified") == 2
             and counts.get("deleted") == 1, json.dumps(counts))
        step("比对阶段磁盘未变",
             (pack / "mods" / "old.jar").read_text(encoding="utf-8") == "OLD")

        # ---- A 策略 ----
        wait_row("mods")
        click_row_chip("mods", "跳过重名")
        rows = {r["name"]: r for r in snapshot().get("strategies") or []}
        step("策略已切到「跳过重名」", rows.get("mods", {}).get("current") == "跳过重名",
             json.dumps(rows.get("mods"), ensure_ascii=False))
        step("跳过计数出现在变更列表", bool(wait_js(
            "/(跳过\\s*2)/.test(document.body.innerText)", 20)))
        groups = {g["top"]: g for g in snapshot().get("groups") or []}
        step("mods 跳过 2 项（快照）", groups.get("mods", {}).get("skipped") == 2,
             json.dumps(groups.get("mods", {}).get("skipped")))
        step("被跳过的条目逐条标记",
             len([i for i in groups.get("mods", {}).get("items", [])
                  if i.get("skipped")]) == 2)

        before = body().count("比对完成")
        call("pack.scan", {}, "重新扫描（改策略后）")
        step("重新扫描完成（提示刷新）", bool(wait_js(
            "(function(){return (document.body.innerText.match(/比对完成/g)||[])"
            f".length > {before};}})()", 120)))
        rows = {r["name"]: r for r in snapshot().get("strategies") or []}
        groups = {g["top"]: g for g in snapshot().get("groups") or []}
        step("重新扫描后策略保留", rows.get("mods", {}).get("current") == "跳过重名")
        step("重新扫描后跳过数保留", groups.get("mods", {}).get("skipped") == 2)

        click_row_chip("mods", "完全匹配")
        rows = {r["name"]: r for r in snapshot().get("strategies") or []}
        groups = {g["top"]: g for g in snapshot().get("groups") or []}
        step("策略已改回「完全匹配」", rows.get("mods", {}).get("current") == "完全匹配")
        step("跳过计数归零", groups.get("mods", {}).get("skipped") == 0)
        step("「跳过 2」标记消失", wait_gone("跳过 2"))

        # ---- B 折叠区 ----
        wait_text("无需更新（1）", 20)
        step("出现「无需更新」分组", True)
        step("折叠时看不到内容", row_count("resourcepacks") == 0)
        click("无需更新")
        step("展开「无需更新」后内容可见",
             bool(wait_js(ROW_COUNT % json.dumps("resourcepacks") + "=== 1", 10)))
        click("无需更新")
        step("收起后内容再次隐藏",
             bool(wait_js(ROW_COUNT % json.dumps("resourcepacks") + "=== 0", 10)))
        click("无需更新")
        wait_js(ROW_COUNT % json.dumps("resourcepacks") + "=== 1", 10)
        click_row("resourcepacks")
        wait_text("已禁用（1）", 20)
        step("取消勾选后出现「已禁用」分组", True)
        step("「已禁用」默认折叠", row_count("resourcepacks") == 0)
        click("已禁用")
        step("展开「已禁用」后内容可见",
             bool(wait_js(ROW_COUNT % json.dumps("resourcepacks") + "=== 1", 10)))
        click_row("resourcepacks")
        step("重新勾选后「已禁用」分组消失", wait_gone("已禁用（1）"))
        step("行回到「无需更新」且仍可见",
             bool(wait_js(ROW_COUNT % json.dumps("resourcepacks") + "=== 1", 10)))
        click("无需更新")
        wait_js(ROW_COUNT % json.dumps("resourcepacks") + "=== 0", 10)

        # ---- C 更新日志 ----
        click("查看更新日志")
        wait_js(DIALOGS + "> 0", 15)
        text = dialog_text()
        step("更新日志含版本号 3.0.0", "3.0.0" in text, text[:120])
        step("更新日志正文已渲染", "e2e" in text)
        press_esc()
        step("Esc 关闭更新日志", bool(wait_js(DIALOGS + "=== 0", 10)))

        # ---- D 首选项 ----
        click("更多")
        click("首选项")
        wait_js(DIALOGS + "> 0", 15)
        text = dialog_text()
        step("首选项有「界面」一栏", "界面" in text, text.replace("\n", " | ")[:160])
        step("「界面」三个选项齐全",
             all(k in text for k in ("自动", "旧界面", "新界面")))
        click("新界面")
        settings = call("app.settings.get", {}, "读取设置") or {}
        step("选「新界面」已保存", settings.get("ui_mode") == "web",
             json.dumps(settings, ensure_ascii=False))
        click("自动")
        settings = call("app.settings.get", {}, "读取设置") or {}
        step("改回「自动」已保存", settings.get("ui_mode") == "auto")
        press_esc()
        step("Esc 关闭首选项", bool(wait_js(DIALOGS + "=== 0", 10)))

        # ---- E 白名单 ----
        before_wl = (call("app.whitelist.get", {}, "读取白名单") or {}).get("whitelist") or []
        got = call("app.whitelist.set", {"items": ["mods", "config"]},
                   "写白名单 [mods,config]") or {}
        step("白名单写入结果正确", got.get("whitelist") == ["mods", "config"],
             json.dumps(got, ensure_ascii=False))
        again = (call("app.whitelist.get", {}, "回读白名单") or {}).get("whitelist")
        step("白名单回读一致", again == ["mods", "config"], json.dumps(again))
        dirty = call("app.whitelist.set", {"items": ["../x", "", "mods"]},
                     "写非法白名单") or {}
        step("非法项被清洗（../x、空串）", dirty.get("whitelist") == ["mods"],
             json.dumps(dirty, ensure_ascii=False))
        after_dirty = (call("app.whitelist.get", {}, "回读白名单") or {}).get("whitelist")
        step("清洗后的值真的落盘", after_dirty == ["mods"], json.dumps(after_dirty))
        call("app.whitelist.set", {"items": before_wl}, "还原白名单")
        step("白名单已还原",
             (call("app.whitelist.get", {}, "回读白名单") or {}).get("whitelist")
             == before_wl)

        # ---- F 数据库 ----
        db_before = (call("app.db.get", {}, "读取数据库位置") or {}).get("path") or ""
        step("数据库路径存在", bool(db_before) and Path(db_before).is_dir(), db_before)
        db_alt = str(Path(cfg["work"]) / "db-alt")
        moved = call("app.db.set", {"path": db_alt}, "切到临时数据库") or {}
        step("切库返回新路径", moved.get("path") == db_alt, json.dumps(moved))
        step("数据库位置回读一致",
             (call("app.db.get", {}, "回读数据库位置") or {}).get("path") == db_alt)
        step("临时数据库已建库",
             (Path(db_alt) / "config.json").is_file()
             and (Path(db_alt) / "projects").is_dir())
        call("app.db.set", {"path": db_before}, "还原数据库位置")
        step("数据库位置已还原",
             (call("app.db.get", {}, "回读数据库位置") or {}).get("path") == db_before)

        # ---- 应用 + 磁盘为准 ----
        click("确认更新")
        wait_text("更新完成", 240)
        step("更新应用完成", True)
        time.sleep(0.6)
        snap = snapshot()
        step("复核 0 失败", (snap.get("report") or {}).get("failed") == 0,
             json.dumps(snap.get("report")))
        def read(rel: str) -> str:
            return (pack / rel).read_text(encoding="utf-8").replace("\r\n", "\n")

        step("mods 已替换", read("mods/old.jar") == "NEW")
        step("新增文件已落地", read("mods/added.jar") == "ADDED")
        step("本地多余文件已删除", not (pack / "mods/gone.jar").exists())
        step("玩家自己的配置没被动", read("config/mine.toml") == "mine = 1\n")
        step("智能剔除保护生效",
             read("config/sodium-options.json") == '{"render_distance": 8}\n')
        step("无需更新的文件没被重写", read("resourcepacks/keep.zip") == "PK-KEEP")

        # ---- G 开发者（需要源 zip；老写法只给包/更新包时跳过）----
        if not fx.get("zip"):
            return
        click("开发者")
        wait_text("开始制作更新包", 30)
        step("切到开发者视图", True)
        call("dev.pick", {"path": fx["zip"]}, "开发者导入整合包")
        wait_text("更新范围", 30)
        dev = dev_snapshot()
        step("开发者列出更新范围", len(dev.get("strategies") or []) >= 2,
             json.dumps([r["name"] for r in dev.get("strategies") or []]))
        wait_row("config")
        click_row("config")
        dev = dev_snapshot()
        step("目录可被排除（config）",
             bool([r for r in dev.get("strategies") or []
                   if r["name"] == "config" and r.get("excluded")]))
        wait_row("mods")
        click_row_chip("mods", "替换重名")
        dev = dev_snapshot()
        step("开发者策略可改（mods → 替换重名）",
             bool([r for r in dev.get("strategies") or []
                   if r["name"] == "mods" and r.get("current") == "替换重名"]))
        click("压缩到最小体积")
        dev = dev_snapshot()
        step("导出选项可切换（min_size）",
             bool((dev.get("options") or {}).get("min_size")))
        changelog = "# 导出夹具\n\n- 开发者端\n"
        if js(SET_INPUT % json.dumps({"sel": "textarea", "value": changelog})) != "OK":
            raise RuntimeError("写不进更新日志输入框")
        time.sleep(1.5)                       # 视图里有 500ms 防抖
        dev = dev_snapshot()
        step("更新日志写回后端", dev.get("changelog") == changelog,
             json.dumps(dev.get("changelog"), ensure_ascii=False))
        out_name = "E2EExport.eapack"
        if js(SET_INPUT % json.dumps({
                "sel": 'input[placeholder="输出文件名"]',
                "value": out_name})) != "OK":
            raise RuntimeError("写不进输出文件名")
        time.sleep(0.3)
        click("开始制作更新包")
        body_txt = wait_text("已生成：", 180)
        step("开发者导出完成（完成态可见）", True)
        exported = Path(str(dev_snapshot().get("outPath") or ""))
        if not exported.name:
            line = [ln for ln in body_txt.splitlines() if "已生成：" in ln][-1]
            exported = Path(line.split("已生成：", 1)[1].strip())
        step("导出的 .eapack 存在",
             exported.is_file() and exported.stat().st_size > 500, str(exported))
        report["artifacts"]["exported"] = str(exported)
        verify_exported(exported, changelog, step)

    # ------------------------------------------------------------------
    def download_suite(dl: dict) -> None:
        def wait_done(before: int, timeout: float = 240.0) -> bool:
            end = time.time() + timeout
            while time.time() < end:
                if body().count("更新完成") > before:
                    return True
                time.sleep(0.3)
            return False

        # 上一段把身份切成了开发者，先切回玩家（同一个进程里跑两套）
        click("玩家")
        wait_text("更新策略", 30)
        call("app.pick_pack", {"path": dl["pack"]}, "定位整合包（真下载）")
        call("update.pick", {"path": dl["update"]}, "导入更新包（带 URL 文件）")
        click("开始更新")
        wait_text("变更内容", 180)
        step("比对完成（真下载）", True)
        counts = snapshot().get("counts") or {}
        step("比对出 4 个新增文件", counts.get("added") == 4, json.dumps(counts))
        # 上一段（基本流程）的变更树还挂在界面上，等新的条目真的画出来再断言
        step("要下载的大文件出现在变更列表", bool(wait_js(
            "document.body.innerText.includes('e2e-big.jar')", 30)))

        before_done = body().count("更新完成")
        click("确认更新")
        seen = {"bar": 0, "slots": set(), "hints": set(), "guard": None, "done": False}
        opened = False
        guard_checked = False
        started = time.time()
        while time.time() - started < 240:
            pct = js(BAR_PCT)
            if pct is not None and 0 < float(pct) < 100:
                seen["bar"] += 1
            if not opened and pct is not None:
                opened = js(OPEN_SLOTS) == "OK"
            if opened:
                busy = js(SLOT_BUSY) or []
                for item in busy:
                    seen["slots"].add(str(item))
                if not guard_checked and (seen["bar"] > 0 or busy):
                    guard_checked = True
                    blocked = not controller.can_close()
                    seen["guard"] = blocked
                    if blocked:
                        click("好")           # 关掉 can_close 弹出的提示
            txt = body()
            for token in ("正在下载", "下载 1/", "下载 2/", "下载 3/"):
                if token in txt:
                    seen["hints"].add(token)
            if txt.count("更新完成") > before_done:
                seen["done"] = True
                break
            time.sleep(0.08)
        step("下载 + 应用完成", seen["done"])
        step("进度条出现过下载中的进度", seen["bar"] > 0, f"采样 {seen['bar']} 次")
        step("槽位面板出现过活动槽位", bool(seen["slots"]),
             json.dumps(sorted(seen["slots"])[:3], ensure_ascii=False))
        step("提示文案有下载进度", bool(seen["hints"]),
             json.dumps(sorted(seen["hints"]), ensure_ascii=False))
        step("下载中关闭守卫拦截（can_close=False）", seen["guard"] is True,
             json.dumps(seen["guard"]))
        report_ = (snapshot().get("report") or {})
        step("复核 0 失败", report_.get("failed") == 0, json.dumps(report_))
        pack = Path(dl["pack"])
        for item in dl.get("files") or []:
            target = pack / item["path"]
            step(f"下载并落地：{item['name']}（{item['size']} B）",
                 target.is_file() and _sha256(target) == item["sha256"], str(target))
        step("包内文件照常落地",
             (pack / "mods/e2e-inpack.jar").read_text(encoding="utf-8") == "INPACK")
        step("配置随包落地", (pack / "config/e2e-dl.toml").is_file())
        step("空闲时关闭守卫放行", bool(controller.can_close()))

        # ---- 中止 / 续传 ----
        call("update.clear", {}, "清空上一轮比对")
        call("app.pick_pack", {"path": dl["abortPack"]}, "定位第二个整合包")
        call("update.pick", {"path": dl["abortUpdate"]}, "导入续传用更新包")
        click("开始更新")
        wait_text("变更内容", 180)
        step("比对完成（续传包）", True)
        click("确认更新")
        step("进入下载态（主按钮变成「取消下载」）",
             bool(wait_js(
                 "(Array.from(document.querySelectorAll('button'))"
                 ".some((b) => (b.innerText||'').trim() === '取消下载'))", 30)))
        click("取消下载")
        step("中止请求已送到后端", bool(wait_js(
            "document.body.innerText.includes('已请求中止')", 15)))
        end = time.time() + 60
        while time.time() < end and controller.flow.busy():
            time.sleep(0.1)
        step("中止后状态回到空闲（不卡死）", not controller.flow.busy(),
             json.dumps(snapshot().get("phase")))
        snap = snapshot()
        step("中止后进入\"待补入\"", len(snap.get("pending") or []) > 0,
             json.dumps(snap.get("pending"), ensure_ascii=False))
        step("界面提示还差文件", bool(wait_js(
            "/还差\\s*\\d+\\s*个文件/.test(document.body.innerText)", 20)))
        step("中止后可以正常关窗（守卫放行）", bool(controller.can_close()))
        before_resume = body().count("更新完成")
        call("update.resume", {}, "继续上次的更新")
        step("续传后更新完成", wait_done(before_resume, 240))
        abort_file = Path(dl["abortPack"]) / dl["abortFile"]["path"]
        step("续传后文件字节与源一致",
             abort_file.is_file()
             and _sha256(abort_file) == dl["abortFile"]["sha256"], str(abort_file))
        step("续传后复核 0 失败",
             (snapshot().get("report") or {}).get("failed") == 0,
             json.dumps(snapshot().get("report")))
        report["artifacts"]["aborted"] = str(abort_file)

    # ------------------------------------------------------------------
    try:
        basic = cfg.get("basic") or {}
        if basic.get("pack") and basic.get("update"):
            basic_suite(basic)
        dl = cfg.get("download") or {}
        if dl.get("pack") and dl.get("update"):
            download_suite(dl)
        report["ok"] = True
    except Exception as exc:                            # noqa: BLE001
        report["error"] = f"{exc}"
        print("[e2e] " + traceback.format_exc(), file=sys.stderr, flush=True)
    finally:
        try:
            Path(cfg["report"]).write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:                               # noqa: BLE001, S110
            pass
        try:
            time.sleep(0.3)
            window.destroy()
        except Exception:                               # noqa: BLE001, S110
            pass


def verify_exported(path: Path, changelog: str, step) -> None:
    """开发者导出的包只有 Python 读得懂：就地复核（与 run_ui_e2e.py 同一批断言）。"""
    import zipfile

    from app.core import eapack as eapack_mod

    settings = eapack_mod.read_settings(path) or {}
    manifest = eapack_mod.read_manifest(path) or {}
    hashes = eapack_mod.read_hashes(path) or {}
    got_log = eapack_mod.read_changelog(path) or ""
    strategies = settings.get("recommended_strategies") or {}
    step("导出包带上了界面改过的策略（mods=替换重名）",
         strategies.get("mods") == "替换重名", json.dumps(strategies, ensure_ascii=False))
    step("导出包记录了被排除的目录（config）",
         "config" in (manifest.get("excluded_folders") or []),
         json.dumps(manifest.get("excluded_folders")))
    step("导出包清单里的包名/版本正确",
         manifest.get("pack_name") == "E2EPack"
         and manifest.get("pack_version") == "3.0.0",
         json.dumps({k: manifest.get(k) for k in ("pack_name", "pack_version")},
                    ensure_ascii=False))
    step("导出包里的更新日志是界面填的", "开发者端" in got_log, got_log[:60])
    step("导出包写了文件哈希清单", "mods/old.jar" in (hashes.get("files") or {}))
    signed, reason = eapack_mod.verify_signature(path)
    step("导出包签名校验通过", bool(signed) and not reason, reason or "")
    with zipfile.ZipFile(path) as zf:
        names = [n.replace("\\", "/") for n in zf.namelist()]
    step("被排除的目录没有进包",
         not any(n.startswith("overrides/config/") for n in names),
         json.dumps([n for n in names if "config" in n][:5]))
    step("导出包内容齐全",
         "overrides/mods/old.jar" in names and "ea_settings.json" in names)


def _load_cfg() -> dict | None:
    path = os.environ.get("PULSES_E2E_CFG", "").strip()
    if path and Path(path).is_file():
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as exc:                        # noqa: BLE001
            print(f"[e2e] 读不了 cfg：{exc}", file=sys.stderr, flush=True)
    # 兼容老写法：只给整合包 / 更新包
    pack = os.environ.get("PULSES_E2E_PACK", "").strip()
    update = os.environ.get("PULSES_E2E_UPDATE", "").strip()
    if pack and update:
        report = os.environ.get("PULSES_E2E_REPORT", "e2e-report.json")
        return {"work": str(Path(report).resolve().parent),
                "basic": {"pack": pack, "update": update}}
    return None


def launch_if_requested(window, controller, bus, delay: float = 1.5) -> None:
    if os.environ.get("PULSES_E2E", "").strip() not in ("1", "true", "yes"):
        return
    cfg = _load_cfg()
    if not cfg:
        return
    cfg["report"] = os.environ.get("PULSES_E2E_REPORT", "e2e-report.json")
    threading.Thread(target=_delayed, args=(window, controller, bus, cfg, delay),
                     daemon=True).start()


def _delayed(window, controller, bus, cfg, delay: float) -> None:
    time.sleep(max(0.0, delay))        # 等窗口与事件泵就绪
    run(window, controller, bus, cfg)
