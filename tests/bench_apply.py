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
       跨卷才分块复制，并按批次让出磁盘

指标：耗时、真实写入字节数、瞬时搬运的文件数。
写字节数用 /proc/self/io 的 write_bytes 实测（不是估算）。

运行：  python3 tests/bench_apply.py
"""

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_HERE))

from app.config import Strategy                       # noqa: E402
from app.core.updater import (                        # noqa: E402
    PlanSource,
    SourceLayer,
    UpdatePlan,
    execute_plan,
)


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
    except Exception:  # noqa: BLE001
        pass
    return 0


def _drop_cache(path: Path):
    """尽量把刚写的内容从页缓存里挤出去，让读操作也落到盘上。"""
    try:
        os.posix_fadvise(os.open(path, os.O_RDONLY),
                        0, 0, os.POSIX_FADV_DONTNEED)
    except Exception:  # noqa: BLE001, S110
        pass


class _XDev:
    """强制走"跨卷"分支（缓存盘 ≠ 整合包盘）。"""

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


def _scene(tmp: Path, n: int, size: int, label: str):
    """同一份内容，跑旧做法 / 新做法(同卷) / 新做法(跨卷)。"""
    from app.core import checkpoint as cp_mod

    total = n * size
    rels = [Path("mods") / f"m{i:06d}.jar" for i in range(n)]

    def _make_src(tag: str) -> Path:
        root = tmp / tag
        for r in rels:
            _write_bytes(root / r, size)
        return root

    # ---- 旧做法：先整包复制成 _merged，再逐文件 copy2（v0.5.0 的数据路径）----
    src = _make_src("old_src")
    inst = tmp / "old_inst"
    inst.mkdir(parents=True, exist_ok=True)
    w0 = _io_write_bytes()
    t0 = time.perf_counter()
    merged = tmp / "old_merged"
    merged.mkdir(parents=True, exist_ok=True)
    for p in src.rglob("*"):
        if p.is_file():
            rel = p.relative_to(src)
            dst = merged / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    t_merge = time.perf_counter() - t0
    t0 = time.perf_counter()
    cp = cp_mod.Checkpoint(pack_root=str(inst), stage="copy", done=[],
                           remaining=[], new_root=str(merged))
    for i, r in enumerate(rels):
        d = inst / r
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(merged / r, d)
        cp.done.append(r.as_posix())
        if i % 25 == 24:
            cp_mod.save_checkpoint(cp)
    t_copy = time.perf_counter() - t0
    old_time = t_merge + t_copy
    old_write = _io_write_bytes() - w0
    shutil.rmtree(merged, ignore_errors=True)

    # ---- 新做法：同卷（瞬时移动，含检查点） ----
    src = _make_src("new_src")
    inst = tmp / "new_inst"
    inst.mkdir(parents=True, exist_ok=True)
    w0 = _io_write_bytes()
    plan = UpdatePlan(copy=list(rels))
    t0 = time.perf_counter()
    rep = execute_plan(plan, inst, source=PlanSource(
        [SourceLayer(src, consume=True)]))
    new_time = time.perf_counter() - t0
    new_write = _io_write_bytes() - w0

    # ---- 新做法：同卷，但把检查点写入关掉（隔离出检查点开销） ----
    src = _make_src("new2_src")
    inst = tmp / "new2_inst"
    inst.mkdir(parents=True, exist_ok=True)
    real_save = cp_mod.save_checkpoint
    cp_mod.save_checkpoint = lambda _cp: True
    try:
        t0 = time.perf_counter()
        execute_plan(plan, inst, source=PlanSource(
            [SourceLayer(src, consume=True)]))
        new_nocp = time.perf_counter() - t0
    finally:
        cp_mod.save_checkpoint = real_save

    # ---- 新做法：跨卷（分块复制 + 批次节流，含检查点） ----
    src = _make_src("x_src")
    inst = tmp / "x_inst"
    inst.mkdir(parents=True, exist_ok=True)
    w0 = _io_write_bytes()
    t0 = time.perf_counter()
    with _XDev():
        rep2 = execute_plan(plan, inst, source=PlanSource(
            [SourceLayer(src, consume=True)]),
            chunk_bytes=4 * 1024 * 1024, batch_bytes=96 * 1024 * 1024,
            batch_pause_ms=12.0)
    x_time = time.perf_counter() - t0
    x_write = _io_write_bytes() - w0

    print(f"\n{label}（{n} 个文件 × {_human(size)} = {_human(total)}）")
    print(f"  旧做法      整包副本 {t_merge:6.2f}s + 逐文件复制 {t_copy:6.2f}s"
          f"  = {old_time:6.2f}s   实际写入 {_human(old_write)}")
    print(f"  新做法·同卷  搬运 {new_time:6.2f}s   实际写入 {_human(new_write)}"
          f"   瞬时搬运 {rep.get('moved', 0)}/{n} 个文件"
          f"   加速 {old_time / max(new_time, 1e-6):.1f}×"
          f"   （其中检查点 {new_time - new_nocp:.2f}s）")
    print(f"  新做法·跨卷  搬运 {x_time:6.2f}s   实际写入 {_human(x_write)}"
          f"   真的复制 {rep2.get('bytes', 0) and 'all' or '0'}")

    for d in ("old_src", "old_inst", "new_src", "new_inst", "new2_src",
              "new2_inst", "x_src", "x_inst", "old_merged"):
        shutil.rmtree(tmp / d, ignore_errors=True)


def main() -> int:
    print(f"平台 {sys.platform}   CPU {os.cpu_count()} 核")
    print("说明：写字节数取自 /proc/self/io 的 write_bytes（真实落盘写入）")
    tmp = Path(tempfile.mkdtemp(prefix="bench_apply_"))
    try:
        _scene(tmp, 400, 2 * 1024 * 1024, "少量大文件")
        _scene(tmp, 4000, 128 * 1024, "中量中文件")
        _scene(tmp, 12000, 8 * 1024, "大量小文件")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
