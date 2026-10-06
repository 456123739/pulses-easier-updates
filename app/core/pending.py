"""
pending.py — 「更新受阻 / 补齐缺口」的纯逻辑与凭证文件
================================================
这是**没有界面**的一层，方便单测；界面在 `app/ui/widgets/pending_window.py`。

包含三块：

一、文件名匹配 / 链接整理
    从 `download_panel.py` 迁过来（浏览器给重复下载加的后缀、大小写差异、
    多源链接文本），行为保持不变。

二、更新未完成凭证（写入整合包，供下次定位时识别）
    <整合包>/!!!更新未完成-请阅读!!!.txt     ← 给人看，`!` 开头排在资源管理器最前
    <整合包>/.pulses_easier/pending_update.json ← 给软件看
    这两个名字都在 `config.LOCAL_ONLY_*` 里，**永不参与更新的比对/删除**。

三、受阻项的构造与核对
    - `entries_from_tasks()`：从下载任务里挑出"仍未就绪"的文件
    - `check_marker()`：核对凭证里的缺失文件是否已经补齐（哈希一致）
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path

from ..config import LOCAL_ONLY_DIRS, LOCAL_ONLY_FILES, is_local_only_name
from .downloader import describe_sources

# ----------------------------------------------------------------------
# 一、文件名匹配 / 链接整理
# ----------------------------------------------------------------------
# 浏览器/系统给重复下载加的后缀："xxx (1).jar"、"xxx - 副本.jar"、"xxx - copy.jar"
COPY_SUFFIX_RE = re.compile(
    r"(?:\s*\(\d+\)|\s*-\s*副本|\s*-\s*copy|\s*_副本)+$", re.IGNORECASE)


def normalize_download_name(name: str) -> str:
    """
    归一化文件名：去浏览器副本后缀 + 统一大小写。

    注意不能把版本号当后缀削掉 —— `create-1.20.1-6.0.7.jar` 必须原样保留，
    所以这里只认 "(1)" / "- 副本" / "- copy" 这几种**明确**的副本标记。
    """
    base = Path(str(name)).name.strip()
    stem, dot, suffix = base.rpartition(".")
    if not dot:                     # 没有扩展名
        stem, suffix = base, ""
    stem = COPY_SUFFIX_RE.sub("", stem).strip()
    return (stem + ("." + suffix if suffix else "")).casefold()


def match_candidates(file_name: str, rels) -> list[str]:
    """
    按拖进来的文件名，找出可能对应的待补入项（可能多个同名的）。

    只剩一个待补入项时直接归给它（投进去的东西还会过哈希校验，
    校验不过会被拒，所以这里放宽是安全的）。
    """
    want = normalize_download_name(file_name)
    rels = list(rels)
    hits = [rel for rel in rels
            if normalize_download_name(Path(rel).name) == want]
    if hits:
        return hits
    if len(rels) == 1:
        return rels
    return []


def dedupe_urls(urls) -> list[str]:
    out: list[str] = []
    for u in urls or []:
        if isinstance(u, str) and u and u not in out:
            out.append(u)
    return out


def links_text(rel: str, urls) -> str:
    """复制到剪贴板的内容：一行一个源，首行带上文件名方便用户对照。"""
    items = dedupe_urls(urls)
    if not items:
        return ""
    name = Path(rel).name
    return "\n".join([f"# {name}"] + items)


def source_rows(urls) -> list[tuple[str, str]]:
    """[(源名称, URL)] —— 面板只显示名称，点击才打开浏览器。"""
    return describe_sources(dedupe_urls(urls))


# ----------------------------------------------------------------------
# 二、更新未完成凭证
# ----------------------------------------------------------------------
MARKER_DIR = ".pulses_easier"
MARKER_FILE = "pending_update.json"
NOTICE_FILENAME = "!!!更新未完成-请阅读!!!.txt"
MARKER_FORMAT_VERSION = 1

# 极端情况下给个上限，避免凭证文件无限膨胀
MAX_ENTRIES = 2000


def marker_dir(pack_root: Path) -> Path:
    return Path(pack_root) / MARKER_DIR


def marker_path(pack_root: Path) -> Path:
    return marker_dir(pack_root) / MARKER_FILE


def notice_path(pack_root: Path) -> Path:
    return Path(pack_root) / NOTICE_FILENAME


def build_marker(pack_root: Path, *, pack_name: str = "",
                 pack_version: str = "", update_zip: str = "",
                 update_fingerprint: str = "", cache_root: str = "",
                 missing: list[dict] | None = None) -> dict:
    return {
        "format_version": MARKER_FORMAT_VERSION,
        "created_at": time.time(),
        "created_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pack_root": str(Path(pack_root)),
        "pack_name": pack_name,
        "pack_version": pack_version,
        "update_zip": update_zip,
        "update_fingerprint": update_fingerprint,
        "cache_root": cache_root,
        "missing": list(missing or [])[:MAX_ENTRIES],
    }


def _atomic_write(path: Path, text: str) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001
        return False


def build_notice_text(marker: dict) -> str:
    """人类可读的说明文件内容（写在整合包根目录）。"""
    missing = marker.get("missing") or []
    root = marker.get("pack_root") or ""
    lines = [
        "Pulses Easier —— 更新未完成说明",
        "=" * 46,
        f"整合包　：{marker.get('pack_name') or Path(root).name or '未命名'}",
        f"更新时间：{marker.get('created_at_text') or ''}",
        (f"更新包　：{Path(marker.get('update_zip') or '').name or '未知'}"
         f"（版本 {marker.get('pack_version') or '未知'}）"),
        "",
        f"有 {len(missing)} 个文件没能下载，**本次更新不完整**：",
        "",
    ]
    for i, item in enumerate(missing, 1):
        rel = str(item.get("rel", ""))
        size = int(item.get("size", 0) or 0)
        lines.append(f"{i}) {rel}")
        target = (Path(root) / rel) if root else rel
        sha1 = str(item.get("sha1", "") or "")
        size_text = _human_size(size)
        if sha1:
            size_text += "　SHA1 " + sha1
        lines.append(f"   应放到：{target}")
        lines.append(f"   大小　：{size_text}")
        rows = source_rows(item.get("urls") or [])
        if rows:
            lines.append("   下载源：")
            for name, url in rows:
                lines.append(f"     {name}  {url}")
        lines.append("")
    lines += [
        "-" * 46,
        "补齐办法（任选其一）：",
        "  1) 打开 Pulses Easier → 定位本整合包 → 按提示补齐（推荐）",
        "  2) 自己下载上面的文件，放到「应放到」的位置（文件名要完全一致）",
        "",
        "补齐完成后，本文件与 .pulses_easier/pending_update.json 可以删除；",
        "软件下次定位本整合包时也会自动核对并清理。",
    ]
    return "\n".join(lines)


def write_marker(pack_root: Path, marker: dict) -> bool:
    """写凭证（json 原子写 + 人类可读说明）。"""
    pack_root = Path(pack_root)
    ok_json = _atomic_write(
        marker_path(pack_root),
        json.dumps(marker, ensure_ascii=False, indent=2))
    ok_txt = _atomic_write(notice_path(pack_root), build_notice_text(marker))
    return bool(ok_json and ok_txt)


def read_marker(pack_root: Path) -> dict | None:
    try:
        p = marker_path(pack_root)
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        if not isinstance(data.get("missing"), list):
            data["missing"] = []
        return data
    except Exception:  # noqa: BLE001
        return None


def clear_marker(pack_root: Path) -> bool:
    """删除凭证（json + 说明；.pulses_easier 空了就一并删掉）。"""
    pack_root = Path(pack_root)
    ok = True
    for p in (marker_path(pack_root), notice_path(pack_root)):
        try:
            if p.is_file():
                p.unlink()
        except Exception:  # noqa: BLE001
            ok = False
    try:
        d = marker_dir(pack_root)
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except Exception:  # noqa: BLE001, S110
        pass
    return ok


# ----------------------------------------------------------------------
# 三、受阻项的构造与核对
# ----------------------------------------------------------------------
def _file_matches(path: Path, item: dict) -> bool:
    """核对文件是否存在且与凭证里的哈希一致（没记哈希就只比大小）。"""
    try:
        if not path.is_file():
            return False
        size = int(item.get("size", 0) or 0)
        if size and path.stat().st_size != size:
            return False
        want = [(algo, str(item.get(algo, "") or ""))
                for algo in ("sha1", "sha256", "sha512")]
        want = [(a, v) for a, v in want if v]
        if not want:
            return True
        for algo, value in want:
            h = hashlib.new(algo)
            with open(path, "rb") as f:
                while chunk := f.read(1 << 20):
                    h.update(chunk)
            if h.hexdigest().lower() != value.lower():
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def file_matches(path: Path, item: dict) -> bool:
    """公开别名：文件是否存在且与凭证/任务里的哈希一致。"""
    return _file_matches(Path(path), item or {})


def entry_from_task(task) -> dict:
    """从下载任务构造一个受阻项（界面与凭证共用）。"""
    urls = dedupe_urls(getattr(task, "urls", []) or [])
    return {
        "rel": str(getattr(task, "rel_path", "") or ""),
        "urls": urls,
        "sources": [n for n, _u in describe_sources(urls)],
        "sha1": str(getattr(task, "sha1", "") or ""),
        "sha256": str(getattr(task, "sha256", "") or ""),
        "sha512": str(getattr(task, "sha512", "") or ""),
        "size": int(getattr(task, "file_size", 0) or 0),
        "error": "",
    }


def entries_from_tasks(tasks, cache_root: Path | None,
                       errors: dict | None = None) -> list[dict]:
    """挑出"仍未就绪"的任务：目标已存在且校验通过的不算。"""
    errors = errors or {}
    out: list[dict] = []
    for t in tasks or []:
        rel = str(getattr(t, "rel_path", "") or "")
        if not rel:
            continue
        if cache_root is not None and _file_matches(Path(cache_root) / rel,
                                                    entry_from_task(t)):
            continue
        item = entry_from_task(t)
        item["error"] = str(errors.get(rel, "") or "")
        out.append(item)
    return out


def check_marker(pack_root: Path) -> dict:
    """
    核对凭证：
      {"has": bool, "marker": dict|None,
       "outstanding": [缺失项], "resolved": bool}
    resolved=True 表示凭证里的缺失文件**现在都已存在且哈希匹配**
    （玩家自己放好了 / 后续更新包已带上）→ 凭证可以静默清理。
    """
    pack_root = Path(pack_root)
    marker = read_marker(pack_root)
    if not marker:
        return {"has": False, "marker": None, "outstanding": [],
                "resolved": False}
    outstanding = [it for it in (marker.get("missing") or [])
                   if not _file_matches(pack_root / str(it.get("rel", "")),
                                        it)]
    return {"has": True, "marker": marker, "outstanding": outstanding,
            "resolved": not outstanding}


def has_local_only(pack_root: Path) -> bool:
    """整合包里是否还留着凭证文件（用于日志/诊断）。"""
    pack_root = Path(pack_root)
    return (marker_path(pack_root).is_file()
            or notice_path(pack_root).is_file())


def _human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


__all__ = [
    "COPY_SUFFIX_RE",
    "LOCAL_ONLY_DIRS",
    "LOCAL_ONLY_FILES",
    "MARKER_DIR",
    "MARKER_FILE",
    "MAX_ENTRIES",
    "NOTICE_FILENAME",
    "build_marker",
    "build_notice_text",
    "check_marker",
    "clear_marker",
    "dedupe_urls",
    "entries_from_tasks",
    "entry_from_task",
    "file_matches",
    "has_local_only",
    "is_local_only_name",
    "links_text",
    "marker_dir",
    "marker_path",
    "match_candidates",
    "normalize_download_name",
    "notice_path",
    "read_marker",
    "source_rows",
    "write_marker",
]
