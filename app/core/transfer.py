"""
transfer.py — 把新内容搬进整合包的**唯一**通道
------------------------------------------------
为什么需要单独一层：

1. **用移动，不用复制。**
   更新内容原本就在这台机器上（下载缓存 / 解压出来的 overrides），
   再 `copy2` 一份到整合包等于把同一份数据读写两遍。
   同卷时 `os.replace` / `os.link` 是**元数据操作**，瞬时完成、不产生
   IO；只有跨卷（缓存盘 ≠ 整合包盘）才真的需要搬字节。

2. **写入必须原子。**
   直接 `copy2` 到目标 = 打开目标就截断，进程被杀/断电/杀软回滚会
   在整合包里留下半截文件（实测 2GB 文件被强杀后留下 512MB 残file）。
   这里一律"先写同目录临时名，再 `os.replace` 覆盖"，失败不留痕。

3. **不要吃满硬盘。**
   大文件按块（默认 4MiB）搬运，块与块之间让出 GIL；批量之间由
   updater 按"每批字节数 + 暂停毫秒"节流。

Windows 注意点（主要目标平台）：
  - `os.replace` 覆盖被占用的文件会抛 PermissionError(winerror 5/32)：
    这里翻译成人话（"请先关闭游戏/启动器"），而不是丢一堆 errno。
  - 跨盘搬运在 Windows 上是 ERROR_NOT_SAME_DEVICE(17)，等价 POSIX 的 EXDEV。
  - 超过 260 字符的路径在 Win10 上会失败：所有系统调用前统一加 `\\\\?\\`
    前缀（见 `sys_path`）。
"""

import errno
import hashlib
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# 分块搬运的默认块大小：4MiB 对机械盘/固态盘都不算小，
# 同时保证每次写入之间有自然的让出点。
CHUNK_BYTES = 4 * 1024 * 1024

# 临时名后缀：同目录、点号开头。正常流程结束后不会留下。
NEW_SUFFIX = ".pulses_new"

_IS_WINDOWS = sys.platform.startswith("win")

# Windows 单个路径（不含前缀）的安全上限，超过就加 \\?\ 前缀
_WIN_LONG_PATH = 240


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------
def sys_path(p) -> str:
    """
    交给系统调用用的路径字符串。

    只在 Windows 上、且路径足够长时加 `\\\\?\\` 前缀（Win10 默认 260
    字符上限；不加前缀的话长路径整合包（嵌套很深的 config/资源包）
    会直接 ENOENT）。
    """
    s = os.fspath(p)
    if not _IS_WINDOWS or len(s) < _WIN_LONG_PATH:
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):                 # UNC：\\server\share
        return "\\\\?\\UNC\\" + s[2:]
    try:
        s = os.path.abspath(s)
    except Exception:  # noqa: BLE001, S110
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def tmp_name(dst: Path) -> Path:
    """目标同目录下的临时名（同卷 rename 才可能是瞬时的）。"""
    return dst.with_name("." + dst.name + NEW_SUFFIX)


def is_transient_name(name: str) -> bool:
    """是不是搬运过程中留下的临时名（比对/清理时应当忽略）。"""
    return name.endswith(NEW_SUFFIX) or ".pulses_tmp" in name


def is_cross_device(exc: BaseException) -> bool:
    """跨卷错误（POSIX 的 EXDEV / Windows 的 ERROR_NOT_SAME_DEVICE）。"""
    if isinstance(exc, OSError):
        if exc.errno == errno.EXDEV:
            return True
        if getattr(exc, "winerror", None) == 17:      # ERROR_NOT_SAME_DEVICE
            return True
    return False


def _existing_ancestor(p: Path) -> Path:
    cur = Path(p)
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur


def same_volume(a: Path, b: Path) -> bool:
    """两个路径是否在同一个卷（同卷才有瞬时搬运）。"""
    try:
        dev_a = os.stat(sys_path(_existing_ancestor(a))).st_dev
        dev_b = os.stat(sys_path(_existing_ancestor(b))).st_dev
        return dev_a == dev_b
    except Exception:  # noqa: BLE001
        return False


def mkdirs(p) -> None:
    """创建目录（Windows 长路径也要能用）。"""
    os.makedirs(sys_path(p), exist_ok=True)


# Windows 上会锁住整合包文件的常见程序（进程名小写，支持前缀匹配）
_BLOCKER_NAMES = (
    "javaw", "java", "minecraft", "hmcl", "pcl", "prismlauncher", "multimc",
    "curseforge", "modrinth", "fabric-installer", "forge", "minecraftlauncher",
    "xboxapp", "gamingservices",
)
_blockers_cache: tuple[float, tuple[str, ...]] = (0.0, ())


