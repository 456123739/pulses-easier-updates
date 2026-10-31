"""
events.py — Web UI 的事件出口
------------------------------------------------
pywebview 的 js_api 方法与窗口回调跑在各自线程里，直接调 `evaluate_js`
既慢又不安全。这里分两层：

  EventBus   线程安全的先进先出队列，seq 单调递增（前端可判断丢事件）
  EventPump  唯一调用 evaluate_js 的线程：33ms 一批，合并高频事件，
             一次 evaluate_js 推一个数组（比逐条调用快一个数量级）

高频合并规则：
  progress  同一时刻只保留最后一条（进度是"最新值即真相"）
  bytes     按 data.key 合并（每个文件一条）
  log       不合并，但单批上限 200 条，超出的直接丢并计数
"""

from __future__ import annotations

import json
import queue
import threading
import time

# 单批上限：一次 evaluate_js 的 JSON 体积控制在 ~64KB 内
MAX_BATCH = 200
PUMP_INTERVAL = 0.033


class EventBus:
    def __init__(self) -> None:
        self._seq = 0
        self._lock = threading.Lock()
        self._queue: queue.Queue[dict] = queue.Queue()
        self.dropped = 0

    def emit(self, kind: str, data=None, *, job_id: str | None = None) -> dict:
        with self._lock:
            self._seq += 1
            event = {"v": 1, "seq": self._seq, "type": kind}
            if job_id:
                event["jobId"] = job_id
            if data is not None:
                event["data"] = data
        self._queue.put(event)
        return event

    # 便捷方法（控制器里到处用到）
    def log(self, message: str, level: str = "INFO", *, time_str: str | None = None):
        return self.emit("log", {
            "level": level,
            "message": message,
            "time": time_str or time.strftime("%H:%M:%S"),
        })

    def progress(self, phase: str = "", message: str = "",
                 percent: float | None = None,
                 current: int | None = None, total: int | None = None):
        return self.emit("progress", {
            "phase": phase, "message": message,
            "percent": percent, "current": current, "total": total,
        })

    def state(self, snapshot: dict):
        return self.emit("state", snapshot)

    def toast(self, message: str, level: str = "info"):
        return self.emit("toast", {"message": message, "level": level})

    def dnd(self, kind: str, message: str = "", paths=None):
        data = {"kind": kind, "message": message}
        if paths:
            data["paths"] = list(paths)
        return self.emit("dnd", data)

    def dialog(self, spec: dict):
        return self.emit("dialog", spec)

    # ------------------------------------------------------------------
    def get_nowait(self) -> dict | None:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def get(self, timeout: float) -> dict | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def qsize(self) -> int:
        return self._queue.qsize()

    def drain(self, limit: int = 20000) -> list[dict]:
        """测试用：取空队列"""
        out = []
        while len(out) < limit:
            item = self.get_nowait()
            if item is None:
                break
            out.append(item)
        return out


def _merge_key(event: dict) -> str | None:
    """可合并事件返回合并键，不可合并返回 None。"""
    kind = event.get("type")
    if kind == "progress":
        return "progress"
    if kind == "bytes":
        key = (event.get("data") or {}).get("key", "")
        return f"bytes:{key}"
    return None


class EventPump:
    """把 EventBus 里的积压事件成批推给前端。"""

    def __init__(self, bus: EventBus, window, logger=None):
        self.bus = bus
        self.window = window
        self.logger = logger
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.sent_batches = 0
        self.sent_events = 0
        self.failed = 0

    # ---- 生命周期 ----
    def start(self) -> EventPump:
        self._thread = threading.Thread(
            target=self._run, name="ui-web-events", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    # ---- 线程体 ----
    def _run(self) -> None:
        while not self._stop.is_set():
            first = self.bus.get(PUMP_INTERVAL)
            if first is None:
                continue
            batch = self._collect(first)
            self._flush(batch)

    def _collect(self, first: dict) -> list[dict]:
        """收集一批：合并同键事件，保留顺序。"""
        merged: list[dict] = []
        index: dict[str, int] = {}

        def add(ev: dict) -> None:
            key = _merge_key(ev)
            if key is not None and key in index:
                merged[index[key]] = ev
                return
            if key is not None:
                index[key] = len(merged)
            merged.append(ev)

        add(first)
        while len(merged) < MAX_BATCH:
            ev = self.bus.get_nowait()
            if ev is None:
                break
            add(ev)
        return merged

    def _flush(self, batch: list[dict]) -> None:
        if not batch:
            return
        try:
            payload = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:      # 数据里有不可序列化对象
            self.failed += 1
            if self.logger:
                self.logger(f"事件序列化失败：{exc}", "WARNING")
            return
        js = f"window.__PE && window.__PE.push({payload});"
        try:
            self.window.evaluate_js(js)
        except Exception as exc:                    # noqa: BLE001
            # 窗口关闭 / 页面重载时不能把 pump 搞崩
            self.failed += 1
            if self.logger:
                try:
                    self.logger(f"事件推送失败：{exc}", "WARNING")
                except Exception:                   # noqa: BLE001, S110
                    pass
            return
        self.sent_batches += 1
        self.sent_events += len(batch)
