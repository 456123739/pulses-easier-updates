#!/usr/bin/env python3
"""
bench_apply.py — 应用阶段「旧做法 vs 新做法」真实文件对比
------------------------------------------------
旧做法（v0.5.0 及以前）：
    1) 把 overrides + 下载缓存全量复制成 temp/_merged 副本（在 UI 线程上）
    2) 再把 _merged 里的文件逐个 copy2 进整合包
    → 同一份内容写两遍，而且第一遍会把窗口冻住。

新做法（0.6.0）：
    1) 不产生任何副本：PlanSource 按相对路径直接取源
    2) transfer.move_in：同卷是 rename（元数据操作，0 字节 IO）；
       跨卷走系统快速复制（Windows CopyFile2 / sendfile）+ 原子替换

指标：耗时（多次取中位数，容器文件系统噪声很大）、真实写入字节数。
写字节数取自 /proc/self/io 的 write_bytes（真实落盘写入，不是估算）。
检查点开销由 tests/test_recover.py 精确断言，这里不测。

运行：  python3 tests/bench_apply.py [重复次数]
"""

import os
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from app.core.updater import (
    PlanSource,
    SourceLayer,
    UpdatePlan,
    execute_plan,
)

REPEATS = 3


def _write_bytes(path: Path, n: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(n))


def _io_write_bytes() -> int:
    try:
        with open("/proc/self/io") as f:
            for line in f:
                if line.startswith("write_bytes:"):
                    return int(line.split()[1])
    except Exception:  # noqa: BLE001, S110
        pass
    return 0


class _XDev:
    """强制走「跨卷」分支（缓存盘 ≠ 整合包盘）。"""

    def __enter__(self):
        self._r = os.replace
        self._l = os.link

        def _replace(a, b, *args, **kw):
            if ".pulses_new" not in str(a):
                raise OSError(18, "Invalid cross-device link")
            return self._r(a, b, *args, **kw)

        def _link(a, b, *args, **kw):
            raise OSError(1, "Operation not permitted")

        os.replace, os.link = _replace, _link
        return self

    def __exit__(self, *exc):
        os.replace, os.link = self._r, self._l
        return False


def _human(n) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}GB"


def _make_src(tmp: Path, tag: str, rels, size: int) -> Path:
    root = tmp / tag
    for r in rels:
        _write_bytes(root / r, size)
    return root


def _old_way(tmp: Path, rels, size: int):
    """整包副本 + 逐文件 copy2（v0.5.0 的数据路径）。"""
    src = _make_src(tmp, "old_src", rels, size)
    inst = tmp / "old_inst"
    shutil.rmtree(inst, ignore_errors=True)
    inst.mkdir(parents=True)

    w0 = _io_write_bytes()
    t0 = time.perf_counter()
    merged = tmp / "old_merged"
    shutil.rmtree(merged, ignore_errors=True)
    merged.mkdir(parents=True)
    for p in src.rglob("*"):
        if p.is_file():
            rel = p.relative_to(src)
            dst = merged / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    t_merge = time.perf_counter() - t0
    t0 = time.perf_counter()
    for r in rels:
        d = inst / r
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(merged / r, d)
    t_copy = time.perf_counter() - t0
    wrote = _io_write_bytes() - w0
    for d in (merged, inst, src):
        shutil.rmtree(d, ignore_errors=True)
    return t_merge + t_copy, wrote


def _new_way(tmp: Path, rels, size: int, cross: bool):
    """按需取源 + 原子搬运。"""
    src = _make_src(tmp, "new_src", rels, size)
    inst = tmp / "new_inst"
    shutil.rmtree(inst, ignore_errors=True)
    inst.mkdir(parents=True)
    plan = UpdatePlan(copy=list(rels))

    w0 = _io_write_bytes()
    t0 = time.perf_counter()
    if cross:
        with _XDev():
            rep = execute_plan(plan, inst, source=PlanSource(
                [SourceLayer(src, consume=True)]))
    else:
        rep = execute_plan(plan, inst, source=PlanSource(
            [SourceLayer(src, consume=True)]))
    dt = time.perf_counter() - t0
    wrote = _io_write_bytes() - w0
    for d in (inst, src):
        shutil.rmtree(d, ignore_errors=True)
    return dt, wrote, rep


def _scene(tmp: Path, n: int, size: int, label: str, repeats: int):
    rels = [Path("mods") / f"m{i:06d}.jar" for i in range(n)]
    old_t: list[float] = []
    old_w: list[int] = []
    new_t: list[float] = []
    new_w: list[int] = []
    x_t: list[float] = []
    x_w: list[int] = []
    moved = 0
    for _ in range(repeats):
        t, w = _old_way(tmp, rels, size)
        old_t.append(t)
        old_w.append(w)
        t, w, rep = _new_way(tmp, rels, size, cross=False)
        new_t.append(t)
        new_w.append(w)
        moved = rep.get("moved", 0)
        t, w, _rep2 = _new_way(tmp, rels, size, cross=True)
        x_t.append(t)
        x_w.append(w)

    total = n * size
    mo, mn, mx = (statistics.median(old_t), statistics.median(new_t),
                  statistics.median(x_t))
    print(f"\n{label}（{n} 个文件 × {_human(size)} = {_human(total)}）"
          f"  中位数（{repeats} 次）")
    print(f"  旧做法      整包副本 + 逐文件复制 {mo:6.2f}s   "
          f"实际写入 {_human(statistics.median(old_w))}")
    print(f"  新做法·同卷  按需取源 + 瞬时移动  {mn:6.2f}s   "
          f"实际写入 {_human(statistics.median(new_w))}"
          f"   瞬时搬运 {moved}/{n}   加速 {mo / max(mn, 1e-6):.1f}×")
    print(f"  新做法·跨卷  分块/快速复制        {mx:6.2f}s   "
          f"实际写入 {_human(statistics.median(x_w))}")


def main() -> int:
    repeats = REPEATS
    if len(sys.argv) > 1:
        repeats = max(1, int(sys.argv[1]))
    print(f"平台 {sys.platform}   CPU {os.cpu_count()} 核   "
          f"重复 {repeats} 次取中位数")
    print("说明：写字节数取自 /proc/self/io 的 write_bytes（真实落盘写入）")
    tmp = Path(tempfile.mkdtemp(prefix="bench_apply_"))
    try:
        _scene(tmp, 400, 2 * 1024 * 1024, "少量大文件", repeats)
        _scene(tmp, 4000, 128 * 1024, "中量中文件", repeats)
        _scene(tmp, 12000, 8 * 1024, "大量小文件", repeats)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
