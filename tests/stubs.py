"""
stubs.py — 让 UI 模块能在没有 GUI 依赖的环境下被导入。

用途：在无显示器 / 未安装 tkinter 的机器上做「导入期错误」检查。
说明：只在 sys.modules 里注册**假的** tkinter / customtkinter / PIL 等，
      不会真的开窗口，也不能替代真机（Windows 10）上的界面测试。
"""

import sys
import types

_THIRD_PARTY = (
    "tkinter", "tkinter.font", "tkinter.filedialog", "tkinter.ttk",
    "tkinter.messagebox", "tkinter.constants", "tkinter.colorchooser",
    "tkinterdnd2", "customtkinter",
    "PIL", "PIL.Image", "PIL.ImageTk", "PIL.ImageDraw",
    "PIL.ImageFont", "PIL.ImageFilter",
    "markdown", "multimark", "tkhtmlview", "webview",
)

_WIDGET_NAMES = (
    "CTk", "CTkToplevel", "CTkFrame", "CTkLabel", "CTkButton",
    "CTkEntry", "CTkScrollableFrame", "CTkScrollbar", "CTkTextbox",
    "CTkCanvas", "CTkImage", "CTkProgressBar", "CTkSlider",
    "CTkSegmentedButton", "CTkOptionMenu", "CTkComboBox",
    "CTkCheckBox", "CTkRadioButton", "CTkSwitch", "CTkTabview",
    "CTkFont", "CTkInputDialog", "Tk", "Toplevel", "Frame", "Label",
    "Button", "Entry", "Text", "Canvas", "Menu", "PhotoImage",
)

_VAR_NAMES = ("StringVar", "IntVar", "DoubleVar", "BooleanVar", "Variable")

_NOOP_ATTRS = (
    "set_appearance_mode", "set_default_color_theme",
    "set_widget_scaling", "set_window_scaling",
    "deactivate_automatic_dpi_awareness", "mainloop", "init",
)


def _noop(*args, **kwargs):
    return None


# Tk 里"有返回值"的查询方法：给个合理默认值，让上层的真实逻辑能继续跑。
# 不返回 None 是因为很多代码会直接迭代 / 做算术。
_QUERY_RETURNS = {
    "winfo_children": lambda self=None, *a, **k: [],
    "winfo_ismapped": lambda self=None, *a, **k: 0,
    "winfo_exists": lambda self=None, *a, **k: 1,
    "winfo_viewable": lambda self=None, *a, **k: 1,
    "winfo_width": lambda self=None, *a, **k: 320,
    "winfo_height": lambda self=None, *a, **k: 24,
    "winfo_reqwidth": lambda self=None, *a, **k: 320,
    "winfo_reqheight": lambda self=None, *a, **k: 24,
    "winfo_x": lambda self=None, *a, **k: 0,
    "winfo_y": lambda self=None, *a, **k: 0,
    "winfo_rootx": lambda self=None, *a, **k: 0,
    "winfo_rooty": lambda self=None, *a, **k: 0,
    "winfo_screenwidth": lambda self=None, *a, **k: 1920,
    "winfo_screenheight": lambda self=None, *a, **k: 1080,
    "winfo_toplevel": lambda self=None, *a, **k: self,
    "winfo_pixels": lambda self=None, *a, **k: 1,
    "grid_slaves": lambda self=None, *a, **k: [],
    "pack_slaves": lambda self=None, *a, **k: [],
    "cget": lambda self=None, *a, **k: "",
    "get": lambda self=None, *a, **k: "",
    "index": lambda self=None, *a, **k: 0,
    "after": lambda self=None, *a, **k: "after#0",   # 不真正调度回调
    "after_cancel": lambda self=None, *a, **k: None,
    "after_idle": lambda self=None, *a, **k: "after#0",
    "clipboard_get": lambda self=None, *a, **k: "",
    "selection_get": lambda self=None, *a, **k: "",
}


