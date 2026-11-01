"""
file_server.py — e2e 专用的"能限速、支持 Range"的静态文件服务器
------------------------------------------------
为什么不用 `python -m http.server`：

  · 它不支持 Range（下载器的分片会全部退化成单连接，分片路径测不到）；
  · 它不限速，几 MB 的文件几十毫秒就下完，"下载中"的界面状态根本采不到样。

所以这里补一个最小实现：Range/206、64KB 分块写、每块之后可选 sleep。

用法：
    python scripts/dev/file_server.py --dir <目录> --port 8233 [--delay-ms 60]
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class Handler(BaseHTTPRequestHandler):
    root: Path
    chunk: int = 64 * 1024
    delay: float = 0.0

    def log_message(self, *args):                        # noqa: D401
        pass

    def _target(self) -> Path | None:
        rel = self.path.split("?", 1)[0].split("#", 1)[0].lstrip("/")
        try:
            target = (type(self).root / rel).resolve()
            if not str(target).startswith(str(type(self).root.resolve())):
                return None
        except Exception:                                # noqa: BLE001
            return None
        return target if target.is_file() else None

    def do_HEAD(self) -> None:                           # noqa: N802
        self._serve(body=False)

    def do_GET(self) -> None:                            # noqa: N802
        self._serve(body=True)

    def _serve(self, body: bool) -> None:
        target = self._target()
        if target is None:
            self.send_error(404, "not found")
            return
        size = target.stat().st_size
        start, end, status = 0, size - 1, 200
        raw = (self.headers.get("Range") or "").strip()
        if raw.lower().startswith("bytes="):
            m = RANGE_RE.match(raw.split(",", 1)[0].strip())
            if m:
                if not m.group(1) and m.group(2):
                    start = max(0, size - int(m.group(2)))
                else:
                    start = int(m.group(1) or 0)
                    end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = 206

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not body:
            return

        remaining = length
        try:
            with open(target, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    block = f.read(min(type(self).chunk, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
                    if type(self).delay:
                        time.sleep(type(self).delay)
        except (BrokenPipeError, ConnectionResetError):
            pass                                         # 客户端中止（正是要测的）


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--delay-ms", type=int, default=0)
    ap.add_argument("--chunk", type=int, default=64 * 1024)
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2
    Handler.root = root
    Handler.delay = max(0, args.delay_ms) / 1000.0
    Handler.chunk = max(1024, args.chunk)

    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    httpd.daemon_threads = True
    print(f"file server on http://127.0.0.1:{httpd.server_address[1]}/"
          f"（{root}，每块 {Handler.chunk}B 后停 {args.delay_ms}ms）",
          flush=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
