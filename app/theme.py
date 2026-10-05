"""
theme.py — 全局视觉主题常量
------------------------------------------------
集中管理色板、字体、尺寸、圆角、动画时长。
UI 各处一律从此处取值，禁止硬编码，便于后续换肤。
"""

# ----------------------------------------------------------------------
# 色彩方案（深灰 + 青色）
# ----------------------------------------------------------------------
class Color:
    # 背景分层
    WINDOW_BG      = "#1E1E1E"
    SIDEBAR_BG     = "#252526"
    WORKSPACE_BG   = "#2D2D30"
    CARD_BG        = "#333337"
    LOG_BG         = "#252526"
    INPUT_BG       = "#252526"   # 输入框默认
    INPUT_FOCUS_BG = "#2D2D30"   # 输入框聚焦

    # 主强调色（青色）三态
    ACCENT         = "#4EC9B0"
    ACCENT_HOVER   = "#3DA890"
    ACCENT_PRESS   = "#2E8B7A"

    # 文字
    TEXT_PRIMARY   = "#E8E8E8"
    TEXT_SECONDARY = "#9A9A9A"
    TEXT_MUTED     = "#6A6A6A"
    TEXT_ON_ACCENT = "#F4F807"

    # 边框/分割线（保留但控件不再使用 border_width）
    BORDER         = "#3E3E42"

    # 变更类型语义色
    ADDED          = "#4EC9B0"
    MODIFIED       = "#DCDCAA"
    DELETED        = "#F48771"

    # 状态色
    WARNING        = "#DCDCAA"
    ERROR          = "#F48771"

    # 策略按钮选中态（亮黄）
    STRATEGY_SELECTED      = "#DCDCAA"   # 同 MODIFIED，语义化命名
    STRATEGY_SELECTED_TX   = "#1E1E1E"   # 选中文字（深色）

    # 启动页
    SPLASH_TEXT = "#1B2A4A"       # 主文字：深蓝
    SPLASH_TEXT_DIM = "#5A6B8C"   # 次要：雾蓝
    
    # 透明
    TRANSPARENT    = "transparent"


# ----------------------------------------------------------------------
# 字体
# ----------------------------------------------------------------------
class Font:
    FAMILY = "Microsoft YaHei UI"

    TITLE     = (FAMILY, 15, "bold")   # 标题统一 15px 粗体
    SUBTITLE  = (FAMILY, 15, "bold")
    BODY      = (FAMILY, 13)
    BODY_B    = (FAMILY, 13, "bold")
    SMALL     = (FAMILY, 11)           # 辅助信息统一 11px
    TINY      = (FAMILY, 11)
    BUTTON    = (FAMILY, 13)
    BTN_LARGE = (FAMILY, 15, "bold")


# ----------------------------------------------------------------------
# 尺寸与圆角
# ----------------------------------------------------------------------
class Size:
    WIN_W, WIN_H         = 1440, 860
    WIN_MIN_W, WIN_MIN_H = 1100, 700

    SIDEBAR_W      = 240
    # 玩家界面：左工作区（选项+更新包） / 右工作区（变更树+日志）
    PLAYER_L_RATIO = 46
    PLAYER_R_RATIO = 54

    # 开发者界面：左工作区（策略表） / 右工作区（编辑器+导出选项）
    DEVELOPER_L_RATIO = 40
    DEVELOPER_R_RATIO = 60
    DEVELOPER_RIGHT_MIN_W = 320

    RADIUS_CARD    = 12
    RADIUS_BUTTON  = 8
    RADIUS_DROP    = 10
    RADIUS_MODAL   = 16   # 弹窗圆角
    RADIUS_INPUT   = 8

    GAP            = 12
    PAD_PANEL      = 16
    PAD_WORKSPACE  = 20

    LOGO_SIZE      = 80
    DROP_H_SIDEBAR = 100
    DROP_H_WORK    = 110
    MAIN_BTN_H     = 48
    SEG_BTN_H      = 36
    PROGRESS_H     = 6     # 细进度条
    LOG_H          = 120

    SCROLLBAR_W    = 6     # 细滚动条
    INPUT_UNDERLINE_H = 2  # 聚焦下划线高度


# ----------------------------------------------------------------------
# 动画时长（毫秒）
# ----------------------------------------------------------------------
class Anim:
    FRAME_MS    = 16    # 约 60fps
    HOVER_MS    = 150
    TREE_MS     = 200
    SWITCH_MS   = 250   # 页面滑动
    SLIDER_MS   = 200   # 策略色块滑动
    TOGGLE_MS   = 150   # 勾选条目渐变
    DROP_MS     = 100

# ----------------------------------------------------------------------
# Markdown 渲染用 CSS
#   - DARK：嵌入式（备用，实际嵌入式用 tk.Text tag，不注入 CSS）
#   - LIGHT：独立弹窗用，GitHub 风格浅色
# 用 string.Template 的 $name 占位，避免 CSS 花括号与 .format 冲突
# ----------------------------------------------------------------------
from string import Template as _Template