class _StubAny:
    """
    万能占位对象：可调用、可链式取属性、可迭代、可当数字。

    这样 `self.text._textbox.tag_config(...)` 这类 CustomTkinter 内部
    属性链也能走通，让上层**自己的**逻辑（字典/列表/算术/分支）真正被执行，
    而不是卡在桩对象上。
    """

    __slots__ = ()

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self

    def __setattr__(self, name, value):
        pass

    def __iter__(self):
        return iter(())

    def __len__(self):
        return 0

    def __bool__(self):
        return True

    def __contains__(self, item):
        return False

    def __getitem__(self, key):
        return self

    def __setitem__(self, key, value):
        pass

    def __str__(self):
        return ""

    def __int__(self):
        return 0

    def __float__(self):
        return 0.0

    def __index__(self):
        return 0

    def __hash__(self):
        return id(self)


_ANY = _StubAny()


class _StubBase:
    """控件桩基类：未知属性返回占位对象，查询类方法返回合理默认值。"""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        fn = _QUERY_RETURNS.get(name)
        if fn is not None:
            # 注意：__getattr__ 返回的函数不会被绑定，需要自己把 self 传进去
            return lambda *a, **k: fn(self, *a, **k)
        return _StubAny()


def _make_widget_class(name):
    return type(name, (_StubBase,), {})


def _make_module(name):
    mod = types.ModuleType(name)

    def _getattr(attr, _mod=mod):
        if attr.startswith("__"):
            raise AttributeError(attr)
        cls = _make_widget_class(attr)
        setattr(_mod, attr, cls)
        return cls

    mod.__getattr__ = _getattr  # type: ignore[attr-defined]
    return mod


def install():
    """把桩模块装进 sys.modules（幂等，可重复调用）。"""
    for name in _THIRD_PARTY:
        mod = sys.modules.get(name) or _make_module(name)
        for attr in _NOOP_ATTRS:
            setattr(mod, attr, _noop)
        for attr in _VAR_NAMES:
            setattr(mod, attr, _make_widget_class(attr))
        for attr, value in (("END", "end"), ("W", "w"), ("E", "e"),
                            ("N", "n"), ("S", "s"), ("NSEW", "nsew"),
                            ("LEFT", "left"), ("RIGHT", "right"),
                            ("TOP", "top"), ("BOTTOM", "bottom"),
                            ("HORIZONTAL", "horizontal"),
                            ("VERTICAL", "vertical")):
            setattr(mod, attr, value)
        sys.modules[name] = mod

    # customtkinter / tkinter 的控件名要能直接作为基类使用
    for name in ("customtkinter", "tkinter", "tkinter.ttk"):
        mod = sys.modules[name]
        for attr in _WIDGET_NAMES:
            setattr(mod, attr, _make_widget_class(attr))

    dnd = sys.modules["tkinterdnd2"]
    dnd.DND_FILES = "DND_Files"
    dnd.DND_TEXT = "DND_Text"
    dnd.TkinterDnD = types.SimpleNamespace(
        DnDWrapper=_make_widget_class("DnDWrapper"),
        _require=lambda *a, **k: "2.0",
    )

    pil_image = sys.modules["PIL.Image"]
    pil_image.open = _noop
    pil_image.new = _noop
    pil_image.Resampling = types.SimpleNamespace(
        LANCZOS=1, BICUBIC=2, BILINEAR=3, NEAREST=0)
    sys.modules["PIL"].Image = pil_image
    return True


def isolate_user_dirs(tmpdir):
    """
    把程序会写到的**用户级目录**全部重定向到临时目录。

    项目里有三处会写 HOME 下的路径：
        app/core/database.py  ~/.pulses_easier/config.json
        app/core/recent.py    ~/.pulses_easier/recent.json
    测试绝不允许污染工作区外的真实文件，所以统一改指到 tmpdir。
    返回一个 restore() 回调。
    """
    from pathlib import Path as _Path

    from app.core import database as _db
    from app.core import recent as _recent

    root = _Path(tmpdir)
    root.mkdir(parents=True, exist_ok=True)
    saved = (
        (_db, "_APP_DIR", _db._APP_DIR),
        (_db, "_APP_CFG", _db._APP_CFG),
        (_recent, "_CONFIG_DIR", _recent._CONFIG_DIR),
        (_recent, "_RECENT_FILE", _recent._RECENT_FILE),
    )
    _db._APP_DIR = root
    _db._APP_CFG = root / "config.json"
    _recent._CONFIG_DIR = root
    _recent._RECENT_FILE = root / "recent.json"

    def restore():
        for mod, name, value in saved:
            setattr(mod, name, value)

    return restore


if __name__ == "__main__":
    install()
    print("stubs installed:", len(_THIRD_PARTY))
