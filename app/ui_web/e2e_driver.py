"""
e2e_driver.py — 让"真机端到端"能在 CI 里无人值守跑完
------------------------------------------------
不引入 CDP、不装额外浏览器：用 pywebview 自己的 `evaluate_js` 在真实页面里
点按钮、读文本，走的是和玩家完全一样的路径（真 WebView2、真前端、真桥）。

由环境变量开启：

    PULSES_E2E=1
    PULSES_E2E_PACK=<整合包目录>
    PULSES_E2E_UPDATE=<更新包路径>
    PULSES_E2E_REPORT=<结果 JSON 路径>

结果写进 report，字段：ok / steps[] / error；失败也会写，方便 CI 直接看。
"""

from __future__ import annotations

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


def run(window, controller, bus, cfg: dict) -> None:
    """在后台线程里驱动界面；结束后写报告并关窗。"""
    report = {"ok": False, "steps": [], "error": ""}

    def js(code: str):
        try:
            return window.evaluate_js(code)
        except Exception:                             # noqa: BLE001
            return None

    def invoke(method: str, params: dict, timeout: float = 60.0):
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
            time.sleep(0.25)
        raise RuntimeError(f"桥调用超时：{method}")

    def step(name: str, ok: bool, detail: str = "") -> None:
        report["steps"].append({"name": name, "ok": bool(ok),
                                "detail": str(detail)[:500]})
        if not ok:
            raise RuntimeError(f"{name} 失败：{detail}"[:600])

    def click(label: str, timeout: float = 30.0) -> None:
        end = time.time() + timeout
        while time.time() < end:
            res = js(CLICK % json.dumps(label))
            if res == "OK":
                return
            time.sleep(0.3)
        raise RuntimeError(f"点不到按钮：{label}")

    def wait_text(substr: str, timeout: float = 120.0) -> str:
        end = time.time() + timeout
        while time.time() < end:
            body = js(TEXT) or ""
            if substr in body:
                return body
            time.sleep(0.4)
        raise RuntimeError(f"等不到界面出现：{substr}")

    try:
        wait_text("定位整合包", 120)
        step("界面渲染", True)

        res = invoke("app.pick_pack", {"path": cfg["pack"]})
        step("定位整合包", bool(res and res.get("ok")), res)
        wait_text(Path(cfg["pack"]).name[:6], 30)
        step("整合包信息显示", True)

        res = invoke("update.pick", {"path": cfg["update"]})
        step("导入更新包", bool(res and res.get("ok")), res)

        click("开始更新")
        wait_text("变更内容", 180)
        step("比对完成", True)

        click("确认更新")
        wait_text("更新完成", 240)
        step("更新应用完成", True)

        time.sleep(1.0)
        body = js(TEXT) or ""
        step("界面显示完成态", "更新完成" in body)
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


def launch_if_requested(window, controller, bus, delay: float = 1.5) -> None:
    if os.environ.get("PULSES_E2E", "").strip() not in ("1", "true", "yes"):
        return
    cfg = {
        "pack": os.environ.get("PULSES_E2E_PACK", ""),
        "update": os.environ.get("PULSES_E2E_UPDATE", ""),
        "report": os.environ.get("PULSES_E2E_REPORT", "e2e-report.json"),
    }
    if not (cfg["pack"] and cfg["update"]):
        return
    threading.Thread(target=_delayed, args=(window, controller, bus, cfg, delay),
                     daemon=True).start()


def _delayed(window, controller, bus, cfg, delay: float) -> None:
    time.sleep(max(0.0, delay))        # 等窗口与事件泵就绪
    run(window, controller, bus, cfg)
