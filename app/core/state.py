"""
state.py — 应用级状态管理
------------------------------------------------
主控制器维护以下状态，UI 通过方法读写，不直接操作控件。

字段：
  current_modpack_path  当前整合包路径（None 表示未定位）
  is_locked             是否锁定（更新流程中防止更换整合包）
"""

from collections.abc import Callable
from pathlib import Path


class AppState:
    def __init__(self):
        self._current_modpack_path: Path | None = None
        self._is_locked: bool = False

        self._on_modpack_changed: list[Callable[[Path | None], None]] = []
        self._on_lock_changed: list[Callable[[bool], None]] = []

    # ------------------------------------------------------------------
    # 属性访问
    # ------------------------------------------------------------------
    @property
    def current_modpack_path(self) -> Path | None:
        return self._current_modpack_path

    @property
    def is_locked(self) -> bool:
        return self._is_locked

    # ------------------------------------------------------------------
    # 状态变更
    # ------------------------------------------------------------------
    def set_modpack(self, path: Path | None):
        """设置当前整合包路径并通知订阅者"""
        self._current_modpack_path = Path(path) if path else None
        for cb in self._on_modpack_changed:
            try:
                cb(self._current_modpack_path)
            except Exception:  # noqa: BLE001, S110
                pass

    def clear_modpack(self):
        """清空整合包"""
        self.set_modpack(None)

    def lock(self):
        """锁定（更新中）"""
        if self._is_locked:
            return
        self._is_locked = True
        for cb in self._on_lock_changed:
            try:
                cb(True)
            except Exception:  # noqa: BLE001, S110
                pass

    def unlock(self):
        """解锁"""
        if not self._is_locked:
            return
        self._is_locked = False
        for cb in self._on_lock_changed:
            try:
                cb(False)
            except Exception:  # noqa: BLE001, S110
                pass

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------
    def on_modpack_changed(self, cb: Callable[[Path | None], None]):
        self._on_modpack_changed.append(cb)

    def on_lock_changed(self, cb: Callable[[bool], None]):
        self._on_lock_changed.append(cb)