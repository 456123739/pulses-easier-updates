# Pulses Easier UI 重建计划（Tk → Web）

> 分支：`feat/ui-web-render`（main 不动）。目标：用 pywebview + React 复刻现有界面，
> **布局结构与配色不变**，动画升级到合成器驱动，交付可运行的 Windows 包与实测截图。

## 0. 目标与非目标

**目标**
1. 渲染层换成 WebView2（pywebview 宿主），`app/core/*` **一行不改**。
2. 视觉 1:1 复刻：色板、字体、圆角、间距、分栏比例全部取自 `app/theme.py`。
3. 全部功能对齐：玩家端（定位/扫描/变更树/下载/应用/受阻/续传）、开发者端（策略表/变更列表/日志编辑/导出）、弹窗族、启动页、覆盖层换页。
4. 动画 60fps 稳定：Framer Motion + CSS，回弹幅度 ≤3%，`prefers-reduced-motion` 可关。
5. 交付：Windows zip + 启动/交互实测截图。

**非目标**
- 不改 core 的算法、网络、数据库格式。
- 不重做信息架构（分区、按钮位置、文案保持原样，除非原样是 bug）。
- 不做 Linux/macOS 交付（仅开发期可跑）。

## 1. 架构

```
main.py  ──►  app/ui_web/host.py ──► pywebview.create_window(url=http://127.0.0.1:PORT)
                    │                        ▲ WebView2
                    ├── serve.py  (本地静态服务, 只读 dist/)
                    ├── bridge.py (Python→JS 事件，60ms 合批 + 按 key 合并)
                    └── api.py    (JS→Python，pywebview.api.*，返回 Promise)
app/core/*  ← 原样复用，回调改为 bridge.post()
web/        ← Vite + React 19 + TS + Zustand + Framer Motion + CSS Modules
```

**状态归属**：UI 状态由 Python 单一持有（与现在一致），前端只渲染 + 本地瞬时状态
（hover、输入中的文本、滚动位置）。桥消息两类：
- `state`（全量/差分快照，低频，变更后推送）
- `event`（进度、日志、字节流，高频，合批）

## 2. 阶段与验收（每阶段跑测试后才进入下一阶段）

### P0 基座（构建链 + 宿主 + 桥）
- [ ] `web/` Vite+React+TS 工程，`npm run build` 输出 `app/ui_web/static/`
- [ ] `app/ui_web/serve.py`：环回地址随机端口，路径穿越防护，MIME/缓存头，`Range` 支持
- [ ] `app/ui_web/bridge.py`：`EventPump`（60ms 合批、进度按 key 覆盖、日志限行、背压丢弃计数）
- [ ] `app/ui_web/api.py`：`ping/version/select_pack/drop_paths/get_state` 最小面
- [ ] `app/ui_web/host.py`：建窗（1440×860，min 1000×620，居中）、关闭守卫、DPI
- [ ] 拖放：JS `drop` → `pywebviewFullPath`（缺则降级为点选），多文件
- **测试**：`tests/test_ui_web_bridge.py`（合批/丢弃/顺序/线程安全）、`tests/test_ui_web_serve.py`（穿越/404/HEAD/Range）；`npm run build` + `tsc --noEmit` 通过；headless 截图一张

### P1 外壳与导航
- [ ] 设计令牌 `tokens.css`（Color/Font/Size/Anim 全量映射）
- [ ] 侧边栏（240px）：logo、包卡、最近、白名单入口、更多、身份切换
- [ ] 工作区分栏（玩家 46/54，开发者 40/60）与窗口最小尺寸自适应
- [ ] logo 覆盖层换页（150/450/170ms，截图滑动语义 → CSS 双图层）
- [ ] 启动页（无边框、伪进度、淡出）
- **测试**：DOM 结构断言 + 截图对照（1280×900 / 1000×620 两档）

