"""
resume.py — 下载中断恢复记录
------------------------------------------------
记录结构：
  {
    "zip_path": "...",
    "zip_hash": "sha256...",
    "pack_root": "...",
    "cache_root": "...",
    "completed": ["mods/a.jar", ...],
    "failed":    ["mods/b.jar", ...],
    "total": 123,
    "started_at": 1700000000.0,
    "updated_at": 1700000123.4,
    "pack_name": "整合包名",
    "version": "1.2.3",
    "ignored_at": 0.0,          # >0 表示用户已勾选"不再提醒"
    "strategies": {...},        # 玩家为这次更新自定义的策略（可选）
    "checked":    {...},        # 玩家这次的勾选状态（可选）
    "strategies_customized": False,
  }

存储位置：<cache_root>/resume.json
"""

import hashlib
import json
import os
import time
from pathlib import Path

RESUME_FILENAME = "resume.json"

# 更新包失效的记录保留多久后清理（期间一直可以"重新定位更新包"）
INVALID_RETENTION_DAYS = 30


def _atomic_write_json(path: Path, data: dict) -> bool:
    """tmp + os.replace：断电不会留下半截 JSON。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _hash_file_quick(path: Path) -> str:
    """快速指纹：大小 + 前 64KB + 后 64KB 的 SHA-256"""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    h = hashlib.sha256()
    h.update(str(size).encode())
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
            h.update(head)
            if size > 131072:
                f.seek(-65536, 2)
                tail = f.read(65536)
                h.update(tail)
    except OSError:
        return ""
    return h.hexdigest()


# ----------------------------------------------------------------------
# 读写
# ----------------------------------------------------------------------
def write_resume(cache_root: Path, zip_path: Path,
                 pack_root: Path | str | None,
                 completed: list[str], failed: list[str],
                 total: int, started_at: float,
                 pack_name: str = "", version: str = "",
                 ignored_at: float | None = None,
                 strategies: dict | None = None,
                 checked: dict | None = None,
                 strategies_customized: bool | None = None) -> bool:
    """
    写入/覆盖 resume.json。

    ignored_at / strategies / checked / strategies_customized：
      - None → **保留原文件里已有的值**（这样"重新定位更新包""不再提醒"
        这类只改个别字段的调用不会把玩家自定义的策略弄丢）
      - 显式传值 → 覆盖
    """
    cache_root = Path(cache_root)
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        resume_file = cache_root / RESUME_FILENAME

        # 保留原字段（只改个别字段的调用方不必重复传）
        prev = _read_existing(resume_file)
        if ignored_at is None:
            ignored_at = float(prev.get("ignored_at", 0.0) or 0.0)
        if strategies is None:
            strategies = prev.get("strategies") or {}
        if checked is None:
            checked = prev.get("checked") or {}
        if strategies_customized is None:
            strategies_customized = bool(
                prev.get("strategies_customized", False))

        pack_root_str = ""
        if pack_root:
            try:
                pack_root_str = str(Path(pack_root).resolve())
            except Exception:  # noqa: BLE001
                pack_root_str = str(pack_root)

        data = {
            "zip_path": str(Path(zip_path).resolve()),
            "zip_hash": _hash_file_quick(Path(zip_path)),
            "pack_root": pack_root_str,
            "cache_root": str(cache_root.resolve()),
            "completed": list(completed),
            "failed": list(failed),
            "total": int(total),
            "started_at": float(started_at),
            "updated_at": time.time(),
            "pack_name": pack_name,
            "version": version,
            "ignored_at": float(ignored_at or 0.0),
            "strategies": dict(strategies or {}),
            "checked": dict(checked or {}),
            "strategies_customized": bool(strategies_customized),
        }
        return _atomic_write_json(resume_file, data)
    except Exception:  # noqa: BLE001
        return False


def _read_existing(resume_file: Path) -> dict:
    """读原记录（用于保留未显式传入的字段），失败返回空 dict。"""
    try:
        if not resume_file.is_file():
            return {}
        data = json.loads(resume_file.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _read_ignored_at(resume_file: Path) -> float:
    """只读 ignored_at 字段，失败返回 0.0"""
    try:
        return float(_read_existing(resume_file).get("ignored_at", 0.0) or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def read_resume(cache_root: Path) -> dict | None:
    try:
        p = Path(cache_root) / RESUME_FILENAME
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def quick_fingerprint(path: Path) -> str:
    """公开的快速指纹（缓存目录命名等场景使用）。"""
    return _hash_file_quick(Path(path))


def clear_resume(cache_root: Path) -> bool:
    try:
        p = Path(cache_root) / RESUME_FILENAME
        if p.is_file():
            p.unlink()
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# 忽略状态
# ----------------------------------------------------------------------
def is_ignored(data: dict) -> bool:
    """是否被用户标记为「不再提醒」"""
    try:
        return float(data.get("ignored_at", 0.0) or 0.0) > 0.0
    except Exception:  # noqa: BLE001
        return False


def mark_ignored(cache_root: Path) -> bool:
    """
    标记为已忽略：只更新 ignored_at，其余字段不动。
    返回是否成功。
    """
    cache_root = Path(cache_root)
    resume_file = cache_root / RESUME_FILENAME
    try:
        if not resume_file.is_file():
            return False
        data = json.loads(resume_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        data["ignored_at"] = time.time()
        data["updated_at"] = data["ignored_at"]
        return _atomic_write_json(resume_file, data)
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# 可用性检查
# ----------------------------------------------------------------------
def check_resume_availability(data: dict) -> dict:
    """
    检查 resume 记录是否可用于「继续更新」。
    返回：
      {
        "ok": bool,
        "zip_ok": bool, "zip_path": Path | None, "zip_reason": str,
        "pack_ok": bool, "pack_root": Path | None, "pack_reason": str,
      }
    """
    result = {
        "ok": False,
        "zip_ok": False,
        "zip_path": None,
        "zip_reason": "",
        "pack_ok": False,
        "pack_root": None,
        "pack_reason": "",
    }

    try:
        zp = Path(data.get("zip_path", ""))
        if not zp.is_file():
            result["zip_reason"] = f"更新包文件不存在（{zp}）"
        else:
            expected = data.get("zip_hash", "")
            if expected and _hash_file_quick(zp) != expected:
                result["zip_reason"] = "更新包已被修改或替换"
            else:
                result["zip_ok"] = True
                result["zip_path"] = zp
    except Exception as e:  # noqa: BLE001
        result["zip_reason"] = f"更新包路径无效（{e}）"

    try:
        pr_raw = data.get("pack_root", "")
        if not pr_raw:
            result["pack_reason"] = "缺少整合包定位记录（旧版本中断记录）"
        else:
            pr = Path(pr_raw)
            if not pr.is_dir():
                result["pack_reason"] = f"整合包目录不存在（{pr}）"
            else:
                has_mods = (pr / "mods").is_dir()
                has_config = (pr / "config").is_dir()
                if not (has_mods or has_config):
                    result["pack_reason"] = "该目录不像整合包根目录"
                else:
                    result["pack_ok"] = True
                    result["pack_root"] = pr
    except Exception as e:  # noqa: BLE001
        result["pack_reason"] = f"整合包路径无效（{e}）"

    result["ok"] = result["zip_ok"] and result["pack_ok"]
    return result


def is_resume_valid(data: dict) -> bool:
    """更新包是否仍可定位（不检查 pack_root）"""
    try:
        zp = Path(data.get("zip_path", ""))
        if not zp.is_file():
            return False
        expected = data.get("zip_hash", "")
        if not expected:
            return True
        return _hash_file_quick(zp) == expected
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------
# 扫描
# ----------------------------------------------------------------------
def scan_all_resumes(db_root: Path,
                     include_ignored: bool = False) -> list[dict]:
    """
    扫描 <db>/cache/update_packs/*/resume.json。

    include_ignored=False（默认）→ 过滤掉已忽略的记录。

    **更新包暂时失效（被移动/改名/在未插入的移动盘上）的记录不会立刻删除**：
    删掉它等于把 completed 清单一起丢掉，「重新定位更新包」这条路径也会变成
    死代码。只在超过 INVALID_RETENTION_DAYS 天后才清理。

    返回按 updated_at 倒序的列表。
    """
    db_root = Path(db_root)
    root = db_root / "cache" / "update_packs"
    if not root.is_dir():
        return []
    result: list[dict] = []
    now = time.time()
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            data = read_resume(child)
            if data is None:
                continue
            if not is_resume_valid(data):
                age = now - float(data.get("updated_at", 0) or 0)
                if age > INVALID_RETENTION_DAYS * 86400:
                    clear_resume(child)
                    continue
                # 保留：交给"重新定位更新包"流程
            if not include_ignored and is_ignored(data):
                continue
            result.append(data)
    except OSError:
        pass
    result.sort(key=lambda d: d.get("updated_at", 0), reverse=True)
    return result


def count_ignored(db_root: Path) -> int:
    """统计被忽略的 resume 记录数（仅用于启动日志提示）"""
    db_root = Path(db_root)
    root = db_root / "cache" / "update_packs"
    if not root.is_dir():
        return 0
    count = 0
    try:
        for child in root.iterdir():
            if not child.is_dir():
                continue
            data = read_resume(child)
            if data is None:
                continue
            if is_ignored(data):
                count += 1
    except OSError:
        pass
    return count


def cleanup_stale_parts(cache_root: Path) -> int:
    """
    清理下载缓存里的残留分片。

    只认下载器自己的两种命名：`*.part` 与 `*.part.meta`
    （旧实现用 `*.part*`，会把名字里带 ".part" 的正常文件也删掉）。
    """
    cache_root = Path(cache_root)
    if not cache_root.is_dir():
        return 0
    removed = 0
    patterns = ("*.part", "*.part.meta")
    try:
        for pattern in patterns:
            for p in cache_root.rglob(pattern):
                if not p.is_file():
                    continue
                try:
                    p.unlink()
                    removed += 1
                except Exception:  # noqa: BLE001, S112
                    continue
    except OSError:
        pass
    return removed