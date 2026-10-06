"""
fixture_server.py — 功能测试用的本地 HTTP 源。

模拟真实下载源的各种行为（都要能触发下载器的降级 / 换源 / 超时 / 校验路径）：

  /data/<name>      正常源，支持 Range（206 + Content-Range）
  /norange/<name>   忽略 Range，永远返回 200 + 完整正文（必须被识别并拒绝）
  /stall/<name>     发完响应头后只发一点点就挂住（触发 stall_timeout）
  /slow/<name>      以极低速度滴数据（触发 min_speed_bps 速度窗口）
  /missing/<name>   404 + HTML 错误页（必须不落盘）
  /boom/<name>      503（可重试状态码）
  /flaky/<name>     前 N 次 500，之后正常（N 由模块级 FLAKY_FAILS 控制）
  /chunked/<name>   不带 Content-Length 的分块传输（未知大小）
  /redirect/<name>  302 跳到 /data/<name>
  /truncate/<name>  声明 Content-Length 但少发若干字节，然后正常关闭
"""

import hashlib
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FLAKY_FAILS = 0
STALL_SLEEP = 30.0
SLOW_CHUNK_INTERVAL = 0.4
TRUNCATE_BYTES = 4096

_MAX_PAYLOAD = 32 * 1024 * 1024

_PAYLOAD_CACHE: dict[tuple[str, int], bytes] = {}
_PAYLOAD_LOCK = threading.Lock()


def payload(name: str, size: int) -> bytes:
    """确定性伪随机内容（同一个 name+size 每次一致，且只生成一次）。"""
    key = (name, size)
    with _PAYLOAD_LOCK:
        cached = _PAYLOAD_CACHE.get(key)
    if cached is not None:
        return cached
    rnd = random.Random(hashlib.sha256(name.encode()).hexdigest())
    buf = bytearray()
    while len(buf) < size:
        buf += rnd.randbytes(65536)
    blob = bytes(buf[:size])
    with _PAYLOAD_LOCK:
        if len(_PAYLOAD_CACHE) > 64:
            _PAYLOAD_CACHE.clear()
        _PAYLOAD_CACHE[key] = blob
    return blob


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = {}          # path -> count
        self.ranges = []            # 收到的 Range 头
        self.max_concurrent = 0
        self.concurrent = 0
        self.connections = 0
        self.bytes_sent = 0
        self.slow_aborts = 0

    def note_request(self, path, range_header):
        with self.lock:
            self.requests[path] = self.requests.get(path, 0) + 1
            if range_header:
                self.ranges.append(range_header)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)

    def note_done(self, nbytes=0):
        with self.lock:
            self.concurrent -= 1
            self.bytes_sent += nbytes

    def count(self, path):
        with self.lock:
            return self.requests.get(path, 0)

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.ranges.clear()
            self.max_concurrent = 0
            self.concurrent = 0
            self.connections = 0
            self.bytes_sent = 0
            self.slow_aborts = 0


