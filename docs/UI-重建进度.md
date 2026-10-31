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
| 端到端（新界面，真前端 + 真 Api） | `scripts/dev/run_ui_e2e.py` | **12 项**：定位 → 导入 → 比对 → 应用 → 磁盘校验 |
| 外壳冒烟（真 WebView2 起窗） | `tests/web_e2e_windows.py` / CI 打包后起窗 | CI 里跑，证据进 artifact |

## 已补完（第二轮）

- 开发者流程：导入 zip → 更新范围（点目录名排除）→ 选项 → 更新日志 → 导出 `.eapack`
  （后台线程、文件级进度、可选保存位置、清空）
- 续传：`resume.json`（记录已完成文件/策略/勾选），主按钮变「继续更新」，
  续跑时已下好的文件不重复下载
- 白名单 / 数据库位置抽屉（读写即生效）
- 受阻条：打开补入抽屉、逐个重试、「放弃…」清空待补
- 变更行/范围行 `content-visibility`，几千行滚动不再掉帧
- 进度文案来自后端（正在下载 / 应用 12/40 / 更新完成）
- 真机端到端：`app/ui_web/e2e_driver.py` 在真实 WebView2 页面里点按钮，
  `tests/web_e2e_windows.py` 搭夹具核对磁盘；CI 里阻断并重试一次

## 端到端是怎么做到"真机"的

浏览器里跑不了新界面，是因为它依赖 pywebview 注入的 `window.pywebview`。
`scripts/dev/bridge_server.py` 用 HTTP 把同一套东西补上（`/api/invoke` 直接调
`app/ui_web/api.py`，`/api/events` 拉事件流），页面里注入同构 shim。

于是 CI 上的端到端是：**真前端产物 + 真 Api + 真流程 + 真磁盘**，Playwright 点按钮；
只有外壳（WebView2）不同，外壳另有一条"打包后起窗冒烟"覆盖。

## 稳定性修补（第三轮）

- `_atomic_write_text`：Windows 上目标被占用时 `os.replace` 会 PermissionError，
  现在重试 5 次再退化为直写，失败会打日志（不再静默丢写）
- 白名单保存后立刻回读校验，不一致直接报错（避免"界面显示已保存其实没存"）
- 比对前先 `recover_leftovers`，清掉上次中断留下的 `.xxx.pulses_tmp`
- 策略表：待更新条数来自后端；「统一策略」浮层可一次改全部；策略配置可导入/导出
- 更新完成后主按钮回到「开始更新」，不再是停在「确认更新」

## 未完成（按优先级）

1. **Tk 完整对齐**：检查点/中断残留恢复（`recover`）、应用阶段的"最低空间"提示、
   多显示器 DPI 细节仍未搬到新界面
2. **拖放**：DOM 拖放已接（pywebview `pywebviewFullPath`），Windows 实机路径待验证
3. **槽位面板**：已按 `bytes` 事件渲染，但"磁盘策略限制后的有效并发"未显示
4. **虚拟列表**：目前用 `content-visibility` 折中；上万行仍可能需要真虚拟化
5. **主题**：只有深色；未做浅色/跟随系统