### P2 玩家端主流程
- [ ] 定位更新包（拖入/选择）→ 包信息卡
- [ ] 扫描：解压/比对进度（含 0.9.x 的"先比对后解压"双阶段文案）
- [ ] 策略表（目录 3 档 / 文件 4 档，含"智能剔除"与"保留键位"）
- [ ] 变更树：分组、展开折叠、跳过、未变条目自动折叠
- [ ] 下载：槽位面板、整体/单文件进度、慢速车道提示、Aster 转交提示
- [ ] 应用：进度、成功/失败汇总、合并键位提示
- [ ] 日志面板（限行、自动滚动、级别色）
- **测试**：pytest 桥处理器（假包 + 假下载器）；前端组件测试；e2e 走完整流程

### P3 弹窗族与异常流
- [ ] alert / confirm / locate（含复选框确认）
- [ ] 受阻窗口（补入、放弃、自动操作、重复弹出修复逻辑保持）
- [ ] 续传提示、失败重试、错误详情
- [ ] 首选项（引擎、线程、镜像优先门控、窗口动画开关、主题）
- [ ] 数据库位置/迁移向导、白名单、更多面板
- **测试**：每个弹窗的键盘/焦点/关闭路径；截图

### P4 开发者端
- [ ] 策略表（目录/文件分区、锁定列、全选、批量套用、>50 行虚拟化）
- [ ] 变更列表（分组、状态色）
- [ ] 更新日志编辑器（TipTap）+ 预览（core 的 markdown 渲染）
- [ ] 打包选项（版本号、排除项、`options.txt` 不打包提示）、导出与校验
- **测试**：组件测试 + 导出产物结构与 Tk 版一致（对比 eapack 输出）

### P5 动效与可访问性
- [ ] Framer Motion 变体库：hover/press/enter/exit/折叠/滑块/开关/进度
- [ ] 窗口进出场（220/420ms 淡入上浮，150ms 退场）
- [ ] 减少动效、键盘 Tab 顺序、Esc 关闭、焦点陷阱、ARIA
- **测试**：动画状态断言（不含时间采样）、`prefers-reduced-motion` 分支

### P6 打包与交付
- [ ] spec：`static/` 作为 datas，隐藏导入，去掉 Tk 专属
- [ ] CI：`npm ci && npm run build` → PyInstaller → 冒烟（Windows）
- [ ] 体积/启动时间/内存测量（对照 Tk 基线 1.23s / 63MB）
- [ ] 入口切换（`main.py` 走 web），删除 `--classic-ui`
- [ ] 截图交付：启动页、玩家端空态、扫描中、变更树、下载中、应用完成、开发者端、首选项、受阻窗口

## 3. 测试矩阵

| 层 | 工具 | 触发 |
|---|---|---|
| Python 单元 | pytest（现有 456 项 + 新增 ~40） | 每次改动 |
| 前端单元/组件 | vitest + @testing-library/react | 每次改动 |
| 类型 | `tsc --noEmit` | 每次改动 |
| 视觉回归 | Playwright(chromium) 截图 diff（自建基线） | 每阶段 |
| 端到端 | Playwright + mock 桥（vite preview） | P2/P3/P4 |
| 真实 Windows | CI windows-latest：启动 3s 存活 + CDP 截图 | 发布前 |

## 4. 风险与对策

| 风险 | 对策 |
|---|---|
| 拖放拿不到全路径 | pywebview ≥5.0.1 的 `pywebviewFullPath`；缺失时降级为"点击选择"，并在 UI 明说 |
| WebView2 未安装 | 启动时检测运行时，缺失给明确文案 + 引导链接（不静默崩） |
| 高频进度事件拖垮渲染 | 桥层合批 + rAF 节流 + `will-change` 限定 |
| 折叠/虚拟列表与滚动锚定 | `@tanstack/react-virtual` + `scrollMargin` 固定行高 |
| 中文输入法（IME）在受控输入框丢字 | 输入框统一非受控 + `onCompositionStart/End` 守卫 |
| 视觉回归噪声 | 关闭动画采样、固定字体渲染、容差 0.5% |