STATS = Stats()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FixtureServer/1.0"

    def log_message(self, *args):  # 静音
        pass

    def setup(self):
        super().setup()
        with STATS.lock:
            STATS.connections += 1

    # ------------------------------------------------------------------
    def _parse(self):
        raw = self.path
        query = ""
        if "?" in raw:
            raw, _, query = raw.partition("?")
        parts = [p for p in raw.split("/") if p]
        kind = parts[0] if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        params = {}
        for item in query.split("&"):
            if "=" in item:
                k, _, v = item.partition("=")
                params[k] = v
        return kind, name, params

    def _size_of(self, name, params):
        try:
            return int(params.get("size", "0"))
        except ValueError:
            return 0

    def _send_body(self, body: bytes, status=200, extra=None,
                   content_length=True):
        self.send_response(status)
        if content_length:
            self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _serve_range(self, body: bytes, allow_range=True):
        """支持 Range 的正常服务。"""
        rng = self.headers.get("Range")
        if allow_range and rng and rng.startswith("bytes="):
            spec = rng[len("bytes="):]
            first, _, last = spec.partition("-")
            try:
                start = int(first)
                end = int(last) if last else len(body) - 1
            except ValueError:
                self._send_body(b"", status=416)
                return
            end = min(end, len(body) - 1)
            if start > end or start >= len(body):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{len(body)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            chunk = body[start:end + 1]
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{end}/{len(body)}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(chunk)
            return len(chunk)
        self._send_body(body)
        return len(body)

    def _serve_range_ignoring(self, body: bytes):
        """故意忽略 Range，返回 200 + 完整正文。"""
        self._send_body(body)
        return len(body)

    def _serve_stall(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body[:1024])
        self.wfile.flush()
        try:
            time.sleep(STALL_SLEEP)
        except Exception:  # noqa: BLE001, S110
            pass
        with STATS.lock:
            STATS.slow_aborts += 1

    def _serve_slow(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        step = 256
        for i in range(0, len(body), step):
            try:
                self.wfile.write(body[i:i + step])
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                return
            time.sleep(SLOW_CHUNK_INTERVAL)

    def _serve_chunked(self, body: bytes):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        step = 8192
        for i in range(0, len(body), step):
            chunk = body[i:i + step]
            self.wfile.write(f"{len(chunk):X}\r\n".encode())
            self.wfile.write(chunk)
            self.wfile.write(b"\r\n")
        self.wfile.write(b"0\r\n\r\n")

    def _serve_truncate(self, body: bytes):
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        cut = max(0, len(body) - TRUNCATE_BYTES)
        self.wfile.write(body[:cut])
        self.wfile.flush()
        self.close_connection = True

    # ------------------------------------------------------------------
    def do_GET(self):
        global FLAKY_FAILS
        kind, name, params = self._parse()
        rng = self.headers.get("Range")
        STATS.note_request(f"/{kind}/{name}", rng)
        sent = 0
        try:
            size = self._size_of(name, params)
            if name.startswith("s") and "_" in name:
                try:
                    size = int(name.rsplit("_", 1)[1])
                except ValueError:
                    pass
            size = max(0, min(size, _MAX_PAYLOAD))
            body = payload(name or kind, size) if size else b""

            if kind == "data":
                sent = self._serve_range(body)
            elif kind == "norange":
                sent = self._serve_range_ignoring(body)
            elif kind == "stall":
                self._serve_stall(body)
            elif kind == "slow":
                self._serve_slow(body)
            elif kind == "missing":
                self._send_body(b"<html>404 not found</html>", status=404)
            elif kind == "boom":
                self._send_body(b"service unavailable", status=503)
            elif kind == "flaky":
                with STATS.lock:
                    if FLAKY_FAILS > 0:
                        FLAKY_FAILS -= 1
                        fail = True
                    else:
                        fail = False
                if fail:
                    self._send_body(b"try again", status=500)
                else:
                    sent = self._serve_range(body)
            elif kind == "chunked":
                self._serve_chunked(body)
            elif kind == "redirect":
                self.send_response(302)
                self.send_header(
                    "Location", f"/data/{name}?size={size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif kind == "truncate":
                self._serve_truncate(body)
            else:
                self._send_body(b"unknown route", status=404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            STATS.note_done(sent)

    def do_HEAD(self):
        self.do_GET()


class FixtureServer:
    def __init__(self, host="127.0.0.1", port=0,
                 certfile=None, keyfile=None):
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.host, self.port = self.httpd.server_address[:2]
        self.scheme = "http"
        if certfile:
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile, keyfile)
            self.httpd.socket = ctx.wrap_socket(
                self.httpd.socket, server_side=True)
            self.scheme = "https"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)

    @property
    def base(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    def url(self, kind, name, size=0):
        u = f"{self.base}/{kind}/{name}"
        if size:
            u += f"?size={size}"
        return u

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001, S110
            pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa: BLE001, S110
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


if __name__ == "__main__":
    with FixtureServer() as srv:
        print("fixture server:", srv.base)
        print("payload sha256:",
              hashlib.sha256(payload("demo", 1024)).hexdigest()[:16])