def running_blockers(max_age_s: float = 10.0) -> list[str]:
    """
    Windows 上正在运行、可能锁住整合包文件的程序。

    只在出错时调用一次（结果缓存 10 秒），任何异常都吞掉——不能因为
    探测失败而改变错误提示的正确性。
    """
    global _blockers_cache
    if not _IS_WINDOWS:
        return []
    now = time.time()
    if now - _blockers_cache[0] < max_age_s:
        return list(_blockers_cache[1])
    found: list[str] = []
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout or ""
        for line in out.splitlines():
            name = line.split(",")[0].strip().strip('"').lower()
            if not name:
                continue
            for bad in _BLOCKER_NAMES:
                if name.startswith(bad):
                    found.append(name)
                    break
    except Exception:  # noqa: BLE001
        found = []
    _blockers_cache = (now, tuple(found))
    return found


def friendly_os_error(e: BaseException, what: str = "") -> str:
    """把 OSError 翻译成玩家能看懂的话（Windows 上尤其重要）。"""
    prefix = f"{what}：" if what else ""
    if isinstance(e, PermissionError):
        hint = ""
        blockers = running_blockers()
        if blockers:
            hint = "；检测到 " + "、".join(sorted(set(blockers))) + " 正在运行"
        return (f"{prefix}文件被占用或没有权限"
                f"（请先关闭游戏 / 启动器 / 杀毒软件后重试{hint}）")
    if isinstance(e, FileNotFoundError):
        return f"{prefix}文件或目录不存在（可能被移动/删除了）"
    if isinstance(e, OSError) and e.errno == errno.ENOSPC:
        return f"{prefix}磁盘空间不足"
    if isinstance(e, OSError) and e.errno == errno.EROFS:
        return f"{prefix}目标磁盘是只读的"
    return f"{prefix}{e}"


def have_space_for(total_bytes: int, target: Path,
                   reserve: int = 64 * 1024 * 1024) -> tuple[bool, str]:
    """
    目标卷是否有足够空间放下 total_bytes（另留 reserve 余量）。

    只做提醒用：拿不到磁盘信息时一律**放行**（不能因为探测失败就不让
    玩家更新）。
    """
    if total_bytes <= 0:
        return True, ""
    probe = Path(target)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(sys_path(probe))
    except Exception:  # noqa: BLE001
        return True, ""
    free = usage.free
    if free >= total_bytes + reserve:
        return True, ""
    return False, (f"目标磁盘可用空间不足：需要约 "
                   f"{human_bytes(total_bytes)}，当前可用 {human_bytes(free)}")


