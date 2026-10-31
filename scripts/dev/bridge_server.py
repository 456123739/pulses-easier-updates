"""
bridge_server.py — 给无头浏览器用的"桥 + 静态站点"
------------------------------------------------
新界面在浏览器里跑不起来，是因为它依赖 pywebview 注入的 `window.pywebview`。
这个脚本把同一套东西用 HTTP 补上：

    GET  /*             → web/dist 静态文件（index.html 里注入 shim）
    POST /api/invoke    → 真正的 app/ui_web/api.py
    POST /api/pick_path → 固定路径（测试里不要弹原生对话框）

所以 Playwright 点的是**真前端 + 真 Api + 真流程**，只有外壳（WebView2）不同。
CI 用它做"真机端到端"的交互部分。

用法：
    python scripts/dev/bridge_server.py --port 8123 --root <整合包> [--db <db>]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui_web.api import Api                      # noqa: E402
from app.ui_web.controller import AppController     # noqa: E402
from app.ui_web.events import EventBus              # noqa: E402

SHIM = """
(function () {
  const post = async (url, body) => {
    const r = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  };
  window.pywebview = {
    api: {
      invoke: (method, params) => post('/api/invoke', {method, params}),
      pick_path: (kind, filters) => post('/api/pick_path', {kind, filters}),
    },
  };
  // 前端在等这个事件；事件泵用轮询 /api/events 拉
  window.setTimeout(() => window.dispatchEvent(new Event('pywebviewready')), 0);
  const pump = async () => {
    try {
      const r = await fetch('/api/events');
      if (r.ok) {
        const list = await r.json();
        if (list.length && window.__PE && window.__PE.push) window.__PE.push(list);
      }
    } catch (e) { /* ignore */ }
    window.setTimeout(pump, 100);
  };
  window.setTimeout(pump, 200);
})();
"""


class Handler(SimpleHTTPRequestHandler):
    bus: EventBus
    controller: AppController
    api: Api
    dist: Path
    pick: str = ""

    def log_message(self, *args):                       # noqa: D401
        pass

    def _json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:                          # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:                               # noqa: BLE001
            payload = {}
        if self.path.startswith("/api/invoke"):
            res = self.api.invoke(payload.get("method", ""),
                                  payload.get("params") or {})
            return self._json(res)
        if self.path.startswith("/api/pick_path"):
            return self._json({"ok": True, "data": {"path": self.pick}})
        self._json({"ok": False, "error": {"message": "not found"}}, 404)

    def do_GET(self) -> None:                           # noqa: N802
        if self.path.startswith("/api/events"):
            return self._json(self.bus.drain(500))
        if self.path.startswith("/__bridge.js"):
            data = SHIM.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return None
        return super().do_GET()

    def translate_path(self, path: str) -> str:
        # 静态目录固定为 dist，且把未知路径回落到 index.html（前端是 SPA）
        rel = path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        target = (self.dist / rel).resolve()
        if not str(target).startswith(str(self.dist.resolve())):
            return str(self.dist / "index.html")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            target = self.dist / "index.html"
        return str(target)

    def send_head(self):
        path = self.translate_path(self.path)
        if path.endswith("index.html"):
            html = Path(path).read_text(encoding="utf-8")
            html = html.replace(
                "</head>", '<script src="/__bridge.js"></script></head>')
            data = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            import io
            return io.BytesIO(data)
        return super().send_head()



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--dist", default=str(ROOT / "web" / "dist"))
    ap.add_argument("--db", default="")
    ap.add_argument("--pick", default="")
    args = ap.parse_args()

    dist = Path(args.dist).resolve()
    if not (dist / "index.html").is_file():
        print(f"缺少前端产物：{dist}", file=sys.stderr)
        return 2

    if args.db:
        from app.core import database as db
        path = Path(args.db)
        path.mkdir(parents=True, exist_ok=True)
        if not (path / "pulses-easier.db").exists():
            db.create_database(path)
        db.set_db_path(path)

    bus = EventBus()
    controller = AppController(bus)
    api = Api(controller, bus)

    Handler.dist = dist
    Handler.bus = bus
    Handler.controller = controller
    Handler.api = api
    Handler.pick = args.pick

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"bridge server on http://127.0.0.1:{args.port}", flush=True)
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