MARKDOWN_CSS_DARK = _Template("""
body {
    background-color: $bg;
    color: $fg;
    font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 13px;
    line-height: 1.5;
    margin: 6px;
}
h1 { color: $fg; font-size: 22px; font-weight: bold; margin: 8px 0 4px 0; }
h2 { color: $fg; font-size: 18px; font-weight: bold; margin: 6px 0 3px 0; }
h3 { color: $fg; font-size: 15px; font-weight: bold; margin: 5px 0 2px 0; }
h4, h5 { color: $fg; font-size: 14px; font-weight: bold; margin: 4px 0 2px 0; }
h6 { color: $muted; font-size: 13px; font-weight: bold; margin: 4px 0 2px 0; }
p { margin: 3px 0; }
a { color: $accent; text-decoration: none; }
code { font-family: "Consolas", monospace; background-color: $code_bg;
       color: $modified; padding: 1px 3px; border-radius: 3px; }
pre { font-family: "Consolas", monospace; background-color: $code_bg;
      color: $added; padding: 8px; border-radius: 6px; margin: 6px 0;
      white-space: pre; }
blockquote { color: $muted; border-left: 3px solid $accent;
             padding-left: 10px; margin: 4px 0; }
ul, ol { margin: 3px 0 3px 20px; padding: 0; }
li { margin: 1px 0; }
hr { border: none; border-top: 1px solid $border; margin: 8px 0; }
table { border-collapse: collapse; margin: 6px 0; }
th, td { border: 1px solid $border; padding: 4px 10px; text-align: left; }
th { background-color: $th_bg; color: $fg; font-weight: bold; }
del { color: $muted; }
img { max-width: 100%; }
""").substitute(
    bg="#2D2D30", fg="#E8E8E8", muted="#9A9A9A",
    accent="#4EC9B0", border="#3E3E42",
    code_bg="#252526", modified="#DCDCAA",
    added="#4EC9B0", th_bg="#333337",
)


# GitHub 风格浅色，用于独立弹窗（pywebview / 系统浏览器）
MARKDOWN_CSS_LIGHT = _Template("""
* { box-sizing: border-box; }
body {
    background-color: #FFFFFF;
    color: #24292F;
    font-family: -apple-system, "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 15px;
    line-height: 1.6;
    margin: 0;
    padding: 32px 40px 64px 40px;
    max-width: 900px;
    margin-left: auto;
    margin-right: auto;
    -webkit-font-smoothing: antialiased;
}
h1 { font-size: 2em; font-weight: 600; margin: 24px 0 16px 0;
     padding-bottom: 0.3em; border-bottom: 1px solid #D8DEE4; }
h2 { font-size: 1.5em; font-weight: 600; margin: 24px 0 16px 0;
     padding-bottom: 0.3em; border-bottom: 1px solid #D8DEE4; }
h3 { font-size: 1.25em; font-weight: 600; margin: 24px 0 16px 0; }
h4 { font-size: 1em; font-weight: 600; margin: 24px 0 16px 0; }
h5 { font-size: 0.875em; font-weight: 600; margin: 24px 0 16px 0; }
h6 { font-size: 0.85em; font-weight: 600; color: #57606A; margin: 24px 0 16px 0; }

p { margin: 0 0 16px 0; }

a { color: #0969DA; text-decoration: none; }
a:hover { text-decoration: underline; }

strong { font-weight: 600; }

code {
    font-family: ui-monospace, "SFMono-Regular", "Consolas", monospace;
    font-size: 85%;
    background-color: rgba(175, 184, 193, 0.2);
    color: #24292F;
    padding: 0.2em 0.4em;
    border-radius: 6px;
}
pre {
    font-family: ui-monospace, "SFMono-Regular", "Consolas", monospace;
    font-size: 85%;
    background-color: #F6F8FA;
    color: #24292F;
    padding: 16px;
    border-radius: 6px;
    overflow: auto;
    line-height: 1.45;
    margin: 0 0 16px 0;
}
pre code {
    background-color: transparent;
    padding: 0;
    font-size: 100%;
}

blockquote {
    color: #57606A;
    border-left: 4px solid #D0D7DE;
    padding: 0 1em;
    margin: 0 0 16px 0;
}

ul, ol { margin: 0 0 16px 0; padding-left: 2em; }
li + li { margin-top: 0.25em; }
li > p { margin-top: 16px; }

hr {
    border: none;
    border-top: 1px solid #D0D7DE;
    margin: 24px 0;
    height: 0;
}

table {
    border-collapse: collapse;
    border-spacing: 0;
    margin: 0 0 16px 0;
    display: block;
    overflow: auto;
    width: max-content;
    max-width: 100%;
}
th, td {
    border: 1px solid #D0D7DE;
    padding: 6px 13px;
}
th { background-color: #F6F8FA; font-weight: 600; }
tr:nth-child(2n) { background-color: #F6F8FA; }

del { color: #57606A; }

img { max-width: 100%; height: auto; }

/* 任务列表 */
li input[type="checkbox"] { margin-right: 0.5em; }
""").substitute()