def human_bytes(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if v < 1024 or unit == "TB":
            return f"{v:.1f}{unit}"
        v /= 1024
    return f"{v:.1f}TB"


# ----------------------------------------------------------------------
# 搬运结果
# ----------------------------------------------------------------------
@dataclass
class TransferResult:
    ok: bool = False
    error: str = ""
    moved: bool = False          # True = 瞬时搬运（rename/硬链接）
    bytes_copied: int = 0
    digest: str = ""             # 分块复制时顺带算出的内容摘要
    missing: bool = False        # True = 来源里没有这个文件

    def __bool__(self) -> bool:
        return self.ok


def _aborted(should_abort) -> bool:
    try:
        return bool(should_abort and should_abort())
    except Exception:  # noqa: BLE001
        return False


def _digest_for(algo: str):
    try:
        return hashlib.new(algo)
    except Exception:  # noqa: BLE001
        return hashlib.sha1()


# ----------------------------------------------------------------------
# 单文件搬运
# ----------------------------------------------------------------------
def move_in(src: Path, dst: Path, *, preserve: bool = False,
            chunk_bytes: int = CHUNK_BYTES, pace_ms: float = 0.0,
            expect_size: int = 0, expect_hash: str = "",
            no_fast_copy: bool = False,
            algo: str = "sha1", ensure_parent: bool = True,
            should_abort=None) -> TransferResult:
    """
    把 src 搬到 dst。

    preserve=True  ：源必须保留（下载缓存要留着，方便重试/续传），
                     做法是"硬链接 + rename"；不支持硬链接时退化成复制。
    preserve=False ：源可以消耗掉（解压出来的 overrides 是临时的），
                     同卷直接 `os.replace`，跨卷才复制后删除源。

    任何情况下目标端要么是**完整的旧文件**，要么是**完整的新文件**。
    """
    src = Path(src)
    dst = Path(dst)
    _no_fast_copy = bool(no_fast_copy)
    if not src.is_file():
        return TransferResult(False, "源文件不存在")

    try:
        if expect_size and src.stat().st_size != expect_size:
            return TransferResult(
                False, f"源文件大小不符（期望 {expect_size}，"
                       f"实际 {src.stat().st_size}）")
    except OSError as e:
        return TransferResult(False, friendly_os_error(e, "读取源文件失败"))

    # 目录创建交给调用方批量处理（这里只在没预建时兜底）：
    # 每个文件都 makedirs 一次在 Windows 上很贵。
    if ensure_parent:
        try:
            mkdirs(dst.parent)
        except OSError as e:
            return TransferResult(False, friendly_os_error(e, "创建目录失败"))

    # ---- 快路径 1：源可消耗 → 直接 rename（原子、瞬时） ----
    if not preserve:
        try:
            os.replace(sys_path(src), sys_path(dst))
            return TransferResult(True, moved=True)
        except OSError as e:
            if not is_cross_device(e):
                return TransferResult(False, friendly_os_error(e, "移动失败"))
        except Exception as e:  # noqa: BLE001
            return TransferResult(False, friendly_os_error(e, "移动失败"))

    tmp = tmp_name(dst)

    # ---- 快路径 2：源要保留 → 硬链接 + rename（瞬时，不占额外空间） ----
    if preserve:
        linked = False
        try:
            _unlink_quiet(tmp)
            os.link(sys_path(src), sys_path(tmp))
            linked = True
        except OSError:
            linked = False            # 跨卷 / FAT32 / 网络盘：走分块复制
        if linked:
            try:
                os.replace(sys_path(tmp), sys_path(dst))
                return TransferResult(True, moved=True)
            except OSError as e:
                _unlink_quiet(tmp)
                return TransferResult(False, friendly_os_error(e, "写入失败"))

    # ---- 慢路径 A：不需要逐块校验/节流 → 交给系统的快速复制 ----
    # Windows 上 shutil.copy2 会走 CopyFile2（内核态复制），Linux 上走
    # sendfile；比 Python 层面的读写循环快得多。写完仍然落到临时名，
    # 所以原子性不变。
    if not expect_hash and pace_ms <= 0 and not _no_fast_copy:
        try:
            _unlink_quiet(tmp)
            shutil.copy2(sys_path(src), sys_path(tmp))
            if expect_size and os.stat(sys_path(tmp)).st_size != expect_size:
                raise RuntimeError(
                    f"大小不符（期望 {expect_size}，"
                    f"实际 {os.stat(sys_path(tmp)).st_size}）")
            os.replace(sys_path(tmp), sys_path(dst))
        except RuntimeError as e:
            _unlink_quiet(tmp)
            return TransferResult(False, str(e))
        except OSError as e:
            _unlink_quiet(tmp)
            return TransferResult(False, friendly_os_error(e, "写入失败"))
        except Exception as e:  # noqa: BLE001
            _unlink_quiet(tmp)
            return TransferResult(False, friendly_os_error(e, "写入失败"))
        if not preserve:
            _unlink_quiet(src)
        return TransferResult(True, moved=False)

    # ---- 慢路径 B：分块复制（要算摘要 / 要按块节流时） ----
    digest = _digest_for(algo) if expect_hash else None
    written = 0
    try:
        _unlink_quiet(tmp)
        with open(sys_path(src), "rb") as fi, \
                open(sys_path(tmp), "wb") as fo:
            while True:
                if _aborted(should_abort):
                    raise RuntimeError("已取消")
                buf = fi.read(chunk_bytes)
                if not buf:
                    break
                fo.write(buf)
                written += len(buf)
                if digest is not None:
                    digest.update(buf)
                if pace_ms > 0:
                    time.sleep(pace_ms / 1000.0)
        if expect_size and written != expect_size:
            raise RuntimeError(
                f"大小不符（期望 {expect_size}，实际 {written}）")
        if digest is not None:
            got = digest.hexdigest()
            if got.lower() != expect_hash.lower():
                raise RuntimeError("内容校验失败（SHA 不一致）")
            got_digest = got
        else:
            got_digest = ""
        try:
            shutil.copystat(sys_path(src), sys_path(tmp))
        except OSError:
            pass
        os.replace(sys_path(tmp), sys_path(dst))
    except RuntimeError as e:
        _unlink_quiet(tmp)
        return TransferResult(False, str(e))
    except OSError as e:
        _unlink_quiet(tmp)
        return TransferResult(False, friendly_os_error(e, "写入失败"))
    except Exception as e:  # noqa: BLE001
        _unlink_quiet(tmp)
        return TransferResult(False, friendly_os_error(e, "写入失败"))

    if not preserve:
        _unlink_quiet(src)
    return TransferResult(True, moved=False, bytes_copied=written,
                          digest=got_digest)


def _unlink_quiet(p: Path):
    try:
        os.unlink(sys_path(p))
    except Exception:  # noqa: BLE001, S110
        pass


def cleanup_transient(target_dir: Path, max_age_s: float = 0.0) -> int:
    """
    清理目录下遗留的 `.xxx.pulses_new` 临时文件。

    max_age_s > 0 时只清理"足够旧"的（避免误删正在进行的搬运）。
    返回删除数量。
    """
    root = Path(target_dir)
    if not root.is_dir():
        return 0
    now = time.time()
    removed = 0
    try:
        for p in root.rglob(".*" + NEW_SUFFIX):
            try:
                if max_age_s > 0 and now - p.stat().st_mtime < max_age_s:
                    continue
                if p.is_file():
                    os.unlink(sys_path(p))
                    removed += 1
            except OSError:
                continue
    except OSError:
        pass
    return removed
