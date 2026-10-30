# 新界面（React + pywebview）重建进度

分支：`feat/ui-web-render`。Tk 界面保持原样，`PULSES_UI=web` 才启动新界面。

## 已完成

### 骨架与桥
- `app/ui_web/host.py`：pywebview 宿主（1440×860，最小 1000×620，DOM 拖放接 Python，
  `http_server=True` 供 ES module 加载，关闭守卫）
- `app/ui_web/api.py`：js_api 三方法（`invoke` / `pick_path` / `handle_drop`），
  信封统一 `{ok, data|error{code,message,detail}}`；方法白名单
- `app/ui_web/events.py`：EventBus + EventPump（60fps 合批、上限丢弃、顺序保留）
- `app/ui_web/controller.py`：无 UI 控制器（快照 / 身份 / 设置 / 整合包 / 最近打开 / 对话框）
- `app/ui_web/flow.py`：**更新流程无头实现**（导入 → 解压+清单比对 → 策略表 →
  任务收集 → 引擎下载 → build_plan/execute_plan → 复核），单文件重试

### 前端
- React 19 + TS + Zustand + Framer Motion + CSS Modules（Vite，产物 `web/dist`）
- 设计系统：哑光深色（无高光/无发光），发丝线 + 极浅投影，强调色只用于主操作
- 组件：Shell / Sidebar / RoleSwitch / Cover（换页遮罩）/ DropZone / StrategyCard
  （折叠分区「无需更新」「已禁用」）/ ChangeTree / ProgressBar / SlotPanel / LogPanel /
  MainButton（六态）/ PendingBanner + PendingSheet / ChangelogSheet / PreferencesSheet /
  DialogHost / ToastHost / Splash
- 与后端真实联通：定位整合包、最近打开、导入更新包、比对、改策略、应用、清空、
  查看更新日志、补入重试、首选项读写

### 打包与 CI
- `pulses-easier.spec` 收集 `web/dist`；hiddenimports 补 `webview.platforms.winforms`
- `build-windows.yml`：setup-node → `npm ci` → `tsc --noEmit` → `npm run build` → 单元测试 → GUI 冒烟 → PyInstaller
- `build_windows.bat`：有 node 就先构前端，没有则跳过并提示

## 测试

| 层 | 位置 | 现状 |
|---|---|---|
| 单元（Python） | `tests/` | 468 项全过（新增 flow 4 项 + 桥 6 项） |
| 界面交互（Playwright） | `web/scripts/ui_test.mjs` | 12 项全过：渲染/胶囊/折叠/换身份/首选项/受阻条/下载态/无控制台错误 |
| 截图 | `web/scripts/shot.mjs` + `ui_test.mjs --shots` | `web/shots/*.png` |
| 端到端（Tk 真窗口） | `tests/e2e_flow.py`（Windows） | 未改，仍覆盖完整流程 |

## 未完成（按优先级）

1. **开发者流程**：导出更新包（eapack 导出、选项、进度、完成页）尚未接到新界面；
   目前开发者页只有界面骨架
2. **断点续传**：`resume.json` / 检查点 / "继续更新" 未接入
3. **槽位面板**：目前是演示数据，未接 `byte_progress` 事件
4. **关闭守卫与对话框**：`can_close` 已实现，但前端的"强关确认"链路未接
5. **白名单 / 数据库位置面板**：侧边栏入口已留，面板未做
6. **虚拟列表**：变更树超过 ~2000 行时需要虚拟化（现在全量渲染）
7. **Windows 实机**：WebView2 下的拖放、字体、动画需真机验证
